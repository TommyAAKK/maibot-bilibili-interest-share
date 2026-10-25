"""B 站公开接口客户端（热门 / 排行榜 / 关键词搜索）。

只使用 B 站 web 端的公开 JSON 接口，不需要登录：

- 热门：``https://api.bilibili.com/x/web-interface/popular?ps=20&pn=1``
- 排行榜：``https://api.bilibili.com/x/web-interface/ranking/v2?rid=0&type=all``
- 搜索：``https://api.bilibili.com/x/web-interface/wbi/search/type?search_type=video&keyword=...``

排行榜与搜索接口需要 WBI 签名（``w_rid`` + ``wts``），签名用的 mixin key 由
``x/web-interface/nav`` 返回的 ``wbi_img`` 派生，本模块会自动获取并缓存。
"""

from __future__ import annotations

import hashlib
import html
import re
import time
import urllib.parse
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import httpx

POPULAR_URL = "https://api.bilibili.com/x/web-interface/popular"
RANKING_URL = "https://api.bilibili.com/x/web-interface/ranking/v2"
NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
SEARCH_URL = "https://api.bilibili.com/x/web-interface/wbi/search/type"
BILIBILI_REFERER = "https://www.bilibili.com/"

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# WBI 签名的 mixin key 置换表（B 站前端固定表）
_MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]

_EM_TAG_RE = re.compile(r"</?em[^>]*>")



class BilibiliError(RuntimeError):
    """B 站接口请求或数据解析失败。"""


def _as_int(value: Any, default: int = 0) -> int:
    try:
        if isinstance(value, bool):
            return int(value)
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value)


def strip_em(value: str) -> str:
    """去掉搜索结果标题里的 ``<em class="keyword">`` 高亮标签并反转义 HTML 实体。"""

    return html.unescape(_EM_TAG_RE.sub("", value or "")).strip()


def parse_duration_string(value: str) -> int:
    """把搜索结果的 ``12:34`` / ``1:02:03`` 解析成秒数。"""

    text = (value or "").strip()
    if not text:
        return 0
    parts = text.split(":")
    try:
        numbers = [int(part) for part in parts]
    except ValueError:
        return 0
    if len(numbers) == 3:
        return numbers[0] * 3600 + numbers[1] * 60 + numbers[2]
    if len(numbers) == 2:
        return numbers[0] * 60 + numbers[1]
    if len(numbers) == 1:
        return numbers[0]
    return 0


def duration_text(seconds: int) -> str:
    """把秒数格式化成 ``12:34`` / ``1:02:03``。"""

    seconds = max(0, int(seconds or 0))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def count_text(value: int) -> str:
    """把播放量/点赞数格式化成 ``1.2万`` / ``3.4亿``。"""

    value = max(0, int(value or 0))
    if value >= 100_000_000:
        return f"{value / 100_000_000:.1f}亿"
    if value >= 10_000:
        return f"{value / 10_000:.1f}万"
    return str(value)


@dataclass(slots=True)
class Video:
    """一个候选视频。"""

    bvid: str
    aid: int = 0
    title: str = ""
    description: str = ""
    owner: str = ""
    owner_mid: int = 0
    partition: str = ""
    duration: int = 0
    pubdate: int = 0
    play: int = 0
    like: int = 0
    coin: int = 0
    favorite: int = 0
    share: int = 0
    reply: int = 0
    danmaku: int = 0
    tags: str = ""
    cid: int = 0
    source: str = ""

    @property
    def url(self) -> str:
        return f"https://www.bilibili.com/video/{self.bvid}"

    @property
    def duration_label(self) -> str:
        return duration_text(self.duration)

    def searchable_text(self) -> str:
        return " ".join(
            part
            for part in (self.title, self.partition, self.description, self.owner, self.tags)
            if part
        ).lower()

    @classmethod
    def from_api(cls, raw: Dict[str, Any], *, source: str = "") -> Optional["Video"]:
        """从接口返回的单条记录构造 Video；缺少 bvid 时返回 None。"""

        if not isinstance(raw, dict):
            return None
        bvid = _as_str(raw.get("bvid")).strip()
        if not bvid:
            return None
        owner_raw = raw.get("owner") if isinstance(raw.get("owner"), dict) else {}
        stat = raw.get("stat") if isinstance(raw.get("stat"), dict) else {}
        return cls(
            bvid=bvid,
            aid=_as_int(raw.get("aid")),
            title=_as_str(raw.get("title")).strip(),
            description=_as_str(raw.get("desc")).strip(),
            owner=_as_str(owner_raw.get("name")).strip(),
            owner_mid=_as_int(owner_raw.get("mid")),
            partition=_as_str(raw.get("tname")).strip(),
            duration=_as_int(raw.get("duration")),
            pubdate=_as_int(raw.get("pubdate")),
            play=_as_int(stat.get("view")),
            like=_as_int(stat.get("like")),
            coin=_as_int(stat.get("coin")),
            favorite=_as_int(stat.get("favorite")),
            share=_as_int(stat.get("share")),
            reply=_as_int(stat.get("reply")),
            danmaku=_as_int(stat.get("danmaku")),
            source=source,
        )

    @classmethod
    def from_search(cls, raw: Dict[str, Any], *, source: str = "search") -> Optional["Video"]:
        """从「搜索」接口的单条记录构造 Video（字段名与热门接口不同）。"""

        if not isinstance(raw, dict):
            return None
        bvid = _as_str(raw.get("bvid")).strip()
        if not bvid:
            return None
        title = strip_em(_as_str(raw.get("title")))
        if not title:
            return None
        return cls(
            bvid=bvid,
            aid=_as_int(raw.get("aid")),
            title=title,
            description=_as_str(raw.get("description")).strip(),
            owner=_as_str(raw.get("author")).strip(),
            owner_mid=_as_int(raw.get("mid")),
            partition=_as_str(raw.get("typename")).strip(),
            duration=parse_duration_string(_as_str(raw.get("duration"))),
            pubdate=_as_int(raw.get("pubdate")),
            play=_as_int(raw.get("play")),
            like=_as_int(raw.get("like")),
            favorite=_as_int(raw.get("favorites")),
            reply=_as_int(raw.get("review")),
            danmaku=_as_int(raw.get("video_review")),
            tags=_as_str(raw.get("tag")).strip(),
            source=source,
        )


def parse_video_list(raw_list: Any, *, source: str = "") -> List[Video]:
    """解析 ``data.list``，跳过无法识别的条目。"""

    videos: List[Video] = []
    if not isinstance(raw_list, list):
        return videos
    for raw in raw_list:
        video = Video.from_api(raw, source=source)
        if video is not None:
            videos.append(video)
    return videos


def parse_search_list(raw_list: Any, *, source: str = "search") -> List[Video]:
    """解析搜索接口的 ``data.result``，跳过无法识别的条目。"""

    videos: List[Video] = []
    if not isinstance(raw_list, list):
        return videos
    for raw in raw_list:
        video = Video.from_search(raw, source=source)
        if video is not None:
            videos.append(video)
    return videos


def _extract_video_list(payload: Dict[str, Any]) -> Any:
    """从接口响应里取出 ``data.list``，结构异常时抛出 :class:`BilibiliError`。"""

    data = payload.get("data")
    if not isinstance(data, dict):
        raise BilibiliError("B 站接口未返回 data 字段")
    items = data.get("list")
    if not isinstance(items, list):
        raise BilibiliError("B 站接口未返回视频列表")
    return items


class WbiSigner:
    """为需要 WBI 签名的接口生成 ``w_rid`` / ``wts`` 参数。

    mixin key 由 ``x/web-interface/nav`` 的 ``wbi_img`` 派生，这里按 TTL 缓存，
    避免每次请求都多打一次 nav 接口。
    """

    def __init__(self, *, ttl_seconds: float = 12 * 3600) -> None:
        self._ttl = max(60.0, float(ttl_seconds))
        self._mixin_key: Optional[str] = None
        self._expires_at: float = 0.0

    @property
    def mixin_key(self) -> Optional[str]:
        return self._mixin_key

    def clear(self) -> None:
        self._mixin_key = None
        self._expires_at = 0.0

    @staticmethod
    def derive_mixin_key(img_key: str, sub_key: str) -> str:
        raw = f"{img_key}{sub_key}"
        if len(raw) < 64:
            raise BilibiliError("nav 接口返回的 wbi_img 结构异常")
        return "".join(raw[index] for index in _MIXIN_KEY_ENC_TAB)[:32]

    async def ensure(self, client: httpx.AsyncClient) -> str:
        now = time.time()
        if self._mixin_key and now < self._expires_at:
            return self._mixin_key
        try:
            response = await client.get(NAV_URL)
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise BilibiliError(f"获取 WBI 签名密钥失败：{exc}") from exc
        # 未登录时 nav 返回 code=-101，但 wbi_img 仍然可用
        data = payload.get("data") if isinstance(payload, dict) else None
        wbi_img = data.get("wbi_img") if isinstance(data, dict) else None
        if not isinstance(wbi_img, dict):
            raise BilibiliError("nav 接口未返回 wbi_img，无法生成签名")
        img_key = _as_str(wbi_img.get("img_url")).rsplit("/", 1)[-1].split(".")[0]
        sub_key = _as_str(wbi_img.get("sub_url")).rsplit("/", 1)[-1].split(".")[0]
        self._mixin_key = self.derive_mixin_key(img_key, sub_key)
        self._expires_at = now + self._ttl
        return self._mixin_key

    def sign(self, params: Dict[str, Any], mixin_key: str) -> Dict[str, Any]:
        signed = {key: value for key, value in params.items() if value is not None}
        signed["wts"] = int(time.time())
        filtered = [
            (key, "".join(ch for ch in str(value) if ch not in "!'()*"))
            for key, value in sorted(signed.items())
        ]
        query = urllib.parse.urlencode(filtered)
        signed["w_rid"] = hashlib.md5((query + mixin_key).encode("utf-8")).hexdigest()
        return signed


class _BilibiliClientBase:
    """异步 B 站接口客户端。

    可以通过 ``client=`` 注入自定义 ``httpx.AsyncClient``（测试时配合
    ``httpx.MockTransport`` 使用），此时 ``aclose()`` 不会关闭外部客户端。
    """

    def __init__(
        self,
        *,
        timeout: float = 12.0,
        cookie: str = "",
        user_agent: str = DEFAULT_USER_AGENT,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        headers = {
            "User-Agent": user_agent or DEFAULT_USER_AGENT,
            "Referer": BILIBILI_REFERER,
            "Accept": "application/json, text/plain, */*",
        }
        if cookie and cookie.strip():
            headers["Cookie"] = cookie.strip()
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=timeout,
            headers=headers,
            follow_redirects=True,
            transport=transport,
        )
        self._signer = WbiSigner()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _request_json(self, url: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """发请求并解析 JSON，不校验业务 ``code``。"""

        try:
            response = await self._client.get(url, params=params)
        except httpx.HTTPError as exc:
            raise BilibiliError(f"请求 B 站失败：{exc}") from exc
        if response.status_code != 200:
            raise BilibiliError(f"B 站接口返回 HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise BilibiliError("B 站接口返回的不是合法 JSON") from exc
        if not isinstance(payload, dict):
            raise BilibiliError("B 站接口返回结构异常")
        return payload

    @staticmethod
    def _check_code(payload: Dict[str, Any]) -> Dict[str, Any]:
        code = payload.get("code")
        if code != 0:
            message = _as_str(payload.get("message") or payload.get("msg")).strip()
            raise BilibiliError(f"B 站接口错误 code={code}{('：' + message) if message else ''}")
        return payload

    async def _get_json(self, url: str, params: Dict[str, Any]) -> Dict[str, Any]:
        return self._check_code(await self._request_json(url, params))

    async def _signed_get_json(self, url: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """带 WBI 签名的请求；遇到签名失效（-352）时刷新密钥重试一次。"""

        for attempt in range(2):
            mixin_key = await self._signer.ensure(self._client)
            signed = self._signer.sign(params, mixin_key)
            payload = await self._request_json(url, signed)
            if payload.get("code") == 0:
                return payload
            if payload.get("code") == -352 and attempt == 0:
                self._signer.clear()
                continue
            return self._check_code(payload)
        raise BilibiliError("B 站接口签名失败")

    async def fetch_popular(self, *, page_size: int = 20, page: int = 1) -> List[Video]:
        """抓取「热门」列表。"""

        payload = await self._get_json(POPULAR_URL, {"ps": page_size, "pn": page})
        return parse_video_list(_extract_video_list(payload), source="popular")

    async def fetch_ranking(self, *, rid: int = 0) -> List[Video]:
        """抓取「排行榜」（rid=0 为全站）。"""

        payload = await self._signed_get_json(RANKING_URL, {"rid": rid, "type": "all"})
        return parse_video_list(_extract_video_list(payload), source=f"ranking:{rid}")

    async def fetch_search(self, keyword: str, *, page: int = 1, order: str = "") -> List[Video]:
        """按关键词搜索视频（WBI 签名接口）。"""

        keyword = (keyword or "").strip()
        if not keyword:
            return []
        params: Dict[str, Any] = {
            "search_type": "video",
            "keyword": keyword,
            "page": max(1, int(page)),
        }
        if order:
            params["order"] = order
        payload = await self._signed_get_json(SEARCH_URL, params)
        data = payload.get("data")
        if not isinstance(data, dict):
            raise BilibiliError("搜索接口未返回 data 字段")
        return parse_search_list(data.get("result"), source=f"search:{keyword}")


def merge_videos(groups: Iterable[Iterable[Video]]) -> List[Video]:
    """按 bvid 去重合并多批候选视频，保留首次出现的顺序。"""

    merged: Dict[str, Video] = {}
    for group in groups:
        for video in group:
            merged.setdefault(video.bvid, video)
    return list(merged.values())


# ======================================================================
# 单视频详情：cid / 弹幕 / 热门评论 / 字幕 / 播放地址
# ======================================================================

VIEW_URL = "https://api.bilibili.com/x/web-interface/view"
PAGELIST_URL = "https://api.bilibili.com/x/player/pagelist"
DANMAKU_URL = "https://api.bilibili.com/x/v1/dm/list.so"
REPLY_URL = "https://api.bilibili.com/x/v2/reply"
PLAYER_URL = "https://api.bilibili.com/x/player/v2"
PLAYURL_URL = "https://api.bilibili.com/x/player/playurl"

_DANMAKU_RE = re.compile(r'<d p="([^"]*)"[^>]*>(.*?)</d>', re.S)


@dataclass(slots=True)
class Danmaku:
    """一条弹幕。"""

    text: str
    progress_ms: int = 0
    mode: int = 1
    color: int = 0xFFFFFF
    ctime: int = 0


@dataclass(slots=True)
class Comment:
    """一条热门评论。"""

    message: str
    user: str = ""
    like: int = 0
    rpid: int = 0
    ctime: int = 0


def parse_danmaku_xml(xml_text: str) -> List[Danmaku]:
    """解析弹幕 XML（``<d p="时间,模式,字号,颜色,时间戳,...">文本</d>``）。"""

    items: List[Danmaku] = []
    if not isinstance(xml_text, str) or not xml_text:
        return items
    for match in _DANMAKU_RE.finditer(xml_text):
        raw_params, raw_text = match.group(1), match.group(2)
        text = html.unescape(raw_text or "").strip()
        if not text:
            continue
        parts = raw_params.split(",")
        progress = _as_int(parts[0]) if parts else 0
        mode = _as_int(parts[1], 1) if len(parts) > 1 else 1
        color = _as_int(parts[3], 0xFFFFFF) if len(parts) > 3 else 0xFFFFFF
        ctime = _as_int(parts[4]) if len(parts) > 4 else 0
        items.append(Danmaku(text=text, progress_ms=progress, mode=mode, color=color, ctime=ctime))
    return items


def _maybe_inflate(raw: bytes) -> bytes:
    """B 站弹幕接口有时返回未声明编码的 deflate 数据，这里兜底解压。"""

    if not raw:
        return raw
    if raw.lstrip()[:1] == b"<":
        return raw
    for wbits in (-zlib.MAX_WBITS, zlib.MAX_WBITS | 32):
        try:
            return zlib.decompressobj(wbits).decompress(raw)
        except zlib.error:
            continue
    return raw


class BilibiliDetailMixin:
    """单视频详情相关接口（弹幕/评论/字幕/播放地址）。

    以 mixin 形式提供，:class:`BilibiliClient` 继承它；单独拆出来便于测试。
    """

    async def fetch_view(self, bvid: str) -> Dict[str, Any]:
        """取视频详情（aid / cid / duration / 分P 列表）。"""

        bvid = (bvid or "").strip()
        if not bvid:
            raise BilibiliError("bvid 为空")
        payload = await self._get_json(VIEW_URL, {"bvid": bvid})  # type: ignore[attr-defined]
        data = payload.get("data")
        if not isinstance(data, dict):
            raise BilibiliError("视频详情接口未返回 data 字段")
        pages = data.get("pages") if isinstance(data.get("pages"), list) else []
        cid = _as_int(data.get("cid"))
        if not cid and pages and isinstance(pages[0], dict):
            cid = _as_int(pages[0].get("cid"))
        return {
            "aid": _as_int(data.get("aid")),
            "cid": cid,
            "duration": _as_int(data.get("duration")),
            "title": _as_str(data.get("title")),
            "pages": len(pages),
        }

    async def _resolve_cid(self, bvid: str, cid: int = 0) -> int:
        if cid:
            return int(cid)
        try:
            detail = await self.fetch_view(bvid)
            return int(detail.get("cid") or 0)
        except BilibiliError:
            payload = await self._get_json(PAGELIST_URL, {"bvid": bvid})  # type: ignore[attr-defined]
            data = payload.get("data")
            if isinstance(data, list) and data and isinstance(data[0], dict):
                return _as_int(data[0].get("cid"))
            return 0

    async def _get_bytes(self, url: str, params: Dict[str, Any]) -> bytes:
        client = self._client  # type: ignore[attr-defined]
        try:
            response = await client.get(url, params=params)
        except httpx.HTTPError as exc:
            raise BilibiliError(f"请求 B 站失败：{exc}") from exc
        if response.status_code != 200:
            raise BilibiliError(f"B 站接口返回 HTTP {response.status_code}")
        return _maybe_inflate(response.content or b"")

    async def fetch_danmaku(
        self,
        bvid: str,
        *,
        cid: int = 0,
        limit: int = 400,
    ) -> List[Danmaku]:
        """抓取弹幕（匿名可用，最多返回 ``limit`` 条）。"""

        bvid = (bvid or "").strip()
        if not bvid:
            return []
        resolved_cid = await self._resolve_cid(bvid, cid)
        if not resolved_cid:
            return []
        raw = await self._get_bytes(DANMAKU_URL, {"oid": resolved_cid})
        try:
            xml_text = raw.decode("utf-8", errors="ignore")
        except Exception:  # noqa: BLE001
            return []
        items = parse_danmaku_xml(xml_text)
        if limit and len(items) > int(limit):
            # 均匀抽样，保留整段视频的弹幕分布
            step = max(1, len(items) // int(limit))
            items = items[::step][: int(limit)]
        return items

    async def fetch_hot_comments(
        self,
        bvid: str,
        *,
        aid: int = 0,
        limit: int = 20,
    ) -> List[Comment]:
        """抓取按热度排序的热门评论。"""

        bvid = (bvid or "").strip()
        if not bvid and not aid:
            return []
        if not aid:
            try:
                aid = _as_int((await self.fetch_view(bvid)).get("aid"))
            except BilibiliError:
                return []
        if not aid:
            return []
        payload = await self._get_json(  # type: ignore[attr-defined]
            REPLY_URL,
            {"type": 1, "oid": aid, "sort": 2, "ps": max(1, min(50, int(limit))), "pn": 1},
        )
        data = payload.get("data")
        replies = data.get("replies") if isinstance(data, dict) else None
        comments: List[Comment] = []
        if isinstance(replies, list):
            for raw in replies:
                if not isinstance(raw, dict):
                    continue
                content = raw.get("content") if isinstance(raw.get("content"), dict) else {}
                member = raw.get("member") if isinstance(raw.get("member"), dict) else {}
                message = _as_str(content.get("message")).strip()
                if not message:
                    continue
                comments.append(
                    Comment(
                        message=message,
                        user=_as_str(member.get("uname")).strip(),
                        like=_as_int(raw.get("like")),
                        rpid=_as_int(raw.get("rpid")),
                        ctime=_as_int(raw.get("ctime")),
                    )
                )
        return comments

    async def fetch_subtitle(self, bvid: str, *, cid: int = 0, aid: int = 0, max_chars: int = 1500) -> str:
        """读取 CC 字幕文本；没有字幕时返回空字符串。"""

        bvid = (bvid or "").strip()
        if not bvid:
            return ""
        resolved_cid = await self._resolve_cid(bvid, cid)
        if not resolved_cid:
            return ""
        if not aid:
            try:
                aid = _as_int((await self.fetch_view(bvid)).get("aid"))
            except BilibiliError:
                aid = 0
        try:
            payload = await self._get_json(  # type: ignore[attr-defined]
                PLAYER_URL, {"bvid": bvid, "cid": resolved_cid, "aid": aid}
            )
        except BilibiliError:
            return ""
        data = payload.get("data")
        subtitle = data.get("subtitle") if isinstance(data, dict) else None
        subtitles = subtitle.get("subtitles") if isinstance(subtitle, dict) else None
        if not isinstance(subtitles, list) or not subtitles:
            return ""
        url = ""
        for item in subtitles:
            if isinstance(item, dict) and _as_str(item.get("subtitle_url")).strip():
                url = _as_str(item.get("subtitle_url")).strip()
                break
        if not url:
            return ""
        if url.startswith("//"):
            url = "https:" + url
        try:
            response = await self._client.get(url)  # type: ignore[attr-defined]
            payload = response.json()
        except Exception:  # noqa: BLE001
            return ""
        body = payload.get("body") if isinstance(payload, dict) else None
        if not isinstance(body, list):
            return ""
        parts: List[str] = []
        for item in body:
            if isinstance(item, dict) and _as_str(item.get("content")).strip():
                parts.append(_as_str(item.get("content")).strip())
            if sum(len(part) for part in parts) >= max_chars:
                break
        text = " ".join(parts)
        return text[:max_chars]

    async def fetch_playurl(self, video: Any, *, cid: int = 0) -> str:
        """取一个可直接下载的 mp4 地址（360P，匿名可用）。"""

        bvid = getattr(video, "bvid", video if isinstance(video, str) else "")
        bvid = (bvid or "").strip()
        if not bvid:
            return ""
        resolved_cid = cid or getattr(video, "cid", 0) or 0
        resolved_cid = await self._resolve_cid(bvid, int(resolved_cid))
        if not resolved_cid:
            return ""
        payload = await self._get_json(  # type: ignore[attr-defined]
            PLAYURL_URL,
            {
                "bvid": bvid,
                "cid": resolved_cid,
                "qn": 32,
                "fnval": 1,
                "fnver": 0,
                "fourk": 0,
                "platform": "html5",
            },
        )
        data = payload.get("data")
        if not isinstance(data, dict):
            return ""
        durl = data.get("durl")
        if isinstance(durl, list) and durl and isinstance(durl[0], dict):
            return _as_str(durl[0].get("url")).strip()
        dash = data.get("dash") if isinstance(data.get("dash"), dict) else None
        if dash:
            videos = dash.get("video")
            if isinstance(videos, list):
                candidates = [item for item in videos if isinstance(item, dict)]
                candidates.sort(key=lambda item: _as_int(item.get("id")))
                if candidates:
                    base = _as_str(candidates[0].get("baseUrl") or candidates[0].get("base_url"))
                    if base:
                        return base.strip()
        return ""

    async def download_to(self, url: str, dest: Path, *, max_bytes: int = 80 * 1024 * 1024) -> Path:
        """流式下载到本地文件，超过 ``max_bytes`` 时中止。"""

        url = (url or "").strip()
        if not url:
            raise BilibiliError("下载地址为空")
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        client = self._client  # type: ignore[attr-defined]
        written = 0
        try:
            async with client.stream("GET", url) as response:
                if response.status_code != 200:
                    raise BilibiliError(f"下载视频失败：HTTP {response.status_code}")
                with dest.open("wb") as handle:
                    async for chunk in response.aiter_bytes():
                        if not chunk:
                            continue
                        written += len(chunk)
                        if written > max_bytes:
                            raise BilibiliError("视频体积超过上限，跳过抽帧")
                        handle.write(chunk)
        except httpx.HTTPError as exc:
            raise BilibiliError(f"下载视频失败：{exc}") from exc
        if written == 0:
            raise BilibiliError("下载到的视频为空")
        return dest


class BilibiliClient(BilibiliDetailMixin, _BilibiliClientBase):
    """B 站接口客户端：热门/搜索列表 + 单视频详情（弹幕/评论/字幕/播放地址）。"""
