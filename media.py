"""视频抽帧：下载视频 -> 用 ffmpeg 抽若干帧 -> 转成 data URL。

不依赖 yt-dlp / OpenCV：视频地址由 :meth:`bilibili.BilibiliClient.fetch_playurl`
从 B 站 playurl 接口拿到（360P 即可，够识图用），抽帧用 ffmpeg。

ffmpeg 查找顺序：

1. ``[advanced].ffmpeg_path``
2. ``imageio_ffmpeg.get_ffmpeg_exe()``（MaiBot 插件环境自带）
3. ``PATH`` 里的 ``ffmpeg``
"""

from __future__ import annotations

import asyncio
import base64
import logging
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger("plugin.bilibili_interest_share.media")

_MAX_FRAME_BYTES = 4 * 1024 * 1024


def resolve_ffmpeg(configured: str = "") -> str:
    """按优先级查找 ffmpeg 可执行文件；找不到返回空字符串。"""

    candidate = (configured or "").strip()
    if candidate:
        path = Path(candidate).expanduser()
        if path.is_file():
            return str(path)
        logger.warning("配置的 ffmpeg 路径不存在：%s", candidate)

    try:
        import imageio_ffmpeg  # type: ignore

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and Path(exe).is_file():
            return str(exe)
    except Exception:  # noqa: BLE001 - 没装 imageio-ffmpeg 属于正常情况
        pass

    return shutil.which("ffmpeg") or ""


def frame_timestamps(duration: float, count: int) -> List[float]:
    """在视频时长里均匀取 ``count`` 个时间点（避开开头结尾各 5%）。"""

    count = max(1, int(count))
    duration = max(0.0, float(duration or 0.0))
    if duration <= 0:
        return [0.0]
    if count == 1:
        return [duration * 0.5]
    start = duration * 0.05
    end = duration * 0.95
    step = (end - start) / float(count - 1)
    return [start + step * index for index in range(count)]


def extract_frames(
    video_path: Path,
    *,
    duration: float,
    count: int = 4,
    max_width: int = 640,
    ffmpeg_path: str = "",
    timeout: float = 90.0,
) -> List[bytes]:
    """从本地视频文件抽帧，返回 JPEG 字节列表（失败时返回空列表）。"""

    exe = resolve_ffmpeg(ffmpeg_path)
    if not exe:
        logger.warning("找不到 ffmpeg，无法抽帧（可安装 imageio-ffmpeg 或配置 [advanced].ffmpeg_path）")
        return []
    if not Path(video_path).is_file():
        return []

    frames: List[bytes] = []
    for timestamp in frame_timestamps(duration, count):
        command = [
            exe,
            "-hide_banner",
            "-loglevel", "error",
            "-ss", f"{timestamp:.3f}",
            "-i", str(video_path),
            "-frames:v", "1",
            "-vf", f"scale='min({int(max_width)},iw)':-2",
            "-f", "image2pipe",
            "-vcodec", "mjpeg",
            "-",
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                timeout=max(5.0, float(timeout) / max(1, count) + 5.0),
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("抽帧失败（%ss）：%s", timestamp, exc)
            continue
        data = completed.stdout or b""
        if completed.returncode != 0 or not data:
            continue
        if len(data) > _MAX_FRAME_BYTES:
            continue
        frames.append(data)
    return frames


def to_data_url(raw: bytes, *, mime: str = "image/jpeg") -> str:
    """把图片字节转成 ``data:image/jpeg;base64,...`` 形式的 URL。"""

    encoded = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{encoded}"


class FrameExtractor:
    """下载 + 抽帧的组合工具（异步接口，内部用线程跑 ffmpeg）。"""

    def __init__(
        self,
        *,
        ffmpeg_path: str = "",
        frame_count: int = 4,
        max_width: int = 640,
        max_download_mb: int = 80,
        timeout: float = 90.0,
    ) -> None:
        self.ffmpeg_path = ffmpeg_path
        self.frame_count = max(1, int(frame_count))
        self.max_width = max(160, int(max_width))
        self.max_download_mb = max(5, int(max_download_mb))
        self.timeout = max(10.0, float(timeout))

    @property
    def available(self) -> bool:
        return bool(resolve_ffmpeg(self.ffmpeg_path))

    async def capture(self, client, video, work_dir: Path) -> List[str]:
        """下载 ``video`` 并抽帧，返回 data URL 列表（任何一步失败都返回空列表）。

        Args:
            client: :class:`bilibili.BilibiliClient`，需要 ``fetch_playurl`` / ``download_to``。
            video: :class:`bilibili.Video`。
            work_dir: 临时文件目录。
        """

        if not self.available:
            return []
        fetch_playurl = getattr(client, "fetch_playurl", None)
        download_to = getattr(client, "download_to", None)
        if fetch_playurl is None or download_to is None:
            return []

        work_dir = Path(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)
        target = work_dir / f"{getattr(video, 'bvid', 'video')}.mp4"
        try:
            url = await fetch_playurl(video)
            if not url:
                return []
            await asyncio.wait_for(
                download_to(url, target, max_bytes=self.max_download_mb * 1024 * 1024),
                timeout=self.timeout,
            )
            raw_frames = await asyncio.wait_for(
                asyncio.to_thread(
                    extract_frames,
                    target,
                    duration=float(getattr(video, "duration", 0) or 0),
                    count=self.frame_count,
                    max_width=self.max_width,
                    ffmpeg_path=self.ffmpeg_path,
                    timeout=self.timeout,
                ),
                timeout=self.timeout,
            )
        except asyncio.TimeoutError:
            logger.warning("抽帧超时：%s", getattr(video, "bvid", ""))
            return []
        except Exception as exc:  # noqa: BLE001 - 抽帧是锦上添花，失败不影响分享
            logger.warning("抽帧失败：%s", exc)
            return []
        finally:
            try:
                if target.exists():
                    target.unlink()
            except OSError:
                pass

        return [to_data_url(raw) for raw in raw_frames]


def cleanup_dir(path: Optional[Path]) -> None:
    """尽量清理临时目录。"""

    if path is None:
        return
    try:
        shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass
