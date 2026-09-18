import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import constraints
import game
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
        game._CACHE.pop(self.sid, None)
        store.DATA_DIR, store.DB_PATH = self._orig_data, self._orig_db
        self._tmp.cleanup()

    def location(self):
        return store.world_snapshot(self.sid)["location"]

    def reconcile(self, action: str, narrative: str, result: dict) -> bool:
        async def complete(messages, **kwargs):
            self.assertEqual(kwargs["request_type"], "location_state")
            self.assertIn(action, messages[-1]["content"])
            self.assertIn(narrative, messages[-1]["content"])
            return json.dumps(result, ensure_ascii=False)

        with patch.object(game, "complete_chat", complete):
            return asyncio.run(
                game.reconcile_location_state(self.sid, action, narrative, turn=1)
            )

    @staticmethod
    def decision(location_id, site_name, destination_id, reason="语义判定"):
        return {
            "location_id": location_id,
            "site_name": site_name,
            "intended_destination_id": destination_id,
            "reason": reason,
        }

    def test_pre_generation_constraints_do_not_mutate_destination(self):
        constraints.action_constraints(self.sid, "去青溪镇")
        self.assertIsNone(self.location()["intended_destination_id"])

    def test_negated_destination_stays_clear(self):
        action = (
            "我不承认这笔债，先请赵德柱拿出祖辈欠债的借据或书面凭证给我核对，"
            "在看清证据前不交财物，也不答应随他去青溪镇。"
        )
        accepted = self.reconcile(
            action,
            "赵德柱催促你同行，你仍站在老槐树下索要借据。",
            self.decision("baishi_village", "村西老槐树", None),
        )
        self.assertTrue(accepted)
        self.assertEqual(self.location()["location_id"], "baishi_village")
        self.assertIsNone(self.location()["intended_destination_id"])

    def test_affirmative_unfinished_travel_records_destination(self):
        accepted = self.reconcile(
            "去青溪镇",
            "你收拾行装，准备沿官道出发。",
            self.decision("baishi_village", "村西老槐树", "qingxi_town"),
        )
        self.assertTrue(accepted)
        self.assertEqual(self.location()["location_id"], "baishi_village")
        self.assertEqual(self.location()["intended_destination_id"], "qingxi_town")

    def test_completed_arrival_moves_and_clears_destination(self):
        store.set_intended_destination(self.sid, "qingxi_town")
        accepted = self.reconcile(
            "继续赶往青溪镇",
            "日落之前，你终于进入青溪镇。",
            self.decision("qingxi_town", "青溪镇", None),
        )
        self.assertTrue(accepted)
        self.assertEqual(self.location()["location_id"], "qingxi_town")
        self.assertEqual(self.location()["site_name"], "青溪镇")
        self.assertIsNone(self.location()["intended_destination_id"])

    def test_npc_movement_does_not_change_player_state(self):
        accepted = self.reconcile(
            "让赵德柱先走",
            "赵德柱朝青溪镇方向快步离去，你仍留在老槐树下。",
            self.decision("baishi_village", "村西老槐树", None),
        )
        self.assertTrue(accepted)
        self.assertEqual(self.location()["location_id"], "baishi_village")
        self.assertIsNone(self.location()["intended_destination_id"])

    def test_explicit_cancellation_clears_existing_destination(self):
        store.set_intended_destination(self.sid, "qingxi_town")
        self.reconcile(
            "我不去青溪镇，留在老槐树下",
            "你明确拒绝同行，留在原地。",
            self.decision("baishi_village", "村西老槐树", None),
        )
        self.assertIsNone(self.location()["intended_destination_id"])

    def test_invented_location_rejects_entire_decision(self):
        store.set_intended_destination(self.sid, "qingxi_town")
        accepted = self.reconcile(
            "去落霞仙城", "你动身了。", self.decision("invented_city", None, None)
        )
        self.assertFalse(accepted)
        self.assertEqual(self.location()["location_id"], "baishi_village")
        self.assertEqual(self.location()["intended_destination_id"], "qingxi_town")

    def test_invalid_json_or_model_error_preserves_state(self):
        store.set_intended_destination(self.sid, "qingxi_town")

        async def invalid(*args, **kwargs):
            return "not-json"

        with patch.object(game, "complete_chat", invalid):
            accepted = asyncio.run(
                game.reconcile_location_state(self.sid, "取消行程", "你留在原地。", turn=1)
            )
        self.assertFalse(accepted)
        self.assertEqual(self.location()["intended_destination_id"], "qingxi_town")

        async def failed(*args, **kwargs):
            raise RuntimeError("model unavailable")

        with patch.object(game, "complete_chat", failed):
            accepted = asyncio.run(
                game.reconcile_location_state(self.sid, "取消行程", "你留在原地。", turn=1)
            )
        self.assertFalse(accepted)
        self.assertEqual(self.location()["intended_destination_id"], "qingxi_town")

    def test_commit_calls_location_model_once_and_model_failure_still_commits(self):
        calls = []

        async def failed(*args, **kwargs):
            calls.append(kwargs.get("request_type"))
            raise RuntimeError("model unavailable")

        with patch.object(game, "complete_chat", failed), patch.object(
            game, "_schedule_memory_extraction"
        ), patch.object(game, "_schedule_narrative_observer"), patch.object(
            game, "_schedule_director_audit"
        ):
            asyncio.run(game.commit(self.sid, "留在原地", "你没有移动。"))

        self.assertEqual(calls, ["location_state"])
        self.assertEqual(store.load(self.sid)["turns"], 1)
        self.assertEqual(self.location()["location_id"], "baishi_village")

    def test_fixed_world_contains_ordered_cultivation_demographics(self):
        tiers = store.world_snapshot(self.sid)["cultivation_demographics"]
        self.assertEqual(
            [row["name"] for row in tiers],
            ["凡人/未入修行", "炼气一至三层", "炼气四至六层", "炼气七至九层", "筑基", "金丹", "元婴"],
        )
        late_qi = next(row for row in tiers if row["id"] == "qi_late")
        self.assertEqual(late_qi["rarity"], "罕见")
        self.assertIn("药店老板", late_qi["npc_rule"])

    def test_all_world_constraints_include_npc_realm_rarity(self):
        blocks = (
            constraints.opening_constraints(self.sid),
            constraints.action_constraints(self.sid, "去药店问问"),
            constraints.inquiry_constraints(self.sid),
        )
        for block in blocks:
            self.assertIn("固定人员能力分布", block)
            self.assertIn("炼气七至九层（罕见）", block)
            self.assertIn("药店老板", block)
        context = constraints.director_context(self.sid, "去药店问问")
        self.assertEqual(context["cultivation_demographics"][3]["id"], "qi_late")


if __name__ == "__main__":
    unittest.main()
