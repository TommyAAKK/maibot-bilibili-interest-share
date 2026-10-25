"""人设自检：打印插件读到的人设，以及注入人设后的 LLM 判断 prompt（不真正调用模型）。

插件运行时是通过宿主的 ``config.get`` 能力读人设的；这个脚本没法调宿主能力，
所以直接解析实例里的 ``config/bot_config.toml``，把同样的字段喂给插件的人设模块，
用来确认「宿主人设 -> 提示词」这条链路是通的。

用法（在插件目录下）：

    python tools/probe_persona.py
    python tools/probe_persona.py --bot-config "D:\\MaiBot\\config\\bot_config.toml"
    python tools/probe_persona.py --data-dir "D:\\MaiBot\\data\\plugins\\github.TommyAAKK.bilibili_interest_share"

不带参数时，会依次尝试：
1. 环境变量 ``MAIBOT_INSTANCE_DIR`` 下的 ``config/bot_config.toml``
2. ``--data-dir`` 反推实例根目录下的 ``config/bot_config.toml``
3. 当前目录 / 插件目录反推
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
import tomllib
from pathlib import Path
from typing import Any, Dict, List, Optional

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_PKG = "bilibili_interest_share_probe"


def _load(name: str):
    """按包内子模块方式加载插件里的模块。"""

    if _PKG in sys.modules:
        return importlib.import_module(f"{_PKG}.{name}")
    spec = importlib.util.spec_from_file_location(
        _PKG,
        PLUGIN_ROOT / "plugin.py",
        submodule_search_locations=[str(PLUGIN_ROOT)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载插件包")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_PKG] = module
    spec.loader.exec_module(module)
    return importlib.import_module(f"{_PKG}.{name}")


class _CapturingLlm:
    """只记录 prompt，不调用任何模型。"""

    def __init__(self) -> None:
        self.prompts: List[str] = []

    async def generate(self, prompt: Any = "", **kwargs: Any) -> Dict[str, Any]:
        self.prompts.append(prompt if isinstance(prompt, str) else str(prompt))
        return {"success": False, "error": "probe only"}


def _candidate_bot_configs(data_dir: str = "") -> List[Path]:
    """按优先级列出可能的人设配置文件。"""

    candidates: List[Path] = []

    def _add(root: Optional[Path]) -> None:
        if root is None:
            return
        path = Path(root) / "config" / "bot_config.toml"
        if path not in candidates:
            candidates.append(path)

    instance = os.environ.get("MAIBOT_INSTANCE_DIR")
    if instance:
        _add(Path(instance))
    if data_dir:
        # data/plugins/<id> -> 实例根目录
        _add(Path(data_dir).parents[2] if len(Path(data_dir).parents) >= 2 else None)
    _add(Path.cwd())
    _add(PLUGIN_ROOT)
    return candidates


def _host_values(path: Path) -> Dict[str, Any]:
    """从 bot_config.toml 里取出插件会用的那几个键。"""

    data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    bot = data.get("bot") if isinstance(data.get("bot"), dict) else {}
    personality = (
        data.get("personality") if isinstance(data.get("personality"), dict) else {}
    )
    return {
        "bot.nickname": bot.get("nickname", ""),
        "bot.alias_names": bot.get("alias_names", []),
        "personality.personality": personality.get("personality", ""),
        "personality.reply_style": personality.get("reply_style", ""),
        "personality.behavior_style": personality.get("behavior_style", ""),
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="人设自检（不调用模型）")
    parser.add_argument("--data-dir", default="", help="插件数据目录（用于反推实例根目录）")
    parser.add_argument("--bot-config", default="", help="直接指定 bot_config.toml")
    parser.add_argument("--style-hint", default="", help="额外风格要求")
    parser.add_argument("--show-prompt", action="store_true", help="打印完整 prompt")
    args = parser.parse_args(argv)

    persona_mod = _load("persona")
    judge_mod = _load("llm_judge")
    bilibili_mod = _load("bilibili")

    configs = [Path(args.bot_config)] if args.bot_config else _candidate_bot_configs(
        args.data_dir
    )
    values: Dict[str, Any] = {}
    used: Optional[Path] = None
    for path in configs:
        if not path.is_file():
            continue
        try:
            values = _host_values(path)
        except Exception as exc:  # noqa: BLE001 - 自检脚本，读失败就继续找
            print(f"跳过 {path}：{exc}")
            continue
        if any(str(item or "").strip() for item in values.values()):
            used = path
            break

    if used is None:
        print("没有读到人设：请用 --bot-config 指定 bot_config.toml，或设置 MAIBOT_INSTANCE_DIR。")
        return 1

    provider = persona_mod.PersonaProvider(
        enabled=True,
        config_get=None,
        style_hint=args.style_hint,
        cache_seconds=0,
    )
    # 直接把从 TOML 读到的值当作宿主的 config.get 结果
    provider._cached = persona_mod.build_persona(values, source="host")

    persona = provider.get()
    print(f"人设文件：{used}")
    print(f"人设来源：{persona.source}")
    print(f"人设名：{persona.name or '（未命名）'}")
    print(f"别名：{'、'.join(persona.aliases) or '（无）'}")

    block = provider.prompt_block()
    print("\n=== 人设提示词 ===")
    print(block)

    video = bilibili_mod.Video(
        bvid="BV1PROBE0001",
        title="原神新版本测评",
        description="这期聊聊新版本的角色和玩法。",
        owner="测试UP",
        partition="游戏",
        duration=300,
        tags="原神,测评",
        play=120000,
        like=8000,
    )
    prompt = judge_mod.build_judge_prompt(
        video,
        interests=["原神", "机械键盘"],
        danmaku=["这个角色好强"],
        comments=["讲得挺清楚"],
        subtitle="",
        frames_desc="",
        persona_block=block,
        intro_enabled=True,
        intro_max_chars=40,
    )
    print("\n=== 注入人设后的判断 prompt（节选） ===")
    print(
        prompt
        if args.show_prompt
        else prompt[:1200] + ("\n……（用 --show-prompt 看全文）" if len(prompt) > 1200 else "")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
