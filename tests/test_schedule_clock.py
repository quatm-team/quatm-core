# -*- coding: utf-8 -*-
"""``quatm.utils.schedule_clock`` 纯函数测试（task-56）。

覆盖：slot key 的分钟精度与稳定性、同一 slot 不重复触发、不同 slot 正常触发、
时钟回拨（now 往回走）后同一 slot 仍不触发、跨日/跨年不同 slot、非法 key 解析、
``slot_seconds`` 的正负与容错、``dedupe_key`` 的 target/now 选择。

约定见 ``tests/__init__.py``：stdlib unittest，不依赖 Qt/时区数据库/网络。
"""

import math
import unittest
from datetime import datetime, timedelta

from quatm.utils.schedule_clock import (SLOT_TIME_FORMAT, dedupe_key,
                                        parse_slot_key, should_fire, slot_key,
                                        slot_seconds)


class TestSlotKey(unittest.TestCase):
    """key 生成/解析：分钟精度、跨日、非法输入。"""

    def test_minute_precision_and_stability(self):
        """同一分钟内不同秒数 → 同一个 key（重复 tick 不会重复触发）。"""
        base = datetime(2026, 11, 5, 3, 0, 0)
        self.assertEqual(slot_key('terminate', base),
                         slot_key('terminate', base + timedelta(seconds=59)))
        self.assertEqual(slot_key('terminate', base),
                         'terminate:2026-11-05T03:00')
        self.assertEqual(SLOT_TIME_FORMAT, '%Y-%m-%dT%H:%M')

    def test_different_kind_or_day_is_different_slot(self):
        """kind 不同/日期不同 → 不同 slot（正常触发，不被误去重）。"""
        a = datetime(2026, 11, 5, 3, 0)
        self.assertNotEqual(slot_key('terminate', a),
                            slot_key('start', a))
        self.assertNotEqual(slot_key('terminate', a),
                            slot_key('terminate', a + timedelta(days=1)))

    def test_parse_roundtrip(self):
        """key → (kind, datetime) 可逆；秒数被规整为分钟。"""
        target = datetime(2026, 11, 5, 3, 0)
        key = slot_key('h5save', target)
        kind, parsed = parse_slot_key(key)
        self.assertEqual(kind, 'h5save')
        self.assertEqual(parsed, target)

    def test_invalid_inputs(self):
        """非法 kind/target/key：不抛异常，返回空串或 None。"""
        self.assertEqual(slot_key('', datetime(2026, 11, 5)), '')
        self.assertEqual(slot_key('terminate', None), '')
        self.assertEqual(slot_key('terminate', 'not-a-date'), '')
        for bad in (None, '', 'no-colon', ':2026-11-05T03:00',
                    'terminate:', 'terminate:not-a-date', 123):
            self.assertIsNone(parse_slot_key(bad), bad)

    def test_accepts_isoformat_string_target(self):
        """target 允许 ISO 字符串（调用方从文件读回时常见）。"""
        self.assertEqual(slot_key('terminate', '2026-11-05T03:00'),
                         'terminate:2026-11-05T03:00')


class TestShouldFire(unittest.TestCase):
    """触发判定：已触发/未到点/时钟回拨/跨日。"""

    def setUp(self):
        self.target = datetime(2026, 11, 5, 3, 0)
        self.key = slot_key('terminate', self.target)

    def test_fires_first_time_when_due(self):
        """从未触发且已到点 → True。"""
        self.assertTrue(should_fire(None, 'terminate', self.target,
                                    self.target + timedelta(seconds=20)))

    def test_same_slot_second_time_is_false(self):
        """同一 slot 已触发过 → False（连续 tick 只执行一次）。"""
        self.assertFalse(should_fire(self.key, 'terminate', self.target,
                                     self.target + timedelta(minutes=1)))

    def test_clock_rolled_back_same_slot_still_false(self):
        """时钟往回拨（now 早于上次触发时刻）后同一 slot 仍 False。"""
        self.assertFalse(should_fire(self.key, 'terminate', self.target,
                                     self.target - timedelta(hours=2)))

    def test_not_due_yet_is_false(self):
        """未到点不提前触发（now < target）。"""
        self.assertFalse(should_fire(None, 'terminate', self.target,
                                     self.target - timedelta(seconds=1)))

    def test_next_day_is_new_slot(self):
        """跨日同一时刻是新 slot → True（每天照常触发一次）。"""
        nxt = self.target + timedelta(days=1)
        self.assertTrue(should_fire(self.key, 'terminate', nxt, nxt))

    def test_without_now_skips_due_check(self):
        """now=None：只回答“这个 slot 触发过没有”（调用方已自行判断到点）。"""
        self.assertTrue(should_fire(None, 'terminate', self.target))
        self.assertFalse(should_fire(self.key, 'terminate', self.target))

    def test_dst_like_repeated_local_time_uses_same_key(self):
        """DST 回拨会让同一本地时间出现两次：naive 时间相同 → 同 key → 只触发一次。"""
        first = datetime(2026, 10, 25, 2, 30)
        second = datetime(2026, 10, 25, 2, 30)   # 时钟回拨后的同一个本地时刻
        self.assertEqual(slot_key('terminate', first),
                         slot_key('terminate', second))
        self.assertFalse(should_fire(slot_key('terminate', first),
                                     'terminate', second, second))


class TestSlotSecondsAndDedupeKey(unittest.TestCase):
    """辅助函数：剩余秒数、触发时写入的 key。"""

    def test_slot_seconds_sign(self):
        """target 在未来为正、过去为负、相等为 0。"""
        now = datetime(2026, 11, 5, 3, 0)
        self.assertEqual(slot_seconds(now, now + timedelta(seconds=30)), 30.0)
        self.assertEqual(slot_seconds(now, now - timedelta(minutes=1)), -60.0)
        self.assertEqual(slot_seconds(now, now), 0.0)

    def test_slot_seconds_bad_input_is_nan(self):
        """非法输入返回 NaN（不抛异常进 GUI 定时器）。"""
        self.assertTrue(math.isnan(slot_seconds(None, datetime.now())))
        self.assertTrue(math.isnan(slot_seconds(datetime.now(), 'bad')))

    def test_dedupe_key_prefers_target(self):
        """有 target 用 target；否则用 now；都没有用当前时间。"""
        target = datetime(2026, 11, 5, 3, 0)
        now = datetime(2026, 11, 5, 2, 0)
        self.assertEqual(dedupe_key('terminate', target, now),
                         slot_key('terminate', target))
        self.assertEqual(dedupe_key('terminate', None, now),
                         slot_key('terminate', now))
        auto = dedupe_key('h5save')
        self.assertTrue(auto.startswith('h5save:'))


if __name__ == '__main__':
    unittest.main()
