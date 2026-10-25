"""真实联网探测：下载一个 B 站视频并用 ffmpeg 抽帧，验证「抽帧」链路可用。

不依赖 MaiBot 宿主，也不调用任何 LLM，只验证：
playurl 取流 -> 下载 -> ffmpeg 抽帧 -> data URL 编码。

用法（在插件目录下）::

    python tools/probe_vision.py                 # 自动挑一个短视频
    python tools/probe_vision.py BV1xxxxxxx      # 指定 bvid
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import sys
import tempfile
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT))

_MODULE_NAME = "bilibili_interest_share_probe"


def load_plugin_module():
    if _MODULE_NAME in sys.modules:
        return sys.modules[_MODULE_NAME]
    spec = importlib.util.spec_from_file_location(
        _MODULE_NAME,
        PLUGIN_ROOT / "plugin.py",
        submodule_search_locations=[str(PLUGIN_ROOT)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载插件模块")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


async def main() -> int:
    module = load_plugin_module()
    bilibili = importlib.import_module(f"{_MODULE_NAME}.bilibili")
    media = importlib.import_module(f"{_MODULE_NAME}.media")

    ffmpeg_path = media.resolve_ffmpeg("")
    print(f"ffmpeg: {ffmpeg_path or '（未找到）'}")
    if not ffmpeg_path:
        print("没有 ffmpeg，无法抽帧")
        return 1

    bvid = ""
    save_dir = ""
    args = [item for item in sys.argv[1:]]
    if "--save" in args:
        index = args.index("--save")
        save_dir = args[index + 1] if index + 1 < len(args) else ""
        del args[index : index + 2]
    if args:
        bvid = args[0]
    client = bilibili.BilibiliClient(timeout=20.0)
    try:
        if not bvid:
            videos = await client.fetch_search("原神", page=1)
            candidates = [item for item in videos if 60 <= item.duration <= 600]
            if not candidates:
                candidates = videos[:1]
            if not candidates:
                print("没有取到候选视频")
                return 1
            video = candidates[0]
        else:
            detail = await client.fetch_view(bvid)
            video = bilibili.Video(
                bvid=bvid,
                title=detail.get("title") or bvid,
                duration=int(detail.get("duration") or 0),
                cid=int(detail.get("cid") or 0),
            )
        print(f"目标视频：{video.bvid} {video.title}（{video.duration} 秒）")

        extractor = media.FrameExtractor(ffmpeg_path=ffmpeg_path, frame_count=4, max_width=640)
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp) / video.bvid
            frames = await extractor.capture(client, video, work_dir)
            print(f"抽到 {len(frames)} 帧")
            for index, frame in enumerate(frames, start=1):
                size = len(frame)
                print(f"  · 第 {index} 帧：{size} 字节（data URL 前缀 {frame[:32]}…）")
                if save_dir:
                    import base64

                    target_dir = Path(save_dir)
                    target_dir.mkdir(parents=True, exist_ok=True)
                    payload = frame.split(",", 1)[-1]
                    (target_dir / f"{video.bvid}_{index}.jpg").write_bytes(
                        base64.b64decode(payload)
                    )
            if save_dir:
                print(f"帧已保存到：{Path(save_dir).resolve()}")
            if not frames:
                return 1
            return 0
    finally:
        await client.aclose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
