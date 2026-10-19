"""Pure-logic checks: moderation, intent routing and queue policy (stdlib only)."""
from __future__ import annotations

import random
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from live.moderation import Moderator  # noqa: E402
from live.queue import CommandQueue  # noqa: E402
from live.router import classify  # noqa: E402


class ModerationTest(unittest.TestCase):
    def setUp(self):
        self.m = Moderator.from_file(ROOT / "moderation.json")

    def test_blocks_suggestive_and_lead_gen(self):
        for text, category in [("站起来看下身材", "低俗擦边"), ("加我微信", "引流广告"), ("V信多少", "引流广告"),
                               ("穿个黑丝", "低俗擦边"), ("带我赚钱", "收益承诺"), ("让我摸一下", "低俗擦边")]:
            verdict = self.m.check("u", text, now=0)
            self.assertFalse(verdict.ok, text)
            self.assertEqual(verdict.category, category, text)

    def test_allows_normal_commands_including_colours(self):
        for text in ["换件衣服", "把头发染成雾霾蓝色", "背景换成海边", "电脑需要什么配置?", "红色的裙子", "你是真人吗", "今天又摸鱼了"]:
            self.assertTrue(self.m.check("u", text, now=0).ok, text)

    def test_dedupe_and_length(self):
        self.assertTrue(self.m.check("a", "牛啊", now=0).ok)
        self.assertFalse(self.m.check("a", "牛啊!", now=10).ok)
        self.assertTrue(self.m.check("b", "牛啊", now=10).ok)
        self.assertTrue(self.m.check("a", "牛啊", now=100).ok)
        self.assertFalse(self.m.check("a", "长" * 41, now=0).ok)
        self.assertFalse(self.m.check("a", "😂😂😂", now=0).ok)


class RouterTest(unittest.TestCase):
    CASES = [
        ("拿出一只烤鸭 吃烤鸡", "object", "烤鸭"),
        ("换件衣服", "garment", None),
        ("把头发染雾霾蓝色", "garment", "雾霾蓝色头发"),
        ("拿出一件东北花棉袄穿上东北花棉袄", "garment", "东北花棉袄"),
        ("戴上一个淑女帽", "garment", "淑女帽"),
        ("换一件白色的衬衫", "garment", "白色的衬衫"),
        ("穿个熊猫装", "garment", "熊猫装"),
        ("弄个丸子头", "garment", "丸子头"),
        ("换白T", "garment", "白T"),
        ("换件白T恤", "garment", "白T恤"),
        ("穿开衫", "garment", "开衫"),
        ("换个高领", "garment", "高领"),
        ("换高领毛衣", "garment", "高领毛衣"),
        ("换黑色长袖", "garment", "黑色长袖"),
        ("穿短袖", "garment", "短袖"),
        ("换睡衣", "garment", "睡衣"),
        ("换吊带", "garment", "吊带"),
        ("你的高领好看", "chat", None),
        ("换衣服要等多久", "chat", None),
        ("换装有冷却时间吗", "chat", None),
        ("你有几套衣服", "chat", None),
        ("能换件白T吗", "garment", "白T"),
        ("背景换成海边日落", "background", "海边日落"),
        ("换个背景", "background", None),
        ("去海边吧", "background", "海边"),
        ("喝一杯奶茶", "object", "奶茶"),
        ("举起一个大西瓜", "object", "大西瓜"),
        ("换回来", "reset", None),
        ("跳个舞", "action", None),
        ("比个心", "action", None),
        ("电脑需要什么配置?", "chat", None),
        ("你吃饭了吗", "chat", None),
        ("你的头发真好看", "chat", None),
        ("我的帽子呢", "chat", None),
        ("你是真人吗", "chat", None),
        ("来一杯奶茶", "object", "奶茶"),
        ("来个动作", "action", None),
        ("我需要一个解释", "chat", None),
        ("表演倒立洗头", "chat", None),
        ("给文少跳个舞", "action", None),
    ]

    def test_compound_orders_merge_visual_parts(self):
        cmd = classify("u", "教室背景，穿校服")
        self.assertEqual([(p.kind, p.item) for p in cmd.parts], [("background", "教室"), ("garment", "校服")])
        single = classify("u", "哈哈，换件衣服")
        self.assertEqual((single.kind, single.parts), ("garment", []))
        car = classify("u", "你需要一辆特斯拉最新跑车roadster，来一辆")
        self.assertEqual((car.kind, car.item), ("object", "特斯拉最新跑车roadster"))

    def test_cases(self):
        for text, kind, item in self.CASES:
            cmd = classify("u", text)
            self.assertEqual((cmd.kind, cmd.item), (kind, item), text)

    def test_action_text_and_gift_only(self):
        self.assertEqual(classify("u", "跳个舞").action_text, "开心地跳一小段舞")
        self.assertEqual(classify("u", "", gift=10).kind, "gift_thanks")


class QueueTest(unittest.TestCase):
    def test_gift_jumps_line_then_random_recent(self):
        q = CommandQueue("gift_first_random", rng=random.Random(1))
        a, b, c = classify("a", "你好"), classify("b", "跳个舞"), classify("c", "比个心", gift=5)
        for cmd in (a, b, c):
            q.push(cmd)
        self.assertIs(q.pick(), c)
        self.assertIn(q.pick(), (a, b))
        self.assertEqual(len(q.items), 1)

    def test_fifo_skips_preparing_and_expires_old(self):
        q = CommandQueue("fifo", max_age_seconds=60)
        old, prep, new = classify("a", "你好"), classify("b", "戴上一个棒球帽"), classify("c", "跳个舞")
        old.created_at -= 120
        prep.status = "preparing"
        for cmd in (old, prep, new):
            q.push(cmd)
        self.assertIs(q.pick(), new)
        self.assertEqual(old.status, "expired")
        self.assertIsNone(q.pick())
        prep.status = "ready"
        self.assertIs(q.pick(), prep)

    def test_full_queue_never_drops_gifts(self):
        q = CommandQueue("fifo", max_size=2)
        gift, x, y = classify("g", "你好", gift=1), classify("x", "你好"), classify("y", "你好")
        for cmd in (gift, x, y):
            q.push(cmd)
        self.assertIn(gift, q.items)
        self.assertEqual(len(q.items), 2)


if __name__ == "__main__":
    unittest.main()
