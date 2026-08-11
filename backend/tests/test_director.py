import asyncio
import json
import unittest
from unittest.mock import patch

import game
import llm


def _context():
    return {
        "location": {
            "region_id": "qingwu_county",
            "region_name": "青梧郡",
            "location_id": "baishi_village",
            "location_name": "白石村",
            "site_name": "村口老槐树",
            "location_state": "village",
            "lost_risk": "none",
        },
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
        "reward_candidates": [{
            "id": "yin_qi_jue",
            "name": "引气诀",
            "reward_kind": "art",
            "summary": "凡人入门吐纳法。",
        }],
        "existing_reward_bindings": [],
        "opportunities": [{
            "id": "ruined_temple_bones",
            "name": "破庙道人遗骨",
            "clue": "破庙石像后有旧物痕迹。",
        }],
        "forbidden_reveals": [],
    }


def _plan(*, action="start", mode="progress", intent="应对山匪", same=False, route="engage"):
    return {
        "event_action": action,
        "turn_mode": mode,
        "route_key": route,
        "intent": {"key": intent, "same_as_previous": same},
        "stage": "山匪封路",
        "progress": "玩家行动在山路冲突中产生明确结果",
        "reveal_boundary": "玩家只确认眼前山匪的行动",
    }


def _event(*, status="offered", turns=0):
    return {
        "id": "event-1",
        "title": "山路劫掠",
        "core": "山匪正在截断白石村通往县城的山路",
        "status": status,
        "created_turn": 1,
        "turns": turns,
        "max_turns": 5,
        "causal_model": "# 幕后事实\n\n黑风寨山匪赵横因缺少粮食拦截白石村行人。",
        "viewpoint_model": "# 主角视角\n\n主角位于白石村村口，知道通往县城的山路近期不安全。",
    }


def _director(*, status="offered", turns=0):
    return {"event": _event(status=status, turns=turns), "agent_outputs": {}}


class DirectorPlanTests(unittest.TestCase):
    def test_player_avoidance_replans_event_without_resetting_it(self):
        first = game._apply_director_plan(_director(), _plan(), "迎战", _context(), 1)
        second_raw = _plan(action="continue", intent="避开山匪", route="escape")
        second_raw["turn_objective"] = "摆脱追踪并取得安全"
        second = game._apply_director_plan(first, second_raw, "钻入山林避战", _context(), 2)

        self.assertEqual(second["event"]["id"], first["event"]["id"])
        self.assertEqual(second["event"]["core"], first["event"]["core"])
        self.assertEqual(second["event"]["turns"], 2)
        self.assertEqual(second["current_plan"]["turn_objective"], "摆脱追踪并取得安全")
        self.assertEqual(second["current_plan"]["event_action"], "resolve")
        self.assertTrue(any("脱离冲突" in reason for reason in second["current_plan"]["forced_reasons"]))

    def test_second_semantically_same_intent_forces_resolution(self):
        first = game._apply_director_plan(
            _director(), _plan(intent="探明玄色小牌"), "细看小牌", _context(), 1
        )
        second_raw = _plan(action="continue", intent="探明玄色小牌", same=True, route="investigate")
        second = game._apply_director_plan(first, second_raw, "继续感应牌中白影", _context(), 2)

        self.assertEqual(second["intent"]["attempts"], 2)
        self.assertEqual(second["current_plan"]["event_action"], "resolve")
        self.assertEqual(second["current_plan"]["turn_mode"], "resolve")
        self.assertTrue(any("2 次" in reason for reason in second["current_plan"]["forced_reasons"]))

    def test_fifth_event_turn_forces_resolution(self):
        state = game._apply_director_plan(_director(), _plan(), "迎战", _context(), 1)
        for turn in range(2, 6):
            raw = _plan(action="continue", intent=f"不同战术{turn}")
            state = game._apply_director_plan(state, raw, f"行动{turn}", _context(), turn)

        self.assertEqual(state["event"]["turns"], 5)
        self.assertEqual(state["current_plan"]["event_action"], "resolve")
        self.assertTrue(any("第 5 轮" in reason for reason in state["current_plan"]["forced_reasons"]))

    def test_new_payoff_has_only_agent_text_plus_internal_lifecycle(self):
        payoff, last = game._reconcile_payoff_state({}, {
            "desc": "获得破庙道人留下的入道机缘",
            "trigger": "亲自进入破庙并检查石像后的遗骨",
            "extra": "ignored",
        }, 4)

        self.assertEqual(payoff["desc"], "获得破庙道人留下的入道机缘")
        self.assertEqual(payoff["trigger"], "亲自进入破庙并检查石像后的遗骨")
        self.assertEqual(payoff["status"], "pending")
        self.assertEqual(payoff["created_turn"], 4)
        self.assertNotIn("extra", payoff)
        self.assertIsNone(last)

    def test_pending_payoff_is_preserved_when_agent_repeats_it(self):
        first, _ = game._reconcile_payoff_state({}, {
            "desc": "获得破庙道人留下的入道机缘",
            "trigger": "检查石像后的遗骨",
        }, 4)
        second, _ = game._reconcile_payoff_state({"payoff_state": first}, {
            "desc": first["desc"],
            "trigger": first["trigger"],
        }, 5)

        self.assertEqual(second["id"], first["id"])
        self.assertEqual(second["created_turn"], 4)

    def test_recent_story_inference_replaces_triggered_payoff(self):
        first, _ = game._reconcile_payoff_state({}, {
            "desc": "获得破庙道人留下的入道机缘",
            "trigger": "检查石像后的遗骨",
        }, 4)
        second, last = game._reconcile_payoff_state({"payoff_state": first}, {
            "desc": "获得玄霄宗正式弟子身份",
            "trigger": "通过三月考较",
        }, 8)

        self.assertNotEqual(second["id"], first["id"])
        self.assertEqual(last["status"], "triggered")
        self.assertEqual(last["trigger_source"], "payoff_agent_recent_story")

    def test_two_field_payoff_binds_existing_opportunity_and_reward(self):
        payoff, _ = game._reconcile_payoff_state({}, {
            "desc": "通过「破庙道人遗骨」获得「引气诀」",
            "trigger": "亲自检查破庙道人遗骨中的旧物",
        }, 4, _context())
        facts = game._payoff_selected_facts(payoff, _context())

        self.assertEqual(payoff["binding"]["opportunity_id"], "ruined_temple_bones")
        self.assertEqual(payoff["binding"]["reward_id"], "yin_qi_jue")
        self.assertEqual({row["id"] for row in facts}, {"ruined_temple_bones", "yin_qi_jue"})

    def test_payoff_with_invented_reward_is_rejected(self):
        payoff, last = game._reconcile_payoff_state({}, {
            "desc": "通过「破庙道人遗骨」获得「太古飞仙经」",
            "trigger": "检查遗骨",
        }, 4, _context())

        self.assertIsNone(payoff)
        self.assertIsNone(last)

    def test_previously_bound_opportunity_cannot_be_reassigned(self):
        context = _context()
        context["existing_reward_bindings"] = [{
            "opportunity_id": "ruined_temple_bones",
            "reward_kind": "art",
            "reward_id": "another_art",
            "status": "triggered",
        }]
        payoff, _ = game._reconcile_payoff_state({}, {
            "desc": "通过「破庙道人遗骨」获得「引气诀」",
            "trigger": "检查遗骨",
        }, 6, context)

        self.assertIsNone(payoff)

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
                "payoff_state": {
                    "id": "payoff-1",
                    "desc": "通过「破庙道人遗骨」获得「引气诀」",
                    "trigger": "检查石像后的遗骨",
                    "status": "pending",
                    "created_turn": 1,
                    "binding": {
                        "opportunity_id": "ruined_temple_bones",
                        "opportunity_name": "破庙道人遗骨",
                        "reward_kind": "art",
                        "reward_id": "yin_qi_jue",
                        "reward_name": "引气诀",
                    },
                },
            },
        }
        with patch.object(game, "DIRECTOR_PLANNER_TIMEOUT_SECONDS", 0.01), patch.object(
            game, "complete_chat", slow_complete
        ):
            planned = asyncio.run(game._plan_director_turn(state, "撤回村里避战", _context()))

        self.assertEqual(planned["current_plan"]["payoff"]["id"], "payoff-1")
        self.assertEqual(planned["current_plan"]["event_action"], "resolve")
        self.assertEqual(planned["event"]["id"], "event-1")

    def test_fallback_respects_negated_investigation(self):
        event = game._fallback_director_pacing(
            _director(), "不要查看埋好的牌子，回家假装没事"
        )

        self.assertEqual(event["route_key"], "escape")
        self.assertEqual(event["event_action"], "none")

    def test_old_immediate_payoff_is_not_migrated_as_maintained_payoff(self):
        state = game._dynamic_director_state({
            "event": None,
            "current_plan": {"payoff": {"type": "mystery", "outcome": "查明真相"}},
        })

        self.assertIsNone(state["payoff_state"])

    def test_legacy_premise_is_removed_from_normalized_event(self):
        state = game._dynamic_director_state({
            "event": {
                "id": "legacy",
                "core": "当前局面",
                "premise": "旧事件前提",
                "cognition_model": "# 旧主角认知",
            },
            "current_plan": None,
            "agent_outputs": {"cognition": {"source": "llm", "output": "# 旧主角认知"}},
        })

        self.assertNotIn("premise", state["event"])
        self.assertNotIn("cognition_model", state["event"])
        self.assertEqual(state["event"]["viewpoint_model"], "# 旧主角认知")
        self.assertNotIn("cognition", state["agent_outputs"])
        self.assertIn("viewpoint", state["agent_outputs"])

    def test_unbound_long_lived_payoff_is_retired_on_migration(self):
        state = game._dynamic_director_state({
            "event": None,
            "current_plan": None,
            "payoff_state": {
                "id": "legacy-payoff",
                "desc": "获得某种未知机缘",
                "trigger": "检查旧物",
                "status": "pending",
            },
        })

        self.assertIsNone(state["payoff_state"])

    def test_invalid_replacement_cannot_discard_pending_bound_payoff(self):
        previous, _ = game._reconcile_payoff_state({}, {
            "desc": "通过「破庙道人遗骨」获得「引气诀」",
            "trigger": "检查遗骨",
        }, 4, _context())
        current, last = game._reconcile_payoff_state({"payoff_state": previous}, {
            "desc": "通过「破庙道人遗骨」获得「虚构仙经」",
            "trigger": "检查遗骨",
        }, 5, _context())

        self.assertEqual(current["id"], previous["id"])
        self.assertIsNone(last)

    def test_director_dynamic_message_puts_player_action_last(self):
        state = {
            "turns": 3,
            "transcript": [
                {"role": "narration", "text": "前两轮正文"},
                {"role": "narration", "text": "上一轮正文"},
            ],
            "character_state": {"realm": "凡人", "updated_at": 123.0},
        }
        payoff_messages = game._director_payoff_messages(
            state,
            "回家",
            _context(),
            {**_director(), "payoff_state": {
                "desc": "获得破庙机缘",
                "trigger": "检查石像后遗骨",
                "status": "pending",
            }},
            [{"id": "m1", "type": "plot", "text": "已知事实"}],
        )
        pacing_messages = game._director_pacing_messages(state, "回家", _director())

        self.assertEqual([m["role"] for m in payoff_messages], ["system", "system", "user"])
        self.assertNotIn("updated_at", payoff_messages[-1]["content"])
        self.assertIn("当前待触发爽点", payoff_messages[-1]["content"])
        self.assertIn("前两轮正文", payoff_messages[-1]["content"])
        self.assertTrue(payoff_messages[-1]["content"].rstrip().endswith("【玩家本轮行动】\n回家"))
        self.assertTrue(pacing_messages[-1]["content"].rstrip().endswith("【玩家本轮行动】\n回家"))

    def test_payoff_and_pacing_run_in_parallel_before_skeleton(self):
        payoff_started = asyncio.Event()
        calls = []

        async def fake_complete(*args, **kwargs):
            request_type = kwargs["request_type"]
            calls.append(request_type)
            if request_type == "director_pacing":
                await asyncio.wait_for(payoff_started.wait(), 0.2)
                return json.dumps({
                    "event_action": "start",
                    "turn_mode": "progress",
                    "route_key": "engage",
                    "intent": {"key": "迎战山匪", "same_as_previous": False},
                    "stage": "山匪封路",
                    "progress": "玩家角色正式介入山路冲突",
                    "reveal_boundary": "玩家确认赵横是拦路者",
                }, ensure_ascii=False)
            if request_type == "director_payoff":
                payoff_started.set()
                return json.dumps({
                    "desc": "通过「破庙道人遗骨」获得「引气诀」",
                    "trigger": "玩家亲自检查破庙道人遗骨中的旧物",
                }, ensure_ascii=False)
            self.assertEqual(request_type, "director_skeleton")
            return json.dumps({
                "turn_objective": "本轮完成一次明确攻防",
                "beats": ["山匪进逼", "主角应对并产生战果"],
                "scene": "山路遭遇",
            }, ensure_ascii=False)

        state = {
            "turns": 0,
            "transcript": [],
            "character_state": {},
            "director_state": _director(),
        }
        with patch.object(game, "complete_chat", fake_complete):
            planned = asyncio.run(game._plan_director_turn(state, "迎战山匪", _context()))

        self.assertEqual(set(calls[:2]), {"director_pacing", "director_payoff"})
        self.assertEqual(calls[2], "director_skeleton")
        self.assertEqual(planned["current_plan"]["turn_objective"], "本轮完成一次明确攻防")
        self.assertEqual(
            planned["current_plan"]["payoff"]["binding"]["reward_id"], "yin_qi_jue"
        )
        self.assertNotIn("current_goal", planned["current_plan"])
        self.assertEqual(planned["event"]["id"], "event-1")
        self.assertEqual(planned["current_plan"]["stage"], "山匪封路")

    def test_new_event_cannot_start_in_creation_turn(self):
        planned = game._apply_director_plan(
            _director(), _plan(), "前往破庙", _context(), 1,
            advance_scene=False, event_just_created=True,
        )

        self.assertEqual(planned["current_plan"]["event_action"], "none")
        self.assertEqual(planned["event"]["status"], "offered")
        self.assertEqual(planned["event"]["turns"], 0)

    def test_previous_hook_is_cleared_when_event_agent_engages_it(self):
        previous = {
            "id": "hook-1",
            "desc": "药农请人查看破庙异动",
            "goal": "前往破庙查看异动",
            "status": "offered",
            "created_turn": 2,
        }
        normalized = game._dynamic_director_state({
            "event": _event(),
            "current_plan": None,
            "hook_state": previous,
            "agent_outputs": {"hook": {"source": "llm", "output": previous}},
        })
        self.assertNotIn("desc", normalized["hook_state"])
        self.assertEqual(normalized["agent_outputs"]["hook"]["output"], {
            "goal": "前往破庙查看异动",
        })

        hook, last = game._reconcile_hook_state(
            {"hook_state": previous},
            {"goal": ""},
            True,
            3,
        )

        self.assertIsNone(hook)
        self.assertEqual(last["status"], "engaged")
        self.assertEqual(last["engaged_turn"], 3)
        self.assertNotIn("desc", last)

    def test_skeleton_receives_backend_forced_resolution(self):
        first = game._apply_director_plan(
            _director(), _plan(intent="查明黑牌"), "查看黑牌", _context(), 1
        )
        raw = _plan(action="continue", intent="查明黑牌", same=True, route="investigate")
        second = game._apply_director_plan(
            first, raw, "继续查看黑牌", _context(), 2, advance_scene=False
        )
        state = {"transcript": [{"role": "narration", "text": "黑牌微微发亮。"}]}

        messages = game._director_skeleton_messages(state, "继续查看黑牌", second)

        self.assertIn('"event_action":"resolve"', messages[-1]["content"])
        self.assertIn("同一意图已连续尝试 2 次", messages[-1]["content"])

    def test_viewpoint_uses_core_and_location_while_causal_runs_without_planner_timeout(self):
        async def scenario():
            calls = []
            causal_started = asyncio.Event()
            causal_release = asyncio.Event()
            core = "采药客周济川没有按约定返回白石村"

            async def fake_complete(messages, *args, **kwargs):
                request_type = kwargs["request_type"]
                calls.append(request_type)
                if request_type == "director_event":
                    return json.dumps({"title": "白石村采药客失踪", "core": core}, ensure_ascii=False)
                if request_type == "director_viewpoint":
                    self.assertEqual(messages[-1]["content"], (
                        "【事件 core】\n" + core
                        + "\n\n【当前主角位置约束】\n"
                        + '{"location_id":"baishi_village","location_name":"白石村",'
                        + '"location_state":"village","lost_risk":"none",'
                        + '"region_id":"qingwu_county","region_name":"青梧郡",'
                        + '"site_name":"村口老槐树"}'
                    ))
                    return (
                        "# 主角视角\n\n## 主角位置\n主角位于白石村村口老槐树。\n\n"
                        "## 与事件的接触关系\n主角在约定地点发现周济川没有返回。\n\n"
                        "## 当前可感知事实\n约定地点没有周济川的踪影。"
                    )
                if request_type == "director_hook":
                    self.assertNotIn("幕后因果模型", messages[-1]["content"])
                    return json.dumps({
                        "desc": "这个旧字段必须被后端丢弃。",
                        "goal": "查看周济川原定返回的道路",
                    }, ensure_ascii=False)
                self.assertEqual(request_type, "director_causal")
                causal_started.set()
                await causal_release.wait()
                return "# 幕后事实\n\n周济川因山路塌方滞留青屏山北坡。"

            sid = "foundation-test"
            state = {
                "session_id": sid,
                "turns": 0,
                "transcript": [],
                "character_state": {},
                "director_state": {},
            }
            game._CACHE[sid] = state
            try:
                with patch.object(game, "DIRECTOR_PLANNER_TIMEOUT_SECONDS", 0.001), patch.object(
                    game, "complete_chat", fake_complete
                ), patch.object(game.store, "save_director_state"):
                    first = await game._ensure_event_foundation(state, "（开场）", _context(), [])
                    self.assertEqual(calls[0], "director_event")
                    self.assertLess(calls.index("director_causal"), calls.index("director_hook"))
                    self.assertIn("director_viewpoint", calls)
                    self.assertEqual(first["agent_outputs"]["causal"]["source"], "pending")
                    await causal_started.wait()
                    await asyncio.sleep(0.01)
                    causal_release.set()
                    await game._CAUSAL_TASKS[first["event"]["id"]]
                    second = await game._ensure_event_foundation(state, "观察四周", _context(), [])
                self.assertEqual(set(calls), {
                    "director_event", "director_viewpoint", "director_hook", "director_causal",
                })
                self.assertEqual(len(calls), 4)
                self.assertEqual(second["event"]["id"], first["event"]["id"])
                self.assertIn("山路塌方", second["event"]["causal_model"])
                self.assertEqual(second["agent_outputs"]["causal"]["source"], "llm")
                self.assertEqual(first["agent_outputs"]["hook"]["output"], {
                    "goal": "查看周济川原定返回的道路",
                })
                self.assertNotIn("desc", first["hook_state"])
                self.assertNotIn("premise", first["event"])
            finally:
                game._CACHE.pop(sid, None)

        asyncio.run(scenario())

    def test_causal_markdown_is_not_rejected_by_word_matching(self):
        async def fake_complete(*args, **kwargs):
            return "老者因为某件事物进入白石村。"

        with patch.object(game, "complete_chat", fake_complete):
            result, meta = asyncio.run(game._call_director_text_agent(
                [{"role": "user", "content": "test"}],
                "director_causal", 100, "vague-test",
            ))

        self.assertEqual(result, "老者因为某件事物进入白石村。")
        self.assertEqual(meta["source"], "llm")
        self.assertEqual(meta["fallback_reason"], "")

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
        planned = game._apply_director_plan(_director(), _plan(), "迎战", _context(), 2)

        async def fake_plan(*args, **kwargs):
            return planned

        async def fake_foundation(*args, **kwargs):
            return _director()

        try:
            with patch.object(game.constraints, "action_constraints", return_value="world"), patch.object(
                game.constraints, "director_context", return_value=_context()
            ), patch.object(game, "_plan_director_turn", fake_plan), patch.object(
                game, "_ensure_event_foundation", fake_foundation
            ), patch.object(
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
        plan.update({
            "plan_id": "random",
            "note": "long note",
            "payoff": {
                "id": "payoff-1",
                "desc": "获得破庙道人留下的入道机缘",
                "trigger": "检查石像后的遗骨",
            },
        })
        prompt = game._director_audit_prompt(
            plan,
            "迎战",
            "正文。《状态》\n境界：凡人\n《/状态》",
        )

        self.assertNotIn("plan_id", prompt)
        self.assertNotIn("long note", prompt)
        self.assertIn("境界：凡人", prompt)
        self.assertIn("检查石像后的遗骨", prompt)

    def test_audit_marks_maintained_payoff_triggered(self):
        sid = "audit-payoff-test"
        payoff = {
            "id": "payoff-1",
            "desc": "通过「破庙道人遗骨」获得「引气诀」",
            "trigger": "检查石像后的遗骨",
            "status": "pending",
            "created_turn": 3,
            "binding": {
                "opportunity_id": "ruined_temple_bones",
                "opportunity_name": "破庙道人遗骨",
                "reward_kind": "art",
                "reward_id": "yin_qi_jue",
                "reward_name": "引气诀",
            },
        }
        plan = {
            "plan_id": "plan-4",
            "event_id": "event-1",
            "turn_mode": "progress",
            "turn_objective": "检查遗骨",
            "payoff": dict(payoff),
            "beats": ["检查遗骨", "取得机缘"],
        }
        game._CACHE[sid] = {
            "director_state": {
                "event": _event(status="active", turns=4),
                "current_plan": plan,
                "payoff_state": dict(payoff),
            }
        }

        async def fake_complete(*args, **kwargs):
            return json.dumps({
                "fulfilled": True,
                "payoff_triggered": True,
                "evidence": "主角检查遗骨后取得入道机缘。",
                "viewpoint_updates": ["玩家角色确认破庙道人遗骨内藏有引气诀。"],
                "violations": [],
                "note": "",
            }, ensure_ascii=False)

        try:
            with patch.object(game, "complete_chat", fake_complete), patch.object(
                game.store, "save_director_state"
            ), patch.object(game.store, "save_opportunity_reward_binding"):
                asyncio.run(game._run_director_audit(
                    sid, "检查遗骨", "主角检查遗骨后取得入道机缘。", 4, plan
                ))
            director = game._CACHE[sid]["director_state"]
            self.assertEqual(director["payoff_state"]["status"], "triggered")
            self.assertEqual(director["payoff_state"]["triggered_turn"], 4)
            self.assertEqual(director["last_payoff"]["id"], "payoff-1")
            self.assertTrue(director["last_audit"]["payoff_triggered"])
            self.assertIn("第 4 回合", director["event"]["viewpoint_model"])
            self.assertIn("引气诀", director["event"]["viewpoint_model"])
            self.assertEqual(
                director["agent_outputs"]["audit"]["output"]["evidence"],
                "主角检查遗骨后取得入道机缘。",
            )
        finally:
            game._CACHE.pop(sid, None)

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
