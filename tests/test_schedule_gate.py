"""跳过概率（骰子）与每日分享上限的单元测试（v2.1.0 新增）。

对应两个配置项：

- ``[schedule].skip_chance``：每轮开始掷一次骰子，按概率放弃整轮；
- ``[schedule].daily_share_max``：每天自动分享的总条数上限（跨会话合计、跨重启保留）。

手动触发的轮次（``manual=True``）两道闸都不拦。
"""

from __future__ import annotations

import unittest

from test_plugin import PluginTestCase, base_config


class ScheduleGateTests(PluginTestCase):
    async def test_daily_cap_blocks_round(self) -> None:
        """当天分享数达到上限后，自动轮次直接跳过、不再搜索。"""

        plugin = self.make_plugin(base_config(schedule={"daily_share_max": 3}))
        plugin._bump_daily_shared(3)
        summary = await plugin.run_round(reason="scheduled")
        self.assertTrue(summary["ok"], summary)
        self.assertEqual(summary["skipped"], "daily_limit")
        self.assertEqual(summary["shared"], 0)
        self.assertEqual(summary["fetched"], 0)
        self.assertEqual(summary["daily_shared"], 3)
        self.assertEqual(summary["daily_max"], 3)

    async def test_daily_cap_zero_means_unlimited(self) -> None:
        """0 表示不限制：无论今天分享多少条都继续跑。"""

        plugin = self.make_plugin(base_config(schedule={"daily_share_max": 0}))
        plugin._bump_daily_shared(99)
        summary = await plugin.run_round(reason="scheduled")
        self.assertNotEqual(summary.get("skipped"), "daily_limit")
        self.assertEqual(summary["shared"], 1)

    async def test_daily_cap_counts_across_streams(self) -> None:
        """上限是跨会话合计：两个会话各发一条就占掉两个额度。"""

        config = base_config(
            schedule={"daily_share_max": 2, "max_share_per_round": 1},
            streams=[
                {
                    "name": "群一",
                    "enabled": True,
                    "stream_id": "stream-1",
                    "group_id": "",
                    "user_id": "",
                    "interests": ["原神"],
                    "categories": [],
                    "auto_interest": False,
                    "max_share_per_round": 0,
                },
                {
                    "name": "群二",
                    "enabled": True,
                    "stream_id": "stream-2",
                    "group_id": "",
                    "user_id": "",
                    "interests": ["猫咪"],
                    "categories": [],
                    "auto_interest": False,
                    "max_share_per_round": 0,
                },
            ],
        )
        plugin = self.make_plugin(config)
        first = await plugin.run_round(reason="scheduled")
        # 两个会话的兴趣不同，各挑到一条（BV1 / BV2），合计 2 条正好用满额度
        self.assertEqual(first["shared"], 2, first)
        self.assertEqual(first["daily_shared"], 2)
        second = await plugin.run_round(reason="scheduled")
        self.assertEqual(second["skipped"], "daily_limit")

    async def test_skip_chance_one_always_skips(self) -> None:
        """skip_chance=1.0 时每轮必掷到「不看」，且不做任何抓取。"""

        plugin = self.make_plugin(base_config(schedule={"skip_chance": 1.0}))
        summary = await plugin.run_round(reason="scheduled")
        self.assertEqual(summary["skipped"], "dice_skip")
        self.assertEqual(summary["fetched"], 0)
        self.assertEqual(summary["shared"], 0)
        self.assertEqual(self.ctx.send.messages, [])

    async def test_skip_chance_zero_never_skips(self) -> None:
        """skip_chance=0.0 时（默认）不应被骰子拦下。"""

        plugin = self.make_plugin(base_config(schedule={"skip_chance": 0.0}))
        summary = await plugin.run_round(reason="scheduled")
        self.assertNotEqual(summary.get("skipped"), "dice_skip")
        self.assertEqual(summary["shared"], 1)

    async def test_skip_does_not_consume_daily_quota(self) -> None:
        """被骰子跳过的轮次不计入每日额度。"""

        plugin = self.make_plugin(base_config(schedule={"skip_chance": 1.0, "daily_share_max": 3}))
        await plugin.run_round(reason="scheduled")
        self.assertEqual(plugin._daily_shared(), 0)

    async def test_manual_bypasses_gate(self) -> None:
        """手动触发的轮次绕过骰子和每日上限。"""

        plugin = self.make_plugin(
            base_config(schedule={"skip_chance": 1.0, "daily_share_max": 1})
        )
        plugin._bump_daily_shared(5)
        summary = await plugin.run_round(reason="manual", manual=True)
        self.assertNotIn(summary.get("skipped"), {"dice_skip", "daily_limit"})
        self.assertEqual(summary["shared"], 1)

    async def test_counter_increments_after_share(self) -> None:
        """真正分享了才加计数。"""

        plugin = self.make_plugin()
        self.assertEqual(plugin._daily_shared(), 0)
        summary = await plugin.run_round(reason="scheduled")
        self.assertEqual(summary["shared"], 1)
        self.assertEqual(plugin._daily_shared(), 1)
        self.assertEqual(summary["daily_shared"], 1)

    async def test_counter_persists_across_reload(self) -> None:
        """计数写在状态文件里，插件重启后仍然记得。"""

        plugin = self.make_plugin(base_config(schedule={"daily_share_max": 3}))
        await plugin.run_round(reason="scheduled")
        expected = plugin._daily_shared()
        self.assertEqual(expected, 1)

        plugin._store.save()
        reloaded = self.module.SeenStore(
            self.tmp_dir / "state.json", dedupe_days=30, max_records=1000
        )
        reloaded.load()
        self.assertEqual(reloaded.get_meta(plugin._daily_key(), 0), expected)

    async def test_daily_key_is_per_date(self) -> None:
        """计数键按自然日区分，昨天的额度不会算到今天头上。"""

        plugin = self.make_plugin()
        yesterday = plugin._daily_key(now=1794000000.0)
        today = plugin._daily_key()
        self.assertNotEqual(yesterday, today)
        self.assertTrue(yesterday.startswith("daily_shared:"))
        plugin._bump_daily_shared(2, now=1794000000.0)
        self.assertEqual(plugin._daily_shared(now=1794000000.0), 2)
        self.assertEqual(plugin._daily_shared(), 0)


if __name__ == "__main__":
    unittest.main()
