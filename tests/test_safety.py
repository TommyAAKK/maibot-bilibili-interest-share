"""安全相关单元测试：下载地址校验、bvid 路径校验、重发白名单。

对应插件市场审核提出的两类风险：

1. B 站接口返回的播放地址/字幕地址会被直接用于下载，若上游数据被污染，
   可能诱导插件访问本机或内网服务（SSRF）——只放行 http/https 且拒绝内网主机，
   重定向每一跳都重新校验。
2. ``bvid`` 会被当作目录名拼接进数据目录路径，若上游返回 ``../xxx``
   这类值，可能越界删除别的目录——按 ``^BV[0-9A-Za-z]+$`` 校验后才使用。
"""

from __future__ import annotations

import unittest

import httpx

from _helpers import load_submodule


class SafeDownloadUrlTest(unittest.TestCase):
    """ensure_safe_download_url 的白名单/黑名单行为。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_submodule("bilibili")

    def test_allows_bilibili_https_urls(self) -> None:
        for url in (
            "https://api.bilibili.com/x/player/playurl?bvid=BV1xx",
            "https://upos-sz-mirror08c.bilivideo.com/upgcxcode/12/34/video.m4s",
            "http://i0.hdslb.com/bfs/subtitle/123.json",
            "https://aisubtitle.hdslb.com/bfs/ai_subtitle/prod/1.json",
        ):
            with self.subTest(url=url):
                self.assertEqual(self.module.ensure_safe_download_url(url), url)

    def test_rejects_non_http_schemes(self) -> None:
        for url in (
            "file:///C:/Windows/win.ini",
            "ftp://example.com/a.mp4",
            "javascript:alert(1)",
            "data:text/plain,hi",
        ):
            with self.subTest(url=url):
                with self.assertRaises(self.module.BilibiliError):
                    self.module.ensure_safe_download_url(url)

    def test_rejects_private_and_loopback_hosts(self) -> None:
        for url in (
            "http://127.0.0.1:8080/secret",
            "http://localhost/admin",
            "http://[::1]/admin",
            "http://169.254.169.254/latest/meta-data/",
            "http://10.0.0.5/internal.mp4",
            "http://192.168.1.1/router.mp4",
            "http://172.16.0.1/a.mp4",
            "http://metadata.google.internal/x",
            "http://printer.local/x",
        ):
            with self.subTest(url=url):
                with self.assertRaises(self.module.BilibiliError):
                    self.module.ensure_safe_download_url(url)

    def test_rejects_empty_url(self) -> None:
        with self.assertRaises(self.module.BilibiliError):
            self.module.ensure_safe_download_url("   ")


class DownloadRedirectTest(unittest.IsolatedAsyncioTestCase):
    """download_to 手动跟随重定向时，每一跳都要重新校验。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_submodule("bilibili")

    async def test_follows_safe_redirect(self) -> None:
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            if str(request.url).endswith("/start"):
                return httpx.Response(
                    302, headers={"location": "https://upos-sz.bilivideo.com/real.mp4"}
                )
            return httpx.Response(200, content=b"video-bytes")

        client = self.module.BilibiliClient(transport=httpx.MockTransport(handler))
        try:
            import tempfile
            from pathlib import Path

            with tempfile.TemporaryDirectory() as tmp:
                dest = Path(tmp) / "out.mp4"
                await client.download_to("https://api.bilibili.com/start", dest)
                self.assertEqual(dest.read_bytes(), b"video-bytes")
        finally:
            await client.aclose()
        self.assertEqual(len(seen), 2)

    async def test_rejects_redirect_to_private_host(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url).endswith("/start"):
                return httpx.Response(302, headers={"location": "http://127.0.0.1:9/x"})
            return httpx.Response(200, content=b"should-not-reach")

        client = self.module.BilibiliClient(transport=httpx.MockTransport(handler))
        try:
            import tempfile
            from pathlib import Path

            with tempfile.TemporaryDirectory() as tmp:
                dest = Path(tmp) / "out.mp4"
                with self.assertRaises(self.module.BilibiliError):
                    await client.download_to("https://api.bilibili.com/start", dest)
                self.assertFalse(dest.exists())
        finally:
            await client.aclose()

    async def test_rejects_direct_private_url(self) -> None:
        client = self.module.BilibiliClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200))
        )
        try:
            import tempfile
            from pathlib import Path

            with tempfile.TemporaryDirectory() as tmp:
                dest = Path(tmp) / "out.mp4"
                with self.assertRaises(self.module.BilibiliError):
                    await client.download_to("http://169.254.169.254/latest/meta-data/", dest)
        finally:
            await client.aclose()


class SafeBvidTest(unittest.TestCase):
    """media.safe_bvid 只接受合法的 BV 号。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_submodule("media")

    def test_accepts_valid_bvid(self) -> None:
        for bvid in ("BV1xx411c7mD", "BV1Hanh65EV1", "BV2"):
            with self.subTest(bvid=bvid):
                self.assertEqual(self.module.safe_bvid(bvid), bvid)

    def test_rejects_path_traversal(self) -> None:
        for value in ("../../etc", "..", "BV1/../..", "/abs/path", "BV1..", "C:\\x"):
            with self.subTest(value=value):
                self.assertEqual(self.module.safe_bvid(value), "video")

    def test_empty_falls_back_to_default(self) -> None:
        self.assertEqual(self.module.safe_bvid(""), "video")
        self.assertEqual(self.module.safe_bvid(None), "video")
        self.assertEqual(self.module.safe_bvid("", default="unknown"), "unknown")


if __name__ == "__main__":
    unittest.main()
