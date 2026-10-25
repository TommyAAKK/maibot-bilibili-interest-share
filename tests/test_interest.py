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
        self.assertIn("机械键盘", terms)
        self.assertNotIn("这个", terms)
        self.assertNotIn("就是", terms)

    def test_extract_keywords_respects_min_count(self) -> None:
        messages = [{"processed_plain_text": "只出现一次的关键词"}]
        keywords = self.module.extract_interest_keywords(messages, min_count=2)
        self.assertEqual(keywords, [])

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
        self.assertIn("机械键盘", keywords)

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


if __name__ == "__main__":
    unittest.main()
