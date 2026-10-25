"""B 站 HTTP 客户端与数据模型的单元测试（使用 httpx.MockTransport，不联网）。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _helpers import load_submodule  # noqa: E402


def _raw_video(**overrides):
    raw = {
        "bvid": "BV1xx411c7mD",
        "aid": 123456,
        "title": "原神新版本测评：这次真的改了",
        "desc": "聊聊原神新版本的改动",
        "owner": {"name": "测试UP", "mid": 42},
        "tname": "游戏",
        "duration": 425,
        "pubdate": 1_700_000_000,
        "stat": {
            "view": 123456,
            "like": 2345,
            "coin": 678,
            "favorite": 900,
            "share": 111,
            "reply": 222,
            "danmaku": 333,
        },
    }
    raw.update(overrides)
    return raw


class VideoModelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_submodule("bilibili")

    def test_from_api_maps_fields(self) -> None:
        video = self.module.Video.from_api(_raw_video(), source="popular")
        self.assertEqual(video.bvid, "BV1xx411c7mD")
        self.assertEqual(video.title, "原神新版本测评：这次真的改了")
        self.assertEqual(video.owner, "测试UP")
        self.assertEqual(video.partition, "游戏")
        self.assertEqual(video.duration, 425)
        self.assertEqual(video.play, 123456)
        self.assertEqual(video.like, 2345)
        self.assertEqual(video.source, "popular")
        self.assertEqual(video.url, "https://www.bilibili.com/video/BV1xx411c7mD")
        self.assertEqual(video.duration_label, "07:05")

    def test_from_api_tolerates_missing_stat(self) -> None:
        raw = _raw_video()
        raw.pop("stat")
        raw.pop("owner")
        video = self.module.Video.from_api(raw)
        self.assertEqual(video.play, 0)
        self.assertEqual(video.owner, "")

    def test_parse_video_list_skips_invalid(self) -> None:
        videos = self.module.parse_video_list(
            [_raw_video(), {"title": "没有 bvid"}, "垃圾数据", _raw_video(bvid="BV2")]
        )
        self.assertEqual([v.bvid for v in videos], ["BV1xx411c7mD", "BV2"])

    def test_duration_text(self) -> None:
        self.assertEqual(self.module.duration_text(0), "00:00")
        self.assertEqual(self.module.duration_text(65), "01:05")
        self.assertEqual(self.module.duration_text(3672), "1:01:12")

    def test_count_text(self) -> None:
        self.assertEqual(self.module.count_text(0), "0")
        self.assertEqual(self.module.count_text(999), "999")
        self.assertEqual(self.module.count_text(12_000), "1.2万")
        self.assertEqual(self.module.count_text(123_456_789), "1.2亿")

    def test_merge_videos_dedupes_by_bvid(self) -> None:
        module = self.module
        a = module.Video.from_api(_raw_video())
        b = module.Video.from_api(_raw_video(bvid="BV2"))
        duplicate = module.Video.from_api(_raw_video(title="重复条目"))
        merged = module.merge_videos([[a, b], [duplicate, b]])
        self.assertEqual([v.bvid for v in merged], ["BV1xx411c7mD", "BV2"])

    def test_parse_duration_string(self) -> None:
        module = self.module
        self.assertEqual(module.parse_duration_string("50:12"), 3012)
        self.assertEqual(module.parse_duration_string("1:02:03"), 3723)
        self.assertEqual(module.parse_duration_string("42"), 42)
        self.assertEqual(module.parse_duration_string(""), 0)
        self.assertEqual(module.parse_duration_string("abc"), 0)

    def test_strip_em(self) -> None:
        module = self.module
        self.assertEqual(module.strip_em('<em class="keyword">原神</em>测评'), "原神测评")
        self.assertEqual(module.strip_em("a &amp; b"), "a & b")

    def test_from_search_maps_fields(self) -> None:
        video = self.module.Video.from_search(_search_item())
        self.assertEqual(video.bvid, "BV1SEARCH001")
        self.assertEqual(video.title, "机械键盘推荐机械键盘横评")
        self.assertEqual(video.owner, "键盘UP")
        self.assertEqual(video.owner_mid, 777)
        self.assertEqual(video.partition, "数码")
        self.assertEqual(video.duration, 754)
        self.assertEqual(video.play, 88888)
        self.assertEqual(video.like, 6666)
        self.assertEqual(video.favorite, 1234)
        self.assertEqual(video.reply, 321)
        self.assertEqual(video.danmaku, 111)
        self.assertEqual(video.tags, "机械键盘,外设")
        self.assertIn("机械键盘", video.searchable_text())

    def test_from_search_requires_bvid_and_title(self) -> None:
        self.assertIsNone(self.module.Video.from_search({"title": "无 bvid"}))
        self.assertIsNone(self.module.Video.from_search({"bvid": "BV1", "title": ""}))
        self.assertIsNone(self.module.Video.from_search("垃圾"))


def _nav_payload(img: str = "a" * 32, sub: str = "b" * 32):
    return {
        "code": -101,
        "message": "账号未登录",
        "data": {
            "wbi_img": {
                "img_url": f"https://i0.hdslb.com/bfs/wbi/{img}.png",
                "sub_url": f"https://i0.hdslb.com/bfs/wbi/{sub}.png",
            }
        },
    }


def _search_item(**overrides):
    item = {
        "bvid": "BV1SEARCH001",
        "aid": 999,
        "title": '机械键盘推荐<em class="keyword">机械键盘</em>横评',
        "description": "三款平价机械键盘",
        "author": "键盘UP",
        "mid": 777,
        "typename": "数码",
        "duration": "12:34",
        "pubdate": 1_700_000_100,
        "play": 88888,
        "like": 6666,
        "favorites": 1234,
        "review": 321,
        "video_review": 111,
        "tag": "机械键盘,外设",
    }
    item.update(overrides)
    return item


class BilibiliClientTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_submodule("bilibili")

    async def test_fetch_popular(self) -> None:
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["ua"] = request.headers.get("user-agent", "")
            return httpx.Response(
                200,
                json={"code": 0, "data": {"list": [_raw_video(), _raw_video(bvid="BV2")]}},
            )

        client = self.module.BilibiliClient(
            transport=httpx.MockTransport(handler), user_agent="TestAgent/1.0"
        )
        try:
            videos = await client.fetch_popular(page_size=10, page=2)
        finally:
            await client.aclose()
        self.assertEqual(len(videos), 2)
        self.assertIn("x/web-interface/popular", seen["url"])
        self.assertIn("ps=10", seen["url"])
        self.assertIn("pn=2", seen["url"])
        self.assertEqual(seen["ua"], "TestAgent/1.0")
        self.assertEqual(videos[0].source, "popular")

    async def test_fetch_ranking_uses_wbi_signature(self) -> None:
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            if "x/web-interface/nav" in str(request.url):
                return httpx.Response(200, json=_nav_payload())
            self.assertIn("ranking/v2", str(request.url))
            self.assertIn("rid=4", str(request.url))
            self.assertIn("w_rid=", str(request.url))
            self.assertIn("wts=", str(request.url))
            return httpx.Response(200, json={"code": 0, "data": {"list": [_raw_video()]}})

        client = self.module.BilibiliClient(transport=httpx.MockTransport(handler))
        try:
            videos = await client.fetch_ranking(rid=4)
        finally:
            await client.aclose()
        self.assertEqual(len(videos), 1)
        self.assertEqual(videos[0].source, "ranking:4")
        self.assertTrue(any("nav" in url for url in calls))

    async def test_fetch_search_parses_results(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "x/web-interface/nav" in url:
                return httpx.Response(200, json=_nav_payload())
            self.assertIn("wbi/search/type", url)
            self.assertIn("search_type=video", url)
            self.assertIn("w_rid=", url)
            return httpx.Response(
                200, json={"code": 0, "data": {"result": [_search_item(), {"no_bvid": 1}]}}
            )

        client = self.module.BilibiliClient(transport=httpx.MockTransport(handler))
        try:
            videos = await client.fetch_search("机械键盘")
        finally:
            await client.aclose()
        self.assertEqual(len(videos), 1)
        video = videos[0]
        self.assertEqual(video.bvid, "BV1SEARCH001")
        self.assertEqual(video.title, "机械键盘推荐机械键盘横评")
        self.assertEqual(video.owner, "键盘UP")
        self.assertEqual(video.partition, "数码")
        self.assertEqual(video.duration, 754)
        self.assertEqual(video.play, 88888)
        self.assertEqual(video.like, 6666)
        self.assertEqual(video.favorite, 1234)
        self.assertEqual(video.danmaku, 111)
        self.assertEqual(video.tags, "机械键盘,外设")
        self.assertEqual(video.source, "search:机械键盘")

    async def test_search_retries_after_risk_control(self) -> None:
        state = {"nav": 0, "search": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "x/web-interface/nav" in url:
                state["nav"] += 1
                return httpx.Response(
                    200, json=_nav_payload(img=("a" if state["nav"] == 1 else "c") * 32)
                )
            state["search"] += 1
            if state["search"] == 1:
                return httpx.Response(200, json={"code": -352, "message": "风控校验失败"})
            return httpx.Response(200, json={"code": 0, "data": {"result": [_search_item()]}})

        client = self.module.BilibiliClient(transport=httpx.MockTransport(handler))
        try:
            videos = await client.fetch_search("原神")
        finally:
            await client.aclose()
        self.assertEqual(len(videos), 1)
        self.assertEqual(state["nav"], 2)  # 第一次签名 → -352 → 刷新密钥重试
        self.assertEqual(state["search"], 2)

    async def test_api_error_code_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"code": -412, "message": "请求被拦截"})

        client = self.module.BilibiliClient(transport=httpx.MockTransport(handler))
        try:
            with self.assertRaises(self.module.BilibiliError) as ctx:
                await client.fetch_popular()
        finally:
            await client.aclose()
        self.assertIn("请求被拦截", str(ctx.exception))

    async def test_http_error_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="busy")

        client = self.module.BilibiliClient(transport=httpx.MockTransport(handler))
        try:
            with self.assertRaises(self.module.BilibiliError):
                await client.fetch_ranking()
        finally:
            await client.aclose()

    async def test_malformed_payload_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"code": 0, "data": None})

        client = self.module.BilibiliClient(transport=httpx.MockTransport(handler))
        try:
            with self.assertRaises(self.module.BilibiliError):
                await client.fetch_popular()
        finally:
            await client.aclose()


if __name__ == "__main__":
    unittest.main()
