"""插件主流程测试：定时/手动轮次、白名单解析、去重、LLM 判断、抽帧、重发与指令。"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _helpers import (  # noqa: E402
    MAIBOT_ROOT,
    PLUGIN_ROOT,
    FakeClient,
    FakeCtx,
    FakeLlm,
    deep_merge,
    load_plugin_module,
    load_submodule,
    make_video,
)

PLUGIN_ID = "github.TommyAAKK.bilibili_interest_share"

JUDGE_OK = json.dumps(
    {
        "relevant": True,
        "score": 8,
        "category": "社区创作",
        "narrative": "这期把新版本的内容聊得挺透，看着还挺有意思的。",
        "reason": "贴合兴趣",
    },
    ensure_ascii=False,
)

JUDGE_WITH_INTRO = json.dumps(
    {
        "relevant": True,
        "score": 8,
        "category": "社区创作",
        "narrative": "这期把新版本的内容聊得挺透，看着还挺有意思的。",
        "intro": "呜哇，这个好好看！",
        "reason": "贴合兴趣",
    },
    ensure_ascii=False,
)

JUDGE_IRRELEVANT = json.dumps(    {
        "relevant": False,
        "score": 2,
        "category": "其他",
        "narrative": "",
        "reason": "与兴趣无关",
    },
    ensure_ascii=False,
)


def base_config(**overrides):
    """返回一份“一定会分享”的测试配置（默认关闭 LLM/抽帧/弹幕，保证确定性）。"""

    config = {
        "plugin": {"enabled": True, "config_version": "1.1.0"},
        "schedule": {
            "interval_minutes": 30,
            "jitter_seconds": 0,
            "run_on_start": False,
            "active_hours": "",
            "max_share_per_round": 1,
            "search_cooldown_seconds": 300,
        },
        "sources": {
            "popular_enabled": True,
            "popular_page_size": 20,
            "popular_pages": 1,
            "search_enabled": False,
            "search_pages": 1,
            "search_keywords_per_round": 3,
            "search_order": "",
            "search_delay_seconds": 0.0,
            "ranking_enabled": False,
            "ranking_rids": [0],
        },
        "categories": [],
        "interest": {
            "use_recent_chat": False,
            "lookback_hours": 48.0,
            "max_messages_per_stream": 200,
            "top_keywords": 10,
            "keyword_min_count": 2,
            "explicit_weight": 3.0,
            "auto_weight": 1.0,
            "use_tags": True,
            "tag_weight": 0.7,
            "use_subtitles": False,
            "use_llm_judge": False,
            "llm_task_name": "planner",
            "llm_max_candidates": 5,
            "llm_temperature": 0.3,
            "extra_stopwords": [],
        },
        "vision": {"enabled": False, "task_name": "vlm"},
        "danmaku": {
            "danmaku_enabled": False,
            "danmaku_fetch_limit": 400,
            "danmaku_limit": 40,
            "comments_enabled": False,
            "comments_limit": 10,
            "filter_ai_spam": True,
        },
        "filter": {
            "min_duration_seconds": 0,
            "max_duration_seconds": 7200,
            "min_play": 0,
            "min_like": 0,
            "max_age_days": 0.0,
            "block_keywords": [],
            "block_up_names": [],
            "exclude_partitions": [],
            "exclude_official": False,
        },
        "scoring": {
            "interest_weight": 1.0,
            "title_match_boost": 2.0,
            "hot_weight": 1.0,
            "freshness_weight": 0.6,
            "freshness_days": 7.0,
            "min_score": 0.5,
            "require_interest_match": True,
        },
        "share": {
            "casual_mode": False,
            "casual_max_chars": 60,
            "send_as_parts": False,
            "part_delay_seconds": 0.0,
            "narrative_enabled": True,
            "narrative_max_chars": 160,
            "include_stats_line": False,
            "intro_enabled": False,
            "intro_max_chars": 40,
        },
        "persona": {
            "enabled": True,
            "name": "",
            "personality": "",
            "reply_style": "",
            "style_hint": "",
            "cache_seconds": 0,
        },
        "reshare": {"enabled": True},
        "shiwei": {
            "enabled": True,
            "cooldown_seconds": 600,
            "max_share": 1,
            "search_keywords_per_round": 2,
            "min_play": 0,
            "min_duration_seconds": 0,
            "max_duration_seconds": 7200,
        },
        "advanced": {
            "data_dir": "",
            "request_timeout_seconds": 12.0,
            "cookie": "",
            "ffmpeg_path": "",
            "dedupe_days": 30,
            "max_seen_records": 1000,
            "debug": False,
        },
        "streams": [
            {
                "name": "测试群",
                "enabled": True,
                "stream_id": "stream-1",
                "group_id": "",
                "user_id": "",
                "interests": ["原神"],
                "categories": [],
                "auto_interest": False,
                "max_share_per_round": 0,
            }
        ],
    }
    return deep_merge(config, overrides)


class FakeFrames:
    """假的抽帧器。"""

    def __init__(self, frames=None) -> None:
        self.frames = frames if frames is not None else ["data:image/jpeg;base64,AAAA"]
        self.calls = []

    @property
    def available(self) -> bool:
        return True

    async def capture(self, client, video, work_dir):
        self.calls.append({"bvid": getattr(video, "bvid", ""), "work_dir": str(work_dir)})
        return list(self.frames)


class FakeDanmaku:
    def __init__(self, text: str, progress_ms: int = 0) -> None:
        self.text = text
        self.progress_ms = progress_ms


class FakeComment:
    def __init__(self, message: str, like: int = 10) -> None:
        self.message = message
        self.like = like


class PluginTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_dir = Path(self._tmp.name)
        self.module = load_plugin_module()
        self.video_module = load_submodule("bilibili")
        self.ctx = FakeCtx(self.tmp_dir)

    def make_plugin(self, config=None, *, videos=None, ctx=None, client=None):
        ctx = ctx or self.ctx
        plugin = self.module.BilibiliInterestSharePlugin()
        plugin._set_context(ctx)
        plugin.set_plugin_config(config or base_config())
        if client is not None:
            plugin._client = client
        else:
            plugin._client = FakeClient(
                videos
                if videos is not None
                else [
                    make_video(self.video_module, bvid="BV1", title="原神新版本测评"),
                    make_video(
                        self.video_module,
                        bvid="BV2",
                        title="猫咪日常vlog",
                        description="猫咪",
                        owner="喵星人",
                        partition="生活",
                        tags="",
                    ),
                ]
            )
        plugin._store = self.module.SeenStore(
            self.tmp_dir / "state.json", dedupe_days=30, max_records=1000
        )
        plugin._store.load()
        plugin._data_dir = self.tmp_dir
        config = plugin.config
        persona_module = load_submodule("persona")
        plugin._persona = persona_module.PersonaProvider(
            enabled=config.persona.enabled,
            config_get=ctx.config.get,
            override_name=config.persona.name,
            override_personality=config.persona.personality,
            override_reply_style=config.persona.reply_style,
            style_hint=config.persona.style_hint,
            cache_seconds=config.persona.cache_seconds,
        )
        plugin._judge = self.module.LlmJudge(
            ctx.llm,
            task_name=config.interest.llm_task_name,
            vision_task_name=config.vision.task_name,
            temperature=config.interest.llm_temperature,
            persona_provider=plugin._persona,
        )
        plugin._frames = None
        return plugin

    # -- 基础轮次 ------------------------------------------------------

    async def test_round_shares_matching_video(self) -> None:
        plugin = self.make_plugin()
        summary = await plugin.run_round(reason="test")
        self.assertTrue(summary["ok"], summary)
        self.assertEqual(summary["shared"], 1)
        self.assertEqual(len(self.ctx.send.messages), 1)
        message = self.ctx.send.messages[0]
        self.assertEqual(message["stream_id"], "stream-1")
        self.assertIn("【原神新版本测评】", message["text"])
        self.assertIn("https://www.bilibili.com/video/BV1", message["text"])
        # 没有 LLM 时用简介兜底成一段自然叙述，不再有「内容：」「看点：」前缀
        self.assertIn("本期聊聊原神新版本", message["text"])
        self.assertNotIn("内容：", message["text"])
        self.assertNotIn("看点：", message["text"])
        self.assertTrue(plugin._store.is_seen("BV1"))
        self.assertFalse(plugin._store.is_seen("BV2"))

    async def test_round_dedupes_across_runs(self) -> None:
        plugin = self.make_plugin()
        await plugin.run_round(reason="test")
        second = await plugin.run_round(reason="test")
        self.assertEqual(second["shared"], 0)
        self.assertEqual(len(self.ctx.send.messages), 1)

    async def test_seen_video_is_not_auto_shared(self) -> None:
        plugin = self.make_plugin()
        plugin._store.mark_seen("BV1")
        summary = await plugin.run_round(reason="test")
        self.assertEqual(summary["shared"], 0)
        self.assertEqual(self.ctx.send.messages, [])

    async def test_round_without_targets_is_skipped(self) -> None:
        plugin = self.make_plugin(base_config(streams=[]))
        summary = await plugin.run_round(reason="test")
        self.assertEqual(summary["skipped"], "no_targets")
        self.assertEqual(summary["shared"], 0)
        self.assertEqual(self.ctx.send.messages, [])

    async def test_round_disabled_is_skipped(self) -> None:
        plugin = self.make_plugin(base_config(plugin={"enabled": False}))
        summary = await plugin.run_round(reason="test")
        self.assertFalse(summary["ok"])
        self.assertEqual(summary["skipped"], "disabled")
        self.assertEqual(self.ctx.send.messages, [])

    async def test_round_outside_active_hours_is_skipped(self) -> None:
        now = datetime.now()
        start = (now + timedelta(hours=2)).strftime("%H:%M")
        end = (now + timedelta(hours=3)).strftime("%H:%M")
        plugin = self.make_plugin(base_config(schedule={"active_hours": f"{start}-{end}"}))
        summary = await plugin.run_round(reason="test")
        self.assertTrue(summary["ok"], summary)
        self.assertEqual(summary["skipped"], "outside_active_hours")
        self.assertEqual(self.ctx.send.messages, [])

    async def test_group_id_lookup(self) -> None:
        self.ctx = FakeCtx(
            self.tmp_dir,
            group_streams={"12345": {"stream_id": "grp-stream", "session_id": "grp-stream"}},
        )
        plugin = self.make_plugin(
            base_config(
                streams=[
                    {
                        "name": "群号白名单",
                        "enabled": True,
                        "group_id": "12345",
                        "interests": ["原神"],
                        "auto_interest": False,
                    }
                ]
            )
        )
        summary = await plugin.run_round(reason="test")
        self.assertTrue(summary["ok"], summary)
        self.assertEqual(self.ctx.send.messages[0]["stream_id"], "grp-stream")

    async def test_unresolvable_target_is_skipped(self) -> None:
        plugin = self.make_plugin(
            base_config(
                streams=[
                    {
                        "name": "找不到的群",
                        "enabled": True,
                        "group_id": "999",
                        "interests": ["原神"],
                    }
                ]
            )
        )
        summary = await plugin.run_round(reason="test")
        self.assertEqual(summary["shared"], 0)
        self.assertEqual(self.ctx.send.messages, [])

    async def test_auto_interest_from_recent_messages(self) -> None:
        self.ctx = FakeCtx(
            self.tmp_dir,
            messages=[{"processed_plain_text": "机械键盘手感真好"} for _ in range(5)]
            + [{"processed_plain_text": "这个就是那个，没什么"}],
        )
        plugin = self.make_plugin(
            base_config(
                interest={"use_recent_chat": True},
                streams=[
                    {
                        "name": "自动兴趣群",
                        "enabled": True,
                        "stream_id": "stream-auto",
                        "interests": [],
                        "auto_interest": True,
                    }
                ],
            ),
            videos=[
                make_video(
                    self.video_module,
                    bvid="BVK",
                    title="机械键盘推荐：三款平价键盘横评",
                    description="机械键盘入门",
                    partition="数码",
                )
            ],
        )
        summary = await plugin.run_round(reason="test")
        self.assertTrue(summary["ok"], summary)
        self.assertEqual(summary["shared"], 1)
        self.assertTrue(self.ctx.message.calls)
        self.assertEqual(self.ctx.message.calls[0]["chat_id"], "stream-auto")

    async def test_search_uses_interest_keywords(self) -> None:
        plugin = self.make_plugin(
            base_config(sources={"search_enabled": True, "search_keywords_per_round": 2})
        )
        summary = await plugin.run_round(reason="test")
        self.assertTrue(summary["ok"], summary)
        self.assertEqual(summary["keywords"], ["原神"])
        self.assertTrue(plugin._client.search_calls)
        self.assertEqual(plugin._client.search_calls[0]["keyword"], "原神")
        self.assertEqual(summary["shared"], 1)

    def test_collect_search_keywords_merges_profiles(self) -> None:
        plugin = self.make_plugin(
            base_config(sources={"search_enabled": True, "search_keywords_per_round": 2})
        )
        module = self.module
        first = module.InterestProfile()
        first.add("原神", 3.0)
        first.add("编程", 1.0)
        second = module.InterestProfile()
        second.add("编程", 2.0)
        second.add("猫咪", 0.5)
        keywords = plugin._collect_search_keywords([first, second])
        self.assertEqual(keywords, ["原神", "编程"])

    # -- 过滤 ----------------------------------------------------------

    async def test_official_video_is_excluded(self) -> None:
        plugin = self.make_plugin(
            base_config(filter={"exclude_official": True}),
            videos=[
                make_video(
                    self.video_module,
                    bvid="BVPV",
                    title="原神新版本官方PV预告",
                    description="官方发布",
                )
            ],
        )
        summary = await plugin.run_round(reason="test")
        self.assertEqual(summary["shared"], 0)
        self.assertEqual(self.ctx.send.messages, [])

    async def test_old_video_is_excluded(self) -> None:
        plugin = self.make_plugin(
            base_config(filter={"max_age_days": 7.0}),
            videos=[
                make_video(
                    self.video_module,
                    bvid="BVOLD",
                    title="原神老版本测评",
                    pubdate=int(time.time()) - 60 * 86400,
                )
            ],
        )
        summary = await plugin.run_round(reason="test")
        self.assertEqual(summary["shared"], 0)

    # -- LLM 判断 / 弹幕评论 / 抽帧 ------------------------------------

    async def test_llm_judge_generates_narrative(self) -> None:
        ctx = FakeCtx(self.tmp_dir, llm=FakeLlm(judge_response=JUDGE_OK))
        plugin = self.make_plugin(
            base_config(
                interest={"use_llm_judge": True},
                share={"send_as_parts": True},
            ),
            ctx=ctx,
        )
        summary = await plugin.run_round(reason="test")
        self.assertEqual(summary["shared"], 1, summary)
        texts = [item["text"] for item in ctx.send.messages]
        narrative = "这期把新版本的内容聊得挺透，看着还挺有意思的。"
        self.assertTrue(any(narrative in text for text in texts), texts)
        # 不再有「内容：」「看点：」这类正式格式
        self.assertFalse(any("内容：" in text or "看点：" in text for text in texts), texts)
        # 介绍文案会被缓存，供之后重发复用
        record = plugin._store.get("BV1")
        self.assertEqual(record["narrative"], narrative)

    async def test_llm_judge_rejects_irrelevant_video(self) -> None:
        ctx = FakeCtx(self.tmp_dir, llm=FakeLlm(judge_response=JUDGE_IRRELEVANT))
        plugin = self.make_plugin(
            base_config(interest={"use_llm_judge": True}), ctx=ctx
        )
        summary = await plugin.run_round(reason="test")
        self.assertEqual(summary["shared"], 0)
        self.assertEqual(ctx.send.messages, [])

    async def test_llm_failure_falls_back_to_rule_score(self) -> None:
        ctx = FakeCtx(self.tmp_dir, llm=FakeLlm(fail=True))
        plugin = self.make_plugin(
            base_config(interest={"use_llm_judge": True}), ctx=ctx
        )
        summary = await plugin.run_round(reason="test")
        self.assertEqual(summary["shared"], 1, summary)
        self.assertIn("本期聊聊原神新版本", ctx.send.messages[0]["text"])
        self.assertNotIn("内容：", ctx.send.messages[0]["text"])

    async def test_danmaku_and_comments_feed_llm(self) -> None:
        ctx = FakeCtx(self.tmp_dir, llm=FakeLlm(judge_response=JUDGE_OK))
        client = FakeClient(
            videos=[make_video(self.video_module, bvid="BV1", title="原神新版本测评")],
            danmaku=[FakeDanmaku("这波操作太秀了", 1000), FakeDanmaku("笑死", 2000)],
            comments=[FakeComment("UP 讲得真好", 99)],
        )
        plugin = self.make_plugin(
            base_config(
                interest={"use_llm_judge": True},
                danmaku={"danmaku_enabled": True, "comments_enabled": True},
            ),
            ctx=ctx,
            client=client,
        )
        await plugin.run_round(reason="test")
        prompts = [call["prompt"] for call in ctx.llm.calls if isinstance(call["prompt"], str)]
        joined = "\n".join(prompts)
        self.assertIn("弹幕节选", joined)
        self.assertIn("这波操作太秀了", joined)
        self.assertIn("UP 讲得真好", joined)

    async def test_ai_spam_danmaku_is_filtered(self) -> None:
        ctx = FakeCtx(self.tmp_dir, llm=FakeLlm(judge_response=JUDGE_OK))
        client = FakeClient(
            videos=[make_video(self.video_module, bvid="BV1", title="原神新版本测评")],
            danmaku=[
                FakeDanmaku("本评论由AI生成", 100),
                FakeDanmaku("感谢分享，学到了", 200),
                FakeDanmaku("这个操作确实离谱", 300),
            ],
        )
        plugin = self.make_plugin(
            base_config(
                interest={"use_llm_judge": True},
                danmaku={"danmaku_enabled": True, "filter_ai_spam": True},
            ),
            ctx=ctx,
            client=client,
        )
        await plugin.run_round(reason="test")
        joined = "\n".join(
            call["prompt"] for call in ctx.llm.calls if isinstance(call["prompt"], str)
        )
        self.assertNotIn("本评论由AI生成", joined)
        self.assertNotIn("感谢分享，学到了", joined)
        self.assertIn("这个操作确实离谱", joined)

    async def test_vision_frames_are_described_by_vlm_task(self) -> None:
        ctx = FakeCtx(self.tmp_dir, llm=FakeLlm(judge_response=JUDGE_OK))
        plugin = self.make_plugin(
            base_config(interest={"use_llm_judge": True}, vision={"enabled": True}),
            ctx=ctx,
        )
        plugin._frames = FakeFrames()
        await plugin.run_round(reason="test")
        self.assertTrue(plugin._frames.calls, "应该对入选视频抽帧")
        task_names = [call.get("task_name") for call in ctx.llm.calls]
        self.assertIn("vlm", task_names)
        # 抽帧描述应被回填进最终判断的 prompt
        joined = "\n".join(
            call["prompt"] for call in ctx.llm.calls if isinstance(call["prompt"], str)
        )
        self.assertIn("画面（抽帧识别）", joined)

    # -- 指令 ----------------------------------------------------------

    async def test_manual_command_sends_summary(self) -> None:
        plugin = self.make_plugin()
        result = await plugin.cmd_share(stream_id="cmd-stream")
        self.assertIsInstance(result, tuple)
        self.assertTrue(result[0])
        self.assertEqual(len(self.ctx.send.messages), 2)
        self.assertEqual(self.ctx.send.messages[-1]["stream_id"], "cmd-stream")
        self.assertIn("麦麦刷B站完成", self.ctx.send.messages[-1]["text"])

    async def test_status_command_text(self) -> None:
        plugin = self.make_plugin()
        await plugin.run_round(reason="test")
        result = await plugin.cmd_status(stream_id="s")
        text = result[1]
        self.assertIn("每 30 分钟", text)
        self.assertIn("白名单会话", text)
        self.assertIn("累计分享", text)

    async def test_test_command_preview(self) -> None:
        plugin = self.make_plugin()
        result = await plugin.cmd_test(stream_id="s")
        text = result[1]
        self.assertIn("候选", text)
        self.assertIn("原神", text)

    # -- 人设语气 ------------------------------------------------------

    async def test_intro_sent_as_first_part(self) -> None:
        ctx = FakeCtx(
            self.tmp_dir,
            llm=FakeLlm(judge_response=JUDGE_WITH_INTRO),
        )
        plugin = self.make_plugin(
            base_config(
                share={"send_as_parts": True, "intro_enabled": True},
                interest={"use_llm_judge": True},
                persona={"enabled": True, "name": "小鲸鱼"},
            ),
            ctx=ctx,
        )
        summary = await plugin.run_round(reason="test")
        self.assertTrue(summary["ok"], summary)
        texts = [item["text"] for item in ctx.send.messages]
        self.assertEqual(texts[0], "呜哇，这个好好看！")
        self.assertIn("【原神新版本测评】", texts[1])

    async def test_intro_disabled_skips_intro_part(self) -> None:
        ctx = FakeCtx(self.tmp_dir, llm=FakeLlm(judge_response=JUDGE_WITH_INTRO))
        plugin = self.make_plugin(
            base_config(
                share={"send_as_parts": True, "intro_enabled": False},
                interest={"use_llm_judge": True},
            ),
            ctx=ctx,
        )
        await plugin.run_round(reason="test")
        texts = [item["text"] for item in ctx.send.messages]
        self.assertNotIn("呜哇，这个好好看！", texts)
        self.assertIn("【原神新版本测评】", texts[0])

    # -- 随口一提模式 --------------------------------------------------

    async def test_casual_mode_sends_single_message(self) -> None:
        ctx = FakeCtx(self.tmp_dir, llm=FakeLlm(judge_response=JUDGE_WITH_INTRO))
        plugin = self.make_plugin(
            base_config(
                share={"casual_mode": True, "intro_enabled": True},
                interest={"use_llm_judge": True},
            ),
            ctx=ctx,
        )
        summary = await plugin.run_round(reason="test")
        self.assertTrue(summary["ok"], summary)
        self.assertEqual(len(ctx.send.messages), 1)
        text = ctx.send.messages[0]["text"]
        self.assertIn("这期把新版本的内容聊得挺透", text)
        self.assertIn("【原神新版本测评】", text)
        self.assertIn("https://www.bilibili.com/video/BV1", text)
        # 随口一提模式不再单独发推荐语
        self.assertNotIn("呜哇，这个好好看！", text)

    async def test_casual_prompt_asks_for_offhand_tone(self) -> None:
        ctx = FakeCtx(self.tmp_dir, llm=FakeLlm(judge_response=JUDGE_WITH_INTRO))
        plugin = self.make_plugin(
            base_config(
                share={"casual_mode": True, "casual_max_chars": 50},
                interest={"use_llm_judge": True},
            ),
            ctx=ctx,
        )
        await plugin.run_round(reason="test")
        prompt = ctx.llm.calls[0]["prompt"]
        self.assertIn("随口", prompt)
        self.assertIn("不要用「推荐」「分享」「安利」", prompt)
        self.assertIn("50 字", prompt)
        # 随口一提模式不再要求单独生成推荐语字段
        self.assertNotIn('"intro"', prompt)

    async def test_standard_prompt_keeps_intro_field(self) -> None:
        ctx = FakeCtx(self.tmp_dir, llm=FakeLlm(judge_response=JUDGE_WITH_INTRO))
        plugin = self.make_plugin(
            base_config(
                share={"casual_mode": False, "intro_enabled": True},
                interest={"use_llm_judge": True},
            ),
            ctx=ctx,
        )
        await plugin.run_round(reason="test")
        prompt = ctx.llm.calls[0]["prompt"]
        self.assertIn('"intro"', prompt)
        self.assertIn("像在群里跟朋友随口安利", prompt)

    async def test_casual_mode_clips_fallback_description(self) -> None:
        long_description = "这是一段很长的视频简介，" * 20
        ctx = FakeCtx(self.tmp_dir)
        plugin = self.make_plugin(
            base_config(share={"casual_mode": True, "casual_max_chars": 30}),
            videos=[make_video(self.video_module, description=long_description)],
            ctx=ctx,
        )
        await plugin.run_round(reason="test")
        self.assertEqual(len(ctx.send.messages), 1)
        text = ctx.send.messages[0]["text"]
        self.assertNotIn(long_description, text)
        self.assertLess(len(text), len(long_description))

    async def test_persona_block_reaches_llm_prompt(self) -> None:
        ctx = FakeCtx(self.tmp_dir, llm=FakeLlm(judge_response=JUDGE_WITH_INTRO))
        plugin = self.make_plugin(
            base_config(
                interest={"use_llm_judge": True},
                persona={
                    "enabled": True,
                    "name": "小鲸鱼",
                    "personality": "你是一只爱看视频的虚拟鲸鱼。",
                },
            ),
            ctx=ctx,
        )
        await plugin.run_round(reason="test")
        self.assertTrue(ctx.llm.calls)
        prompt = ctx.llm.calls[0]["prompt"]
        self.assertIn("小鲸鱼", prompt)
        self.assertIn("虚拟鲸鱼", prompt)

    async def test_persona_read_from_host_config(self) -> None:
        ctx = FakeCtx(
            self.tmp_dir,
            llm=FakeLlm(judge_response=JUDGE_WITH_INTRO),
            config_values={
                "bot.nickname": "小虎鲸",
                "personality.personality": "爱刷 B 站",
            },
        )
        plugin = self.make_plugin(
            base_config(interest={"use_llm_judge": True}), ctx=ctx
        )
        await plugin._persona.refresh()
        text = (await plugin.cmd_status(stream_id="s"))[1]
        self.assertIn("小虎鲸", text)
        self.assertIn("宿主", text)

    async def test_status_reports_missing_persona(self) -> None:
        plugin = self.make_plugin(base_config(persona={"enabled": True}))
        text = (await plugin.cmd_status(stream_id="s"))[1]
        self.assertIn("人设语气", text)
        self.assertIn("未读到人设", text)

    async def test_status_reports_disabled_persona(self) -> None:
        plugin = self.make_plugin(base_config(persona={"enabled": False}))
        text = (await plugin.cmd_status(stream_id="s"))[1]
        self.assertIn("人设语气：已关闭", text)

    async def test_reshare_reuses_cached_intro(self) -> None:
        video = make_video(self.video_module, bvid="BVR2", title="原神新版本测评")
        plugin = self.make_plugin(
            base_config(
                share={"send_as_parts": True, "intro_enabled": True},
                reshare={"enabled": True},
            )
        )
        plugin._store.mark_shared(
            video,
            narrative="旧介绍",
            intro="之前那句推荐语",
            kind="auto",
            stream_id="stream-1",
        )
        await plugin._maybe_reshare(
            {
                "stream_id": "stream-1",
                "session_id": "stream-1",
                "processed_plain_text": "你之前发的原神新版本测评再发一遍",
            }
        )
        texts = [item["text"] for item in self.ctx.send.messages]
        self.assertTrue(texts)
        self.assertEqual(texts[0], "之前那句推荐语")

    async def test_active_search_command_and_cooldown(self) -> None:
        client = FakeClient(
            videos=[],
            search_results=[make_video(self.video_module, bvid="BVS1", title="原神新版本测评")],
        )
        plugin = self.make_plugin(client=client)
        result = await plugin.cmd_search(
            stream_id="stream-1", matched_groups={"query": "原神"}
        )
        self.assertTrue(result[0])
        self.assertIn("已搜索并分享", result[1])
        # 冷却期内再次搜索会被拒绝
        second = await plugin.search_and_share("原神", "stream-1")
        self.assertFalse(second["ok"])
        self.assertEqual(second["reason"], "cooldown")

    async def test_active_search_reuses_cached_narrative(self) -> None:
        video = make_video(self.video_module, bvid="BVS2", title="原神新版本测评")
        plugin = self.make_plugin(
            base_config(),
            client=FakeClient(videos=[], search_results=[video]),
        )
        plugin._store.mark_shared(
            video, narrative="旧的介绍文案", kind="auto", stream_id="stream-1"
        )
        result = await plugin.search_and_share("原神新版本测评", "stream-1")
        self.assertTrue(result["ok"])
        joined = "\n".join(item["text"] for item in self.ctx.send.messages)
        self.assertIn("旧的介绍文案", joined)
        self.assertNotIn("内容：", joined)
        # 不应重新调用 LLM
        self.assertEqual(self.ctx.llm.calls, [])

    async def test_shiwei_command_shares_without_summary(self) -> None:
        client = FakeClient(
            videos=[],
            search_results=[
                make_video(
                    self.video_module,
                    bvid="BVSHI",
                    title="离谱整活：这波操作太抽象了",
                    description="抽象整活",
                    play=200000,
                )
            ],
        )
        plugin = self.make_plugin(client=client)
        result = await plugin.cmd_shiwei(stream_id="stream-1")
        self.assertTrue(result[0])
        texts = [item["text"] for item in self.ctx.send.messages]
        joined = "\n".join(texts)
        self.assertIn("【离谱整活：这波操作太抽象了】", joined)
        self.assertNotIn("内容：", joined)
        self.assertNotIn("看点：", joined)
        # 冷却
        second = await plugin.share_shiwei("stream-1")
        self.assertEqual(second["reason"], "cooldown")

    # -- 重发 Hook -----------------------------------------------------

    async def test_reshare_hook_resends_cached_narrative(self) -> None:
        video = make_video(self.video_module, bvid="BVR", title="原神新版本测评")
        plugin = self.make_plugin(base_config())
        plugin._store.mark_shared(
            video, narrative="缓存介绍文案", kind="auto", stream_id="stream-1"
        )
        message = {
            "processed_plain_text": "之前发的原神新版本测评再发一遍",
            "session_id": "stream-1",
        }
        await plugin.on_chat_receive("chat.receive.before_process", message)
        joined = "\n".join(item["text"] for item in self.ctx.send.messages)
        self.assertIn("【原神新版本测评】", joined)
        self.assertIn("缓存介绍文案", joined)

    async def test_reshare_hook_ignores_unrelated_message(self) -> None:
        plugin = self.make_plugin()
        await plugin.on_chat_receive(
            "chat.receive.before_process",
            {"processed_plain_text": "今天天气不错", "session_id": "stream-1"},
        )
        self.assertEqual(self.ctx.send.messages, [])

    async def test_reshare_hook_ignores_trigger_without_history(self) -> None:
        plugin = self.make_plugin()
        await plugin.on_chat_receive(
            "chat.receive.before_process",
            {"processed_plain_text": "再发一遍", "session_id": "stream-1"},
        )
        self.assertEqual(self.ctx.send.messages, [])

    async def test_reshare_hook_ignores_non_whitelisted_stream(self) -> None:
        """重发只对白名单会话生效，别的群让再发一遍也不发。"""

        video = make_video(self.video_module, bvid="BVR3", title="原神新版本测评")
        plugin = self.make_plugin(base_config())
        plugin._store.mark_shared(
            video, narrative="缓存介绍文案", kind="auto", stream_id="other-stream"
        )
        await plugin.on_chat_receive(
            "chat.receive.before_process",
            {
                "processed_plain_text": "之前发的原神新版本测评再发一遍",
                "session_id": "other-stream",
            },
        )
        self.assertEqual(self.ctx.send.messages, [])

    async def test_reshare_hook_ignores_when_whitelist_empty(self) -> None:
        """没有配置白名单会话时，重发功能整体不生效。"""

        video = make_video(self.video_module, bvid="BVR4", title="原神新版本测评")
        config = base_config()
        config["streams"] = []
        plugin = self.make_plugin(config)
        plugin._store.mark_shared(
            video, narrative="缓存介绍文案", kind="auto", stream_id="stream-1"
        )
        await plugin.on_chat_receive(
            "chat.receive.before_process",
            {
                "processed_plain_text": "之前发的原神新版本测评再发一遍",
                "session_id": "stream-1",
            },
        )
        self.assertEqual(self.ctx.send.messages, [])

    # -- 生命周期与配置 ------------------------------------------------

    async def test_lifecycle_starts_and_stops_loop(self) -> None:
        plugin = self.make_plugin()
        await plugin.on_load()
        try:
            self.assertIsNotNone(plugin._loop_task)
            self.assertFalse(plugin._loop_task.done())
        finally:
            await plugin.on_unload()
        self.assertIsNone(plugin._loop_task)
        self.assertIsNone(plugin._client)
        self.assertTrue((self.tmp_dir / PLUGIN_ID / "state.json").exists())

    async def test_disabled_on_load_does_not_start_loop(self) -> None:
        plugin = self.make_plugin(base_config(plugin={"enabled": False}))
        await plugin.on_load()
        self.assertIsNone(plugin._loop_task)
        await plugin.on_unload()

    def test_active_window_helper(self) -> None:
        module = self.module
        noon = datetime(2026, 1, 1, 12, 0)
        early = datetime(2026, 1, 1, 8, 30)
        late = datetime(2026, 1, 1, 23, 30)
        self.assertTrue(module._in_active_window("", now=noon))
        self.assertFalse(module._in_active_window("08:00-09:00", now=noon))
        self.assertTrue(module._in_active_window("08:00-09:00", now=early))
        self.assertTrue(module._in_active_window("22:00-02:00", now=late))
        self.assertFalse(module._in_active_window("22:00-02:00", now=noon))

    def test_default_config_shape(self) -> None:
        defaults = self.module.BilibiliInterestSharePlugin.build_default_config()
        self.assertEqual(defaults["schedule"]["interval_minutes"], 60)
        self.assertEqual(defaults["schedule"]["search_cooldown_seconds"], 300)
        self.assertTrue(defaults["plugin"]["enabled"])
        self.assertEqual(defaults["plugin"]["config_version"], "1.1.0")
        self.assertEqual(defaults["interest"]["llm_task_name"], "planner")
        self.assertEqual(defaults["vision"]["task_name"], "vlm")
        self.assertTrue(defaults["categories"])
        self.assertTrue(defaults["share"]["send_as_parts"])

    def test_config_schema_includes_streams(self) -> None:
        schema = self.module.BilibiliInterestSharePlugin.build_config_schema(
            plugin_id=PLUGIN_ID, plugin_name="麦麦刷B站"
        )
        dumped = json.dumps(schema, ensure_ascii=False)
        for key in ("streams", "interval_minutes", "categories", "shiwei", "vision"):
            self.assertIn(key, dumped)


class PackagingTest(unittest.TestCase):
    """用 MaiBot 自带的校验器检查清单与配置能被宿主接受。"""

    def test_manifest_accepted_by_host_validator(self) -> None:
        import os

        if MAIBOT_ROOT is None:
            self.skipTest("未设置 MAIBOT_INSTANCE_DIR，跳过需要真实 MaiBot 宿主的测试")
        src_dir = str(MAIBOT_ROOT / "src")
        if src_dir not in sys.path:
            sys.path.insert(0, src_dir)
        old_instance = os.environ.get("MAIBOT_INSTANCE_DIR")
        os.environ["MAIBOT_INSTANCE_DIR"] = str(MAIBOT_ROOT)
        try:
            from plugin_runtime.runner.manifest_validator import ManifestValidator
        except Exception as exc:  # pragma: no cover - 宿主依赖缺失时跳过
            self.skipTest(f"无法导入宿主校验器: {exc}")
        finally:
            if old_instance is None:
                os.environ.pop("MAIBOT_INSTANCE_DIR", None)
            else:
                os.environ["MAIBOT_INSTANCE_DIR"] = old_instance
        validator = ManifestValidator(
            host_version="1.3.8",
            sdk_version="2.15.0",
            project_root=MAIBOT_ROOT,
            validate_python_package_dependencies=False,
            log_errors=False,
        )
        manifest = validator.load_from_plugin_path(PLUGIN_ROOT)
        self.assertIsNotNone(manifest, "清单校验失败: " + "; ".join(validator.errors))
        self.assertEqual(manifest.id, PLUGIN_ID)
        self.assertTrue(manifest.capabilities)
        self.assertIn("llm.generate", manifest.capabilities)

    def test_plugin_loads_with_host_loader(self) -> None:
        """用 MaiBot 真实的 PluginLoader 加载插件，确认能被宿主接受。"""

        import os
        import shutil

        if MAIBOT_ROOT is None:
            self.skipTest("未设置 MAIBOT_INSTANCE_DIR，跳过需要真实 MaiBot 宿主的测试")
        src_dir = str(MAIBOT_ROOT / "src")
        if src_dir not in sys.path:
            sys.path.insert(0, src_dir)
        old_instance = os.environ.get("MAIBOT_INSTANCE_DIR")
        os.environ["MAIBOT_INSTANCE_DIR"] = str(MAIBOT_ROOT)
        try:
            try:
                from plugin_runtime.runner.plugin_loader import PluginLoader
            except Exception as exc:  # pragma: no cover - 宿主依赖缺失时跳过
                self.skipTest(f"无法导入宿主加载器: {exc}")

            with tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp) / PLUGIN_ID
                shutil.copytree(
                    PLUGIN_ROOT,
                    target,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
                )
                loader = PluginLoader(host_version="1.3.8")
                loaded = loader.discover_and_load([tmp])
                self.assertEqual([meta.plugin_id for meta in loaded], [PLUGIN_ID])
                self.assertEqual(dict(loader.failed_plugins), {})
                instance = loaded[0].instance
                self.assertEqual(type(instance).__name__, "BilibiliInterestSharePlugin")
                self.assertEqual(instance.config_model.__name__, "BilibiliInterestShareConfig")
                command_names = {
                    component.get("name") for component in instance.get_components()
                }
                self.assertIn("bili_share", command_names)
                self.assertIn("shiwei_video", command_names)
        finally:
            if old_instance is None:
                os.environ.pop("MAIBOT_INSTANCE_DIR", None)
            else:
                os.environ["MAIBOT_INSTANCE_DIR"] = old_instance

    def test_config_example_matches_model(self) -> None:
        import tomllib

        with (PLUGIN_ROOT / "config.example.toml").open("rb") as handle:
            raw = tomllib.load(handle)
        plugin = load_plugin_module().BilibiliInterestSharePlugin()
        plugin.set_plugin_config(raw)  # 不应抛异常
        self.assertEqual(plugin.config.plugin.config_version, "1.1.0")
        self.assertEqual(plugin.config.streams, [])
        self.assertTrue(plugin.config.categories)
        self.assertEqual(plugin.config.vision.task_name, "vlm")
        self.assertEqual(plugin.config.interest.llm_task_name, "planner")


class MessageLookbackTests(unittest.TestCase):
    """宿主 ``message.get_recent`` 不支持时间窗，插件要自己按 timestamp 过滤。"""

    def test_filters_old_messages(self) -> None:
        module = load_plugin_module()
        now = time.time()
        messages = [
            {"timestamp": str(now - 60), "processed_plain_text": "刚刚聊的"},
            {"timestamp": str(now - 3 * 24 * 3600), "processed_plain_text": "三天前"},
        ]
        kept = module.filter_messages_by_lookback(messages, 48.0)
        self.assertEqual([item["processed_plain_text"] for item in kept], ["刚刚聊的"])

    def test_keeps_messages_without_timestamp(self) -> None:
        module = load_plugin_module()
        kept = module.filter_messages_by_lookback(
            [{"processed_plain_text": "没有时间戳"}, {"timestamp": "not-a-number"}], 1.0
        )
        self.assertEqual(len(kept), 2)

    def test_zero_lookback_keeps_everything(self) -> None:
        module = load_plugin_module()
        kept = module.filter_messages_by_lookback(
            [{"timestamp": "1", "processed_plain_text": "很久以前"}], 0.0
        )
        self.assertEqual(len(kept), 1)


class DataDirResolutionTests(unittest.TestCase):
    """宿主注入的 paths.data_dir 已经是 data/plugins/<plugin_id>，不能再套一层。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.module = load_plugin_module()

    def _plugin_with_data_dir(self, data_dir: Path, advanced: dict | None = None):
        ctx = FakeCtx(data_dir)
        plugin = self.module.BilibiliInterestSharePlugin()
        plugin._set_context(ctx)
        config = base_config()
        config["advanced"] = deep_merge(config.get("advanced", {}), advanced or {})
        plugin.set_plugin_config(config)
        return plugin

    def test_host_plugin_data_dir_is_used_directly(self) -> None:
        data_dir = self.root / "data" / "plugins" / "github.TommyAAKK.bilibili_interest_share"
        plugin = self._plugin_with_data_dir(data_dir)
        self.assertEqual(plugin._resolve_data_dir(), data_dir)

    def test_plugins_root_gets_plugin_id_appended(self) -> None:
        plugins_root = self.root / "data" / "plugins"
        plugin = self._plugin_with_data_dir(plugins_root)
        self.assertEqual(
            plugin._resolve_data_dir(), plugins_root / "github.TommyAAKK.bilibili_interest_share"
        )

    def test_relative_configured_dir_resolves_under_plugin_dir(self) -> None:
        data_dir = self.root / "data" / "plugins" / "github.TommyAAKK.bilibili_interest_share"
        plugin = self._plugin_with_data_dir(data_dir, {"data_dir": "custom"})
        self.assertEqual(plugin._resolve_data_dir(), data_dir / "custom")

    def test_absolute_configured_dir_wins(self) -> None:
        data_dir = self.root / "data" / "plugins" / "github.TommyAAKK.bilibili_interest_share"
        target = self.root / "elsewhere"
        plugin = self._plugin_with_data_dir(data_dir, {"data_dir": str(target)})
        self.assertEqual(plugin._resolve_data_dir(), target)


class RecentMessagesSdkSignatureTests(unittest.IsolatedAsyncioTestCase):
    """真实 SDK 的 get_recent(chat_id, limit) 不接受 hours，传了会 TypeError。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_dir = Path(self._tmp.name)
        self.module = load_plugin_module()
        self.ctx = FakeCtx(self.tmp_dir)

    def _make_plugin(self):
        plugin = self.module.BilibiliInterestSharePlugin()
        plugin._set_context(self.ctx)
        plugin.set_plugin_config(base_config())
        plugin._data_dir = self.tmp_dir
        return plugin

    async def test_calls_sdk_without_hours(self) -> None:
        plugin = self._make_plugin()
        self.ctx.message.messages = [
            {"timestamp": str(time.time() - 60), "processed_plain_text": "原神真好玩"}
        ]
        messages = await plugin._recent_messages("stream-1")
        self.assertEqual(len(messages), 1)
        self.assertEqual(
            self.ctx.message.calls[0],
            {"chat_id": "stream-1", "limit": plugin.config.interest.max_messages_per_stream},
        )

    async def test_old_messages_dropped(self) -> None:
        plugin = self._make_plugin()
        self.ctx.message.messages = [
            {"timestamp": str(time.time() - 10 * 24 * 3600), "processed_plain_text": "十天前"}
        ]
        messages = await plugin._recent_messages("stream-1")
        self.assertEqual(messages, [])


class _SendStub:
    """返回指定结果的假 send 能力，用来覆盖宿主不同版本的返回结构。"""

    def __init__(self, result) -> None:
        self.result = result
        self.calls = []

    async def text(self, text: str, stream_id: str, **kwargs):
        self.calls.append({"text": text, "stream_id": stream_id})
        return self.result


class SendResultShapeTests(unittest.IsolatedAsyncioTestCase):
    """宿主 1.3.15 的 send.text 返回 {"success": bool}，旧版返回 {"sent": bool}。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_dir = Path(self._tmp.name)
        self.module = load_plugin_module()

    def _make_plugin(self, send_result):
        ctx = FakeCtx(self.tmp_dir)
        ctx.send = _SendStub(send_result)
        plugin = self.module.BilibiliInterestSharePlugin()
        plugin._set_context(ctx)
        plugin.set_plugin_config(base_config())
        plugin._data_dir = self.tmp_dir
        return plugin, ctx

    async def test_success_key_means_sent(self) -> None:
        plugin, ctx = self._make_plugin({"success": True})
        self.assertTrue(await plugin._send_text("stream-1", "随便说一句"))
        self.assertEqual(len(ctx.send.calls), 1)

    async def test_sent_key_still_works(self) -> None:
        plugin, _ctx = self._make_plugin({"sent": True, "message_id": "123"})
        self.assertTrue(await plugin._send_text("stream-1", "随便说一句"))

    async def test_success_false_is_failure(self) -> None:
        plugin, _ctx = self._make_plugin({"success": False, "error": "没权限"})
        self.assertFalse(await plugin._send_text("stream-1", "随便说一句"))

    async def test_plain_bool_results(self) -> None:
        plugin, _ctx = self._make_plugin(True)
        self.assertTrue(await plugin._send_text("stream-1", "随便说一句"))
        plugin, _ctx = self._make_plugin(False)
        self.assertFalse(await plugin._send_text("stream-1", "随便说一句"))


class ConfigFieldBehaviorTests(unittest.IsolatedAsyncioTestCase):
    """几个「配置项写了就得真生效」的行为，防止 schema 与实现对不上。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_dir = Path(self._tmp.name)
        self.module = load_plugin_module()
        self.video_module = load_submodule("bilibili")
        self.ctx = FakeCtx(self.tmp_dir)

    def _make_plugin(self, config, *, client=None):
        plugin = self.module.BilibiliInterestSharePlugin()
        plugin._set_context(self.ctx)
        plugin.set_plugin_config(config)
        plugin._client = client if client is not None else FakeClient()
        plugin._store = self.module.SeenStore(
            self.tmp_dir / "state.json", dedupe_days=30, max_records=1000
        )
        plugin._store.load()
        plugin._data_dir = self.tmp_dir
        return plugin

    async def test_search_pages_is_respected(self) -> None:
        """`[sources].search_pages = 2` 时每个关键词要搜两页。"""

        client = FakeClient(videos=[make_video(self.video_module, bvid="BVSP")])
        plugin = self._make_plugin(
            base_config(
                sources={"search_enabled": True, "search_pages": 2, "popular_enabled": False}
            ),
            client=client,
        )
        await plugin._fetch_candidates(["原神"])
        self.assertEqual([call["page"] for call in client.search_calls], [1, 2])

    async def test_category_min_play_filters_candidates(self) -> None:
        """`[[categories]].min_play` 生效：播放量不够的候选被挡掉。"""

        video = make_video(self.video_module, bvid="BVC1", title="原神新版本测评", play=120000)
        stream = {
            "name": "测试群",
            "enabled": True,
            "stream_id": "stream-1",
            "group_id": "",
            "user_id": "",
            "interests": ["原神"],
            "categories": ["游戏"],
            "auto_interest": False,
            "max_share_per_round": 0,
        }
        category = {
            "name": "游戏",
            "enabled": True,
            "keywords": ["原神"],
            "search_order": "",
            "min_play": 10_000_000,
        }
        strict = base_config(
            sources={"search_enabled": True, "popular_enabled": False},
            categories=[category],
            streams=[stream],
        )
        plugin = self._make_plugin(strict, client=FakeClient(videos=[video]))
        summary = await plugin.run_round(reason="test")
        self.assertEqual(summary["shared"], 0)

        # 同样的候选，把类别门槛降为 0 就能分享
        relaxed = deep_merge(
            strict, {"categories": [dict(category, min_play=0)]}
        )
        self.ctx.send.messages.clear()
        plugin2 = self._make_plugin(relaxed, client=FakeClient(videos=[video]))
        summary2 = await plugin2.run_round(reason="test")
        self.assertEqual(summary2["shared"], 1)

    async def test_reshare_reply_enabled_sends_confirmation(self) -> None:
        """`[reshare].reply_enabled = true` 时重发后回一句确认。"""

        video = make_video(self.video_module, bvid="BVRP", title="原神新版本测评")
        plugin = self._make_plugin(
            base_config(reshare={"enabled": True, "reply_enabled": True})
        )
        plugin._store.mark_shared(
            video, narrative="缓存介绍文案", kind="auto", stream_id="stream-1"
        )
        await plugin._maybe_reshare(
            {
                "stream_id": "stream-1",
                "session_id": "stream-1",
                "processed_plain_text": "之前发的原神新版本测评再发一遍",
            }
        )
        texts = [item["text"] for item in self.ctx.send.messages]
        self.assertIn("好嘞，再发一遍～", texts)

    async def test_reshare_reply_disabled_sends_no_confirmation(self) -> None:
        video = make_video(self.video_module, bvid="BVRQ", title="原神新版本测评")
        plugin = self._make_plugin(
            base_config(reshare={"enabled": True, "reply_enabled": False})
        )
        plugin._store.mark_shared(
            video, narrative="缓存介绍文案", kind="auto", stream_id="stream-1"
        )
        await plugin._maybe_reshare(
            {
                "stream_id": "stream-1",
                "session_id": "stream-1",
                "processed_plain_text": "之前发的原神新版本测评再发一遍",
            }
        )
        texts = [item["text"] for item in self.ctx.send.messages]
        self.assertNotIn("好嘞，再发一遍～", texts)
        self.assertTrue(texts)


if __name__ == "__main__":
    unittest.main()
