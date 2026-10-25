"""读取 MaiBot 宿主的人设配置，让分享文案贴合麦麦的人设。

MaiBot 的人设写在宿主实例的 ``config/bot_config.toml`` 里：

.. code-block:: toml

    [bot]
    nickname = "小鲸鱼"
    alias_names = ["小女仆"]

    [personality]
    personality = "是一个可爱机灵的鲸鱼娘女仆……"
    behavior_style = "……"
    reply_style = "你的风格可爱机灵又自然……"

插件运行在宿主进程内，拿到的 ``ctx.paths.data_dir`` 形如
``<实例根>/data/plugins/<插件ID>``，因此可以从它向上三级反推实例根目录，
再读 ``config/bot_config.toml``。也可以用 ``[persona].bot_config_path`` 手动指定，
或用 ``[persona].name`` / ``personality`` / ``reply_style`` 直接覆盖。

配置了覆盖值时以覆盖值为准；否则读宿主文件；都拿不到就返回空人设，
调用方会退化成中性语气（不会报错）。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("plugin.bilibili_interest_share.persona")

BOT_CONFIG_RELATIVE = Path("config") / "bot_config.toml"


@dataclass
class Persona:
    """一份人设描述。"""

    name: str = ""
    personality: str = ""
    reply_style: str = ""
    behavior_style: str = ""
    aliases: List[str] = field(default_factory=list)
    source: str = "none"  # override / host / none

    @property
    def is_empty(self) -> bool:
        return not (self.name or self.personality or self.reply_style)

    def prompt_block(self) -> str:
        """拼成喂给 LLM 的人设段落；空人设返回空串。"""

        if self.is_empty:
            return ""
        lines = ["【你的人设】"]
        if self.name:
            lines.append(f"你的名字：{self.name}")
            if self.aliases:
                lines.append("别人可能这样叫你：" + "、".join(self.aliases[:5]))
        if self.personality:
            lines.append(f"你的性格与身份：{self.personality}")
        if self.reply_style:
            lines.append(f"你的说话风格：{self.reply_style}")
        if self.behavior_style:
            lines.append(f"你的行为准则：{self.behavior_style}")
        return "\n".join(lines)


def find_instance_root(data_dir: Optional[Path]) -> Optional[Path]:
    """从 ``<实例根>/data/plugins/<插件ID>`` 反推实例根目录。"""

    if data_dir is None:
        return None
    try:
        path = Path(data_dir).resolve()
    except (OSError, TypeError, ValueError):
        return None
    # data_dir 的期望结构：root / data / plugins / <plugin_id>
    if len(path.parents) < 3:
        return None
    if path.parent.name != "plugins" or path.parent.parent.name != "data":
        return None
    return path.parent.parent.parent


def load_persona_from_toml(path: Path) -> Persona:
    """从 ``bot_config.toml`` 读取人设；解析失败返回空人设。"""

    try:
        import tomllib
    except ImportError:  # pragma: no cover - Python < 3.11
        logger.debug("当前 Python 没有 tomllib，无法读取宿主人设")
        return Persona()

    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except (OSError, ValueError) as exc:
        logger.debug("读取宿主人设失败（%s）：%s", path, exc)
        return Persona()

    if not isinstance(data, dict):
        return Persona()
    bot = data.get("bot") if isinstance(data.get("bot"), dict) else {}
    personality_section = (
        data.get("personality") if isinstance(data.get("personality"), dict) else {}
    )

    aliases_raw = bot.get("alias_names")
    aliases = [str(item).strip() for item in aliases_raw if str(item).strip()] if isinstance(
        aliases_raw, list
    ) else []

    persona = Persona(
        name=str(bot.get("nickname") or "").strip(),
        personality=str(personality_section.get("personality") or "").strip(),
        reply_style=str(personality_section.get("reply_style") or "").strip(),
        behavior_style=str(personality_section.get("behavior_style") or "").strip(),
        aliases=aliases,
        source="host",
    )
    if persona.is_empty:
        return Persona()
    return persona


class PersonaProvider:
    """带缓存的人设读取器：优先覆盖值，其次宿主配置。"""

    def __init__(
        self,
        *,
        enabled: bool = True,
        data_dir: Optional[Path] = None,
        override_name: str = "",
        override_personality: str = "",
        override_reply_style: str = "",
        style_hint: str = "",
        config_path: str = "",
        cache_seconds: int = 120,
    ) -> None:
        self.enabled = bool(enabled)
        self.data_dir = data_dir
        self.override_name = (override_name or "").strip()
        self.override_personality = (override_personality or "").strip()
        self.override_reply_style = (override_reply_style or "").strip()
        self.style_hint = (style_hint or "").strip()
        self.config_path = (config_path or "").strip()
        self.cache_seconds = max(0, int(cache_seconds or 0))
        self._cached: Optional[Persona] = None
        self._cached_at: float = 0.0
        self._cached_mtime: Optional[float] = None

    # -- 路径 ----------------------------------------------------------

    def resolve_config_path(self) -> Optional[Path]:
        """定位 ``bot_config.toml``。"""

        if self.config_path:
            path = Path(self.config_path).expanduser()
            return path if path.is_file() else None
        root = find_instance_root(self.data_dir)
        if root is None:
            return None
        candidate = root / BOT_CONFIG_RELATIVE
        return candidate if candidate.is_file() else None

    # -- 读取 ----------------------------------------------------------

    def _load(self) -> Persona:
        if not self.enabled:
            return Persona()
        # 有任意覆盖值就用覆盖值（缺失的字段仍可从宿主补齐）
        host = Persona()
        path = self.resolve_config_path()
        if path is not None:
            host = load_persona_from_toml(path)
        persona = Persona(
            name=self.override_name or host.name,
            personality=self.override_personality or host.personality,
            reply_style=self.override_reply_style or host.reply_style,
            behavior_style=host.behavior_style,
            aliases=list(host.aliases),
            source="override" if (self.override_name or self.override_personality or self.override_reply_style) else host.source,
        )
        if persona.is_empty:
            return Persona()
        return persona

    def get(self) -> Persona:
        """取人设（带 TTL 与 mtime 双重缓存）。"""

        now = time.time()
        path = self.resolve_config_path()
        mtime: Optional[float] = None
        if path is not None:
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = None
        fresh = (
            self._cached is not None
            and (now - self._cached_at) < self.cache_seconds
            and mtime == self._cached_mtime
        )
        if fresh:
            return self._cached  # type: ignore[return-value]
        persona = self._load()
        self._cached = persona
        self._cached_at = now
        self._cached_mtime = mtime
        return persona

    def invalidate(self) -> None:
        self._cached = None
        self._cached_at = 0.0
        self._cached_mtime = None

    def prompt_block(self) -> str:
        """给人设段落加上额外风格提示。"""

        persona = self.get()
        block = persona.prompt_block()
        if not block:
            return ""
        if self.style_hint:
            block = block + "\n额外要求：" + self.style_hint
        return block
