import os
import tempfile
import unittest

import constraints
import store


class StructuredLocationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_data, self._orig_db = store.DATA_DIR, store.DB_PATH
        store.DATA_DIR = self._tmp.name
        store.DB_PATH = os.path.join(self._tmp.name, "test_saves.db")
        store.init()
        self.sid = store.create("位置测试", [{"role": "system", "content": "x"}])

    def tearDown(self):
        store.DATA_DIR, store.DB_PATH = self._orig_data, self._orig_db
        self._tmp.cleanup()

    def location(self):
        return store.world_snapshot(self.sid)["location"]

    def test_movement_adjudication_records_intent_without_moving(self):
        constraints.action_constraints(self.sid, "去破庙")

        location = self.location()
        self.assertEqual(location["location_id"], "baishi_village")
        self.assertEqual(location["site_name"], "村西老槐树")
        self.assertEqual(location["intended_destination_id"], "baishi_ruined_temple")

    def test_confirmed_arrival_updates_actual_location(self):
        constraints.action_constraints(self.sid, "前往村外破庙")
        constraints.reconcile_location(self.sid, "日落之前，你终于赶到了村外破庙，推门而入。")

        location = self.location()
        self.assertEqual(location["location_id"], "baishi_ruined_temple")
        self.assertEqual(location["site_name"], "村外破庙")
        self.assertEqual(location["location_state"], "安全")
        self.assertIsNone(location["intended_destination_id"])

    def test_postposed_arrival_updates_actual_location(self):
        constraints.reconcile_location(
            self.sid,
            "行至晌午前，前方地平线上渐次浮现一片屋舍轮廓，青溪镇到了。",
        )

        location = self.location()
        self.assertEqual(location["location_id"], "qingxi_town")
        self.assertEqual(location["site_name"], "青溪镇")

    def test_directional_or_planned_postposed_phrase_does_not_move(self):
        constraints.reconcile_location(self.sid, "前往青溪镇方向，沿官道继续赶路。")
        self.assertEqual(self.location()["location_id"], "baishi_village")

        constraints.reconcile_location(self.sid, "青溪镇尚未到达，只能在野外暂歇。")
        self.assertEqual(self.location()["location_id"], "baishi_village")

    def test_intent_or_failed_arrival_does_not_move(self):
        constraints.action_constraints(self.sid, "前往村外破庙")

        constraints.reconcile_location(self.sid, "你沿荒草路寻找村外破庙，打算天黑前进入破庙。")
        self.assertEqual(self.location()["location_id"], "baishi_village")

        constraints.reconcile_location(self.sid, "若进入村外破庙，或许能避开这场雨。")
        self.assertEqual(self.location()["location_id"], "baishi_village")

        constraints.reconcile_location(self.sid, "暴雨阻路，你没能赶到村外破庙，只得原地避雨。")
        self.assertEqual(self.location()["location_id"], "baishi_village")

    def test_short_alias_can_confirm_return_to_fixed_location(self):
        store.update_player_location(
            self.sid,
            region_id="qingwu_county",
            location_id="baishi_ruined_temple",
        )

        constraints.reconcile_location(self.sid, "你循着荒草路返回，入夜前终于回到村中。")

        self.assertEqual(self.location()["location_id"], "baishi_village")

    def test_last_confirmed_arrival_wins(self):
        constraints.reconcile_location(
            self.sid,
            "清晨你来到村外破庙，搜寻无果后，傍晚又回到白石村。",
        )

        self.assertEqual(self.location()["location_id"], "baishi_village")

    def test_unknown_place_is_never_created_or_selected(self):
        constraints.reconcile_location(self.sid, "村民说破庙附近有狼。你终于抵达落霞仙城。")

        self.assertEqual(self.location()["location_id"], "baishi_village")

    def test_confirmed_local_scene_updates_site_without_changing_location(self):
        constraints.reconcile_location(self.sid, "你沿村西小径走了一阵，终于来到村口。")

        location = self.location()
        self.assertEqual(location["location_id"], "baishi_village")
        self.assertEqual(location["site_name"], "白石村村口")

    def test_planned_local_scene_does_not_update_site(self):
        constraints.reconcile_location(self.sid, "你打算明日来到村口，再去青溪镇。")

        self.assertEqual(self.location()["site_name"], "村西老槐树")

    def test_macro_arrival_replaces_stale_local_scene(self):
        constraints.reconcile_location(self.sid, "日落前，你终于赶到村外破庙。")

        location = self.location()
        self.assertEqual(location["location_id"], "baishi_ruined_temple")
        self.assertEqual(location["site_name"], "村外破庙")


if __name__ == "__main__":
    unittest.main()
