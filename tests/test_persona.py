"""人设（persona）测试：读取宿主 bot_config.toml、覆盖优先级、缓存与提示词注入。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _helpers import load_plugin_module, load_submodule, make_video  # noqa: E402

BOT_CONFIG = """
[bot]
nickname = "小鲸鱼"
alias_names = ["小女仆", "鲸鱼娘"]

[personality]
personality = "你是一只爱看视频的虚拟鲸鱼，喜欢和群友聊天。"
reply_style = "说话简短活泼，偶尔带「呜哇」这样的语气词。"
behavior_style = "看到有趣的视频会主动分享，不刷屏。"
"""


class PersonaModuleTest(unittest.TestCase):
    def setUp(self) -> None:
        load_plugin_module()
        self.persona = load_submodule("persona")
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def _write_bot_config(self, text: str = BOT_CONFIG) -> Path:
        config_dir = self.tmp / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        path = config_dir / "bot_config.toml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_load_from_host_toml(self) -> None:
        path = self._write_bot_config()
        persona = self.persona.load_persona_from_toml(path)
        self.assertFalse(persona.is_empty)
        self.assertEqual(persona.name, "小鲸鱼")
        self.assertEqual(persona.aliases, ["小女仆", "鲸鱼娘"])
        self.assertIn("虚拟鲸鱼", persona.personality)
        self.assertIn("呜哇", persona.reply_style)
        self.assertIn("不刷屏", persona.behavior_style)
        self.assertEqual(persona.source, "host")

    def test_missing_file_yields_empty_persona(self) -> None:
        persona = self.persona.load_persona_from_toml(self.tmp / "nope.toml")
        self.assertTrue(persona.is_empty)
        self.assertEqual(persona.prompt_block(), "")

    def test_malformed_toml_is_tolerated(self) -> None:
        path = self._write_bot_config("this is not = valid [toml")
        persona = self.persona.load_persona_from_toml(path)
        self.assertTrue(persona.is_empty)

    def test_find_instance_root(self) -> None:
        root = self.tmp / "MaiBot"
        data_dir = root / "data" / "plugins" / "github.TommyAAKK.bilibili_interest_share"
        data_dir.mkdir(parents=True)
        found = self.persona.find_instance_root(data_dir)
        self.assertEqual(found, root)

    def test_find_instance_root_rejects_unrelated_dir(self) -> None:
        other = self.tmp / "somewhere" / "else"
        other.mkdir(parents=True)
        self.assertIsNone(self.persona.find_instance_root(other))

    def test_prompt_block_contents(self) -> None:
        path = self._write_bot_config()
        persona = self.persona.load_persona_from_toml(path)
        block = persona.prompt_block()
        self.assertIn("小鲸鱼", block)
        self.assertIn("小女仆", block)
        self.assertIn("虚拟鲸鱼", block)
        self.assertIn("呜哇", block)
        self.assertIn("不刷屏", block)


class PersonaProviderTest(unittest.TestCase):
    def setUp(self) -> None:
        load_plugin_module()
        self.persona = load_submodule("persona")
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.root = self.tmp / "MaiBot"
        self.data_dir = self.root / "data" / "plugins" / "github.TommyAAKK.bilibili_interest_share"
        self.data_dir.mkdir(parents=True)
        self.config_path = self.root / "config" / "bot_config.toml"
        self.config_path.parent.mkdir(parents=True)
        self.config_path.write_text(BOT_CONFIG, encoding="utf-8")

    def _provider(self, **kwargs):
        params = {
            "enabled": True,
            "data_dir": self.data_dir,
            "cache_seconds": 0,
        }
        params.update(kwargs)
        return self.persona.PersonaProvider(**params)

    def test_reads_host_config(self) -> None:
        provider = self._provider()
        persona = provider.get()
        self.assertEqual(persona.name, "小鲸鱼")
        self.assertEqual(persona.source, "host")

    def test_override_wins_over_host(self) -> None:
        provider = self._provider(
            override_name="小海豹",
            override_personality="你是一只严肃的海豹。",
        )
        persona = provider.get()
        self.assertEqual(persona.name, "小海豹")
        self.assertEqual(persona.personality, "你是一只严肃的海豹。")
        # 未覆盖的字段仍从宿主补
        self.assertIn("呜哇", persona.reply_style)
        self.assertEqual(persona.source, "override")

    def test_explicit_bot_config_path(self) -> None:
        provider = self._provider(config_path=str(self.config_path))
        self.assertEqual(provider.resolve_config_path(), self.config_path)
        self.assertEqual(provider.get().name, "小鲸鱼")

    def test_disabled_returns_empty(self) -> None:
        provider = self._provider(enabled=False)
        self.assertTrue(provider.get().is_empty)

    def test_missing_host_config_is_empty(self) -> None:
        self.config_path.unlink()
        provider = self._provider()
        persona = provider.get()
        self.assertTrue(persona.is_empty)
        self.assertEqual(persona.source, "none")

    def test_cache_invalidated_by_mtime(self) -> None:
        provider = self._provider()
        self.assertEqual(provider.get().name, "小鲸鱼")
        updated = BOT_CONFIG.replace('nickname = "小鲸鱼"', 'nickname = "小虎鲸"')
        self.config_path.write_text(updated, encoding="utf-8")
        # 保证 mtime 变化（部分文件系统精度较粗）
        stat = self.config_path.stat()
        os.utime(self.config_path, (stat.st_atime + 5, stat.st_mtime + 5))
        self.assertEqual(provider.get().name, "小虎鲸")

    def test_style_hint_appended(self) -> None:
        provider = self._provider(style_hint="多提一句视频看点")
        block = provider.prompt_block()
        self.assertIn("多提一句视频看点", block)


class JudgePromptTest(unittest.TestCase):
    def setUp(self) -> None:
        load_plugin_module()
        self.video_module = load_submodule("bilibili")
        self.persona = load_submodule("persona")
        self.judge = load_submodule("llm_judge")
        self.video = make_video(self.video_module)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.block = self.persona.Persona(
            name="小鲸鱼",
            personality="你是一只爱看视频的虚拟鲸鱼。",
            reply_style="说话活泼。",
            behavior_style="",
            aliases=[],
            source="host",
        ).prompt_block()

    def test_prompt_contains_persona_and_intro_field(self) -> None:
        prompt = self.judge.build_judge_prompt(
            self.video,
            interests=["原神"],
            danmaku=[],
            comments=[],
            subtitle="",
            frames_desc="",
            persona_block=self.block,
            intro_enabled=True,
            intro_max_chars=30,
        )
        self.assertIn("小鲸鱼", prompt)
        self.assertIn("虚拟鲸鱼", prompt)
        self.assertIn('"intro"', prompt)
        self.assertIn("30", prompt)
        self.assertIn("人设的口吻", prompt)

    def test_prompt_without_persona_has_no_intro(self) -> None:
        prompt = self.judge.build_judge_prompt(
            self.video,
            interests=["原神"],
            danmaku=[],
            comments=[],
            subtitle="",
            frames_desc="",
            persona_block="",
            intro_enabled=False,
        )
        self.assertNotIn('"intro"', prompt)


class JudgeIntroParseTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        load_plugin_module()
        self.video_module = load_submodule("bilibili")
        self.judge_mod = load_submodule("llm_judge")
        self.persona_mod = load_submodule("persona")
        self.video = make_video(self.video_module)

    class _Llm:
        def __init__(self, response: str) -> None:
            self.response = response
            self.calls = []

        async def generate(self, prompt="", **kwargs):
            self.calls.append({"prompt": prompt, **kwargs})
            return {"success": True, "response": self.response, "model": "mock"}

    async def test_judge_returns_intro(self) -> None:
        payload = json.dumps(
            {
                "relevant": True,
                "score": 8,
                "category": "社区创作",
                "narrative": "介绍文案",
                "intro": "呜哇，这个好好看！",
                "reason": "贴合兴趣",
            },
            ensure_ascii=False,
        )
        llm = self._Llm(payload)
        judge = self.judge_mod.LlmJudge(llm, task_name="planner")
        result = await judge.judge(
            self.video,
            interests=["原神"],
            intro_enabled=True,
            intro_max_chars=30,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["intro"], "呜哇，这个好好看！")

    async def test_judge_intro_empty_when_disabled(self) -> None:
        payload = json.dumps(
            {"relevant": True, "score": 8, "narrative": "介绍文案", "intro": "不该出现"},
            ensure_ascii=False,
        )
        llm = self._Llm(payload)
        judge = self.judge_mod.LlmJudge(llm, task_name="planner")
        result = await judge.judge(
            self.video, interests=["原神"], intro_enabled=False
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.get("intro"), "")

    async def test_persona_block_injected_into_call(self) -> None:
        payload = json.dumps(
            {"relevant": True, "score": 8, "narrative": "介绍文案", "intro": "嗨"},
            ensure_ascii=False,
        )
        llm = self._Llm(payload)
        persona = self.persona_mod.Persona(
            name="小鲸鱼",
            personality="虚拟鲸鱼",
            reply_style="活泼",
            behavior_style="",
            aliases=[],
            source="host",
        )
        provider = self.persona_mod.PersonaProvider(
            enabled=True, data_dir=None, cache_seconds=0
        )
        provider._cached = persona
        provider._cached_at = 10 ** 12
        judge = self.judge_mod.LlmJudge(
            llm, task_name="planner", persona_provider=provider
        )
        await judge.judge(
            self.video, interests=["原神"], intro_enabled=True
        )
        self.assertTrue(llm.calls)
        self.assertIn("小鲸鱼", llm.calls[0]["prompt"])


if __name__ == "__main__":
    unittest.main()
