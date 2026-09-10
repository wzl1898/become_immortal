import asyncio
import copy
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import constraints
import game
import main
import prompts
import store
import story_cards


class StoryCardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = patch.object(store, "DATA_DIR", self.tmp.name)
        self.db = patch.object(
            store, "DB_PATH", os.path.join(self.tmp.name, "saves.db")
        )
        self.data.start()
        self.db.start()
        game._CACHE.clear()
        store.init()
        self.client = TestClient(main.app)
        self.headers = {"X-User-ID": "card-test"}

    def tearDown(self):
        self.client.close()
        game._CACHE.clear()
        self.db.stop()
        self.data.stop()
        self.tmp.cleanup()

    def create(self, card_id="orbital"):
        return game.create_session(user_id="card-test", story_card_id=card_id)

    def test_card_catalog_and_creation_do_not_reveal_hidden_world(self):
        response = self.client.get("/api/story-cards", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        cards = response.json()["story_cards"]
        self.assertEqual({c["id"] for c in cards}, {"orbital", "xiuxian"})
        for card in cards:
            self.assertNotIn("world", card)
            self.assertNotIn("prompts", card)
        result = self.client.post(
            "/api/new", headers=self.headers, json={"story_card_id": "orbital"}
        )
        self.assertEqual(result.status_code, 200)
        self.assertEqual(
            result.json()["world_state"]["location"]["location_name"], "维修舱"
        )
        self.assertNotIn("prompts", result.json()["story_card"])
        self.assertEqual(self.client.get("/api/story-cards").status_code, 422)
        self.assertEqual(
            self.client.get(
                f"/api/load?sid={result.json()['session_id']}",
                headers={"X-User-ID": "another-user"},
            ).status_code,
            404,
        )

    def test_invalid_card_does_not_create_save(self):
        before = store.list_saves("card-test")
        result = self.client.post(
            "/api/new", headers=self.headers, json={"story_card_id": "../xiuxian"}
        )
        self.assertEqual(result.status_code, 400)
        self.assertEqual(store.list_saves("card-test"), before)

    def test_world_and_reward_candidates_are_isolated(self):
        a, b = self.create("xiuxian"), self.create("orbital")
        first, second = constraints.director_context(
            a, "观察"
        ), constraints.director_context(b, "观察")
        self.assertEqual(store.world_snapshot(a)["location"]["location_name"], "白石村")
        self.assertEqual(store.world_snapshot(b)["location"]["location_name"], "维修舱")
        self.assertIn("yin_qi_jue", {r["id"] for r in first["reward_candidates"]})
        self.assertNotIn("yin_qi_jue", {r["id"] for r in second["reward_candidates"]})
        self.assertTrue(second["characters"])
        self.assertTrue(second["items"])
        binding = game._resolve_payoff_binding(
            {"desc": "获得便携维修无人机", "trigger": "完成检查并办理领用"}, second
        )
        self.assertEqual(binding["reward_kind"], "equipment")
        self.assertIsNone(
            game._resolve_payoff_binding(
                {"desc": "获得引气诀", "trigger": "完成检查"}, second
            )
        )
        constraints.reconcile_location(b, "你抵达中央大厅。", "去中央大厅")
        self.assertEqual(
            store.world_snapshot(b)["location"]["location_name"], "中央大厅"
        )
        self.assertEqual(store.world_snapshot(a)["location"]["location_name"], "白石村")

    def test_snapshot_survives_reload_and_catalog_change_or_removal(self):
        sid = self.create()
        pinned = store.load(sid)["story_card"]
        with patch.dict(story_cards._CARDS, {}, clear=True):
            game._CACHE.clear()
            self.assertEqual(game.get_story_card_info(sid)["name"], "星港余波")
            self.assertEqual(game._get(sid)["story_card"], pinned)
            self.assertIn("晨曦轨道站", constraints.opening_constraints(sid))
            self.assertNotIn("玄苍大陆", constraints.opening_constraints(sid))

    def test_two_cards_can_reuse_entity_ids_without_changing_old_world(self):
        original = story_cards.get("orbital")
        edited = copy.deepcopy(original)
        edited["id"] = "another_station"
        edited["world"]["locations"][0]["name"] = "第二维修区"
        first = store.create("甲", [], story_card=original)
        second = store.create("乙", [], story_card=edited)
        self.assertEqual(
            store.world_snapshot(first)["location"]["location_name"], "维修舱"
        )
        self.assertEqual(
            store.world_snapshot(second)["location"]["location_name"], "第二维修区"
        )

    def test_state_and_inventory_follow_card_fields_after_commit_and_reload(self):
        sid = self.create()
        narration = "你完成检查。\n《状态》\n岗位：初级维修员\n健康：手指擦伤\n体力：略有消耗\n技能：基础电气检修\n状态：清醒\n补给：饮水 1 袋\n装备：多功能维修工具（绝缘握柄，可测电压）、便携维修无人机（折叠机身）\n《/状态》"
        with patch.object(game, "_schedule_memory_extraction"), patch.object(
            game, "_schedule_narrative_observer"
        ), patch.object(game, "_schedule_director_audit"):
            game.commit(sid, "检查设备", narration)
        game._CACHE.clear()
        state = game.get_character_state(sid)
        self.assertEqual(state["health"], "手指擦伤")
        self.assertEqual(state["energy"], "略有消耗")
        self.assertNotIn("realm", state)
        self.assertNotIn("spiritual_power", state)
        self.assertIn("便携维修无人机", {r["name"] for r in game.get_inventory(sid)})
        data = self.client.get(f"/api/load?sid={sid}", headers=self.headers).json()
        self.assertEqual(data["story_card"]["status_fields"][0]["label"], "岗位")

    def test_state_reconciliation_uses_selected_card_schema(self):
        sid = self.create()

        async def complete(messages, **kwargs):
            text = messages[-1]["content"]
            self.assertIn('"energy":"体力"', text)
            self.assertNotIn("灵力", text)
            return json.dumps(
                {
                    "updates": {"energy": "疲惫", "spiritual_power": "充满"},
                    "evidence": {
                        "energy": "你感到疲惫。",
                        "spiritual_power": "你感到疲惫。",
                    },
                }
            )

        with patch.object(game, "complete_chat", complete):
            asyncio.run(game.reconcile_character_state_from_text(sid, "你感到疲惫。"))
        state = game.get_character_state(sid)
        self.assertEqual(state["energy"], "疲惫")
        self.assertNotIn("spiritual_power", state)

    def test_legacy_migration_preserves_history_location_and_custom_world(self):
        sid = self.create("xiuxian")
        history = [{"role": "narration", "text": "旧剧情，不得改写。"}]
        with store._conn() as conn:
            conn.execute(
                "UPDATE saves SET story_card='{}', transcript=?, turns=6 WHERE id=?",
                (json.dumps(history), sid),
            )
            conn.execute(
                "UPDATE world_locations SET summary='已有自定义设定' WHERE id='baishi_village'"
            )
            conn.execute(
                "UPDATE save_player_location SET site_name='旧存档现场' WHERE save_id=?",
                (sid,),
            )
        store.init()
        first = store.load(sid)
        store.init()
        second = store.load(sid)
        self.assertEqual(first, second)
        self.assertEqual(first["transcript"], history)
        self.assertEqual(first["turns"], 6)
        snap = store.world_snapshot(sid)
        self.assertEqual(snap["location"]["site_name"], "旧存档现场")
        self.assertEqual(snap["location"]["location_summary"], "已有自定义设定")

    def test_validation_rejects_broken_references_and_duplicate_fields(self):
        for mutation in [
            lambda c: c["world"]["routes"][0].update(to_location_id="missing"),
            lambda c: c["initial"]["location"].update(region_id="missing"),
            lambda c: c["initial"]["inventory"].append("missing"),
            lambda c: c["status_fields"].append(dict(c["status_fields"][0])),
            lambda c: c["world"]["rewards"][0].update(source_location_id="missing"),
        ]:
            card = story_cards.get("orbital")
            mutation(card)
            with self.assertRaises(ValueError):
                store.create("bad card", [], story_card=card)
        self.assertEqual(store.list_saves(), [])

    def test_concurrent_agent_flows_do_not_leak_another_card(self):
        a, b = self.create("xiuxian"), self.create("orbital")
        captured = []

        async def complete(messages, **kwargs):
            sid, kind = kwargs["session_id"], kwargs["request_type"]
            captured.append((sid, kind, messages))
            if sid == b:
                text = json.dumps(messages, ensure_ascii=False)
                for token in (
                    "修仙",
                    "玄苍",
                    "白石村",
                    "炼气",
                    "灵力",
                    "引气诀",
                    "法宝",
                ):
                    self.assertNotIn(token, text, f"{kind} leaked {token}")
            outputs = {
                "character_setting": {
                    "reason": "本地发生故障",
                    "attitude": "谨慎",
                    "goal": "检查异常",
                },
                "guidance_conflict": {
                    "conflict_seed": "值班负责人要求核对记录，暂缓离开。"
                },
                "director_event": {
                    "title": "记录差异",
                    "core": "值班负责人正在核对记录",
                    "benefit": "确认当前问题",
                    "end_condition": "差异已查明",
                },
                "director_payoff": {"desc": "", "trigger": ""},
                "director_pacing": {
                    "intent": {"key": "观察", "same_as_previous": False},
                    "resolved": True,
                },
                "director_progression": {
                    "reason": "核对记录",
                    "direction": "确认差异",
                    "ended": False,
                },
                "director_hook": {"goal": "询问记录来源"},
                "director_skeleton": {
                    "turn_objective": "回应观察",
                    "beats": ["出现可确认的信息"],
                    "scene": "当前地点",
                    "scene_change": False,
                },
            }
            if kind in {"director_viewpoint", "director_causal"}:
                return "# 已确认事实\n当事人位于当前地点，正在核对记录。"
            return json.dumps(outputs[kind], ensure_ascii=False)

        async def scenario():
            with patch.object(game, "complete_chat", complete):
                messages = await asyncio.gather(
                    game.prepare_opening(a), game.prepare_opening(b)
                )
                tasks = list(game._CAUSAL_TASKS.values())
                if tasks:
                    await asyncio.gather(*tasks)
                self.assertIn("晨曦轨道站", json.dumps(messages[1], ensure_ascii=False))
                self.assertNotIn("修仙", json.dumps(messages[1], ensure_ascii=False))
                await game.prepare_action(b, "观察记录")

        asyncio.run(scenario())
        for sid, kind, messages in captured:
            if sid == b:
                for token in (
                    "修仙",
                    "玄苍",
                    "白石村",
                    "炼气",
                    "灵力",
                    "引气诀",
                    "法宝",
                ):
                    self.assertNotIn(
                        token, json.dumps(messages, ensure_ascii=False), kind
                    )
        self.assertTrue(
            {
                "director_event",
                "director_causal",
                "director_viewpoint",
                "director_pacing",
                "director_progression",
                "director_hook",
                "director_skeleton",
                "director_payoff",
                "guidance_conflict",
            }.issubset({kind for sid, kind, _ in captured if sid == b})
        )
