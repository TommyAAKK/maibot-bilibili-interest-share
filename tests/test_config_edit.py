"""``config_edit`` 的单元测试：把 Cookie / 自动兴趣写回插件自己的 config.toml。"""

from __future__ import annotations

import tempfile
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace

from _helpers import load_submodule

config_edit = load_submodule("config_edit")


SAMPLE_CONFIG = """\
# 顶部注释要保留
[plugin]
# 启用插件
enabled = true
# 配置版本
config_version = "2.0.0"

[account]
# B 站 Cookie
cookie = ""

[[streams]]
# 备注名
name = "小群"
# 启用该会话
enabled = true
# 群号
group_id = "111"
# 手写兴趣
interests = ["原神"]
# 自动提取的兴趣
auto_interests = ["旧词"]

[[streams]]
# 备注名
name = "另一个群"
# 启用该会话
enabled = true
# 群号
group_id = "222"
# 手写兴趣
interests = []
# 自动提取的兴趣
auto_interests = []
"""


def _write(tmp: Path, text: str = SAMPLE_CONFIG) -> Path:
    path = tmp / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def _target(**kwargs) -> SimpleNamespace:
    base = {
        "name": "",
        "enabled": True,
        "stream_id": "",
        "group_id": "",
        "user_id": "",
        "interests": [],
        "auto_interests": [],
        "categories": [],
        "auto_interest": True,
        "max_share_per_round": 0,
    }
    base.update(kwargs)
    return SimpleNamespace(**base)


class FindConfigPathTests(unittest.TestCase):
    def test_explicit_directory(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            self.assertEqual(
                config_edit.find_config_path(tmp), tmp / "config.toml"
            )

    def test_default_is_plugin_directory(self):
        path = config_edit.find_config_path()
        self.assertEqual(path.name, "config.toml")
        self.assertTrue((path.parent / "plugin.py").exists())


class ReadCookieTests(unittest.TestCase):
    def test_missing_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as raw:
            self.assertEqual(config_edit.read_cookie(Path(raw) / "config.toml"), "")

    def test_reads_account_cookie(self):
        with tempfile.TemporaryDirectory() as raw:
            path = _write(Path(raw))
            config_edit.update_cookie(path, "SESSDATA=abc")
            self.assertEqual(config_edit.read_cookie(path), "SESSDATA=abc")

    def test_missing_account_section_returns_empty(self):
        with tempfile.TemporaryDirectory() as raw:
            path = _write(Path(raw), "[plugin]\nenabled = true\n")
            self.assertEqual(config_edit.read_cookie(path), "")


class UpdateCookieTests(unittest.TestCase):
    def test_writes_into_existing_account_section(self):
        with tempfile.TemporaryDirectory() as raw:
            path = _write(Path(raw))
            self.assertTrue(config_edit.update_cookie(path, "SESSDATA=xyz"))
            data = tomllib.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["account"]["cookie"], "SESSDATA=xyz")

    def test_creates_account_section_when_absent(self):
        with tempfile.TemporaryDirectory() as raw:
            path = _write(Path(raw), "[plugin]\nenabled = true\n")
            self.assertTrue(config_edit.update_cookie(path, "a=1"))
            data = tomllib.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["account"]["cookie"], "a=1")

    def test_keeps_comments_and_other_sections(self):
        with tempfile.TemporaryDirectory() as raw:
            path = _write(Path(raw))
            config_edit.update_cookie(path, "SESSDATA=xyz")
            text = path.read_text(encoding="utf-8")
            self.assertIn("# 顶部注释要保留", text)
            self.assertIn("# B 站 Cookie", text)
            data = tomllib.loads(text)
            self.assertEqual(data["plugin"]["config_version"], "2.0.0")
            self.assertEqual(len(data["streams"]), 2)

    def test_no_bom_after_write(self):
        with tempfile.TemporaryDirectory() as raw:
            path = _write(Path(raw))
            config_edit.update_cookie(path, "a=1")
            self.assertFalse(path.read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_creates_missing_file(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "config.toml"
            self.assertTrue(config_edit.update_cookie(path, "a=1"))
            self.assertEqual(config_edit.read_cookie(path), "a=1")

    def test_failure_returns_false(self):
        with tempfile.TemporaryDirectory() as raw:
            # 传一个目录当文件，写入必然失败
            path = Path(raw) / "config.toml"
            path.mkdir()
            self.assertFalse(config_edit.update_cookie(path, "a=1"))


class StreamIdentityTests(unittest.TestCase):
    def test_priority_order(self):
        self.assertEqual(
            config_edit.stream_identity(
                _target(stream_id="s1", group_id="111", user_id="9", name="n")
            ),
            "stream_id:s1",
        )
        self.assertEqual(
            config_edit.stream_identity(_target(group_id="111", user_id="9", name="n")),
            "group_id:111",
        )
        self.assertEqual(
            config_edit.stream_identity(_target(user_id="9", name="n")), "user_id:9"
        )
        self.assertEqual(config_edit.stream_identity(_target(name="n")), "name:n")

    def test_empty_target(self):
        self.assertEqual(config_edit.stream_identity(_target()), "")


class UpdateStreamAutoInterestsTests(unittest.TestCase):
    def test_writes_by_group_id(self):
        with tempfile.TemporaryDirectory() as raw:
            path = _write(Path(raw))
            self.assertTrue(
                config_edit.update_stream_auto_interests(
                    path, _target(group_id="222"), ["原神", "编程"]
                )
            )
            data = tomllib.loads(path.read_text(encoding="utf-8"))
            by_group = {item["group_id"]: item for item in data["streams"]}
            self.assertEqual(by_group["222"]["auto_interests"], ["原神", "编程"])
            # 另一个流不受影响
            self.assertEqual(by_group["111"]["auto_interests"], ["旧词"])

    def test_writes_by_stream_id(self):
        with tempfile.TemporaryDirectory() as raw:
            path = _write(
                Path(raw),
                '[[streams]]\nstream_id = "abc"\nauto_interests = []\n',
            )
            self.assertTrue(
                config_edit.update_stream_auto_interests(
                    path, _target(stream_id="abc"), ["猫"]
                )
            )
            data = tomllib.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["streams"][0]["auto_interests"], ["猫"])

    def test_cleans_duplicates_and_blanks(self):
        with tempfile.TemporaryDirectory() as raw:
            path = _write(Path(raw))
            config_edit.update_stream_auto_interests(
                path, _target(group_id="111"), ["原神", "原神", " ", "编程"]
            )
            data = tomllib.loads(path.read_text(encoding="utf-8"))
            by_group = {item["group_id"]: item for item in data["streams"]}
            self.assertEqual(by_group["111"]["auto_interests"], ["原神", "编程"])

    def test_can_clear(self):
        with tempfile.TemporaryDirectory() as raw:
            path = _write(Path(raw))
            config_edit.update_stream_auto_interests(path, _target(group_id="111"), [])
            data = tomllib.loads(path.read_text(encoding="utf-8"))
            by_group = {item["group_id"]: item for item in data["streams"]}
            self.assertEqual(by_group["111"]["auto_interests"], [])

    def test_unmatched_stream_returns_false(self):
        with tempfile.TemporaryDirectory() as raw:
            path = _write(Path(raw))
            before = path.read_text(encoding="utf-8")
            self.assertFalse(
                config_edit.update_stream_auto_interests(
                    path, _target(group_id="999"), ["猫"]
                )
            )
            self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_no_streams_section_returns_false(self):
        with tempfile.TemporaryDirectory() as raw:
            path = _write(Path(raw), "[plugin]\nenabled = true\n")
            self.assertFalse(
                config_edit.update_stream_auto_interests(
                    path, _target(group_id="111"), ["猫"]
                )
            )

    def test_preserves_comments(self):
        with tempfile.TemporaryDirectory() as raw:
            path = _write(Path(raw))
            config_edit.update_stream_auto_interests(
                path, _target(group_id="111"), ["新词"]
            )
            text = path.read_text(encoding="utf-8")
            self.assertIn("# 自动提取的兴趣", text)
            self.assertIn("# 顶部注释要保留", text)


class ShouldWriteBackTests(unittest.TestCase):
    def test_empty_current_allows_write(self):
        self.assertTrue(config_edit.should_write_back([], ["旧的"]))
        self.assertTrue(config_edit.should_write_back([], None))

    def test_equal_to_last_written_allows_write(self):
        self.assertTrue(config_edit.should_write_back(["原神", "编程"], ["原神", "编程"]))

    def test_user_modified_blocks_write(self):
        self.assertFalse(config_edit.should_write_back(["手改"], ["旧的"]))

    def test_no_history_with_values_blocks_write(self):
        self.assertFalse(config_edit.should_write_back(["手填"], None))

    def test_whitespace_is_normalised(self):
        self.assertTrue(config_edit.should_write_back([" 原神 "], ["原神"]))


class DescribeWriteBackStateTests(unittest.TestCase):
    def test_empty(self):
        self.assertIn("暂无", config_edit.describe_write_back_state([], None))

    def test_auto(self):
        self.assertIn("自动维护", config_edit.describe_write_back_state(["猫"], ["猫"]))

    def test_locked(self):
        self.assertIn(
            "手动修改", config_edit.describe_write_back_state(["手改"], ["旧的"])
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
