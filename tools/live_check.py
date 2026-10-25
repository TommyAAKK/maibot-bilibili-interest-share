"""真实联网自检脚本：用真实 B 站接口跑一轮「抓取 -> 打分 -> 详情 -> 分享」。

不会真的发消息到 QQ，而是把要发送的内容打印出来，方便确认插件效果。

用法（在插件目录下）::

    python tools/live_check.py 机械键盘 原神
    python tools/live_check.py 原神 --limit 2 --danmaku
    python tools/live_check.py 原神 --vision          # 真下载视频并抽帧识图
    python tools/live_check.py --popular-only 编程

说明：脚本本身没有 MaiBot 宿主，所以默认关闭 LLM 判断（``--llm`` 需要宿主），
弹幕/热门评论走真实接口，``--vision`` 会真实下载视频并用 ffmpeg 抽帧。
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import importlib.util
import sys
import tempfile
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT))

_MODULE_NAME = "bilibili_interest_share_live"


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


class _Logger:
    def info(self, message, *args):
        print(f"[INFO] {message % args if args else message}")

    def warning(self, message, *args):
        print(f"[WARN] {message % args if args else message}")

    def debug(self, message, *args):
        pass

    def error(self, message, *args):
        print(f"[ERROR] {message % args if args else message}")


class _Paths:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = str(data_dir)


class _Send:
    def __init__(self) -> None:
        self.messages = []

    async def text(self, text, stream_id, **kwargs):
        self.messages.append((stream_id, text))
        return True


class _Chat:
    async def get_stream_by_group_id(self, group_id):
        return None

    async def get_stream_by_user_id(self, user_id):
        return None


class _Message:
    async def get_recent(self, **kwargs):
        return []


class _Llm:
    """没有宿主时的占位 LLM：一律失败，插件会回退到规则打分。"""

    async def generate(self, prompt=None, **kwargs):
        return {"success": False, "error": "live_check 无宿主 LLM"}


class _Ctx:
    def __init__(self, data_dir: Path) -> None:
        self.paths = _Paths(data_dir)
        self.logger = _Logger()
        self.send = _Send()
        self.chat = _Chat()
        self.message = _Message()
        self.llm = _Llm()


async def main() -> int:
    parser = argparse.ArgumentParser(description="麦麦刷B站插件真实联网自检")
    parser.add_argument("keywords", nargs="*", default=["机械键盘"], help="兴趣关键词")
    parser.add_argument("--popular-only", action="store_true", help="只刷热门，不按关键词搜索")
    parser.add_argument("--loose", action="store_true", help="放宽过滤/打分条件，便于看到更多候选")
    parser.add_argument("--limit", type=int, default=3, help="最多分享几个")
    parser.add_argument("--danmaku", action="store_true", help="抓取弹幕与热门评论并打印样本")
    parser.add_argument("--vision", action="store_true", help="真实下载视频并抽帧（需 ffmpeg）")
    parser.add_argument("--llm", action="store_true", help="启用 LLM 判断（需要 MaiBot 宿主）")
    parser.add_argument("--parts", action="store_true", help="按分条格式发送（推荐语/标题链接/自然语言介绍分开）")
    parser.add_argument("--shiwei", action="store_true", help="改为验证 /赤石视频（抽象整活类）")
    args = parser.parse_args()

    module = load_plugin_module()
    with tempfile.TemporaryDirectory() as tmp:
        ctx = _Ctx(Path(tmp))
        plugin = module.BilibiliInterestSharePlugin()
        plugin._set_context(ctx)

        config = plugin.build_default_config()
        config["plugin"]["enabled"] = True
        config["schedule"]["max_share_per_round"] = args.limit
        config["schedule"]["run_on_start"] = False
        config["interest"]["use_llm_judge"] = bool(args.llm)
        config["vision"]["enabled"] = bool(args.vision)
        config["danmaku"]["danmaku_enabled"] = bool(args.danmaku)
        config["danmaku"]["comments_enabled"] = bool(args.danmaku)
        config["share"]["send_as_parts"] = bool(args.parts)
        if args.loose:
            config["filter"]["min_play"] = 0
            config["filter"]["min_like"] = 0
            config["filter"]["exclude_official"] = False
            config["scoring"]["min_score"] = 0.0
            config["scoring"]["require_interest_match"] = False
        config["sources"]["search_enabled"] = not args.popular_only
        if args.popular_only:
            config["sources"]["popular_enabled"] = True
        config["streams"] = [
            {
                "name": "自检聊天流",
                "enabled": True,
                "stream_id": "live-check",
                "interests": args.keywords,
                "auto_interest": False,
                "max_share_per_round": args.limit,
            }
        ]
        plugin.set_plugin_config(config)
        await plugin.on_load()
        try:
            if args.danmaku:
                await _dump_detail_samples(plugin)
            if args.shiwei:
                summary = await plugin.share_shiwei("live-check")
                summary.setdefault("fetched", 0)
            else:
                summary = await plugin.run_round(reason="live-check", manual=True)
        finally:
            await plugin.on_unload()

        print("=" * 60)
        shared = summary.get("shared") or 0
        shared_count = len(shared) if isinstance(shared, (list, tuple)) else int(shared)
        if args.shiwei:
            print(f"赤石视频：分享 {shared_count} 个")
        else:
            print(f"候选：{summary.get('fetched', 0)} 个｜分享：{shared_count} 个")
            print(f"搜索词：{'、'.join(summary.get('keywords') or []) or '（未搜索）'}")
        print("=" * 60)
        if not ctx.send.messages:
            print("没有挑选出合适的视频（可以放宽 filter/scoring 配置）")
            return 1
        for index, (stream_id, text) in enumerate(ctx.send.messages, start=1):
            print(f"--- 第 {index} 条（发送到 {stream_id}）---")
            print(text)
            print()
    return 0


async def _dump_detail_samples(plugin) -> None:
    """抓一个候选的弹幕/评论，打印过滤前后的样本，便于确认 AI 鱼灾过滤效果。"""

    config = plugin.config
    keywords = ["原神"]
    try:
        videos = await plugin._client.fetch_search(keywords[0], page=1)
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] 详情取样失败：{exc}")
        return
    if not videos:
        print("[WARN] 没有取到详情样本")
        return
    video = videos[0]
    danmaku = await plugin._client.fetch_danmaku(video.bvid, limit=config.danmaku.danmaku_fetch_limit)
    comments = await plugin._client.fetch_hot_comments(video.bvid, limit=20)
    textfilter = importlib.import_module(f"{_MODULE_NAME}.textfilter")
    clean_danmaku = textfilter.filter_danmaku(
        danmaku, limit=config.danmaku.danmaku_limit, filter_ai=config.danmaku.filter_ai_spam
    )
    clean_comments = textfilter.filter_comments(
        comments, limit=config.danmaku.comments_limit, filter_ai=config.danmaku.filter_ai_spam
    )
    print("=" * 60)
    print(f"详情样本：{video.bvid} {video.title}")
    print(f"弹幕：原始 {len(danmaku)} 条 -> 过滤后 {len(clean_danmaku)} 条")
    for text in clean_danmaku[:8]:
        print(f"  · {text}")
    print(f"热门评论：原始 {len(comments)} 条 -> 过滤后 {len(clean_comments)} 条")
    for text in clean_comments[:5]:
        print(f"  · {text}")
    print("=" * 60)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
