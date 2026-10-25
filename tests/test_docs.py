"""文档与配置注释一致性测试。

确保「每个配置项都有中文备注」这件事不会随时间退化：

1. ``config.example.toml``（随仓库发布的参考模板）里每个键上方都有一行紧邻的中文注释；
2. 模板里的每个键都在 ``README.md`` 的配置说明里有说明；
3. ``config.py`` 里每个配置字段都有中文 ``description``；
4. 模板里的值与 ``config.py`` 的默认值完全一致（发布副本必须还原初始设定）。

运行时真正生效的 ``config.toml`` 由 MaiBot 按插件 schema 生成，不进仓库。
"""

from __future__ import annotations

import re
import sys
import tomllib
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

import config as config_module  # noqa: E402

CONFIG_PATH = PLUGIN_ROOT / "config.example.toml"
README_PATH = PLUGIN_ROOT / "README.md"

# [[streams]] 在模板里是注释掉的示例（默认没有白名单会话），
# 所以单独校验它的字段有没有在 README 里说明。
STREAM_KEYS = [
    "name",
    "enabled",
    "stream_id",
    "group_id",
    "user_id",
    "interests",
    "categories",
    "auto_interest",
    "max_share_per_round",
]

_CN_RE = re.compile(r"[\u4e00-\u9fff]")
_KEY_RE = re.compile(r"^\s*([A-Za-z0-9_\-]+)\s*=")
_SECTION_RE = re.compile(r"^\s*\[\[?([^\]]+)\]\]?\s*$")


def _read_config_lines() -> list[str]:
    return CONFIG_PATH.read_text(encoding="utf-8").splitlines()


def _iter_config_keys(lines: list[str]):
    """按出现顺序产出 (section, key, 行号, 上一行)。"""
    section = ""
    for index, line in enumerate(lines):
        match = _SECTION_RE.match(line)
        if match:
            section = match.group(1).strip()
            continue
        key_match = _KEY_RE.match(line)
        if not key_match:
            continue
        previous = lines[index - 1].strip() if index > 0 else ""
        yield section, key_match.group(1), index + 1, previous


def _flatten_keys(data, prefix: str = "") -> list[str]:
    """把解析后的配置展开成 a.b.c 形式的键列表。"""
    keys: list[str] = []
    if isinstance(data, dict):
        for key, value in data.items():
            keys.extend(_flatten_keys(value, f"{prefix}.{key}" if prefix else key))
    elif isinstance(data, list) and data and isinstance(data[0], dict):
        for key, value in data[0].items():
            keys.extend(_flatten_keys(value, f"{prefix}.{key}" if prefix else key))
    else:
        keys.append(prefix)
    return keys


class ConfigTemplateCommentTests(unittest.TestCase):
    """config.example.toml 的每个键都必须有一行中文注释。"""

    def test_every_key_has_adjacent_chinese_comment(self):
        lines = _read_config_lines()
        missing = []
        for section, key, line_no, previous in _iter_config_keys(lines):
            if not previous.startswith("#") or previous == "#":
                missing.append(f"[{section}] {key} (line {line_no}) 上方缺少注释")
            elif not _CN_RE.search(previous):
                missing.append(f"[{section}] {key} (line {line_no}) 注释不是中文：{previous}")
        self.assertEqual(missing, [], "\n".join(missing))

    def test_template_has_no_bom(self):
        raw = CONFIG_PATH.read_bytes()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), "模板文件不能带 BOM")
        self.assertEqual(raw[0], ord("#"), "模板文件应以注释开头")

    def test_template_parses(self):
        data = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        self.assertIn("schedule", data)
        self.assertNotIn("streams", data)  # 模板里 [[streams]] 是注释掉的示例


class ConfigTemplateMatchesDefaultsTests(unittest.TestCase):
    """模板里的值必须与 config.py 的默认值完全一致。"""

    def test_values_equal_model_defaults(self):
        data = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        template = config_module.BilibiliInterestShareConfig(**data).model_dump(
            mode="json"
        )
        defaults = config_module.BilibiliInterestShareConfig().model_dump(mode="json")
        self.assertEqual(template, defaults)


class ReadmeCoverageTests(unittest.TestCase):
    """配置项都要在 README 中有说明。"""

    def test_every_config_key_is_documented_in_readme(self):
        data = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        readme = README_PATH.read_text(encoding="utf-8")
        missing = [
            key for key in _flatten_keys(data) if key.split(".")[-1] not in readme
        ]
        self.assertEqual(sorted(set(missing)), [], f"README 缺少这些配置项的说明：{missing}")

    def test_readme_documents_all_sections(self):
        data = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        readme = README_PATH.read_text(encoding="utf-8")
        missing = [
            name
            for name in data
            if f"[{name}]" not in readme and f"[[{name}]]" not in readme
        ]
        self.assertEqual(missing, [], f"README 缺少这些 section：{missing}")

    def test_readme_documents_stream_keys(self):
        readme = README_PATH.read_text(encoding="utf-8")
        missing = [key for key in STREAM_KEYS if key not in readme]
        self.assertEqual(missing, [], f"README 缺少 [[streams]] 这些字段的说明：{missing}")


class ConfigFieldDescriptionTests(unittest.TestCase):
    """config.py 的每个字段都必须有中文 description。"""

    def _iter_models(self):
        from pydantic import BaseModel

        seen: set[type] = set()
        pending = [config_module.BilibiliInterestShareConfig]
        while pending:
            model = pending.pop()
            if model in seen:
                continue
            seen.add(model)
            yield model
            for field in model.model_fields.values():
                annotation = field.annotation
                for candidate in getattr(annotation, "__args__", (annotation,)):
                    if isinstance(candidate, type) and issubclass(candidate, BaseModel):
                        pending.append(candidate)

    def test_every_field_has_chinese_description(self):
        missing = []
        for model in self._iter_models():
            for name, field in model.model_fields.items():
                description = field.description or ""
                if not description:
                    missing.append(f"{model.__name__}.{name} 缺少 description")
                elif not _CN_RE.search(description):
                    missing.append(
                        f"{model.__name__}.{name} description 不是中文：{description}"
                    )
        self.assertEqual(missing, [], "\n".join(missing))


class CommandDocTests(unittest.TestCase):
    """README 指令表里写的指令和别名，代码里必须真的有。"""

    ROW_RE = re.compile(
        r"^\|\s*`(?P<primary>/[^\s`]+)[^`]*`\s*\|\s*(?P<alias>`/[^`]+`|—)\s*\|"
    )

    @classmethod
    def setUpClass(cls) -> None:
        sys.path.insert(0, str(PLUGIN_ROOT / "tests"))
        from _helpers import load_plugin_module  # noqa: PLC0415

        cls.module = load_plugin_module()
        cls.commands = []
        for attr in dir(cls.module.BilibiliInterestSharePlugin):
            func = getattr(cls.module.BilibiliInterestSharePlugin, attr, None)
            info = getattr(func, "__maibot_component_info__", None)
            pattern = getattr(info, "command_pattern", "") or ""
            if pattern:
                cls.commands.append(
                    {
                        "pattern": pattern,
                        "aliases": list(getattr(info, "aliases", []) or []),
                    }
                )
        cls.rows = []
        for line in README_PATH.read_text(encoding="utf-8").splitlines():
            match = cls.ROW_RE.match(line.strip())
            if match:
                alias = match.group("alias")
                cls.rows.append(
                    (
                        match.group("primary"),
                        None if alias == "—" else alias.strip("`"),
                    )
                )

    def test_readme_rows_exist(self) -> None:
        self.assertTrue(self.rows, "README 里没解析到指令表")

    def test_every_readme_command_is_declared(self) -> None:
        patterns = [item["pattern"] for item in self.commands]
        for primary, _alias in self.rows:
            self.assertTrue(
                any(pattern.startswith("^" + primary) for pattern in patterns),
                f"README 写了 {primary}，但代码里没有对应指令",
            )

    def test_every_readme_alias_is_declared(self) -> None:
        declared = {alias for item in self.commands for alias in item["aliases"]}
        for primary, alias in self.rows:
            if alias is None:
                continue
            self.assertIn(
                alias,
                declared,
                f"README 说 {primary} 有别名 {alias}，但代码里没声明",
            )


if __name__ == "__main__":
    unittest.main()
