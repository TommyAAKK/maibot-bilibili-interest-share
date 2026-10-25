"""测试辅助：模拟 MaiBot 插件加载方式加载插件模块，并提供假的运行时上下文。"""

from __future__ import annotations

import copy
import importlib.util
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
#: 真实 MaiBot 实例根目录。由环境变量 ``MAIBOT_INSTANCE_DIR`` 指定；
#: 未设置时为 ``None``，依赖宿主源码的打包测试会自动跳过。
MAIBOT_ROOT: Optional[Path] = None
_instance_dir = os.environ.get("MAIBOT_INSTANCE_DIR", "").strip()
if _instance_dir:
    MAIBOT_ROOT = Path(_instance_dir)
_MODULE_NAME = "bilibili_interest_share_under_test"


def load_plugin_module():
    """按插件加载器的方式（package + submodule_search_locations）加载 plugin.py。"""

    if _MODULE_NAME in sys.modules:
        return sys.modules[_MODULE_NAME]
    spec = importlib.util.spec_from_file_location(
        _MODULE_NAME,
        PLUGIN_ROOT / "plugin.py",
        submodule_search_locations=[str(PLUGIN_ROOT)],
    )
    if spec is None or spec.loader is None:  # pragma: no cover - 环境异常
        raise RuntimeError("无法加载插件模块")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


def load_submodule(name: str):
    """加载插件包内的子模块（如 ``bilibili`` / ``interest`` / ``store``）。"""

    load_plugin_module()
    return importlib.import_module(f"{_MODULE_NAME}.{name}")


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """递归合并配置字典（override 覆盖 base）。"""

    merged = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


class FakePaths:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.runtime_dir = data_dir


class FakeLogger:
    def __init__(self) -> None:
        self.records: List[str] = []

    def _log(self, level: str, message: Any, *args: Any, **kwargs: Any) -> None:
        try:
            text = str(message) % args if args else str(message)
        except Exception:  # noqa: BLE001
            text = str(message)
        self.records.append(f"{level}:{text}")

    def debug(self, message: Any, *args: Any, **kwargs: Any) -> None:
        self._log("debug", message, *args)

    def info(self, message: Any, *args: Any, **kwargs: Any) -> None:
        self._log("info", message, *args)

    def warning(self, message: Any, *args: Any, **kwargs: Any) -> None:
        self._log("warning", message, *args)

    def error(self, message: Any, *args: Any, **kwargs: Any) -> None:
        self._log("error", message, *args)


class FakeSend:
    def __init__(self) -> None:
        self.messages: List[Dict[str, str]] = []

    async def text(self, text: str, stream_id: str, **kwargs: Any) -> bool:
        self.messages.append({"stream_id": stream_id, "text": text})
        return True


class FakeChat:
    def __init__(self, *, group_streams: Optional[Dict[str, Dict[str, Any]]] = None,
                 user_streams: Optional[Dict[str, Dict[str, Any]]] = None) -> None:
        self.group_streams = group_streams or {}
        self.user_streams = user_streams or {}

    async def get_stream_by_group_id(self, group_id: str) -> Optional[Dict[str, Any]]:
        return self.group_streams.get(str(group_id))

    async def get_stream_by_user_id(self, user_id: str) -> Optional[Dict[str, Any]]:
        return self.user_streams.get(str(user_id))


class FakeMessage:
    def __init__(self, messages: Optional[List[Dict[str, Any]]] = None) -> None:
        self.messages = messages or []
        self.calls: List[Dict[str, Any]] = []

    async def get_recent(self, chat_id: str, limit: int = 10):
        # 与宿主 SDK 2.15 的真实签名保持一致：只接受 chat_id / limit。
        self.calls.append({"chat_id": chat_id, "limit": limit})
        return list(self.messages)


class FakeLlm:
    """假的 LLM 能力：按任务名返回预设文本，并记录调用。"""

    def __init__(
        self,
        *,
        judge_response: str = "",
        vision_response: str = "画面是一个游戏实机演示。",
        fail: bool = False,
    ) -> None:
        self.judge_response = judge_response
        self.vision_response = vision_response
        self.fail = fail
        self.calls: List[Dict[str, Any]] = []

    async def generate(self, prompt: Any = "", **kwargs: Any) -> Dict[str, Any]:
        self.calls.append({"prompt": prompt, **kwargs})
        if self.fail:
            return {"success": False, "error": "mock failure"}
        task_name = str(kwargs.get("task_name") or "")
        text = self.vision_response if task_name == "vlm" else self.judge_response
        return {"success": True, "response": text, "model": f"mock-{task_name}"}


class FakeCtx:
    def __init__(
        self,
        data_dir: Path,
        *,
        messages: Optional[List[Dict[str, Any]]] = None,
        group_streams: Optional[Dict[str, Dict[str, Any]]] = None,
        user_streams: Optional[Dict[str, Dict[str, Any]]] = None,
        llm: Optional[FakeLlm] = None,
    ) -> None:
        self.paths = FakePaths(data_dir)
        self.logger = FakeLogger()
        self.send = FakeSend()
        self.chat = FakeChat(group_streams=group_streams, user_streams=user_streams)
        self.message = FakeMessage(messages)
        self.llm = llm if llm is not None else FakeLlm()


class FakeClient:
    """假的 B 站客户端，返回预设候选视频。"""

    def __init__(
        self,
        videos: Optional[List[Any]] = None,
        *,
        search_results: Optional[List[Any]] = None,
        danmaku: Optional[List[Any]] = None,
        comments: Optional[List[Any]] = None,
        subtitle: str = "",
        playurl: str = "",
    ) -> None:
        self.videos = videos or []
        self.search_results = search_results if search_results is not None else list(self.videos)
        self.danmaku = danmaku or []
        self.comments = comments or []
        self.subtitle = subtitle
        self.playurl = playurl
        self.closed = False
        self.popular_calls: List[Dict[str, Any]] = []
        self.ranking_calls: List[Dict[str, Any]] = []
        self.search_calls: List[Dict[str, Any]] = []
        self.detail_calls: List[Dict[str, Any]] = []
        self.download_calls: List[Dict[str, Any]] = []

    async def fetch_popular(self, **kwargs: Any):
        self.popular_calls.append(kwargs)
        return list(self.videos)

    async def fetch_ranking(self, **kwargs: Any):
        self.ranking_calls.append(kwargs)
        return []

    async def fetch_search(self, keyword: str, **kwargs: Any):
        self.search_calls.append({"keyword": keyword, **kwargs})
        return list(self.search_results)

    async def fetch_danmaku(self, bvid: str, **kwargs: Any):
        self.detail_calls.append({"kind": "danmaku", "bvid": bvid, **kwargs})
        return list(self.danmaku)

    async def fetch_hot_comments(self, bvid: str, **kwargs: Any):
        self.detail_calls.append({"kind": "comments", "bvid": bvid, **kwargs})
        return list(self.comments)

    async def fetch_subtitle(self, bvid: str, **kwargs: Any):
        self.detail_calls.append({"kind": "subtitle", "bvid": bvid, **kwargs})
        return self.subtitle

    async def fetch_playurl(self, video: Any, **kwargs: Any):
        self.detail_calls.append({"kind": "playurl", "bvid": getattr(video, "bvid", "")})
        return self.playurl

    async def download_to(self, url: str, dest: Path, **kwargs: Any):
        self.download_calls.append({"url": url, "dest": str(dest)})
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b"fake-video")
        return Path(dest)

    async def aclose(self) -> None:
        self.closed = True


def make_video(module, **overrides: Any):
    """构造一个测试用 Video。"""

    import time as _time

    data = {
        "bvid": "BV1TEST00001",
        "title": "原神新版本测评",
        "description": "本期聊聊原神新版本",
        "owner": "测试UP",
        "partition": "游戏",
        "duration": 300,
        "pubdate": int(_time.time()) - 3600,
        "play": 120000,
        "like": 8000,
        "coin": 2000,
        "favorite": 1500,
        "share": 300,
        "reply": 800,
        "danmaku": 500,
        "tags": "原神,游戏,测评",
        "source": "popular",
    }
    data.update(overrides)
    return module.Video(**data)
