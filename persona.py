"""读取 MaiBot 宿主的人设配置，让分享文案贴合麦麦的人设。

宿主的人设写在实例的 ``config/bot_config.toml`` 里：

.. code-block:: toml

    [bot]
    nickname = "小鲸鱼"
    alias_names = ["小女仆"]

    [personality]
    personality = "是一个可爱机灵的鲸鱼娘女仆……"
    behavior_style = "……"
    reply_style = "你的风格可爱机灵又自然……"

插件不直接读这个文件，而是通过宿主提供的 ``config.get`` 能力按需取值
（``bot.nickname`` / ``bot.alias_names`` / ``personality.personality`` /
``personality.behavior_style`` / ``personality.reply_style``），
这样既不用猜测实例目录结构，也符合插件的权限边界。

``config.get`` 是异步能力，因此本模块把读取拆成两步：

* ``await provider.refresh()``：从宿主拉一次人设并缓存（插件在 on_load 和
  每轮分享开始前调用，带 TTL，不会频繁请求）；
* ``provider.get()`` / ``provider.prompt_block()``：同步读取缓存结果，
  供同步的提示词拼装代码使用。

配置里也可以用 ``[persona].name`` / ``personality`` / ``reply_style``
直接覆盖对应字段；都拿不到人设时返回空人设，调用方退化成中性语气。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger("plugin.bilibili_interest_share.persona")

# 宿主人设字段在全局配置里的键名
KEY_NICKNAME = "bot.nickname"
KEY_ALIASES = "bot.alias_names"
KEY_PERSONALITY = "personality.personality"
KEY_BEHAVIOR_STYLE = "personality.behavior_style"
KEY_REPLY_STYLE = "personality.reply_style"

HOST_KEYS = (
    KEY_NICKNAME,
    KEY_ALIASES,
    KEY_PERSONALITY,
    KEY_BEHAVIOR_STYLE,
    KEY_REPLY_STYLE,
)

# config.get 的可调用类型：async (key, default) -> value
ConfigGetter = Callable[[str, Any], Awaitable[Any]]


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


def _as_text(value: Any) -> str:
    """把宿主配置值转成去掉首尾空白的字符串。"""

    if value is None:
        return ""
    return str(value).strip()


def _as_aliases(value: Any) -> List[str]:
    """把宿主配置里的别名列表规整成字符串列表。"""

    if isinstance(value, str):
        items: List[Any] = [item for item in value.replace("，", ",").split(",")]
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        return []
    aliases: List[str] = []
    for item in items:
        text = _as_text(item)
        if text and text not in aliases:
            aliases.append(text)
    return aliases


def build_persona(values: Dict[str, Any], *, source: str = "host") -> Persona:
    """用 ``config.get`` 的取值结果拼出一份人设。"""

    persona = Persona(
        name=_as_text(values.get(KEY_NICKNAME)),
        personality=_as_text(values.get(KEY_PERSONALITY)),
        reply_style=_as_text(values.get(KEY_REPLY_STYLE)),
        behavior_style=_as_text(values.get(KEY_BEHAVIOR_STYLE)),
        aliases=_as_aliases(values.get(KEY_ALIASES)),
        source=source,
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
        config_get: Optional[ConfigGetter] = None,
        override_name: str = "",
        override_personality: str = "",
        override_reply_style: str = "",
        style_hint: str = "",
        cache_seconds: int = 120,
    ) -> None:
        self.enabled = bool(enabled)
        self.config_get = config_get
        self.override_name = (override_name or "").strip()
        self.override_personality = (override_personality or "").strip()
        self.override_reply_style = (override_reply_style or "").strip()
        self.style_hint = (style_hint or "").strip()
        self.cache_seconds = max(0, int(cache_seconds or 0))
        self._cached: Optional[Persona] = None
        self._cached_at: float = 0.0

    # -- 读取 ----------------------------------------------------------

    async def _read_host(self) -> Persona:
        """通过宿主 ``config.get`` 能力读人设。"""

        getter = self.config_get
        if getter is None:
            return Persona()
        values: Dict[str, Any] = {}
        for key in HOST_KEYS:
            try:
                values[key] = await getter(key, "")
            except Exception as exc:  # noqa: BLE001 - 读不到人设不算错误
                logger.debug("读取宿主人设字段 %s 失败：%s", key, exc)
                values[key] = ""
        return build_persona(values, source="host")

    async def refresh(self) -> Persona:
        """从宿主重新拉取人设并写缓存；禁用或失败时缓存空人设。"""

        if not self.enabled:
            self._cached = Persona()
            self._cached_at = time.time()
            return self._cached

        host = await self._read_host()
        persona = Persona(
            name=self.override_name or host.name,
            personality=self.override_personality or host.personality,
            reply_style=self.override_reply_style or host.reply_style,
            behavior_style=host.behavior_style,
            aliases=list(host.aliases),
            source=(
                "override"
                if (self.override_name or self.override_personality or self.override_reply_style)
                else host.source
            ),
        )
        if persona.is_empty:
            persona = Persona()
        self._cached = persona
        self._cached_at = time.time()
        return persona

    def needs_refresh(self) -> bool:
        """缓存是否已过期（或从未拉取过）。"""

        if self._cached is None:
            return True
        return (time.time() - self._cached_at) >= self.cache_seconds

    async def ensure(self) -> Persona:
        """需要时刷新，否则直接返回缓存。"""

        if self.needs_refresh():
            return await self.refresh()
        return self._cached or Persona()

    def get(self) -> Persona:
        """同步读取缓存的人设；还没拉取过时返回空人设。"""

        return self._cached or Persona()

    def invalidate(self) -> None:
        self._cached = None
        self._cached_at = 0.0

    def prompt_block(self) -> str:
        """给人设段落加上额外风格提示。"""

        persona = self.get()
        block = persona.prompt_block()
        if not block:
            return ""
        if self.style_hint:
            block = block + "\n额外要求：" + self.style_hint
        return block
