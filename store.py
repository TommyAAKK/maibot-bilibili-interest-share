"""本地状态文件：已刷视频名单 + 分享记录 + 冷却时间。

状态保存在插件数据目录下的 ``state.json``，结构大致为::

    {
      "version": 2,
      "videos": {
        "BV1xx...": {
          "first_seen": 1710000000.0,
          "last_shared": 1710000000.0,
          "title": "...", "url": "...", "owner": "...",
          "partition": "...", "duration": 300, "kind": "auto",
          "summary": "...", "points": ["...", "..."], "frames": "...",
          "shared_count": 1, "streams": ["123"]
        }
      },
      "runs": [{"at": ..., "fetched": 40, "shared": 1, "targets": [...]}],
      "streams": {"<stream_id>": {"last_shared_at": ..., "shared_total": 3}},
      "cooldowns": {"active_search": 1710000000.0, "shiwei": 1710000000.0}
    }

- ``videos`` 就是「已经刷过的视频名单」：名单里的视频不会再被抓取/主动分享，
  但可以通过自然语言或 ``/bili-reshare`` 用缓存里的总结再次分享。
- ``cooldowns`` 记录「主动搜索」与「赤石视频」各自的冷却时间。

写入采用「临时文件 + 原子替换」，避免进程中断把状态写坏。
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("plugin.bilibili_interest_share.store")

_MAX_RUN_HISTORY = 20
COOLDOWN_ACTIVE_SEARCH = "active_search"
COOLDOWN_SHIWEI = "shiwei"


class SeenStore:
    """已刷视频名单、分享记录与冷却状态存储。"""

    VERSION = 2

    def __init__(
        self,
        path: Path,
        *,
        dedupe_days: int = 30,
        max_records: int = 3000,
    ) -> None:
        self.path = Path(path)
        self.dedupe_days = max(1, int(dedupe_days))
        self.max_records = max(100, int(max_records))
        self._videos: Dict[str, Dict[str, Any]] = {}
        self._runs: List[Dict[str, Any]] = []
        self._streams: Dict[str, Dict[str, Any]] = {}
        self._cooldowns: Dict[str, float] = {}
        self._loaded = False

    # ------------------------------------------------------------------
    # 读写
    # ------------------------------------------------------------------

    def load(self) -> None:
        self._loaded = True
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - 状态损坏不应阻止插件运行
            logger.warning("读取状态文件失败，将重新开始记录：%s", exc)
            return
        if not isinstance(data, dict):
            return

        # 兼容 v1：seen 是 {bvid: 时间戳}
        legacy = data.get("seen")
        if isinstance(legacy, dict):
            for bvid, timestamp in legacy.items():
                try:
                    self._videos.setdefault(
                        str(bvid),
                        {"first_seen": float(timestamp), "last_shared": float(timestamp)},
                    )
                except (TypeError, ValueError):
                    continue

        videos = data.get("videos")
        if isinstance(videos, dict):
            for bvid, record in videos.items():
                if isinstance(record, dict):
                    self._videos[str(bvid)] = dict(record)

        runs = data.get("runs")
        if isinstance(runs, list):
            self._runs = [item for item in runs if isinstance(item, dict)][-_MAX_RUN_HISTORY:]
        streams = data.get("streams")
        if isinstance(streams, dict):
            self._streams = {
                str(key): value for key, value in streams.items() if isinstance(value, dict)
            }
        cooldowns = data.get("cooldowns")
        if isinstance(cooldowns, dict):
            for key, value in cooldowns.items():
                try:
                    self._cooldowns[str(key)] = float(value)
                except (TypeError, ValueError):
                    continue
        self._prune()

    def save(self) -> None:
        self._prune()
        payload = {
            "version": self.VERSION,
            "videos": self._videos,
            "runs": self._runs[-_MAX_RUN_HISTORY:],
            "streams": self._streams,
            "cooldowns": self._cooldowns,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_name(self.path.name + ".tmp")
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(tmp_path, self.path)

    # ------------------------------------------------------------------
    # 已刷名单
    # ------------------------------------------------------------------

    def _prune(self, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        ttl = self.dedupe_days * 86400.0
        expired = [
            bvid
            for bvid, record in self._videos.items()
            if now - float(record.get("last_shared") or record.get("first_seen") or 0.0) > ttl
        ]
        for bvid in expired:
            self._videos.pop(bvid, None)
        if len(self._videos) > self.max_records:
            ordered = sorted(
                self._videos.items(),
                key=lambda item: float(
                    item[1].get("last_shared") or item[1].get("first_seen") or 0.0
                ),
            )
            for bvid, _record in ordered[: len(self._videos) - self.max_records]:
                self._videos.pop(bvid, None)

    def is_seen(self, bvid: str, *, now: Optional[float] = None) -> bool:
        """视频是否已经在「已刷名单」里（名单内的不再自动分享）。"""

        now = time.time() if now is None else now
        record = self._videos.get(str(bvid))
        if record is None:
            return False
        last = float(record.get("last_shared") or record.get("first_seen") or 0.0)
        return now - last <= self.dedupe_days * 86400.0

    def mark_seen(self, bvid: str, *, now: Optional[float] = None) -> None:
        """只登记到名单（不覆盖已有分享信息）。"""

        if not bvid:
            return
        timestamp = time.time() if now is None else now
        record = self._videos.setdefault(str(bvid), {})
        record.setdefault("first_seen", timestamp)
        record.setdefault("last_shared", timestamp)

    def mark_shared(
        self,
        video: Any,
        *,
        narrative: str = "",
        summary: str = "",
        points: Optional[Sequence[str]] = None,
        intro: str = "",
        frames: str = "",
        kind: str = "auto",
        stream_id: str = "",
        now: Optional[float] = None,
    ) -> None:
        """记录一次分享（视频信息 + 缓存介绍文案，供之后重发复用）。

        ``narrative`` 是分享时实际发出去的那段自然语言介绍；``summary``/``points``
        保留给旧数据（v0.3 及更早）兼容。
        """

        bvid = getattr(video, "bvid", "") or ""
        if not bvid:
            return
        timestamp = time.time() if now is None else now
        record = self._videos.setdefault(bvid, {})
        record.setdefault("first_seen", timestamp)
        record["last_shared"] = timestamp
        record["shared_count"] = int(record.get("shared_count") or 0) + 1
        record["title"] = getattr(video, "title", "") or record.get("title") or ""
        record["url"] = getattr(video, "url", "") or record.get("url") or ""
        record["owner"] = getattr(video, "owner", "") or record.get("owner") or ""
        record["partition"] = getattr(video, "partition", "") or record.get("partition") or ""
        record["duration"] = int(getattr(video, "duration", 0) or 0)
        record["kind"] = kind
        if narrative:
            record["narrative"] = narrative
        if summary:
            record["summary"] = summary
        if points:
            record["points"] = [str(item) for item in points][:8]
        if intro:
            record["intro"] = intro
        if frames:
            record["frames"] = frames
        streams = record.get("streams")
        if not isinstance(streams, list):
            streams = []
        if stream_id and stream_id not in streams:
            streams.append(stream_id)
        record["streams"] = streams[-10:]

    def get(self, bvid: str) -> Optional[Dict[str, Any]]:
        record = self._videos.get(str(bvid))
        return dict(record) if isinstance(record, dict) else None

    def all_videos(self) -> Dict[str, Dict[str, Any]]:
        return {bvid: dict(record) for bvid, record in self._videos.items()}

    def find_by_query(
        self,
        query: str,
        *,
        limit: int = 5,
        window_days: float = 60.0,
        min_score: float = 0.25,
        now: Optional[float] = None,
    ) -> List[Tuple[float, Dict[str, Any]]]:
        """按聊天里的自然语言模糊匹配历史分享记录。

        打分方式：查询词与标题/UP 名的字符二元组重合度（0~1），
        再叠加「越近分享越高」的小幅加成。
        """

        now = time.time() if now is None else now
        window = max(1.0, float(window_days)) * 86400.0
        query_grams = _char_grams(_clean_query(query))
        if not query_grams:
            return []

        scored: List[Tuple[float, Dict[str, Any]]] = []
        for bvid, record in self._videos.items():
            last = float(record.get("last_shared") or record.get("first_seen") or 0.0)
            if now - last > window:
                continue
            haystack = " ".join(
                str(record.get(key) or "")
                for key in ("title", "owner", "partition")
            )
            hay_grams = _char_grams(haystack)
            if not hay_grams:
                continue
            overlap = len(query_grams & hay_grams)
            if not overlap:
                continue
            similarity = overlap / float(len(query_grams))
            if similarity < min_score:
                continue
            freshness = max(0.0, 1.0 - (now - last) / window) * 0.2
            scored.append((similarity + freshness, dict(record, bvid=bvid)))

        scored.sort(key=lambda item: -item[0])
        return scored[: max(1, int(limit))]

    # ------------------------------------------------------------------
    # 冷却
    # ------------------------------------------------------------------

    def cooldown_remaining(self, kind: str, seconds: float, *, now: Optional[float] = None) -> float:
        """返回剩余冷却秒数；0 表示已冷却完毕。"""

        now = time.time() if now is None else now
        if seconds <= 0:
            return 0.0
        last = float(self._cooldowns.get(kind) or 0.0)
        remaining = float(seconds) - (now - last)
        return max(0.0, remaining)

    def mark_cooldown(self, kind: str, *, now: Optional[float] = None) -> None:
        self._cooldowns[str(kind)] = time.time() if now is None else now

    # ------------------------------------------------------------------
    # 运行记录
    # ------------------------------------------------------------------

    def record_run(self, summary: Dict[str, Any], *, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        compact = {
            "at": now,
            "reason": summary.get("reason", ""),
            "fetched": int(summary.get("fetched") or 0),
            "shared": int(summary.get("shared") or 0),
            "targets": [
                {
                    "target": item.get("target", ""),
                    "stream_id": item.get("stream_id", ""),
                    "shared": int(item.get("shared") or 0),
                    "matched": list(item.get("matched") or [])[:6],
                }
                for item in summary.get("targets", [])
                if isinstance(item, dict)
            ][:20],
        }
        self._runs.append(compact)
        self._runs = self._runs[-_MAX_RUN_HISTORY:]
        for item in summary.get("targets", []):
            if not isinstance(item, dict):
                continue
            stream_id = str(item.get("stream_id") or "")
            if not stream_id:
                continue
            stats = self._streams.setdefault(stream_id, {"shared_total": 0, "last_shared_at": 0.0})
            shared = int(item.get("shared") or 0)
            stats["shared_total"] = int(stats.get("shared_total") or 0) + shared
            if shared:
                stats["last_shared_at"] = now

    @property
    def last_run(self) -> Optional[Dict[str, Any]]:
        return self._runs[-1] if self._runs else None

    def stats(self) -> Dict[str, Any]:
        last = self.last_run or {}
        return {
            "seen_count": len(self._videos),
            "run_count": len(self._runs),
            "last_run_at": float(last.get("at") or 0.0),
            "last_shared": int(last.get("shared") or 0),
            "total_shared": sum(int(item.get("shared") or 0) for item in self._runs),
        }


# ----------------------------------------------------------------------
# 文本相似度工具
# ----------------------------------------------------------------------

_PUNCT = " \t\r\n，。！？、；：“”‘’（）【】《》…—,.!?;:\"'()[]<>-~·|/\\"


def _clean_query(text: str) -> str:
    return "".join(ch for ch in (text or "").lower() if ch not in _PUNCT)


def _char_grams(text: str) -> set:
    """字符二元组集合（中文按字切，英文按词切后也切成二元组）。"""

    cleaned = _clean_query(text)
    if not cleaned:
        return set()
    if len(cleaned) == 1:
        return {cleaned}
    grams = {cleaned[index : index + 2] for index in range(len(cleaned) - 1)}
    # 单个较长的英文/数字词也保留整体，避免「keyboard」被切成无意义片段后漏匹配
    for token in (text or "").lower().replace("-", " ").split():
        if len(token) >= 3 and all(ord(ch) < 128 for ch in token):
            grams.add(token)
    return grams


def jaccard(a: set, b: set) -> float:
    """两个集合的 Jaccard 相似度。"""

    if not a or not b:
        return 0.0
    return len(a & b) / float(len(a | b))


def log_scale(value: float) -> float:
    """安全的对数缩放（用于热度类指标）。"""

    return math.log10(1.0 + max(0.0, float(value)))
