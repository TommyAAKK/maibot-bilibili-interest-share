"""人设（persona）测试：通过宿主 config.get 读取人设、覆盖优先级、缓存与提示词注入。"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _helpers import (  # noqa: E402
    FakeConfig,
    load_plugin_module,
    load_submodule,
    make_video,
)

HOST_VALUES = {
    "bot.nickname": "小鲸鱼",
    "bot.alias_names": ["小女仆", "鲸鱼娘"],
    "personality.personality": "你是一只爱看视频的虚拟鲸鱼，喜欢和群友聊天。",
    "personality.reply_style": "说话简短活泼，偶尔带「呜哇」这样的语气词。",
    "personality.behavior_style": "看到有趣的视频会主动分享，不刷屏。",
}


class PersonaModuleTest(unittest.TestCase):
    """纯函数部分：取值规整与人设段落拼装。"""

    def setUp(self) -> None:
        load_plugin_module()
        self.persona = load_submodule("persona")

    def test_build_persona_from_values(self) -> None:
        persona = self.persona.build_persona(HOST_VALUES)
        self.assertFalse(persona.is_empty)
        self.assertEqual(persona.name, "小鲸鱼")
        self.assertEqual(persona.aliases, ["小女仆", "鲸鱼娘"])
        self.assertIn("虚拟鲸鱼", persona.personality)
        self.assertIn("呜哇", persona.reply_style)
        self.assertIn("不刷屏", persona.behavior_style)
        self.assertEqual(persona.source, "host")

    def test_empty_values_yield_empty_persona(self) -> None:
        persona = self.persona.build_persona({})
        self.assertTrue(persona.is_empty)
        self.assertEqual(persona.prompt_block(), "")

    def test_aliases_accept_comma_string(self) -> None:
        persona = self.persona.build_persona(
            {"bot.nickname": "小鲸鱼", "bot.alias_names": "小女仆，鲸鱼娘, 大肥鱼"}
        )
        self.assertEqual(persona.aliases, ["小女仆", "鲸鱼娘", "大肥鱼"])

    def test_prompt_block_contents(self) -> None:
        block = self.persona.build_persona(HOST_VALUES).prompt_block()
        self.assertIn("【你的人设】", block)
        self.assertIn("你的名字：小鲸鱼", block)
        self.assertIn("小女仆", block)
        self.assertIn("你的性格与身份：", block)
        self.assertIn("你的说话风格：", block)
        self.assertIn("你的行为准则：", block)


class PersonaProviderTest(unittest.IsolatedAsyncioTestCase):
    """通过 config.get 读取宿主配置的行为。"""

    def setUp(self) -> None:
        load_plugin_module()
        self.persona = load_submodule("persona")

    async def test_reads_host_config(self) -> None:
        fake = FakeConfig(HOST_VALUES)
        provider = self.persona.PersonaProvider(config_get=fake.get, cache_seconds=0)
        persona = await provider.refresh()
        self.assertEqual(persona.name, "小鲸鱼")
        self.assertEqual(persona.source, "host")
        self.assertIn("bot.nickname", fake.calls)
        self.assertIn("personality.reply_style", fake.calls)

    async def test_override_wins_over_host(self) -> None:
        fake = FakeConfig(HOST_VALUES)
        provider = self.persona.PersonaProvider(
            config_get=fake.get,
            override_name="小虎鲸",
            override_personality="是一只虎鲸。",
            cache_seconds=0,
        )
        persona = await provider.refresh()
        self.assertEqual(persona.name, "小虎鲸")
        self.assertEqual(persona.personality, "是一只虎鲸。")
        # 没覆盖的字段仍从宿主补
        self.assertIn("呜哇", persona.reply_style)
        self.assertEqual(persona.source, "override")

    async def test_disabled_returns_empty(self) -> None:
        fake = FakeConfig(HOST_VALUES)
        provider = self.persona.PersonaProvider(
            enabled=False, config_get=fake.get, cache_seconds=0
        )
        persona = await provider.refresh()
        self.assertTrue(persona.is_empty)
        self.assertEqual(fake.calls, [])  # 禁用时不该去读宿主
        self.assertEqual(provider.prompt_block(), "")

    async def test_no_config_getter_is_empty(self) -> None:
        provider = self.persona.PersonaProvider(config_get=None, cache_seconds=0)
        persona = await provider.refresh()
        self.assertTrue(persona.is_empty)

    async def test_getter_error_is_tolerated(self) -> None:
        async def boom(key, default=None):
            raise RuntimeError("capability down")

        provider = self.persona.PersonaProvider(config_get=boom, cache_seconds=0)
        persona = await provider.refresh()
        self.assertTrue(persona.is_empty)

    async def test_style_hint_appended(self) -> None:
        provider = self.persona.PersonaProvider(
            config_get=FakeConfig(HOST_VALUES).get,
            style_hint="分享时别太啰嗦。",
            cache_seconds=0,
        )
        await provider.refresh()
        self.assertIn("额外要求：分享时别太啰嗦。", provider.prompt_block())

    async def test_cache_avoids_second_read(self) -> None:
        fake = FakeConfig(HOST_VALUES)
        provider = self.persona.PersonaProvider(config_get=fake.get, cache_seconds=600)
        await provider.ensure()
        first = len(fake.calls)
        self.assertTrue(first)
        await provider.ensure()  # 未过期，不应再读
        self.assertEqual(len(fake.calls), first)

    async def test_invalidate_forces_reread(self) -> None:
        fake = FakeConfig(HOST_VALUES)
        provider = self.persona.PersonaProvider(config_get=fake.get, cache_seconds=600)
        await provider.ensure()
        first = len(fake.calls)
        provider.invalidate()
        await provider.ensure()
        self.assertGreater(len(fake.calls), first)

    async def test_get_before_refresh_is_empty(self) -> None:
        provider = self.persona.PersonaProvider(
            config_get=FakeConfig(HOST_VALUES).get, cache_seconds=600
        )
        self.assertTrue(provider.get().is_empty)


class JudgePromptTest(unittest.TestCase):
    """人设段落要真正进入 LLM 提示词。"""

    def setUp(self) -> None:
        load_plugin_module()
        self.persona = load_submodule("persona")
        self.llm_judge = load_submodule("llm_judge")
        self.video_module = load_submodule("bilibili")

    def test_prompt_contains_persona_and_intro_field(self) -> None:
        block = self.persona.build_persona(HOST_VALUES).prompt_block()
        prompt = self.llm_judge.build_judge_prompt(
            make_video(self.video_module),
            interests=["原神"],
            danmaku=["好看"],
            comments=["顶"],
            persona_block=block,
            intro_enabled=True,
        )
        self.assertIn("小鲸鱼", prompt)
        self.assertIn("intro", prompt)
        self.assertIn("narrative", prompt)

    def test_prompt_without_persona_has_no_intro(self) -> None:
        prompt = self.llm_judge.build_judge_prompt(
            make_video(self.video_module),
            interests=["原神"],
            danmaku=[],
            comments=[],
            intro_enabled=False,
        )
        self.assertNotIn("【你的人设】", prompt)


class JudgeIntroParseTest(unittest.IsolatedAsyncioTestCase):
    """judge() 要解析出 narrative 与 intro，并把人设段注入调用。"""

    def setUp(self) -> None:
        load_plugin_module()
        self.persona = load_submodule("persona")
        self.llm_judge = load_submodule("llm_judge")
        self.video_module = load_submodule("bilibili")

    async def test_judge_returns_intro(self) -> None:
        provider = self.persona.PersonaProvider(
            config_get=FakeConfig(HOST_VALUES).get, cache_seconds=600
        )
        await provider.refresh()
        judge = self.llm_judge.LlmJudge(None, persona_provider=provider)
        self.assertIn("小鲸鱼", judge.persona_block())

    async def test_persona_block_injected_into_call(self) -> None:
        provider = self.persona.PersonaProvider(
            config_get=FakeConfig(HOST_VALUES).get, cache_seconds=600
        )
        await provider.refresh()
        prompts = []

        class _Llm:
            async def generate(self, prompt, **kwargs):
                prompts.append(prompt)
                return {
                    "success": True,
                    "response": json.dumps(
                        {
                            "relevant": True,
                            "score": 8,
                            "category": "游戏",
                            "narrative": "介绍文案",
                            "intro": "快来康康！",
                            "reason": "贴合兴趣",
                        },
                        ensure_ascii=False,
                    ),
                }

        judge = self.llm_judge.LlmJudge(_Llm(), persona_provider=provider)
        result = await judge.judge(
            make_video(self.video_module), interests=["原神"], intro_enabled=True
        )
        self.assertTrue(prompts)
        self.assertIn("小鲸鱼", prompts[0])
        self.assertEqual(result["intro"], "快来康康！")
        self.assertEqual(result["narrative"], "介绍文案")


if __name__ == "__main__":
    unittest.main()
