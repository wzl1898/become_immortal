import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import constraints
import store


class WorldTimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        store.init()

    def setUp(self):
        self.sid = store.create("世界时间测试", [])

    def tearDown(self):
        store.delete(self.sid)

    def time(self):
        return store.world_snapshot(self.sid)["time"]

    def test_new_save_has_persistent_world_time(self):
        value = constraints.get_world_state(self.sid)["time"]

        self.assertEqual(value["day"], 1)
        self.assertEqual(value["clock"], "15:30")
        self.assertEqual(value["season"], "深秋")
        self.assertEqual(value["calendar_label"], "仙历")

    def test_normal_action_advances_by_action_type(self):
        constraints.reconcile_location(
            self.sid, "你仔细查看了纸页，没有发现新字。", "仔细查看纸页"
        )

        self.assertEqual(self.time()["minute_of_day"], 945)

    def test_explicit_completed_duration_wins(self):
        constraints.reconcile_location(
            self.sid, "你照着口诀打坐了半个时辰，随后睁开眼。", "开始修炼"
        )

        self.assertEqual(self.time()["minute_of_day"], 990)

    def test_planned_duration_does_not_jump_time(self):
        constraints.reconcile_location(
            self.sid, "你计划明日修炼两个时辰，眼下只查看了经页。", "查看经页"
        )

        self.assertEqual(self.time()["minute_of_day"], 945)

    def test_crossing_midnight_increments_day(self):
        store.advance_world_time(self.sid, 9 * 60)

        value = constraints.get_world_state(self.sid)["time"]
        self.assertEqual(value["day"], 2)
        self.assertEqual(value["clock"], "00:30")

    def test_negative_elapsed_time_never_moves_clock_backward(self):
        before = dict(self.time())
        store.advance_world_time(self.sid, -60)

        after = self.time()
        self.assertEqual(after["day"], before["day"])
        self.assertEqual(after["minute_of_day"], before["minute_of_day"])


if __name__ == "__main__":
    unittest.main()
