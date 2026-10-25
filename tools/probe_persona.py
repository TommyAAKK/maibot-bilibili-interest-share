"""人设自检：打印插件读到的人设，以及注入人设后的 LLM 判断 prompt（不真正调用模型）。

用法（在插件目录下）：

    python tools/probe_persona.py
    python tools/probe_persona.py --data-dir "D:\\MaiBot\\data\\plugins\\github.TommyAAKK.bilibili_interest_share"
    python tools/probe_persona.py --bot-config "D:\\MaiBot\\config\\bot_config.toml"

不带参数时，会依次尝试：
1. 环境变量 ``MAIBOT_INSTANCE_DIR`` 下的 ``config/bot_config.toml``
2. ``--data-dir`` 反推实例根目录
3. 当前目录反推（若本插件已放在 ``<实例>/plugins`` 或 ``<实例>/data/plugins`` 下）
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
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


def _candidate_data_dirs() -> List[Path]:
    dirs: List[Path] = []
    instance = os.environ.get("MAIBOT_INSTANCE_DIR")
    if instance:
        dirs.append(Path(instance) / "data" / "plugins" / "github.TommyAAKK.bilibili_interest_share")
    dirs.append(Path.cwd())
    dirs.append(PLUGIN_ROOT)
    return dirs


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

    data_dirs: List[Path] = []
    if args.data_dir:
        data_dirs.append(Path(args.data_dir))
    data_dirs.extend(_candidate_data_dirs())

    provider = None
    for data_dir in data_dirs:
        candidate = persona_mod.PersonaProvider(
            enabled=True,
            data_dir=data_dir,
            style_hint=args.style_hint,
            config_path=args.bot_config,
            cache_seconds=0,
        )
        persona = candidate.get()
        if not persona.is_empty:
            provider = candidate
            print(f"数据目录：{data_dir}")
            print(f"实例根目录：{persona_mod.find_instance_root(data_dir)}")
            print(f"人设文件：{candidate.resolve_config_path()}")
            print(f"人设来源：{persona.source}")
            break

    if provider is None:
        print("没有读到人设：请用 --bot-config 指定 bot_config.toml，或设置 MAIBOT_INSTANCE_DIR。")
        return 1

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
    print(prompt if args.show_prompt else prompt[:1200] + ("\n……（用 --show-prompt 看全文）" if len(prompt) > 1200 else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
