"""把运行时得到的结果写回插件自己的 ``config.toml``。

MaiBot 的 SDK **没有** ``config.set`` 能力（宿主只暴露 ``config.get``），
``component.update_plugin_config`` 也硬编码只允许内置的插件管理插件调用。
所以「扫码登录后保存 Cookie」「自动提取的兴趣写回设置页」这两件事，
只能由插件直接改写自己目录下的 ``config.toml``：

- WebUI 读取配置走的是 ``plugins/<plugin_id>/config.toml`` 这个文件，
  写完刷新页面就能看到新值；
- 宿主没有对 config.toml 做文件监听，所以写完**不会**触发 ``on_config_update``，
  调用方需要自己同步内存里的 config 对象（本模块只管文件）。

写文件用 ``tomlkit``（宿主环境自带），可以保留用户手写的注释与格式。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

logger = logging.getLogger("plugin.bilibili_interest_share.config_edit")

CONFIG_FILENAME = "config.toml"


# ----------------------------------------------------------------------
# 路径与读写
# ----------------------------------------------------------------------


def find_config_path(plugin_dir: Optional[Path] = None) -> Path:
    """找到插件的 ``config.toml``。

    默认用本模块所在目录（插件目录），测试里可以显式传一个临时目录。
    """

    base = Path(plugin_dir) if plugin_dir is not None else Path(__file__).resolve().parent
    return base / CONFIG_FILENAME


def _load_document(path: Path):
    """读取 toml 文档；文件不存在时返回一个空文档。"""

    import tomlkit

    if path.exists():
        text = path.read_text(encoding="utf-8-sig")
        if not text.strip():
            return tomlkit.document()
        return tomlkit.parse(text)
    return tomlkit.document()


def _save_document(path: Path, document: Any) -> None:
    """原子写入 toml（先写临时文件再替换），保持 UTF-8 无 BOM。"""

    import os

    path.parent.mkdir(parents=True, exist_ok=True)
    text = _dumps(document)
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(text, encoding="utf-8")
    os.replace(tmp_path, path)


def _dumps(document: Any) -> str:
    import tomlkit

    return tomlkit.dumps(document)


def read_cookie(path: Path) -> str:
    """读取 ``[account].cookie``（读不到返回空串）。"""

    try:
        document = _load_document(path)
    except Exception as exc:  # noqa: BLE001
        logger.debug("读取配置文件失败（%s）：%s", path, exc)
        return ""
    account = document.get("account")
    if account is None:
        return ""
    try:
        value = account.get("cookie")
    except AttributeError:
        return ""
    return str(value or "").strip() if value is not None else ""


def update_cookie(path: Path, cookie: str) -> bool:
    """把 Cookie 写进 ``[account].cookie``；写成功返回 True。"""

    try:
        import tomlkit

        document = _load_document(path)
        if "account" not in document:
            document["account"] = tomlkit.table()
        account = document["account"]
        account["cookie"] = str(cookie or "")
        _save_document(path, document)
        logger.info("已把 B 站 Cookie 写入 %s", path)
        return True
    except Exception as exc:  # noqa: BLE001 - 写配置失败不应影响插件运行
        logger.warning("写入 B 站 Cookie 失败（%s）：%s", path, exc)
        return False


# ----------------------------------------------------------------------
# 聊天流身份
# ----------------------------------------------------------------------


def stream_identity(target: Any) -> str:
    """给一个 ``[[streams]]`` 配置项算一个稳定的 key，用于记住「上次写了什么」。

    优先级和解析会话时一致：会话 ID > 群号 > 用户 ID > 备注名。
    """

    for attr in ("stream_id", "group_id", "user_id", "name"):
        value = str(getattr(target, attr, "") or "").strip()
        if value:
            return f"{attr}:{value}"
    return ""


def _match_stream_table(item: Any, target: Any) -> bool:
    """判断一个 toml 里的 ``[[streams]]`` 表是否对应给定的配置项。"""

    def _get(key: str) -> str:
        try:
            value = item.get(key)
        except AttributeError:
            return ""
        return str(value or "").strip() if value is not None else ""

    for attr in ("stream_id", "group_id", "user_id", "name"):
        wanted = str(getattr(target, attr, "") or "").strip()
        if wanted and _get(attr) == wanted:
            return True
    return False


# ----------------------------------------------------------------------
# 自动兴趣写回
# ----------------------------------------------------------------------


def update_stream_auto_interests(
    path: Path,
    target: Any,
    interests: Sequence[str],
) -> bool:
    """把一个聊天流的自动兴趣写回 ``[[streams]].auto_interests``。

    找不到对应的 ``[[streams]]`` 表时返回 False（不做任何修改）。
    """

    cleaned = _clean_interests(interests)
    try:
        import tomlkit

        document = _load_document(path)
        streams = document.get("streams")
        if streams is None:
            return False
        matched = None
        for item in streams:
            if _match_stream_table(item, target):
                matched = item
                break
        if matched is None:
            return False
        if "auto_interests" not in matched:
            matched["auto_interests"] = tomlkit.array()
        array = matched["auto_interests"]
        # 原地清空再填充，尽量保留 tomlkit 的数组格式
        while len(array):
            array.pop()
        for term in cleaned:
            array.append(term)
        if not len(array):
            array.multiline(False)
        _save_document(path, document)
        logger.info(
            "已把自动兴趣写回配置（%s）：%s",
            stream_identity(target),
            "、".join(cleaned) or "（空）",
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("写回自动兴趣失败（%s）：%s", path, exc)
        return False


def update_persona_seed_interests(path: Path, interests: Sequence[str]) -> bool:
    """把按人设生成的初始兴趣写回 ``[interest].persona_seed_interests``。

    这是一项通用配置（所有聊天流共用），不是某个流的私有字段。
    """

    cleaned = _clean_interests(interests)
    try:
        import tomlkit

        document = _load_document(path)
        section = document.get("interest")
        if section is None:
            return False
        if "persona_seed_interests" not in section:
            section["persona_seed_interests"] = tomlkit.array()
        array = section["persona_seed_interests"]
        while len(array):
            array.pop()
        for term in cleaned:
            array.append(term)
        if not len(array):
            array.multiline(False)
        _save_document(path, document)
        logger.info("已把人设初始兴趣写回配置：%s", "、".join(cleaned) or "（空）")
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("写回人设初始兴趣失败（%s）：%s", path, exc)
        return False


def _clean_interests(values: Iterable[Any]) -> List[str]:
    """去重、去空白、保持顺序。"""

    seen = set()
    result: List[str] = []
    for value in values or []:
        term = str(value or "").strip()
        if not term or term in seen:
            continue
        seen.add(term)
        result.append(term)
    return result


# ----------------------------------------------------------------------
# 状态里记录「上次写了什么」（用于判断用户是否手改过）
# ----------------------------------------------------------------------


def should_write_back(
    current: Sequence[str],
    last_written: Optional[Sequence[str]],
) -> bool:
    """判断这次能不能自动覆盖 ``auto_interests``。

    规则：
    - 配置里现在是空的 → 可以写（用户清空即表示恢复自动维护）；
    - 现在的值正好等于插件上次写进去的值 → 可以写（还是插件自己的）；
    - 其他情况（用户手动改过）→ 不写，视为锁定。
    """

    current_list = _clean_interests(current)
    if not current_list:
        return True
    if last_written is None:
        # 没有历史记录，但配置里已有值：当作是用户手填的，别覆盖
        return False
    return current_list == _clean_interests(last_written)


def describe_write_back_state(
    current: Sequence[str],
    last_written: Optional[Sequence[str]],
) -> str:
    """给状态输出用的一句中文描述。"""

    current_list = _clean_interests(current)
    if not current_list:
        return "自动兴趣：暂无（下一轮会重新提取）"
    if should_write_back(current_list, last_written):
        return "自动兴趣：由插件自动维护"
    return "自动兴趣：已被手动修改，插件不再覆盖"
