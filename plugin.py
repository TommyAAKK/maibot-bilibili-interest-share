"""麦麦刷B站插件。

按白名单聊天流的用户兴趣，定时刷 B 站并挑视频分享。v0.2 相比 v0.1 增加了：

- 抽帧看画面（视觉模型）+ 弹幕 / 热门评论 / CC 字幕一起参与判断；
- LLM 语义判断（默认走 ``planner`` 任务），生成「内容总结 + 看点」；
- 排除主流官方物料，按「新鲜资讯 / 社区创作 / 玩家投稿 / 外网搬运」分类抓取；
- 自动分享与主动搜索各自独立冷却；
- 已刷名单：名单内视频不再自动分享，但可被要求重发（复用缓存的总结）；
- ``/赤石视频`` 指令单独分享抽象整活类视频。

宿主能力都通过 ``self.ctx`` 调用，模型由 ``model_config.toml`` 的任务配置决定。
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from maibot_sdk import Command, HookHandler, MaiBotPlugin

try:  # 文档推荐的导入路径（HookMode 定义在 types 里）
    from maibot_sdk.types import HookMode
except ImportError:  # 兼容把 HookMode 只在 components 里导出的旧版本 SDK
    from maibot_sdk.components import HookMode

from .bilibili import BilibiliClient, BilibiliError, Video, merge_videos
from .config import BilibiliInterestShareConfig
from .interest import (
    InterestProfile,
    ScoreResult,
    extract_interest_keywords,
    rank_videos,
)
from .llm_judge import LlmJudge
from .media import FrameExtractor, cleanup_dir, safe_bvid
from .persona import PersonaProvider
from .store import COOLDOWN_ACTIVE_SEARCH, COOLDOWN_SHIWEI, SeenStore
from .textfilter import filter_comments, filter_danmaku

logger = logging.getLogger("plugin.bilibili_interest_share")

PLUGIN_ID = "github.TommyAAKK.bilibili_interest_share"
_MAX_HISTORY_ITEMS = 8
# 白名单 stream_id 缓存时间（秒）
_ALLOWED_STREAMS_TTL_SECONDS = 300.0


# ----------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------


def _parse_hhmm(value: str) -> Optional[int]:
    """``"8:30"`` → 510（当天第几分钟）。"""

    text = (value or "").strip()
    if not text:
        return None
    parts = text.replace("：", ":").split(":")
    if len(parts) != 2:
        return None
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError:
        return None
    if not (0 <= hour <= 24 and 0 <= minute < 60):
        return None
    return min(hour * 60 + minute, 24 * 60)


def _parse_active_hours(value: str) -> Optional[Tuple[int, int]]:
    """解析 ``"9:00-23:30"``；空字符串或非法值返回 ``None``（表示全天）。"""

    text = (value or "").strip()
    if not text:
        return None
    for separator in ("-", "~", "～", "—"):
        if separator in text:
            start_text, _, end_text = text.partition(separator)
            start = _parse_hhmm(start_text)
            end = _parse_hhmm(end_text)
            if start is None or end is None:
                return None
            return start, end
    return None


def _in_active_window(value: str, *, now: Optional[datetime] = None) -> bool:
    """判断当前时间是否落在活跃时间段内（支持跨午夜）。"""

    window = _parse_active_hours(value)
    if window is None:
        return True
    start, end = window
    current = now or datetime.now()
    minute = current.hour * 60 + current.minute
    if start == end:
        return True
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end


def _message_text(message: Dict[str, Any]) -> str:
    """从宿主序列化的消息 dict 里取纯文本。"""

    if not isinstance(message, dict):
        return ""
    text = message.get("processed_plain_text")
    if isinstance(text, str) and text.strip():
        return text.strip()
    parts: List[str] = []
    raw = message.get("raw_message")
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            data = item.get("data")
            if isinstance(data, dict) and isinstance(data.get("text"), str):
                parts.append(data["text"])
            elif isinstance(item.get("text"), str):
                parts.append(item["text"])
    return "".join(parts).strip()


def _stream_id_of(stream: Any) -> str:
    """从 stream 对象 / dict / 字符串里取 stream_id。"""

    if isinstance(stream, str):
        return stream.strip()
    if isinstance(stream, dict):
        for key in ("stream_id", "session_id"):
            value = stream.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def filter_messages_by_lookback(
    messages: Sequence[Dict[str, Any]], lookback_hours: float
) -> List[Dict[str, Any]]:
    """按时间窗过滤消息（宿主 ``get_recent`` 不支持时间参数）。

    ``timestamp`` 缺失或无法解析时保留该条，避免把格式差异当成「没有消息」。
    """

    if lookback_hours <= 0:
        return list(messages)
    cutoff = time.time() - lookback_hours * 3600
    kept: List[Dict[str, Any]] = []
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        raw = message.get("timestamp")
        try:
            stamp = float(str(raw).strip()) if raw not in (None, "") else None
        except (TypeError, ValueError):
            stamp = None
        if stamp is None or stamp >= cutoff:
            kept.append(message)
    return kept


def _video_from_record(record: Dict[str, Any]) -> Video:
    """把已刷名单里的记录还原成 :class:`Video`（用于重发）。"""

    return Video(
        bvid=str(record.get("bvid") or ""),
        title=str(record.get("title") or ""),
        description="",
        owner=str(record.get("owner") or ""),
        partition=str(record.get("partition") or ""),
        duration=int(record.get("duration") or 0),
        source="history",
    )


def format_share_header(video: Video) -> str:
    """B 站标准转发格式：【标题】 链接。"""

    return f"【{video.title}】 {video.url}"


# ----------------------------------------------------------------------
# 插件主体
# ----------------------------------------------------------------------


class BilibiliInterestSharePlugin(MaiBotPlugin):
    """定时刷 B 站并按兴趣分享视频。"""

    config_model = BilibiliInterestShareConfig

    def __init__(self) -> None:
        super().__init__()
        self._loop_task: Optional[asyncio.Task] = None
        self._client: Optional[BilibiliClient] = None
        self._store: Optional[SeenStore] = None
        self._frames: Optional[FrameExtractor] = None
        self._judge: Optional[LlmJudge] = None
        self._persona: Optional[PersonaProvider] = None
        self._data_dir: Optional[Path] = None
        self._allowed_stream_ids: Set[str] = set()
        self._allowed_streams_at: float = 0.0
        self._round_lock = asyncio.Lock()
        self.last_round: Optional[Dict[str, Any]] = None
        self.last_round_at: Optional[float] = None
        self.next_run_at: Optional[float] = None

    # -- 生命周期 ------------------------------------------------------

    async def on_load(self) -> None:
        config = self.config
        if not config.plugin.enabled:
            logger.info("麦麦刷B站插件已禁用，跳过启动")
            return

        self._data_dir = self._resolve_data_dir()
        self._data_dir.mkdir(parents=True, exist_ok=True)

        self._store = SeenStore(
            self._data_dir / "state.json",
            dedupe_days=config.advanced.dedupe_days,
            max_records=config.advanced.max_seen_records,
        )
        self._store.load()

        self._client = BilibiliClient(
            timeout=config.advanced.request_timeout_seconds,
            cookie=config.advanced.cookie,
            user_agent=config.advanced.user_agent,
        )
        self._frames = FrameExtractor(
            ffmpeg_path=config.advanced.ffmpeg_path,
            frame_count=config.vision.frame_count,
            max_width=config.vision.max_width,
            max_download_mb=config.vision.max_download_mb,
            timeout=config.vision.timeout_seconds,
        )
        self._persona = PersonaProvider(
            enabled=config.persona.enabled,
            config_get=self._config_getter(),
            override_name=config.persona.name,
            override_personality=config.persona.personality,
            override_reply_style=config.persona.reply_style,
            style_hint=config.persona.style_hint,
            cache_seconds=config.persona.cache_seconds,
        )
        self._judge = LlmJudge(
            getattr(self.ctx, "llm", None),
            task_name=config.interest.llm_task_name,
            vision_task_name=config.vision.task_name,
            temperature=config.interest.llm_temperature,
            persona_provider=self._persona,
        )
        await self._persona.refresh()
        await self._refresh_allowed_streams()

        self._loop_task = asyncio.create_task(
            self._run_loop(), name="bilibili-interest-share-loop"
        )
        logger.info(
            "麦麦刷B站插件已启动：每 %s 分钟一轮，白名单 %s 个会话",
            config.schedule.interval_minutes,
            len([item for item in config.streams if item.enabled]),
        )

    async def on_unload(self) -> None:
        task = self._loop_task
        self._loop_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001
                logger.warning("后台任务退出时出错：%s", exc)
        if self._store is not None:
            try:
                self._store.save()
            except Exception as exc:  # noqa: BLE001
                logger.warning("保存状态失败：%s", exc)
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception as exc:  # noqa: BLE001
                logger.warning("关闭 HTTP 客户端失败：%s", exc)
        self._client = None
        self._judge = None
        self._persona = None

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        logger.info("麦麦刷B站配置已更新（scope=%s, version=%s），重启后台循环", scope, version)
        await self.on_unload()
        await self.on_load()

    # -- 后台循环 ------------------------------------------------------

    async def _run_loop(self) -> None:
        config = self.config
        interval = max(60.0, float(config.schedule.interval_minutes) * 60.0)
        if config.schedule.run_on_start:
            await self._safe_round(reason="startup")
        while True:
            jitter = 0.0
            if config.schedule.jitter_seconds > 0:
                jitter = random.uniform(0.0, float(config.schedule.jitter_seconds))
            delay = max(30.0, interval + jitter)
            self.next_run_at = time.time() + delay
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return
            await self._safe_round(reason="scheduled")

    async def _safe_round(self, *, reason: str) -> None:
        try:
            summary = await self.run_round(reason=reason)
            if self._debug_enabled():
                logger.info("分享轮次结束：%s", summary)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 后台循环不能因单轮失败而退出
            logger.warning("分享轮次失败：%s", exc, exc_info=self._debug_enabled())

    # -- 主流程 --------------------------------------------------------

    async def run_round(self, *, reason: str = "scheduled", manual: bool = False) -> Dict[str, Any]:
        """跑一轮自动分享。"""

        config = self.config
        if not config.plugin.enabled:
            return {"ok": False, "skipped": "disabled", "reason": reason, "fetched": 0, "shared": 0}
        if not manual and not _in_active_window(config.schedule.active_hours):
            return {
                "ok": True,
                "skipped": "outside_active_hours",
                "reason": reason,
                "fetched": 0,
                "shared": 0,
            }
        targets = [item for item in config.streams if item.enabled]
        if not targets:
            return {"ok": True, "skipped": "no_targets", "reason": reason, "fetched": 0, "shared": 0}
        if self._client is None or self._store is None:
            return {"ok": False, "skipped": "not_loaded", "reason": reason, "fetched": 0, "shared": 0}

        async with self._round_lock:
            # 每轮开始前刷新人设与白名单缓存（都带 TTL，不会频繁请求宿主）
            if self._persona is not None:
                try:
                    await self._persona.ensure()
                except Exception as exc:  # noqa: BLE001 - 人设读不到就用中性语气
                    logger.debug("刷新人设失败：%s", exc)
            await self._ensure_allowed_streams()
            resolved: List[Tuple[Any, str, InterestProfile]] = []
            for target in targets:
                stream_id = await self._resolve_stream(target)
                if not stream_id:
                    logger.warning("白名单会话无法解析：%s", target.name or target.group_id)
                    continue
                profile = await self._build_profile(target, stream_id)
                if not profile:
                    logger.info("会话 %s 没有可用兴趣，跳过", stream_id)
                    continue
                resolved.append((target, stream_id, profile))
            if not resolved:
                return {
                    "ok": True,
                    "skipped": "no_stream",
                    "reason": reason,
                    "fetched": 0,
                    "shared": 0,
                }

            keywords = self._collect_search_keywords([item[2] for item in resolved])
            videos, category_of = await self._fetch_candidates(keywords)
            videos = self._prefilter(videos)
            shared: List[str] = []
            target_details: List[Dict[str, Any]] = []
            for target, stream_id, profile in resolved:
                try:
                    detail = await self._process_target(
                        target, stream_id, profile, videos, category_of
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - 单个会话失败不影响其它会话
                    logger.warning("处理会话 %s 失败：%s", stream_id, exc)
                    detail = {
                        "target": target.name or stream_id,
                        "stream_id": stream_id,
                        "shared": 0,
                        "matched": [],
                        "bvids": [],
                    }
                target_details.append(detail)
                shared.extend(detail.get("bvids") or [])

            summary = {
                "ok": True,
                "reason": reason,
                "fetched": len(videos),
                "shared": len(shared),
                "keywords": keywords,
                "targets": target_details,
                "target_count": len(resolved),
                "at": time.time(),
                "bvids": shared,
            }
            self.last_round = summary
            self.last_round_at = time.time()
            self._store.record_run(
                {key: value for key, value in summary.items() if key != "bvids"}
            )
            self._store.save()
            return summary

    async def _process_target(
        self,
        target: Any,
        stream_id: str,
        profile: InterestProfile,
        videos: Sequence[Video],
        category_of: Dict[str, str],
    ) -> Dict[str, Any]:
        """给一个会话挑视频并分享，返回本次会话的分享详情。"""

        config = self.config
        detail: Dict[str, Any] = {
            "target": target.name or stream_id,
            "stream_id": stream_id,
            "shared": 0,
            "matched": [],
            "bvids": [],
        }
        allowed = [item.strip() for item in (target.categories or []) if item and item.strip()]
        candidates = [
            video
            for video in videos
            if not allowed
            or not category_of.get(video.bvid)
            or category_of.get(video.bvid) in allowed
        ]
        # 每个类别可以有自己的最低播放量（0 = 沿用 [filter].min_play）
        category_min_play = {
            category.name: int(category.min_play)
            for category in config.categories
            if category.min_play and category.min_play > 0
        }
        if category_min_play:
            candidates = [
                video
                for video in candidates
                if video.play
                >= category_min_play.get(category_of.get(video.bvid, ""), 0)
            ]
        # 已刷名单内的视频不再自动分享
        candidates = [video for video in candidates if not self._store.is_seen(video.bvid)]
        if not candidates:
            return detail

        results = rank_videos(
            candidates,
            profile,
            now=time.time(),
            min_score=config.scoring.min_score,
            require_interest_match=config.scoring.require_interest_match,
            interest_weight=config.scoring.interest_weight,
            title_match_boost=config.scoring.title_match_boost,
            hot_weight=config.scoring.hot_weight,
            freshness_weight=config.scoring.freshness_weight,
            freshness_days=config.scoring.freshness_days,
            min_duration=config.filter.min_duration_seconds,
            max_duration=config.filter.max_duration_seconds,
            min_play=config.filter.min_play,
            min_like=config.filter.min_like,
            block_keywords=config.filter.block_keywords,
            block_up_names=config.filter.block_up_names,
            exclude_partitions=config.filter.exclude_partitions,
            use_tags=config.interest.use_tags,
            tag_weight=config.interest.tag_weight,
        )
        for result in results:
            result.category = category_of.get(result.video.bvid, "")
        if not results:
            return detail

        limit = int(target.max_share_per_round or config.schedule.max_share_per_round or 1)
        picked = await self._pick_candidates(results, profile, limit)
        shared: List[str] = []
        for result, judged in picked:
            narrative_text, intro_text = "", ""
            if judged:
                narrative_text = str(judged.get("narrative") or "").strip()
                intro_text = str(judged.get("intro") or "").strip()
            if not narrative_text and config.share.narrative_enabled:
                narrative_text = (result.video.description or "").strip()[
                    : config.share.narrative_max_chars
                ]
            sent = await self._send_parts(
                stream_id,
                result.video,
                narrative_text,
                kind="auto",
                intro=intro_text,
            )
            if sent:
                self._store.mark_shared(
                    result.video,
                    narrative=narrative_text,
                    intro=intro_text,
                    kind="auto",
                    stream_id=stream_id,
                )
                shared.append(result.video.bvid)
        detail["shared"] = len(shared)
        detail["bvids"] = shared
        detail["matched"] = [
            {"bvid": result.video.bvid, "title": result.video.title, "score": round(result.score, 2)}
            for result in results[:6]
        ]
        return detail

    async def _pick_candidates(
        self,
        results: Sequence[ScoreResult],
        profile: InterestProfile,
        limit: int,
    ) -> List[Tuple[ScoreResult, Optional[Dict[str, Any]]]]:
        """用 LLM 语义判断筛选最终入选视频（含抽帧识图）。"""

        config = self.config
        limit = max(1, int(limit))
        if not config.interest.use_llm_judge or self._judge is None or not self._judge.available:
            return [(result, None) for result in results[:limit]]

        interests = [term for term, _weight in profile.sorted_terms()[:10]]
        max_check = max(limit, int(config.interest.llm_max_candidates or 0), 1)
        picked: List[Tuple[ScoreResult, Optional[Dict[str, Any]]]] = []
        for result in results[:max_check]:
            video = result.video
            danmaku, comments, subtitle = await self._collect_detail(video)
            judged = await self._judge.judge(
                video,
                interests=interests,
                danmaku=danmaku,
                comments=comments,
                subtitle=subtitle,
                narrative_max_chars=config.share.narrative_max_chars,
                intro_enabled=config.share.intro_enabled,
                intro_max_chars=config.share.intro_max_chars,
            )
            if judged is not None:
                if not judged.get("relevant"):
                    continue
                if float(judged.get("score") or 0.0) < 4.0:
                    continue
                category = str(judged.get("category") or "").strip()
                if category:
                    result.category = category

            # 只对入选视频抽帧识图，减少下载与视觉模型调用
            frames_desc = ""
            if config.vision.enabled and self._frames is not None:
                frames = await self._capture_frames(video)
                if frames:
                    frames_desc = await self._judge.describe_frames(video, frames)
                    if frames_desc:
                        refined = await self._judge.judge(
                            video,
                            interests=interests,
                            danmaku=danmaku,
                            comments=comments,
                            subtitle=subtitle,
                            frames_desc=frames_desc,
                            narrative_max_chars=config.share.narrative_max_chars,
                            intro_enabled=config.share.intro_enabled,
                            intro_max_chars=config.share.intro_max_chars,
                        )
                        if refined is not None:
                            judged = refined

            picked.append((result, judged))
            if len(picked) >= limit:
                break
        return picked

    async def _collect_detail(self, video: Video) -> Tuple[List[str], List[str], str]:
        """抓取弹幕 / 热门评论 / 字幕（失败时返回空）。"""

        config = self.config
        danmaku: List[str] = []
        comments: List[str] = []
        subtitle = ""
        if self._client is None:
            return danmaku, comments, subtitle

        if config.danmaku.danmaku_enabled:
            try:
                raw = await self._client.fetch_danmaku(
                    video.bvid, limit=config.danmaku.danmaku_fetch_limit
                )
                danmaku = filter_danmaku(
                    raw,
                    limit=config.danmaku.danmaku_limit,
                    filter_ai=config.danmaku.filter_ai_spam,
                )
            except BilibiliError as exc:
                logger.debug("抓取弹幕失败 %s：%s", video.bvid, exc)
        if config.danmaku.comments_enabled:
            try:
                raw_comments = await self._client.fetch_hot_comments(
                    video.bvid, limit=config.danmaku.comments_limit
                )
                comments = filter_comments(
                    raw_comments,
                    limit=config.danmaku.comments_limit,
                    filter_ai=config.danmaku.filter_ai_spam,
                )
            except BilibiliError as exc:
                logger.debug("抓取评论失败 %s：%s", video.bvid, exc)
        if config.interest.use_subtitles:
            try:
                subtitle = await self._client.fetch_subtitle(video.bvid)
            except BilibiliError as exc:
                logger.debug("抓取字幕失败 %s：%s", video.bvid, exc)
        return danmaku, comments, subtitle

    async def _capture_frames(self, video: Video) -> List[str]:
        """下载视频并抽帧，返回 data URL 列表。"""

        if self._frames is None or self._client is None or self._data_dir is None:
            return []
        if not self._frames.available:
            logger.debug("未找到 ffmpeg，跳过抽帧")
            return []
        work_dir = self._data_dir / "frames" / safe_bvid(getattr(video, "bvid", ""))
        try:
            frames = await self._frames.capture(self._client, video, work_dir)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.debug("抽帧失败 %s：%s", video.bvid, exc)
            frames = []
        finally:
            cleanup_dir(work_dir)
        return frames

    # -- 抓取 ----------------------------------------------------------

    async def _fetch_candidates(
        self, keywords: Sequence[str]
    ) -> Tuple[List[Video], Dict[str, str]]:
        """按兴趣关键词与配置分类抓候选视频，返回 ``(视频列表, bvid→分类)``。"""

        config = self.config
        if self._client is None:
            raise BilibiliError("客户端尚未初始化")
        videos: List[Video] = []
        category_of: Dict[str, str] = {}
        attempts = 0
        failures = 0
        delay = max(0.0, float(config.sources.search_delay_seconds))

        async def _search(keyword: str, order: str, category: str) -> None:
            nonlocal attempts, failures
            keyword = (keyword or "").strip()
            if not keyword:
                return
            pages = max(1, int(config.sources.search_pages))
            for page in range(1, pages + 1):
                attempts += 1
                try:
                    found = await self._client.fetch_search(
                        keyword, page=page, order=order or ""
                    )
                except BilibiliError as exc:
                    failures += 1
                    logger.debug("搜索「%s」第 %s 页失败：%s", keyword, page, exc)
                    return
                for video in found:
                    videos.append(video)
                    # 先用普通关键词搜到的没有分类，后续按分类搜到时要补上
                    if category or video.bvid not in category_of:
                        category_of[video.bvid] = category
                if not found:
                    return
                if delay:
                    await asyncio.sleep(delay)

        if config.sources.search_enabled:
            for keyword in keywords:
                await _search(keyword, config.sources.search_order, "")
        for category in config.categories:
            if not category.enabled:
                continue
            for keyword in category.keywords or []:
                await _search(keyword, category.search_order, category.name)

        if config.sources.popular_enabled:
            for page in range(1, max(1, int(config.sources.popular_pages)) + 1):
                attempts += 1
                try:
                    found = await self._client.fetch_popular(
                        page_size=config.sources.popular_page_size, page=page
                    )
                except BilibiliError as exc:
                    failures += 1
                    logger.debug("抓取热门失败：%s", exc)
                    continue
                videos.extend(found)
            if delay:
                await asyncio.sleep(delay)

        if config.sources.ranking_enabled:
            for rid in config.sources.ranking_rids or [0]:
                attempts += 1
                try:
                    videos.extend(await self._client.fetch_ranking(rid=int(rid)))
                except BilibiliError as exc:
                    failures += 1
                    logger.debug("抓取排行榜失败：%s", exc)

        if attempts and failures == attempts and not videos:
            raise BilibiliError("所有 B 站接口都请求失败，请检查网络或 cookie")
        return merge_videos([videos]), category_of

    def _prefilter(self, videos: Sequence[Video]) -> List[Video]:
        """排除官方物料与过旧视频。"""

        config = self.config
        now = time.time()
        kept: List[Video] = []
        for video in videos:
            if config.filter.exclude_official and self._is_official(video):
                continue
            if config.filter.max_age_days > 0 and video.pubdate:
                age_days = max(0.0, (now - float(video.pubdate)) / 86400.0)
                if age_days > config.filter.max_age_days:
                    continue
            kept.append(video)
        return kept

    def _is_official(self, video: Video) -> bool:
        haystack = f"{video.title or ''} {video.description or ''}".lower()
        for keyword in self.config.filter.official_keywords or []:
            token = (keyword or "").strip().lower()
            if token and token in haystack:
                return True
        return False

    # -- 兴趣与关键词 --------------------------------------------------

    def _config_getter(self):
        """拿到宿主的 ``config.get`` 能力（拿不到时返回 None）。"""

        config_cap = getattr(self.ctx, "config", None)
        getter = getattr(config_cap, "get", None)
        return getter if callable(getter) else None

    async def _refresh_allowed_streams(self) -> Set[str]:
        """解析白名单里所有启用的会话，缓存可用的 stream_id 集合。

        重发（reshare）等被动触发的发送只允许落在白名单会话里，
        这个集合就是判断依据。
        """

        config = self.config
        allowed: Set[str] = set()
        for target in config.streams or []:
            if not getattr(target, "enabled", False):
                continue
            stream_id = await self._resolve_stream(target)
            if stream_id:
                allowed.add(stream_id)
        self._allowed_stream_ids = allowed
        self._allowed_streams_at = time.time()
        return allowed

    async def _ensure_allowed_streams(self) -> Set[str]:
        """白名单集合为空或过期时重新解析。"""

        if self._allowed_stream_ids and (
            time.time() - self._allowed_streams_at
        ) < _ALLOWED_STREAMS_TTL_SECONDS:
            return self._allowed_stream_ids
        return await self._refresh_allowed_streams()

    async def _resolve_stream(self, target: Any) -> str:
        """把白名单配置解析成真实的 stream_id。"""

        if target.stream_id:
            return str(target.stream_id).strip()
        chat = getattr(self.ctx, "chat", None)
        if chat is None:
            return ""
        try:
            if target.group_id:
                stream = await chat.get_stream_by_group_id(str(target.group_id))
                resolved = _stream_id_of(stream)
                if resolved:
                    return resolved
            if target.user_id:
                stream = await chat.get_stream_by_user_id(str(target.user_id))
                resolved = _stream_id_of(stream)
                if resolved:
                    return resolved
        except Exception as exc:  # noqa: BLE001
            logger.warning("解析白名单会话失败（%s）：%s", target.name or target.group_id, exc)
        return ""

    async def _build_profile(self, target: Any, stream_id: str) -> InterestProfile:
        """构建会话兴趣画像：配置里的显式兴趣 + 近期聊天关键词。"""

        config = self.config
        profile = InterestProfile()
        for term in target.interests or []:
            normalized = (term or "").strip()
            if normalized:
                profile.add(normalized, config.interest.explicit_weight)

        if config.interest.use_recent_chat and target.auto_interest:
            messages = await self._recent_messages(stream_id)
            keywords = extract_interest_keywords(
                messages,
                top_n=config.interest.top_keywords,
                min_count=config.interest.keyword_min_count,
                extra_stopwords=config.interest.extra_stopwords,
            )
            for term, count in keywords:
                weight = config.interest.auto_weight * min(
                    2.0, 0.6 + 0.4 * math.log2(1 + count)
                )
                profile.add(term, weight)
        return profile

    async def _recent_messages(self, stream_id: str) -> List[Dict[str, Any]]:
        message_cap = getattr(self.ctx, "message", None)
        if message_cap is None:
            return []
        config = self.config
        try:
            # 宿主 SDK 的 get_recent 只接受 chat_id / limit，时间窗需要自己过滤。
            result = await message_cap.get_recent(
                chat_id=stream_id,
                limit=int(config.interest.max_messages_per_stream),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取会话 %s 近期消息失败：%s", stream_id, exc)
            return []
        if isinstance(result, dict):
            messages = result.get("messages")
            messages = list(messages) if isinstance(messages, list) else []
        else:
            messages = list(result or [])
        return filter_messages_by_lookback(
            messages, float(config.interest.lookback_hours)
        )

    def _collect_search_keywords(self, profiles: Sequence[InterestProfile]) -> List[str]:
        """按出现频次从各会话画像里挑出本轮搜索关键词。"""

        counter: Dict[str, float] = {}
        for profile in profiles:
            for term, weight in profile.sorted_terms():
                counter[term] = counter.get(term, 0.0) + float(weight)
        ranked = sorted(counter.items(), key=lambda item: (-item[1], item[0]))
        limit = max(1, int(self.config.sources.search_keywords_per_round))
        return [term for term, _weight in ranked[:limit]]

    # -- 发送 ----------------------------------------------------------

    async def _send_text(self, stream_id: str, text: str) -> bool:
        """发送一条文本；``stream_id`` 为空时返回 False。"""

        text = (text or "").strip()
        if not text or not stream_id:
            return False
        send = getattr(self.ctx, "send", None)
        if send is None:
            return False
        try:
            result = await send.text(text, stream_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("发送消息失败（%s）：%s", stream_id, exc)
            return False
        if isinstance(result, dict):
            return bool(result.get("sent"))
        return bool(result)

    async def _send_parts(
        self,
        stream_id: str,
        video: Video,
        narrative: str,
        *,
        kind: str = "auto",
        intro: str = "",
    ) -> bool:
        """按「推荐语 / 标题+链接 / 自然语言介绍」分条发送。"""

        config = self.config
        parts: List[str] = []
        if kind != "shiwei" and config.share.intro_enabled:
            intro_text = (intro or "").strip()
            if intro_text:
                parts.append(intro_text)
        parts.append(format_share_header(video))
        if kind != "shiwei" and config.share.narrative_enabled:
            body = (narrative or "").strip()
            if body:
                parts.append(body)
        if config.share.include_stats_line:
            parts.append(
                f"🔥 {video.play} 播放 · {video.like} 点赞"
                + (f" · UP：{video.owner}" if video.owner else "")
            )

        if not config.share.send_as_parts:
            return await self._send_text(stream_id, "\n".join(parts))

        sent_any = False
        for index, part in enumerate(parts):
            if await self._send_text(stream_id, part):
                sent_any = True
            if index < len(parts) - 1 and config.share.part_delay_seconds > 0:
                await asyncio.sleep(float(config.share.part_delay_seconds))
        return sent_any

    # -- 主动搜索 ------------------------------------------------------

    async def search_and_share(
        self, query: str, stream_id: str, *, limit: int = 1
    ) -> Dict[str, Any]:
        """响应用户要求主动搜索并分享（有独立冷却）。"""

        config = self.config
        query = (query or "").strip()
        if not query:
            return {"ok": False, "reason": "empty_query"}
        if not config.plugin.enabled:
            return {"ok": False, "reason": "disabled"}
        if self._client is None or self._store is None:
            return {"ok": False, "reason": "not_loaded"}

        remaining = self._store.cooldown_remaining(
            COOLDOWN_ACTIVE_SEARCH, config.schedule.search_cooldown_seconds
        )
        if remaining > 0:
            return {"ok": False, "reason": "cooldown", "remaining": int(remaining)}

        try:
            found = await self._client.fetch_search(query, page=1, order=config.sources.search_order)
        except BilibiliError as exc:
            return {"ok": False, "reason": "fetch_failed", "error": str(exc)}
        videos = self._prefilter(found)
        if not videos:
            return {"ok": False, "reason": "no_candidates"}

        profile = InterestProfile()
        profile.add(query, config.interest.explicit_weight)
        results = rank_videos(
            videos,
            profile,
            now=time.time(),
            min_score=0.0,
            require_interest_match=False,
            interest_weight=config.scoring.interest_weight,
            title_match_boost=config.scoring.title_match_boost,
            hot_weight=config.scoring.hot_weight,
            freshness_weight=config.scoring.freshness_weight,
            freshness_days=config.scoring.freshness_days,
            min_duration=config.filter.min_duration_seconds,
            max_duration=config.filter.max_duration_seconds,
            min_play=config.filter.min_play,
            min_like=config.filter.min_like,
            block_keywords=config.filter.block_keywords,
            block_up_names=config.filter.block_up_names,
            exclude_partitions=config.filter.exclude_partitions,
            use_tags=config.interest.use_tags,
            tag_weight=config.interest.tag_weight,
        )
        if not results:
            return {"ok": False, "reason": "filtered_out"}

        shared: List[str] = []
        for result in results[: max(1, int(limit))]:
            video = result.video
            cached = self._store.get(video.bvid)
            if cached and (cached.get("narrative") or cached.get("summary")):
                # 名单内视频再次分享：直接复用之前的介绍文案，不重新生成
                await self._send_parts(
                    stream_id,
                    video,
                    str(cached.get("narrative") or cached.get("summary") or ""),
                    kind="reshare",
                    intro=str(cached.get("intro") or ""),
                )
                shared.append(video.bvid)
                continue
            danmaku, comments, subtitle = await self._collect_detail(video)
            judged = None
            if self._judge is not None and self._judge.available:
                judged = await self._judge.judge(
                    video,
                    interests=[query],
                    danmaku=danmaku,
                    comments=comments,
                    subtitle=subtitle,
                    narrative_max_chars=config.share.narrative_max_chars,
                    intro_enabled=config.share.intro_enabled,
                    intro_max_chars=config.share.intro_max_chars,
                )
            narrative_text = str((judged or {}).get("narrative") or "") or (
                video.description or ""
            )[: config.share.narrative_max_chars]
            intro_text = str((judged or {}).get("intro") or "")
            if await self._send_parts(
                stream_id,
                video,
                narrative_text,
                kind="search",
                intro=intro_text,
            ):
                self._store.mark_shared(
                    video,
                    narrative=narrative_text,
                    intro=intro_text,
                    kind="search",
                    stream_id=stream_id,
                )
                shared.append(video.bvid)

        if shared:
            self._store.mark_cooldown(COOLDOWN_ACTIVE_SEARCH)
            self._store.save()
        return {"ok": bool(shared), "shared": shared, "query": query}

    async def share_shiwei(self, stream_id: str) -> Dict[str, Any]:
        """``/赤石视频``：抓取并分享抽象整活类视频，不带总结和看点。"""

        config = self.config
        if not config.plugin.enabled:
            return {"ok": False, "reason": "disabled"}
        if not config.shiwei.enabled:
            return {"ok": False, "reason": "disabled"}
        if self._client is None or self._store is None:
            return {"ok": False, "reason": "not_loaded"}

        remaining = self._store.cooldown_remaining(COOLDOWN_SHIWEI, config.shiwei.cooldown_seconds)
        if remaining > 0:
            return {"ok": False, "reason": "cooldown", "remaining": int(remaining)}

        keywords = [item for item in config.shiwei.keywords if item][
            : max(1, int(config.shiwei.search_keywords_per_round))
        ]
        videos: List[Video] = []
        for keyword in keywords:
            try:
                videos.extend(await self._client.fetch_search(keyword, page=1))
            except BilibiliError as exc:
                logger.debug("赤石搜索「%s」失败：%s", keyword, exc)
            if config.sources.search_delay_seconds > 0:
                await asyncio.sleep(float(config.sources.search_delay_seconds))
        if not videos:
            return {"ok": False, "reason": "no_candidates"}

        blocked = [item.lower() for item in config.shiwei.block_keywords or []]
        shiwei_words = [item for item in config.shiwei.keywords if item]
        scored: List[Tuple[float, Video]] = []
        for video in merge_videos([videos]):
            haystack = f"{video.title} {video.description} {getattr(video, 'tags', '')}".lower()
            if any(token and token in haystack for token in blocked):
                continue
            if video.play < config.shiwei.min_play:
                continue
            if video.duration < config.shiwei.min_duration_seconds:
                continue
            if video.duration > config.shiwei.max_duration_seconds:
                continue
            hits = sum(1 for word in shiwei_words if word.lower() in haystack)
            if not hits:
                continue
            scored.append((hits * 2.0 + math.log10(1 + video.play) * 0.3, video))
        if not scored:
            return {"ok": False, "reason": "no_candidates"}
        scored.sort(key=lambda item: -item[0])
        shared: List[str] = []
        for _score, video in scored[: max(1, int(config.shiwei.max_share))]:
            if await self._send_parts(stream_id, video, "", kind="shiwei"):
                self._store.mark_shared(video, kind="shiwei", stream_id=stream_id)
                shared.append(video.bvid)
        if shared:
            self._store.mark_cooldown(COOLDOWN_SHIWEI)
            self._store.save()
        return {"ok": bool(shared), "shared": shared}

    # -- 重发（聊天自然语言触发） --------------------------------------

    @HookHandler(
        "chat.receive.before_process",
        mode=HookMode.OBSERVE,
        description="识别「再发一遍之前那个视频」这类请求，复用缓存总结重发",
    )
    async def on_chat_receive(
        self, hook_name: str, message: Dict[str, Any], **kwargs: Any
    ) -> None:
        del hook_name, kwargs
        try:
            await self._maybe_reshare(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - Hook 里不能抛错
            logger.debug("重发意图识别失败：%s", exc)
        return None

    async def _maybe_reshare(self, message: Dict[str, Any]) -> None:
        config = self.config
        if not config.plugin.enabled or not config.reshare.enabled:
            return
        if self._store is None:
            return
        text = _message_text(message)
        if not text or len(text) > 80:
            return
        if text.startswith("【"):  # 跳过插件自己发的分享消息
            return
        if not any(keyword in text for keyword in config.reshare.trigger_keywords):
            return
        if not any(keyword in text for keyword in config.reshare.history_keywords):
            return
        stream_id = _stream_id_of(message)
        if not stream_id:
            return
        # 只在白名单会话里重发：这个 Hook 是聊天消息触发的被动发送，
        # 必须确认当前会话确实是配置里启用的白名单会话。
        allowed = await self._ensure_allowed_streams()
        if stream_id not in allowed:
            logger.debug("会话 %s 不在白名单，忽略重发请求", stream_id)
            return
        matches = self._store.find_by_query(
            text,
            limit=1,
            window_days=config.reshare.match_window_days,
            min_score=config.reshare.min_match_score,
        )
        if not matches:
            return
        _score, record = matches[0]
        video = _video_from_record(record)
        if not video.bvid or not video.title:
            return
        sent = await self._send_parts(
            stream_id,
            video,
            str(record.get("narrative") or record.get("summary") or ""),
            kind="reshare",
            intro=str(record.get("intro") or ""),
        )
        if sent and config.reshare.reply_enabled:
            await self._send_text(stream_id, "好嘞，再发一遍～")
        logger.info("按用户要求重发了 %s（%s）", video.bvid, video.title)

    # -- 指令 ----------------------------------------------------------

    @Command("bili_share", description="立即跑一轮 B 站分享", pattern=r"^/bili-share")
    async def cmd_share(self, **kwargs: Any) -> Tuple[bool, str, int]:
        stream_id = _stream_id_of(kwargs)
        summary = await self.run_round(reason="manual", manual=True)
        text = self._format_summary(summary)
        if stream_id:
            await self._send_text(stream_id, text)
        return True, text, 2

    @Command("bili_status", description="查看 B 站分享插件状态", pattern=r"^/bili-status")
    async def cmd_status(self, **kwargs: Any) -> Tuple[bool, str, int]:
        stream_id = _stream_id_of(kwargs)
        text = self._format_status()
        if stream_id:
            await self._send_text(stream_id, text)
        return True, text, 2

    @Command("bili_test", description="预览本轮候选与打分（不发送）", pattern=r"^/bili-test")
    async def cmd_test(self, **kwargs: Any) -> Tuple[bool, str, int]:
        stream_id = _stream_id_of(kwargs)
        text = await self._format_preview()
        if stream_id:
            await self._send_text(stream_id, text)
        return True, text, 2

    @Command(
        "bili_search",
        description="主动搜索并分享指定关键词的视频",
        pattern=r"^/bili-search\s*(?P<query>.*)$",
        aliases=["/b站搜索"],
    )
    async def cmd_search(self, **kwargs: Any) -> Tuple[bool, str, int]:
        stream_id = _stream_id_of(kwargs)
        query = self._command_arg(kwargs, "query")
        if not query:
            text = "用法：/bili-search 关键词"
            if stream_id:
                await self._send_text(stream_id, text)
            return True, text, 2
        result = await self.search_and_share(query, stream_id)
        if result.get("ok"):
            text = f"已搜索并分享：{query}（{len(result.get('shared') or [])} 个）"
        elif result.get("reason") == "cooldown":
            text = f"主动搜索还在冷却中，请 {int(result.get('remaining') or 0)} 秒后再试"
        else:
            text = f"主动搜索没有成功：{result.get('reason')}"
        if stream_id:
            await self._send_text(stream_id, text)
        return True, text, 2

    @Command(
        "shiwei_video",
        description="抓一个抽象整活类视频分享（不带总结）",
        pattern=r"^/赤石视频",
    )
    async def cmd_shiwei(self, **kwargs: Any) -> Tuple[bool, str, int]:
        stream_id = _stream_id_of(kwargs)
        result = await self.share_shiwei(stream_id)
        if result.get("ok"):
            text = "赤石视频已送达。"
        elif result.get("reason") == "cooldown":
            text = f"赤石视频还在冷却中，请 {int(result.get('remaining') or 0)} 秒后再试"
        else:
            text = f"这次没捞到合适的赤石视频（{result.get('reason')}）"
        if stream_id:
            await self._send_text(stream_id, text)
        return True, text, 2

    # -- 文本输出 ------------------------------------------------------

    def _command_arg(self, kwargs: Dict[str, Any], name: str) -> str:
        groups = kwargs.get("matched_groups")
        if isinstance(groups, dict):
            value = groups.get(name)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for key in ("query", "text", "raw_message"):
            value = kwargs.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    def _format_summary(self, summary: Dict[str, Any]) -> str:
        if not summary.get("ok"):
            return f"B 站分享未能执行：{summary.get('skipped') or summary.get('reason') or '未知原因'}"
        skipped = summary.get("skipped")
        if skipped == "outside_active_hours":
            return "当前不在允许分享的时间段内。"
        if skipped == "no_targets":
            return "没有启用中的白名单会话，请先在配置里添加 [[streams]]。"
        if skipped == "no_stream":
            return "白名单会话解析失败，请检查 group_id / stream_id 配置。"
        return (
            f"麦麦刷B站完成：抓取 {summary.get('fetched', 0)} 个候选，"
            f"分享 {summary.get('shared', 0)} 个（会话 {summary.get('target_count', 0)} 个）。"
        )

    def _format_status(self) -> str:
        config = self.config
        lines = [
            "【麦麦刷B站】",
            f"启用：{'是' if config.plugin.enabled else '否'}",
            f"自动频率：每 {config.schedule.interval_minutes} 分钟（抖动 ±{config.schedule.jitter_seconds} 秒）",
            f"活跃时段：{config.schedule.active_hours or '全天'}",
            f"主动搜索冷却：{config.schedule.search_cooldown_seconds} 秒",
            f"白名单会话：{len([item for item in config.streams if item.enabled])} 个",
            f"LLM 判断：{'开' if config.interest.use_llm_judge else '关'}（{config.interest.llm_task_name}）",
            f"抽帧识图：{'开' if config.vision.enabled else '关'}（{config.vision.task_name}）",
        ]
        persona_desc = self._persona_desc()
        if persona_desc:
            lines.append(persona_desc)
        if self.next_run_at:
            remain = max(0, int(self.next_run_at - time.time()))
            lines.append(f"下一轮：约 {remain // 60} 分 {remain % 60} 秒后")
        if self._store is not None:
            stats = self._store.stats()
            lines.append(
                f"已记录视频：{stats.get('seen_count', 0)} 个，累计分享：{stats.get('total_shared', 0)} 次"
            )
        return "\n".join(lines)

    def _persona_desc(self) -> str:
        """状态里显示当前人设来源。"""

        provider = self._persona
        if provider is None:
            return ""
        if not getattr(provider, "enabled", True):
            return "人设语气：已关闭（分享文案用中性语气）"
        try:
            persona = provider.get()
        except Exception:  # noqa: BLE001
            return ""
        if persona.is_empty:
            return "人设语气：未读到人设（分享文案用中性语气）"
        source = "手动配置" if persona.source == "override" else "宿主配置"
        name = persona.name or "（未命名）"
        return f"人设语气：{name}（来自{source}）"

    async def _format_preview(self) -> str:
        config = self.config
        if self._client is None:
            return "插件尚未加载完成。"
        targets = [item for item in config.streams if item.enabled]
        if not targets:
            return "没有启用中的白名单会话。"
        target = targets[0]
        stream_id = await self._resolve_stream(target)
        if not stream_id:
            return "白名单会话解析失败。"
        profile = await self._build_profile(target, stream_id)
        keywords = self._collect_search_keywords([profile])
        videos, category_of = await self._fetch_candidates(keywords)
        videos = self._prefilter(videos)
        videos = [video for video in videos if not self._store.is_seen(video.bvid)]
        results = rank_videos(
            videos,
            profile,
            now=time.time(),
            min_score=config.scoring.min_score,
            require_interest_match=config.scoring.require_interest_match,
            interest_weight=config.scoring.interest_weight,
            title_match_boost=config.scoring.title_match_boost,
            hot_weight=config.scoring.hot_weight,
            freshness_weight=config.scoring.freshness_weight,
            freshness_days=config.scoring.freshness_days,
            min_duration=config.filter.min_duration_seconds,
            max_duration=config.filter.max_duration_seconds,
            min_play=config.filter.min_play,
            min_like=config.filter.min_like,
            block_keywords=config.filter.block_keywords,
            block_up_names=config.filter.block_up_names,
            exclude_partitions=config.filter.exclude_partitions,
            use_tags=config.interest.use_tags,
            tag_weight=config.interest.tag_weight,
        )
        if not results:
            return f"候选 {len(videos)} 个，但没有视频通过过滤与打分阈值。"
        lines = [f"候选 {len(videos)} 个，前 5 名："]
        for index, result in enumerate(results[:5], 1):
            video = result.video
            matched = "、".join(result.matched[:3]) or "无"
            lines.append(
                f"{index}. {video.title}（{result.score:.1f} 分｜{video.play} 播放｜匹配：{matched}）"
            )
        return "\n".join(lines)

    # -- 杂项 ----------------------------------------------------------

    def _resolve_data_dir(self) -> Path:
        config = self.config
        root = self._default_data_root()
        configured = (config.advanced.data_dir or "").strip()
        if not configured:
            return root
        path = Path(configured).expanduser()
        if not path.is_absolute():
            path = root / path
        return path

    def _default_data_root(self) -> Path:
        """本插件自己的数据目录。

        宿主注入的 ``ctx.paths.data_dir`` 已经是 ``data/plugins/<plugin_id>``，
        这里不能再拼一次插件 ID（早期版本这么写，会多套一层目录，
        导致 state.json 落在 ``data/plugins/<id>/<id>/`` 下）。
        """
        paths = getattr(self.ctx, "paths", None)
        data_dir = getattr(paths, "data_dir", None) if paths is not None else None
        if data_dir:
            try:
                path = Path(data_dir)
            except TypeError:
                path = None
            if path is not None:
                if path.name != PLUGIN_ID:
                    path = path / PLUGIN_ID
                return path
        return Path.cwd() / "data" / "plugins" / PLUGIN_ID

    def _debug_enabled(self) -> bool:
        try:
            return bool(self.config.advanced.debug)
        except Exception:  # noqa: BLE001
            return False


def create_plugin() -> BilibiliInterestSharePlugin:
    """宿主加载入口。"""

    return BilibiliInterestSharePlugin()
