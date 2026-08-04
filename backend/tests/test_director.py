import asyncio
import json
import unittest
from unittest.mock import patch

import game
import llm


def _context():
    return {
        "allowed_reference_ids": [
            "opportunity:ruined_temple_bones",
            "ruined_temple_bones",
            "yin_qi_jue",
        ],
        "facts": [{
            "id": "opportunity:ruined_temple_bones",
            "kind": "opportunity",
            "text": "破庙石像后有旧物痕迹。",
        }],
        "arts": [{"id": "yin_qi_jue", "name": "引气诀", "summary": "凡人入门吐纳法。"}],
        "opportunities": [{
            "id": "ruined_temple_bones",
            "name": "破庙道人遗骨",
            "clue": "破庙石像后有旧物痕迹。",
        }],
        "forbidden_reveals": [],
    }


def _plan(*, action="start", mode="progress", intent="应对山匪", same=False, payoff="combat"):
    route = {
        "combat": "engage",
        "escape": "escape",
        "mystery": "investigate",
        "gain": "acquire",
        "reversal": "other",
    }[payoff]
    return {
        "event_action": action,
        "event_core": "解决山匪造成的直接威胁",
        "turn_objective": "在本轮让玩家行动产生明确结果",
        "turn_mode": mode,
        "route_key": route,
        "interpreted_route": route,
        "intent": {"key": intent, "same_as_previous": same},
        "payoff": {
            "type": payoff,
            "outcome": "完成冲突并取得明确结果",
            "proof": "正文给出明确战果",
            "source_ids": [],
        },
        "facts_to_reveal": [],
        "beats": ["敌人逼近", "主角行动产生结果"],
        "must_not": [],
        "scene": "山路遭遇",
    }


class DirectorPlanTests(unittest.TestCase):
    def test_player_avoidance_replans_payoff_without_resetting_event(self):
        first = game._apply_director_plan({}, _plan(), "迎战", _context(), 1)
        second_raw = _plan(action="continue", intent="避开山匪", payoff="escape")
        second_raw["turn_objective"] = "摆脱追踪并取得安全"
        second_raw["payoff"]["outcome"] = "彻底摆脱山匪"
        second_raw["payoff"]["proof"] = "追兵失去踪迹，主角确认安全"
        second = game._apply_director_plan(first, second_raw, "钻入山林避战", _context(), 2)

        self.assertEqual(second["event"]["id"], first["event"]["id"])
        self.assertEqual(second["event"]["core"], first["event"]["core"])
        self.assertEqual(second["event"]["turns"], 2)
        self.assertEqual(second["current_plan"]["payoff"]["type"], "escape")
        self.assertEqual(second["current_plan"]["turn_objective"], "摆脱追踪并取得安全")
        self.assertEqual(second["current_plan"]["event_action"], "resolve")
        self.assertTrue(any("脱离冲突" in reason for reason in second["current_plan"]["forced_reasons"]))

    def test_second_semantically_same_intent_forces_resolution(self):
        first = game._apply_director_plan({}, _plan(intent="探明玄色小牌"), "细看小牌", _context(), 1)
        second_raw = _plan(action="continue", intent="探明玄色小牌", same=True, payoff="mystery")
        second = game._apply_director_plan(first, second_raw, "继续感应牌中白影", _context(), 2)

        self.assertEqual(second["intent"]["attempts"], 2)
        self.assertEqual(second["current_plan"]["event_action"], "resolve")
        self.assertEqual(second["current_plan"]["turn_mode"], "resolve")
        self.assertTrue(any("2 次" in reason for reason in second["current_plan"]["forced_reasons"]))

    def test_fifth_event_turn_forces_resolution(self):
        state = game._apply_director_plan({}, _plan(), "迎战", _context(), 1)
        for turn in range(2, 6):
            raw = _plan(action="continue", intent=f"不同战术{turn}")
            state = game._apply_director_plan(state, raw, f"行动{turn}", _context(), turn)

        self.assertEqual(state["event"]["turns"], 5)
        self.assertEqual(state["current_plan"]["event_action"], "resolve")
        self.assertTrue(any("第 5 轮" in reason for reason in state["current_plan"]["forced_reasons"]))

    def test_invalid_gain_reference_is_removed(self):
        raw = _plan(payoff="gain")
        raw["payoff"]["source_ids"] = ["invented_heaven_art"]
        raw["arts_to_grant"] = ["invented_heaven_art"]
        state = game._apply_director_plan({}, raw, "获得天外神功", _context(), 1)
        plan = state["current_plan"]

        self.assertEqual(plan["payoff"]["source_ids"], [])
        self.assertEqual(plan["arts_to_grant"], [])
        self.assertEqual(plan["payoff"]["type"], "reversal")

    def test_valid_fixed_art_can_back_gain(self):
        raw = _plan(payoff="gain")
        raw["payoff"]["source_ids"] = ["yin_qi_jue"]
        raw["arts_to_grant"] = ["yin_qi_jue"]
        state = game._apply_director_plan({}, raw, "取得引气诀", _context(), 1)

        self.assertEqual(state["current_plan"]["payoff"]["type"], "gain")
        self.assertEqual(state["current_plan"]["arts_to_grant"], ["yin_qi_jue"])
        self.assertEqual(state["current_plan"]["selected_facts"][0]["id"], "yin_qi_jue")

    def test_conflicting_routes_drop_payoff_world_grants(self):
        raw = _plan(payoff="gain")
        raw["route_key"] = "escape"
        raw["interpreted_route"] = "acquire"
        raw["payoff"]["source_ids"] = ["yin_qi_jue"]
        raw["arts_to_grant"] = ["yin_qi_jue"]
        state = game._apply_director_plan({}, raw, "撤回村中", _context(), 1)

        plan = state["current_plan"]
        self.assertEqual(plan["payoff"]["type"], "escape")
        self.assertEqual(plan["payoff"]["source_ids"], [])
        self.assertEqual(plan["arts_to_grant"], [])
        self.assertEqual(plan["selected_facts"], [])

    def test_planner_timeout_uses_dynamic_fallback(self):
        async def slow_complete(*args, **kwargs):
            await asyncio.sleep(0.05)
            return "{}"

        state = {
            "turns": 1,
            "transcript": [],
            "character_state": {},
            "director_state": {
                "event": {
                    "id": "event-1",
                    "core": "解决山匪威胁",
                    "status": "active",
                    "start_turn": 1,
                    "turns": 1,
                    "max_turns": 5,
                },
                "intent": {"key": "迎战", "attempts": 1},
                "current_plan": None,
            },
        }
        with patch.object(game, "DIRECTOR_PLANNER_TIMEOUT_SECONDS", 0.01), patch.object(
            game, "complete_chat", slow_complete
        ):
            planned = asyncio.run(game._plan_director_turn(state, "撤回村里避战", _context()))

        self.assertEqual(planned["current_plan"]["payoff"]["type"], "escape")
        self.assertEqual(planned["current_plan"]["event_action"], "resolve")
        self.assertEqual(planned["event"]["id"], "event-1")

    def test_fallback_respects_negated_investigation(self):
        event = game._fallback_director_event({}, "不要查看埋好的牌子，回家假装没事")
        payoff = game._fallback_director_payoff("不要查看埋好的牌子，回家假装没事")

        self.assertEqual(payoff["payoff"]["type"], "escape")
        self.assertEqual(event["route_key"], "escape")
        self.assertIn("牌子", event["event_core"])

    def test_memory_refs_are_validated_and_compacted(self):
        raw = _plan()
        raw["memory_refs"] = ["memory-b", "invented-memory"]
        memories = [
            {"id": "memory-b", "type": "plot", "text": "阿七曾在夜里异变。"},
            {"id": "memory-a", "type": "plot", "text": "村外有一块黑牌。"},
        ]
        state = game._apply_director_plan(
            {}, raw, "迎战", _context(), 1, memory_candidates=memories
        )

        plan = state["current_plan"]
        self.assertEqual(plan["memory_refs"], ["memory-b"])
        self.assertEqual(plan["selected_memories"], [memories[0]])

    def test_director_dynamic_message_puts_player_action_last(self):
        state = {
            "turns": 3,
            "transcript": [{"role": "narration", "text": "上一轮正文"}],
            "character_state": {"realm": "凡人", "updated_at": 123.0},
        }
        event_messages, payoff_messages = game._director_parallel_messages(
            state,
            "回家",
            _context(),
            {},
            [{"id": "m1", "type": "plot", "text": "已知事实"}],
        )

        self.assertEqual([m["role"] for m in event_messages], ["system", "user"])
        self.assertEqual([m["role"] for m in payoff_messages], ["system", "system", "user"])
        for messages in (event_messages, payoff_messages):
            self.assertNotIn("updated_at", messages[-1]["content"])
            self.assertLess(messages[-1]["content"].find("最近一轮正文"), messages[-1]["content"].find("玩家本轮行动"))
            self.assertTrue(messages[-1]["content"].rstrip().endswith("【玩家本轮行动】\n回家"))

    def test_event_and_payoff_agents_run_in_parallel_before_pacing(self):
        payoff_started = asyncio.Event()
        calls = []

        async def fake_complete(*args, **kwargs):
            request_type = kwargs["request_type"]
            calls.append(request_type)
            if request_type == "director_event":
                await asyncio.wait_for(payoff_started.wait(), 0.2)
                return json.dumps({
                    "event_action": "start",
                    "event_core": "解决山匪威胁",
                    "turn_mode": "progress",
                    "route_key": "engage",
                    "intent": {"key": "迎战山匪", "same_as_previous": False},
                }, ensure_ascii=False)
            if request_type == "director_payoff":
                payoff_started.set()
                return json.dumps({
                    "interpreted_route": "engage",
                    "payoff": {
                        "type": "combat",
                        "outcome": "击退山匪",
                        "proof": "正文给出明确战果",
                        "source_ids": [],
                    },
                }, ensure_ascii=False)
            self.assertEqual(request_type, "director_pacing")
            return json.dumps({
                "turn_objective": "本轮完成一次明确攻防",
                "beats": ["山匪进逼", "主角应对并产生战果"],
                "scene": "山路遭遇",
            }, ensure_ascii=False)

        state = {
            "turns": 0,
            "transcript": [],
            "character_state": {},
            "director_state": {},
        }
        with patch.object(game, "complete_chat", fake_complete):
            planned = asyncio.run(game._plan_director_turn(state, "迎战山匪", _context()))

        self.assertEqual(set(calls[:2]), {"director_event", "director_payoff"})
        self.assertEqual(calls[2], "director_pacing")
        self.assertEqual(planned["current_plan"]["turn_objective"], "本轮完成一次明确攻防")
        self.assertNotIn("current_goal", planned["current_plan"])

    def test_pacing_receives_backend_forced_resolution(self):
        first = game._apply_director_plan({}, _plan(intent="查明黑牌"), "查看黑牌", _context(), 1)
        raw = _plan(action="continue", intent="查明黑牌", same=True, payoff="mystery")
        second = game._apply_director_plan(
            first, raw, "继续查看黑牌", _context(), 2, advance_scene=False
        )
        state = {"transcript": [{"role": "narration", "text": "黑牌微微发亮。"}]}

        messages = game._director_pacing_messages(state, "继续查看黑牌", second)

        self.assertIn('"event_action":"resolve"', messages[-1]["content"])
        self.assertIn("同一意图已连续尝试 2 次", messages[-1]["content"])

    def test_narrative_plan_is_appended_after_history(self):
        sid = "cache-prefix-test"
        historical = [
            {"role": "system", "content": "fixed"},
            {"role": "user", "content": "old action"},
            {"role": "assistant", "content": "old result"},
        ]
        game._CACHE[sid] = {
            "messages": list(historical),
            "transcript": [],
            "turns": 1,
            "character_state": {},
            "world_memory": [],
            "inventory": [],
            "director_state": {},
            "_injected": [],
        }
        planned = game._apply_director_plan({}, _plan(), "迎战", _context(), 2)

        async def fake_plan(*args, **kwargs):
            return planned

        try:
            with patch.object(game.constraints, "action_constraints", return_value="world"), patch.object(
                game.constraints, "director_context", return_value=_context()
            ), patch.object(game, "_plan_director_turn", fake_plan), patch.object(
                game.store, "save_director_state"
            ) as save_director:
                messages = asyncio.run(game.prepare_action(sid, "迎战"))
                self.assertEqual(game._CACHE[sid]["_pending_director_prev"], {})
                self.assertEqual(game._CACHE[sid]["director_state"], planned)
                save_director.assert_not_called()
                game.rollback_prepared_action(sid)
                self.assertEqual(game._CACHE[sid]["director_state"], {})
                self.assertNotIn("_pending_director_prev", game._CACHE[sid])
        finally:
            game._CACHE.pop(sid, None)

        self.assertEqual(messages[:-1], historical)
        self.assertIn("本轮导演骨架", messages[-1]["content"])
        self.assertTrue(messages[-1]["content"].endswith("【玩家原始行动】\n迎战"))

    def test_audit_prompt_omits_plan_metadata(self):
        plan = _plan()
        plan.update({"plan_id": "random", "note": "long note", "selected_facts": []})
        prompt = game._director_audit_prompt(
            plan,
            "迎战",
            "正文。《状态》\n境界：凡人\n《/状态》",
        )

        self.assertNotIn("plan_id", prompt)
        self.assertNotIn("long note", prompt)
        self.assertIn("境界：凡人", prompt)

    def test_stage_summary_updates_only_on_boundary(self):
        messages = [{"role": "system", "content": "fixed"}]
        for turn in range(1, 21):
            messages.extend([
                {"role": "user", "content": f"行动{turn}"},
                {"role": "assistant", "content": f"结果{turn}"},
            ])
        state = {
            "messages": messages,
            "turns": 20,
            "stage_summary": "",
            "summary_turn": 0,
        }

        game._trim(state)

        self.assertEqual(len(state["messages"]), 1 + game.RECENT_RAW_ROUNDS * 2)
        self.assertEqual(state["summary_turn"], 20)
        self.assertIn("行动1", state["stage_summary"])
        self.assertEqual(state["messages"][1]["content"], "行动5")

    def test_deepseek_cache_usage_is_parsed_tolerantly(self):
        direct = llm._usage_metrics({
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "prompt_cache_hit_tokens": 70,
            "prompt_cache_miss_tokens": 30,
        })
        nested = llm._usage_metrics({
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "prompt_tokens_details": {"cached_tokens": 60},
        })

        self.assertEqual(direct["cache_hit_tokens"], 70)
        self.assertEqual(direct["cache_miss_tokens"], 30)
        self.assertEqual(nested["cache_hit_tokens"], 60)
        self.assertEqual(nested["cache_miss_tokens"], 40)


if __name__ == "__main__":
    unittest.main()
