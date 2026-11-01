"""扫码登录模块的单元测试（使用 httpx.MockTransport 构造假响应，不联网）。"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _helpers import load_submodule  # noqa: E402

GENERATE_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
NAV_URL = "https://api.bilibili.com/x/web-interface/nav"


def _generate_payload(**overrides):
    data = {
        "url": "https://passport.biligame.com/x/passport-login/web/qrcode?qrcode_key=KEY123",
        "qrcode_key": "KEY123",
    }
    data.update(overrides)
    return {"code": 0, "message": "0", "data": data}


def _poll_payload(code: int):
    return {
        "code": 0,
        "message": "0",
        "data": {
            "url": "https://passport.biligame.com/x/passport-login/web/qrcode?qrcode_key=KEY123",
            "refresh_token": "refresh-token",
            "timestamp": 1_700_000_000,
            "code": code,
            "message": "",
        },
    }


def _login_cookie_headers():
    """四条 Set-Cookie，带 Path/Domain/HttpOnly 等属性，用于验证属性过滤。"""

    return [
        ("set-cookie", "SESSDATA=sess%2Cvalue; Path=/; Domain=.bilibili.com; HttpOnly"),
        ("set-cookie", "bili_jct=jct-value; Path=/; Domain=.bilibili.com"),
        ("set-cookie", "DedeUserID=123456; Path=/; Domain=.bilibili.com"),
        ("set-cookie", "DedeUserID__ckMd5=ckmd5-value; Path=/; Domain=.bilibili.com"),
    ]


def _mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class CreateQrcodeTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_submodule("bilibili_login")

    async def test_returns_url_and_key(self) -> None:
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["ua"] = request.headers.get("user-agent", "")
            seen["referer"] = request.headers.get("referer", "")
            return httpx.Response(200, json=_generate_payload())

        async with _mock_client(handler) as client:
            session = await self.module.create_qrcode(client, user_agent="TestAgent/1.0")

        self.assertEqual(session.url, _generate_payload()["data"]["url"])
        self.assertEqual(session.qrcode_key, "KEY123")
        self.assertEqual(seen["url"], GENERATE_URL)
        self.assertEqual(seen["ua"], "TestAgent/1.0")
        self.assertEqual(seen["referer"], "https://www.bilibili.com/")

    async def test_non_zero_code_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"code": -400, "message": "请求错误", "data": None})

        async with _mock_client(handler) as client:
            with self.assertRaises(self.module.QrLoginError) as ctx:
                await self.module.create_qrcode(client)
        self.assertIn("请求错误", str(ctx.exception))

    async def test_http_error_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="busy")

        async with _mock_client(handler) as client:
            with self.assertRaises(self.module.QrLoginError) as ctx:
                await self.module.create_qrcode(client)
        self.assertIn("503", str(ctx.exception))

    async def test_missing_fields_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"code": 0, "data": {"url": ""}})

        async with _mock_client(handler) as client:
            with self.assertRaises(self.module.QrLoginError):
                await self.module.create_qrcode(client)


class PollQrcodeTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_submodule("bilibili_login")

    async def test_status_code_mapping(self) -> None:
        cases = [
            (0, "success"),
            (86101, "waiting"),
            (86090, "scanned"),
            (86038, "expired"),
        ]
        for code, expected in cases:
            with self.subTest(code=code):
                def handler(request: httpx.Request, code: int = code) -> httpx.Response:
                    return httpx.Response(
                        200, json=_poll_payload(code), headers=_login_cookie_headers()
                    )

                async with _mock_client(handler) as client:
                    state = await self.module.poll_qrcode(client, "KEY123")
                self.assertEqual(state.status, expected)
                self.assertTrue(state.message)
                if expected == "success":
                    self.assertIn("SESSDATA=", state.cookie)
                else:
                    self.assertEqual(state.cookie, "")

    async def test_success_builds_cookie(self) -> None:
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["referer"] = request.headers.get("referer", "")
            return httpx.Response(
                200, json=_poll_payload(0), headers=_login_cookie_headers()
            )

        async with _mock_client(handler) as client:
            state = await self.module.poll_qrcode(client, "KEY123")

        self.assertEqual(state.status, "success")
        self.assertEqual(
            state.cookie,
            "SESSDATA=sess%2Cvalue; bili_jct=jct-value; DedeUserID=123456; "
            "DedeUserID__ckMd5=ckmd5-value",
        )
        self.assertIn("qrcode_key=KEY123", seen["url"])
        self.assertIn("source=main-fe-header", seen["url"])
        self.assertEqual(seen["referer"], "https://www.bilibili.com/")

    async def test_network_error_returns_error_state(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("连接失败", request=request)

        async with _mock_client(handler) as client:
            state = await self.module.poll_qrcode(client, "KEY123")
        self.assertEqual(state.status, "error")
        self.assertIn("轮询登录状态失败", state.message)

    async def test_timeout_returns_error_state(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("超时", request=request)

        async with _mock_client(handler) as client:
            state = await self.module.poll_qrcode(client, "KEY123")
        self.assertEqual(state.status, "error")
        self.assertIn("超时", state.message)

    async def test_invalid_json_returns_error_state(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<html>not json</html>")

        async with _mock_client(handler) as client:
            state = await self.module.poll_qrcode(client, "KEY123")
        self.assertEqual(state.status, "error")
        self.assertIn("轮询登录状态失败", state.message)

    async def test_outer_code_non_zero_returns_error_state(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"code": -412, "message": "请求被拦截", "data": None})

        async with _mock_client(handler) as client:
            state = await self.module.poll_qrcode(client, "KEY123")
        self.assertEqual(state.status, "error")
        self.assertIn("请求被拦截", state.message)

    async def test_empty_key_skips_request(self) -> None:
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(200, json=_poll_payload(86101))

        async with _mock_client(handler) as client:
            state = await self.module.poll_qrcode(client, "   ")
        self.assertEqual(state.status, "error")
        self.assertEqual(calls, [])


class FetchLoginStateTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_submodule("bilibili_login")

    async def test_logged_in_returns_success_with_profile(self) -> None:
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["cookie"] = request.headers.get("cookie", "")
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "message": "0",
                    "data": {"isLogin": True, "uname": "测试用户", "mid": 123456},
                },
            )

        async with _mock_client(handler) as client:
            state = await self.module.fetch_login_state(client, "SESSDATA=abc; bili_jct=def")

        self.assertEqual(state.status, "success")
        self.assertEqual(state.uname, "测试用户")
        self.assertEqual(state.mid, "123456")
        self.assertEqual(state.cookie, "SESSDATA=abc; bili_jct=def")
        self.assertIn("测试用户", state.message)
        self.assertEqual(seen["url"], NAV_URL)
        self.assertIn("SESSDATA=abc", seen["cookie"])

    async def test_not_logged_in_returns_not_login_state(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"code": 0, "message": "0", "data": {"isLogin": False}}
            )

        async with _mock_client(handler) as client:
            state = await self.module.fetch_login_state(client, "SESSDATA=expired")
        self.assertIn(state.status, ("expired", "waiting"))
        self.assertIn("未登录", state.message)
        self.assertEqual(state.cookie, "")

    async def test_api_code_minus_101_treated_as_not_logged_in(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"code": -101, "message": "账号未登录", "data": {}}
            )

        async with _mock_client(handler) as client:
            state = await self.module.fetch_login_state(client, "SESSDATA=stale")
        self.assertEqual(state.status, "expired")
        self.assertIn("未登录", state.message)

    async def test_empty_cookie_skips_request(self) -> None:
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(200, json={"code": 0, "data": {"isLogin": True}})

        async with _mock_client(handler) as client:
            state = await self.module.fetch_login_state(client, "   ")
        self.assertIn(state.status, ("expired", "waiting"))
        self.assertEqual(calls, [])

    async def test_network_error_returns_error_state(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("读取超时", request=request)

        async with _mock_client(handler) as client:
            state = await self.module.fetch_login_state(client, "SESSDATA=abc")
        self.assertEqual(state.status, "error")
        self.assertIn("检查登录状态失败", state.message)


class CookieFromResponseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_submodule("bilibili_login")

    def test_multiple_set_cookie_headers_and_attributes_filtered(self) -> None:
        response = httpx.Response(200, headers=_login_cookie_headers())
        cookie = self.module.cookie_from_response(response)
        self.assertEqual(
            cookie,
            "SESSDATA=sess%2Cvalue; bili_jct=jct-value; DedeUserID=123456; "
            "DedeUserID__ckMd5=ckmd5-value",
        )
        for attribute in ("Path=", "Domain=", "HttpOnly"):
            self.assertNotIn(attribute, cookie)

    def test_ignores_unrelated_cookies(self) -> None:
        response = httpx.Response(
            200,
            headers=[
                ("set-cookie", "SESSDATA=only; Path=/"),
                ("set-cookie", "buvid3=tracking; Path=/"),
                ("set-cookie", "b_nut=1; Path=/"),
            ],
        )
        cookie = self.module.cookie_from_response(response)
        self.assertEqual(cookie, "SESSDATA=only")

    def test_missing_fields_do_not_raise(self) -> None:
        response = httpx.Response(200)
        self.assertEqual(self.module.cookie_from_response(response), "")

    def test_falls_back_to_cookie_jar(self) -> None:
        # 未绑定 request 的 Response 访问 cookies 会抛 RuntimeError，这里模拟真实响应。
        request = httpx.Request("GET", POLL_URL)
        response = httpx.Response(200, request=request)
        response.cookies.set("SESSDATA", "jar-value")
        self.assertEqual(self.module.cookie_from_response(response), "SESSDATA=jar-value")


class RenderQrcodeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_submodule("bilibili_login")

    def test_returns_png_bytes(self) -> None:
        data = self.module.render_qrcode_png("https://example.com/login?key=abc")
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertGreater(len(data), 100)

    def test_box_size_scales_image(self) -> None:
        small = self.module.render_qrcode_png("https://example.com/login?key=abc", box_size=4)
        large = self.module.render_qrcode_png("https://example.com/login?key=abc", box_size=8)
        self.assertGreater(len(large), len(small))

        try:
            from PIL import Image
        except Exception:  # noqa: BLE001 - PIL 不可用时只比较字节数
            return
        with Image.open(io.BytesIO(small)) as img_small:
            self.assertEqual(img_small.size[0] % 4, 0)
            self.assertGreater(img_small.size[0], 40)
            width_small = img_small.size[0]
        with Image.open(io.BytesIO(large)) as img_large:
            self.assertEqual(img_large.size[0], width_small * 2)

    def test_empty_text_raises(self) -> None:
        with self.assertRaises(self.module.QrLoginError):
            self.module.render_qrcode_png("   ")


class SaveLoadLoginTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_submodule("bilibili_login")

    def test_round_trip(self) -> None:
        state = self.module.LoginState(
            status="success",
            message="登录成功",
            cookie="SESSDATA=abc; bili_jct=def; DedeUserID=123",
            uname="测试用户",
            mid="123",
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "login.json"
            self.module.save_login(path, state)
            self.assertTrue(path.exists())

            raw = path.read_bytes()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
            payload = json.loads(raw.decode("utf-8"))
            self.assertEqual(payload["cookie"], state.cookie)
            self.assertEqual(payload["uname"], state.uname)
            self.assertEqual(payload["mid"], state.mid)
            self.assertIsInstance(payload["saved_at"], int)

            loaded = self.module.load_login(path)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.cookie, state.cookie)
        self.assertEqual(loaded.uname, state.uname)
        self.assertEqual(loaded.mid, state.mid)

    def test_load_missing_file_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(self.module.load_login(Path(tmp) / "nope.json"))

    def test_load_corrupted_file_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.json"
            path.write_text("{not json", encoding="utf-8")
            self.assertIsNone(self.module.load_login(path))

    def test_load_payload_without_cookie_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "empty.json"
            path.write_text(json.dumps({"uname": "x"}), encoding="utf-8")
            self.assertIsNone(self.module.load_login(path))

    def test_save_to_invalid_path_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)  # 以目录当文件写，必然失败
            with self.assertRaises(self.module.QrLoginError):
                self.module.save_login(directory, self.module.LoginState("success", "ok"))


if __name__ == "__main__":
    unittest.main()
