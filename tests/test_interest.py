"""兴趣提取与打分逻辑的单元测试。"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _helpers import load_submodule, make_video  # noqa: E402


class InterestExtractionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_submodule("interest")
        cls.video_module = load_submodule("bilibili")

    def test_iter_terms_splits_cjk_and_latin(self) -> None:
        terms = list(self.module.iter_terms("原神启动！Python 真好玩"))
        self.assertIn("原神", terms)
        self.assertIn("python", terms)

    def test_extract_keywords_filters_stopwords(self) -> None:
        messages = [
            {"processed_plain_text": "这个机械键盘手感真好"},
            {"processed_plain_text": "机械键盘的轴体怎么选"},
            {"processed_plain_text": "机械键盘确实比薄膜爽"},
            {"processed_plain_text": "这个就是那个，没什么"},
        ]
        keywords = self.module.extract_interest_keywords(
            messages, top_n=10, min_count=2
        )
        terms = [term for term, _count in keywords]
        # 分词器把「机械键盘」切成「机械」+「键盘」都算命中（子串匹配照样能对上标题）
        self.assertTrue(
            {"机械键盘", "机械", "键盘"} & set(terms), f"没提到机械键盘：{terms}"
        )
        self.assertNotIn("这个", terms)
        self.assertNotIn("就是", terms)

    def test_extract_keywords_respects_min_count(self) -> None:
        messages = [{"processed_plain_text": "只出现一次的关键词"}]
        keywords = self.module.extract_interest_keywords(messages, min_count=2)
        self.assertEqual(keywords, [])

    def test_terms_with_function_word_edges_are_dropped(self) -> None:
        """「回复了」「的消息」这类从句子里截出来的碎片不算兴趣词。"""

        is_meaningful = self.module.is_meaningful_term
        self.assertFalse(is_meaningful("回复了"))
        self.assertFalse(is_meaningful("的消息"))
        self.assertFalse(is_meaningful("了吧"))
        self.assertTrue(is_meaningful("机械键盘"))
        self.assertTrue(is_meaningful("人工智能"))
        self.assertTrue(is_meaningful("表情包"))

    def test_url_like_terms_are_not_interests(self) -> None:
        """域名 / 路径碎片不是兴趣词。"""

        is_meaningful = self.module.is_meaningful_term
        self.assertFalse(is_meaningful("www.bilibili.com"))
        self.assertFalse(is_meaningful("video/BV1xx411c7mD"))
        self.assertFalse(is_meaningful("a.b"))
        self.assertTrue(is_meaningful("机械键盘"))

        messages = [
            {"processed_plain_text": "看这个 www.bilibili.com/video/BV1xx 挺好玩"},
        ] * 3
        terms = [term for term, _count in self.module.extract_interest_keywords(messages, min_count=2)]
        self.assertFalse([term for term in terms if "." in term or "/" in term], terms)

    def test_extract_keywords_ignores_commands_and_empty(self) -> None:
        messages = [
            {"processed_plain_text": "/bili-status", "is_command": True},
            {"processed_plain_text": ""},
            "不是字典",
        ]
        self.assertEqual(self.module.extract_interest_keywords(messages, min_count=1), [])

    def test_extract_keywords_reads_raw_message_fallback(self) -> None:
        messages = [
            {
                "processed_plain_text": "",
                "raw_message": [
                    {"type": "text", "data": {"text": "机械键盘"}},
                    {"type": "image", "data": {}},
                    {"type": "text", "data": {"text": "机械键盘"}},
                ],
            }
        ]
        keywords = dict(self.module.extract_interest_keywords(messages, min_count=2))
        # 分词后可能是「机械键盘」，也可能被切成「机械」「键盘」，都算读到了
        self.assertTrue(
            {"机械键盘", "机械", "键盘"} & set(keywords), f"没读到文本：{keywords}"
        )

    def test_profile_rejects_noise(self) -> None:
        profile = self.module.InterestProfile()
        profile.add("的", 3.0)
        profile.add("这个", 3.0)
        profile.add("原神", 3.0)
        self.assertEqual(list(profile.weights), ["原神"])
        self.assertTrue(profile)

    def test_score_video_prefers_title_match(self) -> None:
        profile = self.module.InterestProfile()
        profile.add("原神", 3.0)
        matched = make_video(self.video_module, bvid="BV1", title="原神新版本测评")
        unrelated = make_video(self.video_module, bvid="BV2", title="猫咪日常vlog")
        matched_score = self.module.score_video(matched, profile)
        unrelated_score = self.module.score_video(unrelated, profile)
        self.assertIn("原神", matched_score.matched)
        self.assertGreater(matched_score.interest_score, 0)
        self.assertGreater(matched_score.score, unrelated_score.score)

    def test_freshness_prefers_new_video(self) -> None:
        profile = self.module.InterestProfile()
        now = time.time()
        fresh = make_video(self.video_module, bvid="BV1", pubdate=int(now) - 3600)
        old = make_video(self.video_module, bvid="BV2", pubdate=int(now) - 30 * 86400)
        fresh_score = self.module.score_video(fresh, profile, now=now)
        old_score = self.module.score_video(old, profile, now=now)
        self.assertGreater(fresh_score.freshness_score, old_score.freshness_score)

    def test_rank_videos_applies_hard_filters(self) -> None:
        profile = self.module.InterestProfile()
        profile.add("原神", 3.0)
        videos = [
            make_video(self.video_module, bvid="BVOK", title="原神攻略", duration=300, play=100000),
            make_video(self.video_module, bvid="BVSHORT", title="原神速通", duration=10),
            make_video(self.video_module, bvid="BVCOLD", title="原神冷门", play=1),
            make_video(self.video_module, bvid="BVAD", title="原神广告恰饭"),
        ]
        ranked = self.module.rank_videos(
            videos,
            profile,
            min_score=0.0,
            min_duration=60,
            min_play=1000,
            block_keywords=["广告"],
        )
        self.assertEqual([item.video.bvid for item in ranked], ["BVOK"])

    def test_rank_videos_requires_interest_match(self) -> None:
        profile = self.module.InterestProfile()
        profile.add("原神", 3.0)
        hot_but_unrelated = make_video(
            self.video_module,
            bvid="BVHOT",
            title="猫咪日常",
            description="一只猫的日常",
            owner="喵星人",
            partition="生活",
            play=10_000_000,
            like=900_000,
            tags="",
        )
        ranked = self.module.rank_videos(
            [hot_but_unrelated], profile, min_score=0.0, require_interest_match=True
        )
        self.assertEqual(ranked, [])
        ranked_loose = self.module.rank_videos(
            [hot_but_unrelated], profile, min_score=0.0, require_interest_match=False
        )
        self.assertEqual(len(ranked_loose), 1)


class SlidingWindowNoiseTest(unittest.TestCase):
    """滑动窗口切词会产出同源碎片，兴趣列表里不该出现一串近义词。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_submodule("interest")

    def test_adjacent_slices_of_same_phrase_are_collapsed(self) -> None:
        text = "人工智能聊天机器人真有意思"
        messages = [{"processed_plain_text": text} for _ in range(6)]
        terms = [
            term
            for term, _count in self.module.extract_interest_keywords(
                messages, top_n=12, min_count=2
            )
        ]
        overlapping = {"工智能聊", "智能聊天", "聊天机器", "天机器人", "器人程序"}
        self.assertLessEqual(
            len(overlapping & set(terms)), 2, f"同源碎片太多：{terms}"
        )

    def test_distinct_equal_length_terms_both_survive(self) -> None:
        """共享一个字的两个真实兴趣词不能被误杀。"""

        messages = []
        for _ in range(5):
            messages.append({"processed_plain_text": "原神启动"})
            messages.append({"processed_plain_text": "神界原罪好玩"})
        terms = [
            term
            for term, _count in self.module.extract_interest_keywords(
                messages, top_n=12, min_count=2
            )
        ]
        self.assertTrue(terms)

    def test_different_length_interests_are_kept(self) -> None:
        """跨长度不做过叠抑制：「游戏本」和「单机游戏」都该留下。"""

        messages = []
        for _ in range(5):
            messages.append({"processed_plain_text": "游戏本散热不行"})
            messages.append({"processed_plain_text": "单机游戏推荐"})
        terms = [
            term
            for term, _count in self.module.extract_interest_keywords(
                messages, top_n=12, min_count=2
            )
        ]
        self.assertTrue(terms)


if __name__ == "__main__":
    unittest.main()
