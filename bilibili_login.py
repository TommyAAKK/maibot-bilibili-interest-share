"""B 站扫码登录（二维码生成 / 状态轮询 / Cookie 校验），与插件主体解耦。

流程对应 B 站 web 端登录接口：

- 生成二维码：``https://passport.bilibili.com/x/passport-login/web/qrcode/generate``
- 轮询状态：``https://passport.bilibili.com/x/passport-login/web/qrcode/poll``
- 校验 Cookie：``https://api.bilibili.com/x/web-interface/nav``

本模块不依赖插件运行时，也不落盘全局状态：所有网络请求都由调用方传入
``httpx.AsyncClient``，方便复用连接池与在测试里替换 transport。失败统一转成
:class:`LoginState`（``status="error"``），不向调用方抛裸异常。
"""

from __future__ import annotations

import io
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import httpx

QRCODE_GENERATE_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
QRCODE_POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
BILIBILI_REFERER = "https://www.bilibili.com/"

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

#: 单个请求的超时时间（秒）。调用方的 client 不一定配置了超时，这里显式指定。
REQUEST_TIMEOUT = 10.0

#: poll 接口 ``data.code`` → (状态, 中文说明)。
_POLL_STATUS_MAP: Dict[int, tuple[str, str]] = {
    0: ("success", "登录成功"),
    86101: ("waiting", "等待扫码"),
    86090: ("scanned", "已扫码，请在手机上确认登录"),
    86038: ("expired", "二维码已失效，请重新生成"),
}

#: 登录需要保留的 Cookie 字段（顺序即拼接顺序）。
_LOGIN_COOKIE_KEYS = ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5")

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class QrLoginError(Exception):
    """扫码登录过程中的异常（网络失败、接口返回异常、二维码渲染不可用等）。"""


@dataclass
class QrCodeSession:
    """一次二维码登录会话。"""

    url: str  # 二维码内容（手机扫码/相机扫描后打开的登录页地址）
    qrcode_key: str  # 轮询用的 key


@dataclass
class LoginState:
    """一次登录状态查询的结果。"""

    status: str  # waiting | scanned | expired | success | error
    message: str  # 给用户看的中文说明
    cookie: str = ""  # 成功时是可直接放进请求头 Cookie 的字符串
    uname: str = ""  # 成功时的 B 站用户名（poll 接口通常拿不到，留空）
    mid: str = ""  # 成功时的 B 站 UID（同上，可留空）


def _request_headers(user_agent: str) -> Dict[str, str]:
    """构造请求头：B 站接口对缺少 UA / Referer 的请求会拒绝或降级。"""

    return {
        "User-Agent": user_agent or DEFAULT_USER_AGENT,
        "Referer": BILIBILI_REFERER,
    }


def _reason(exc: Exception) -> str:
    """把底层异常翻译成简短的中文原因。"""

    if isinstance(exc, httpx.TimeoutException):
        return "请求超时"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    text = str(exc).strip()
    return text or exc.__class__.__name__


def _split_set_cookie(raw: str) -> tuple[str, str]:
    """从单条 ``Set-Cookie`` 头里取出 (name, value)，忽略 Path/HttpOnly 等属性。"""

    head = (raw or "").split(";", 1)[0].strip()
    name, _, value = head.partition("=")
    return name.strip(), value.strip()


async def create_qrcode(
    client: httpx.AsyncClient, *, user_agent: str = DEFAULT_USER_AGENT
) -> QrCodeSession:
    """请求 B 站生成登录二维码，返回二维码内容与轮询 key。

    接口返回异常、网络失败或字段缺失时抛 :class:`QrLoginError`。
    """

    try:
        response = await client.get(
            QRCODE_GENERATE_URL,
            headers=_request_headers(user_agent),
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise QrLoginError(f"获取登录二维码失败：{_reason(exc)}") from exc

    if not isinstance(payload, dict):
        raise QrLoginError("获取登录二维码失败：接口返回格式异常")

    code = payload.get("code")
    data = payload.get("data")
    if code != 0 or not isinstance(data, dict):
        message = str(payload.get("message") or "接口返回异常")
        raise QrLoginError(f"获取登录二维码失败：{message}（code={code}）")

    url = str(data.get("url") or "").strip()
    qrcode_key = str(data.get("qrcode_key") or "").strip()
    if not url or not qrcode_key:
        raise QrLoginError("获取登录二维码失败：接口未返回二维码地址或 key")
    return QrCodeSession(url=url, qrcode_key=qrcode_key)


async def poll_qrcode(
    client: httpx.AsyncClient, qrcode_key: str, *, user_agent: str = DEFAULT_USER_AGENT
) -> LoginState:
    """轮询二维码登录状态。

    ``data.code`` 的含义：0=成功、86101=未扫码、86090=已扫码未确认、
    86038=二维码已失效。网络失败或接口异常时返回 ``status="error"``。
    """

    key = (qrcode_key or "").strip()
    if not key:
        return LoginState(status="error", message="二维码 key 为空，无法轮询登录状态")

    try:
        response = await client.get(
            QRCODE_POLL_URL,
            params={"qrcode_key": key, "source": "main-fe-header"},
            headers=_request_headers(user_agent),
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        return LoginState(status="error", message=f"轮询登录状态失败：{_reason(exc)}")

    if not isinstance(payload, dict):
        return LoginState(status="error", message="轮询登录状态失败：接口返回格式异常")

    outer_code = payload.get("code")
    data = payload.get("data")
    if outer_code != 0 or not isinstance(data, dict):
        message = str(payload.get("message") or "接口返回异常")
        return LoginState(status="error", message=f"轮询登录状态失败：{message}（code={outer_code}）")

    try:
        state_code = int(data.get("code"))
    except (TypeError, ValueError):
        return LoginState(status="error", message="轮询登录状态失败：接口未返回有效的状态码")

    status, message = _POLL_STATUS_MAP.get(
        state_code, ("error", f"未知的登录状态码：{state_code}")
    )
    if status != "success":
        return LoginState(status=status, message=message)

    cookie = cookie_from_response(response)
    if not cookie:
        return LoginState(status="error", message="登录成功但未取到 Cookie，请重新扫码")
    return LoginState(status="success", message=message, cookie=cookie)


async def fetch_login_state(
    client: httpx.AsyncClient, cookie: str, *, user_agent: str = DEFAULT_USER_AGENT
) -> LoginState:
    """用 ``x/web-interface/nav`` 检查 Cookie 是否仍然有效。"""

    text = (cookie or "").strip()
    if not text:
        return LoginState(status="expired", message="Cookie 为空，当前未登录")

    headers = _request_headers(user_agent)
    headers["Cookie"] = text
    try:
        response = await client.get(NAV_URL, headers=headers, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        return LoginState(status="error", message=f"检查登录状态失败：{_reason(exc)}")

    if not isinstance(payload, dict):
        return LoginState(status="error", message="检查登录状态失败：接口返回格式异常")

    data = payload.get("data")
    if not isinstance(data, dict) or not data.get("isLogin"):
        # code=-101（账号未登录）也走这里：对调用方来说就是“没有有效登录态”。
        detail = str(payload.get("message") or "").strip()
        suffix = f"（{detail}）" if detail else ""
        return LoginState(status="expired", message=f"Cookie 已失效或未登录{suffix}")

    uname = str(data.get("uname") or "")
    mid = str(data.get("mid") or "")
    message = f"已登录：{uname}" if uname else "登录有效"
    return LoginState(status="success", message=message, cookie=text, uname=uname, mid=mid)


def render_qrcode_png(text: str, *, box_size: int = 8, border: int = 2) -> bytes:
    """把文本渲染成 PNG 二维码字节。

    优先用 ``qrcode`` 库，其次退回 ``segno``；两者都不可用时抛
    :class:`QrLoginError`。返回的字节一定以 PNG magic 开头。
    """

    content = (text or "").strip()
    if not content:
        raise QrLoginError("二维码内容为空，无法渲染")

    size = max(1, int(box_size))
    margin = max(0, int(border))
    errors: List[str] = []

    try:
        import qrcode
        from qrcode.constants import ERROR_CORRECT_M
    except Exception as exc:  # noqa: BLE001 - 缺库时退回 segno
        errors.append(f"qrcode 不可用：{exc}")
    else:
        try:
            qr = qrcode.QRCode(
                box_size=size, border=margin, error_correction=ERROR_CORRECT_M
            )
            qr.add_data(content)
            qr.make(fit=True)
            buffer = io.BytesIO()
            qr.make_image(fill_color="black", back_color="white").save(
                buffer, format="PNG"
            )
            data = buffer.getvalue()
            if data.startswith(_PNG_MAGIC):
                return data
            errors.append("qrcode 未生成 PNG 数据")
        except Exception as exc:  # noqa: BLE001 - 渲染失败继续尝试 segno
            errors.append(f"qrcode 渲染失败：{exc}")

    try:
        import segno
    except Exception as exc:  # noqa: BLE001 - 两个库都不可用
        errors.append(f"segno 不可用：{exc}")
    else:
        try:
            buffer = io.BytesIO()
            segno.make(content, error="m").save(
                buffer, kind="png", scale=size, border=margin
            )
            data = buffer.getvalue()
            if data.startswith(_PNG_MAGIC):
                return data
            errors.append("segno 未生成 PNG 数据")
        except Exception as exc:  # noqa: BLE001 - 渲染失败
            errors.append(f"segno 渲染失败：{exc}")

    raise QrLoginError("二维码渲染失败：" + "；".join(errors))


def cookie_from_response(response: httpx.Response) -> str:
    """从 poll 响应的 ``Set-Cookie`` 里拼出 Cookie 头值。

    只保留 ``SESSDATA`` / ``bili_jct`` / ``DedeUserID`` / ``DedeUserID__ckMd5``，
    有就带上、没有跳过；Path、HttpOnly 等属性一律不写进值里。
    """

    pairs: Dict[str, str] = {}

    # 同一次响应可能有多个 Set-Cookie 头，get_list 才能全部读到。
    try:
        raw_headers = response.headers.get_list("set-cookie")
    except AttributeError:  # pragma: no cover - 极老版本 httpx 兜底
        raw_headers = []
    for raw in raw_headers:
        name, value = _split_set_cookie(raw)
        if name in _LOGIN_COOKIE_KEYS and value and name not in pairs:
            pairs[name] = value

    # 兼容 Set-Cookie 未暴露到 headers 的情况，从 client 已解析的 cookie jar 补齐。
    # 未绑定 request 的 Response 访问 cookies 会抛 RuntimeError，这里直接跳过。
    try:
        jar = response.cookies
    except Exception:  # noqa: BLE001 - 拿不到 cookie jar 就只用 headers
        jar = None
    if jar is not None:
        for name in _LOGIN_COOKIE_KEYS:
            if name in pairs:
                continue
            value = jar.get(name)
            if value:
                pairs[name] = str(value)

    return "; ".join(f"{name}={pairs[name]}" for name in _LOGIN_COOKIE_KEYS if name in pairs)


def save_login(path: Path, state: LoginState) -> None:
    """把登录结果写到 path（JSON，UTF-8 无 BOM）；写失败抛 :class:`QrLoginError`。"""

    payload = {
        "cookie": state.cookie,
        "uname": state.uname,
        "mid": state.mid,
        "saved_at": int(time.time()),
    }
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8", newline="\n") as fp:
            json.dump(payload, fp, ensure_ascii=False, indent=2)
            fp.write("\n")
    except OSError as exc:
        raise QrLoginError(f"保存登录信息失败：{exc}") from exc


def load_login(path: Path) -> Optional[LoginState]:
    """读回登录结果；文件不存在、损坏或没有 Cookie 时返回 ``None``，不抛异常。"""

    target = Path(path)
    try:
        with target.open("r", encoding="utf-8") as fp:
            payload = json.load(fp)
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None

    cookie = str(payload.get("cookie") or "").strip()
    if not cookie:
        return None
    return LoginState(
        status="success",
        message="已读取本地登录信息",
        cookie=cookie,
        uname=str(payload.get("uname") or ""),
        mid=str(payload.get("mid") or ""),
    )
