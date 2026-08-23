import asyncio
import json
import unittest
from unittest.mock import patch

import game
import llm
import prompts


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


def _plan(
    *, action="start", mode="progress", intent="应对山匪", same=False,
    route="engage", intent_resolved=False, event_ended=False,
    progression_direction="迫使赵横带领的山匪让出山路",
):
    return {
        "event_action": action,
        "turn_mode": mode,
        "route_key": route,
        "intent": {"key": intent, "same_as_previous": same},
        "intent_resolved": intent_resolved,
        "progression_direction": progression_direction,
        "event_ended": event_ended,
        "stage": "山匪封路",
        "progress": "玩家行动在山路冲突中产生明确结果",
        "reveal_boundary": "玩家只确认眼前山匪的行动",
    }


def _event(*, status="offered", turns=0):
    return {
        "id": "event-1",
        "title": "山路劫掠",
        "core": "山匪正在截断白石村通往县城的山路",
        "benefit": "恢复白石村通往县城的道路",
        "end_condition": "赵横带领的山匪不再封锁白石村通往县城的山路",
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
    def test_event_system_prompt_places_world_then_memory_after_rules(self):
        system = game._director_event_system_prompt(
            _context(),
            [{"id": "memory-1", "type": "plot", "text": "主角已经甩脱追兵。"}],
        )

        self.assertLess(system.index("# 规则"), system.index("【稳定世界】"))
        self.assertLess(system.index("【稳定世界】"), system.index("【近期世界记忆】"))
        self.assertLess(system.index("【近期世界记忆】"), system.index("# 输出"))
        self.assertIn('["主角已经甩脱追兵。"]', system)
        self.assertNotIn("memory-1", system)
        self.assertNotIn('"type":"plot"', system)

    def test_event_prompt_forbids_continuing_pursuit_after_escape(self):
        self.assertIn("若上一个事件是主角被追杀、追踪、搜捕或围堵", prompts.DIRECTOR_EVENT_SYSTEM_PROMPT)
        self.assertIn("不得继续保留被追杀、可能暴露或搜捕网收紧的压力", prompts.DIRECTOR_EVENT_SYSTEM_PROMPT)

    def test_causal_messages_put_world_in_system_and_order_user_sections(self):
        messages = game._director_causal_messages(
            _event(),
            _context(),
            [{"id": "memory-1", "type": "plot", "text": "主角已经甩脱追兵。"}],
            {"realm": "炼气一层"},
            "主角在后山收功。",
        )

        system = messages[0]["content"]
        user = messages[1]["content"]
        self.assertIn("【稳定世界】", system)
        self.assertNotIn("【稳定世界】", user)
        self.assertLess(user.index("【事件】"), user.index("【近期世界记忆】"))
        self.assertLess(user.index("【近期世界记忆】"), user.index("【主角状态】"))
        self.assertLess(user.index("【主角状态】"), user.index("【最近剧情】"))
        self.assertIn('["主角已经甩脱追兵。"]', user)
        self.assertNotIn("memory-1", user)
        self.assertNotIn('"type":"plot"', user)
        self.assertNotIn("【当前输入】", user)

    def test_cultivation_rules_are_at_tail_of_story_event_and_causal_prompts(self):
        prompts_to_check = (
            prompts.SYSTEM_PROMPT,
            prompts.DIRECTOR_EVENT_SYSTEM_PROMPT,
            prompts.DIRECTOR_CAUSAL_SYSTEM_PROMPT,
        )
        for prompt in prompts_to_check:
            self.assertIn("“引气诀”是固定世界中的功法名称，不是境界名称", prompt)
            self.assertIn("不得创造“引气期”“纳气期”或其他未定义境界", prompt)
            self.assertTrue(prompt.rstrip().endswith("不得继续扩散该错误术语。"))

    def test_player_avoidance_replans_event_without_resetting_it(self):
        first = game._apply_director_plan(_director(), _plan(), "迎战", _context(), 1)
        second_raw = _plan(action="continue", intent="避开山匪", route="escape")
        second_raw["turn_objective"] = "摆脱追踪并取得安全"
        second = game._apply_director_plan(first, second_raw, "钻入山林避战", _context(), 2)

        self.assertEqual(second["event"]["id"], first["event"]["id"])
        self.assertEqual(second["event"]["core"], first["event"]["core"])
        self.assertEqual(second["event"]["turns"], 2)
        self.assertEqual(second["current_plan"]["turn_objective"], "摆脱追踪并取得安全")
        self.assertEqual(second["current_plan"]["event_action"], "continue")
        self.assertFalse(second["current_plan"]["event_ended"])
        self.assertEqual(second["current_plan"]["forced_reasons"], [])

    def test_second_semantically_same_intent_forces_resolution(self):
        first = game._apply_director_plan(
            _director(), _plan(intent="探明玄色小牌"), "细看小牌", _context(), 1
        )
        second_raw = _plan(action="continue", intent="探明玄色小牌", same=True, route="investigate")
        second = game._apply_director_plan(first, second_raw, "继续感应牌中白影", _context(), 2)

        self.assertEqual(second["intent"]["attempts"], 2)
        self.assertTrue(second["current_plan"]["intent_resolved"])
        self.assertEqual(second["current_plan"]["event_action"], "continue")
        self.assertEqual(second["current_plan"]["turn_mode"], "progress")
        self.assertFalse(second["current_plan"]["event_ended"])
        self.assertTrue(any("2 次" in reason for reason in second["current_plan"]["forced_reasons"]))

    def test_fifth_event_turn_does_not_force_resolution(self):
        state = game._apply_director_plan(_director(), _plan(), "迎战", _context(), 1)
        for turn in range(2, 6):
            raw = _plan(action="continue", intent=f"不同战术{turn}")
            state = game._apply_director_plan(state, raw, f"行动{turn}", _context(), turn)

        self.assertEqual(state["event"]["turns"], 5)
        self.assertEqual(state["current_plan"]["event_action"], "continue")
        self.assertFalse(state["current_plan"]["event_ended"])
        self.assertFalse(any("第 5 轮" in reason for reason in state["current_plan"]["forced_reasons"]))

    def test_progression_ended_marks_event_for_resolution(self):
        async def fake_complete(*args, **kwargs):
            request_type = kwargs["request_type"]
            if request_type == "director_progression":
                return json.dumps({
                    "direction": "让赵横带领山匪撤离山路并恢复白石村通行",
                    "ended": True,
                }, ensure_ascii=False)
            if request_type == "director_payoff":
                return '{"desc":"","trigger":""}'
            if request_type == "director_pacing":
                return json.dumps({
                    "intent": {"key": "确认山路已经安全", "same_as_previous": False},
                    "resolved": True,
                }, ensure_ascii=False)
            if request_type == "director_hook":
                return json.dumps({"goal": "向赵横确认黑风寨撤离路线"}, ensure_ascii=False)
            self.assertEqual(request_type, "director_skeleton")
            return json.dumps({
                "turn_objective": "确认山匪撤离并恢复道路通行",
                "beats": ["赵横下令撤离", "白石村通往县城的道路恢复通行"],
            }, ensure_ascii=False)

        state = {
            "turns": 2,
            "transcript": [],
            "character_state": {},
            "director_state": _director(status="active", turns=2),
        }
        with patch.object(game, "complete_chat", fake_complete):
            planned = asyncio.run(game._plan_director_turn(
                state, "确认山路已经安全", _context()
            ))

        self.assertTrue(planned["current_plan"]["event_ended"])
        self.assertEqual(planned["current_plan"]["event_action"], "resolve")
        state = {"turns": 3, "director_state": planned}

        game._finalize_director_state(state, "山匪威胁已经解除。")

        self.assertEqual(state["director_state"]["event"]["status"], "resolved")
        self.assertEqual(state["director_state"]["event"]["ended_turn"], 3)

    def test_progression_end_does_not_pre_generate_next_event(self):
        """事件判定结束的那一轮，不再抢在玩家开口前预生成下一事件。

        预生成已迁移到"无事件过渡轮"：见 test_eventless_turn_*。
        这里只验证 _plan_director_turn 结束事件时正确落 resolve 状态机、
        且 _NEXT_EVENT_TASKS 保持为空。
        """
        async def fake_complete(messages, *args, **kwargs):
            request_type = kwargs["request_type"]
            if request_type == "director_progression":
                return json.dumps({
                    "direction": "让赵横带领山匪撤离山路并恢复白石村通行",
                    "ended": True,
                }, ensure_ascii=False)
            if request_type == "director_payoff":
                return '{"desc":"","trigger":""}'
            if request_type == "director_pacing":
                return json.dumps({
                    "intent": {"key": "确认山路已经安全", "same_as_previous": False},
                    "resolved": True,
                }, ensure_ascii=False)
            if request_type == "director_event":
                raise AssertionError("事件结束轮不应再调用事件 Agent 预生成")
            if request_type == "director_hook":
                return json.dumps({"goal": "查看村口留下的脚印"}, ensure_ascii=False)
            self.assertEqual(request_type, "director_skeleton")
            return json.dumps({
                "turn_objective": "确认山匪撤离并恢复道路通行",
                "beats": ["赵横下令撤离", "白石村通往县城的道路恢复通行"],
            }, ensure_ascii=False)

        state = {
            "session_id": "next-event-chain-test",
            "turns": 2,
            "transcript": [{"role": "narration", "text": "前一轮正文已经写明山匪开始撤离。"}],
            "character_state": {},
            "director_state": _director(status="active", turns=2),
        }
        game._CACHE[state["session_id"]] = state

        async def scenario():
            try:
                with patch.object(game, "complete_chat", fake_complete), patch.object(
                    game.store, "save_director_state"
                ):
                    planned = await game._plan_director_turn(
                        state, "确认山路已经安全", _context()
                    )
                    self.assertTrue(planned["current_plan"]["event_ended"])
                    self.assertEqual(planned["current_plan"]["event_action"], "resolve")
                    self.assertEqual(planned["event"]["status"], "resolving")
                    # 关键：事件结束轮不预生成，任务表保持空。
                    self.assertNotIn(state["session_id"], game._NEXT_EVENT_TASKS)
                    state["director_state"] = planned
                    game._finalize_director_state(state, "山匪威胁已经解除。")

                self.assertEqual(state["director_state"]["event"]["status"], "resolved")
                self.assertIsNone(state["director_state"].get("next_event_seed"))
            finally:
                game._CACHE.pop(state["session_id"], None)
                game._NEXT_EVENT_TASKS.pop(state["session_id"], None)

        asyncio.run(scenario())

    def test_eventless_turn_runs_pacing_only_and_schedules_generation(self):
        """无事件过渡轮：只跑节奏 Agent，current_plan 置 None，异步孵化下一事件，

        且孵化 context 以玩家输入+意图为主、不续写刚结束的事件。正文 messages 只含
        世界约束+主角档案+玩家输入，不含事件模型/导演骨架。
        """
        sid = "eventless-turn-test"
        seen_types: list[str] = []

        async def fake_complete(messages, *args, **kwargs):
            request_type = kwargs["request_type"]
            seen_types.append(request_type)
            if request_type == "director_pacing":
                return json.dumps({
                    "intent": {"key": "取出引气诀翻看研习", "same_as_previous": False},
                    "resolved": True,
                }, ensure_ascii=False)
            if request_type == "director_event":
                content = messages[-1]["content"]
                system = messages[0]["content"]
                self.assertIn("【稳定世界】", system)
                self.assertIn("【近期世界记忆】", system)
                self.assertNotIn("【稳定世界】", content)
                self.assertNotIn("【近期世界记忆】", content)
                self.assertIn("【玩家当前输入】", content)
                self.assertIn("开始练引气决", content)
                self.assertIn("【节奏 Agent 判出的玩家意图】", content)
                self.assertIn("取出引气诀翻看研习", content)
                self.assertIn("而不是延续刚结束事件的冲突", content)
                return json.dumps({
                    "title": "坡凹参悟",
                    "core": "主角伏在坡凹翻看引气诀，初窥引气门径",
                    "benefit": "对引气入门形成可用的修行认识",
                    "end_condition": "对引气入门的门径形成明确认识",
                }, ensure_ascii=False)
            raise AssertionError(f"无事件轮不应调用 {request_type} Agent")

        state = {
            "session_id": sid,
            "turns": 14,
            "messages": [{"role": "system", "content": "fixed"}],
            "transcript": [{"role": "narration", "text": "你甩脱追兵，躲进坡凹喘息。"}],
            "character_state": {"realm": "凡人未入修行"},
            "world_memory": [],
            "inventory": [],
            "director_state": _director(status="resolved", turns=13),
            "_injected": [],
        }
        game._CACHE[sid] = state

        async def scenario():
            try:
                with patch.object(game, "complete_chat", fake_complete), patch.object(
                    game.constraints, "action_constraints", return_value="world"
                ), patch.object(
                    game.constraints, "director_context", return_value=_context()
                ), patch.object(game.store, "save_director_state"):
                    messages = await game.prepare_action(sid, "开始练引气决")
                    task = game._NEXT_EVENT_TASKS.get(sid)
                    self.assertIsNotNone(task)
                    await task

                # 无事件轮：current_plan 置 None，event 保留 resolved 原样。
                self.assertIsNone(state["director_state"]["current_plan"])
                self.assertEqual(state["director_state"]["event"]["status"], "resolved")
                # 只跑了节奏 + 事件孵化，没有推进/钩子/骨架。
                self.assertEqual(seen_types.count("director_progression"), 0)
                self.assertEqual(seen_types.count("director_hook"), 0)
                self.assertEqual(seen_types.count("director_skeleton"), 0)
                # 异步孵化落了 seed，且贴合玩家意图方向。
                self.assertEqual(state["director_state"]["next_event_seed"]["title"], "坡凹参悟")
                # 正文 messages 不含事件模型/导演骨架标记。
                content = messages[-1]["content"]
                self.assertIn("【玩家原始行动】", content)
                self.assertIn("开始练引气决", content)
                self.assertNotIn("本轮导演骨架", content)
                self.assertNotIn("当前事件因果模型", content)
            finally:
                game._CACHE.pop(sid, None)
                game._NEXT_EVENT_TASKS.pop(sid, None)

        asyncio.run(scenario())

    def test_event_agent_is_needed_only_without_event_or_after_end_marker(self):
        self.assertTrue(game._event_requires_new({"event": None}))
        self.assertFalse(game._event_requires_new(_director(status="active")))
        self.assertFalse(game._event_requires_new(_director(status="offered")))
        self.assertTrue(game._event_requires_new(_director(status="resolved")))

    def test_ended_event_async_pre_generates_next_event_seed(self):
        sid = "next-event-test"
        state = {
            "session_id": sid,
            "director_state": _director(status="resolved"),
        }
        game._CACHE[sid] = state

        async def fake_complete(*args, **kwargs):
            self.assertEqual(kwargs["request_type"], "director_event")
            return json.dumps({
                "title": "青溪镇散修传闻",
                "core": "王满仓提到的背剑散修在青溪镇留下了新的动静",
                "benefit": "获得引气诀抄本的修行机缘",
                "end_condition": "背剑散修留下的动静得到明确查证",
            }, ensure_ascii=False)

        async def scenario():
            with patch.object(game, "complete_chat", fake_complete), patch.object(
                game.store, "save_director_state"
            ):
                await game._run_next_event_generation(sid, "上一事件已结束")

        try:
            asyncio.run(scenario())
            seed = state["director_state"]["next_event_seed"]
            self.assertEqual(seed["title"], "青溪镇散修传闻")
            self.assertEqual(
                state["director_state"]["agent_outputs"]["next_event"]["output"]["benefit"],
                "获得引气诀抄本的修行机缘",
            )
        finally:
            game._CACHE.pop(sid, None)

    def test_new_event_rebuilds_causal_and_viewpoint_models(self):
        sid = "new-event-causal-refresh-test"
        previous = _director(status="resolved", turns=3)
        previous["event"]["causal_model"] = "# 旧事件幕后事实\n旧因果"
        previous["next_event_seed"] = {
            "title": "青溪镇夜行传闻",
            "core": "镇上夜行人留下新的线索",
            "benefit": "查明夜行人的真实目的",
            "end_condition": "夜行人的目的得到确认",
        }
        state = {
            "session_id": sid,
            "turns": 4,
            "transcript": [],
            "character_state": {},
            "director_state": previous,
        }
        scheduled = []
        viewpoint_calls = []

        async def fake_viewpoint(*args, **kwargs):
            viewpoint_calls.append(args[0][-1]["content"])
            return "# 主角视角\n\n主角位于青溪镇。", {"source": "llm", "model": "test", "fallback_reason": ""}

        def fake_schedule(*args):
            scheduled.append(args[1])

        try:
            with patch.object(game, "_schedule_causal_foundation", fake_schedule), patch.object(
                game, "_call_director_text_agent", fake_viewpoint
            ):
                result = asyncio.run(game._ensure_event_foundation(
                    state, "进入青溪镇", _context(), []
                ))
            self.assertNotEqual(result["event"]["id"], previous["event"]["id"])
            self.assertEqual(result["event"]["causal_model"], "")
            self.assertEqual(result["event"]["viewpoint_model"], "# 主角视角\n\n主角位于青溪镇。")
            self.assertEqual(len(viewpoint_calls), 1)
            self.assertIn("镇上夜行人留下新的线索", viewpoint_calls[0])
            self.assertEqual(scheduled, [result["event"]["id"]])
            self.assertEqual(result["agent_outputs"]["causal"]["source"], "pending")
            self.assertEqual(result["agent_outputs"]["viewpoint"]["source"], "llm")
        finally:
            game._CACHE.pop(sid, None)

    def test_eventless_generation_context_centers_player_input_and_intent(self):
        sid = "eventless-context-test"
        state = {
            "session_id": sid,
            "transcript": [{"role": "narration", "text": "你甩脱追兵，躲进坡凹喘息。"}],
            "character_state": {"realm": "凡人未入修行", "condition": "刚初悟引气门径"},
            "world_memory": [{
                "id": "memory:qingxi-rumor",
                "type": "plot",
                "text": "王满仓提到青溪镇散修",
            }],
            "director_state": _director(status="resolved"),
        }
        content = game._eventless_event_generation_context(
            state,
            "开始练引气决",
            {"key": "取出引气诀翻看研习", "same_as_previous": False},
        )

        # 主体是玩家输入 + 意图。
        self.assertIn("【玩家当前输入】", content)
        self.assertIn("开始练引气决", content)
        self.assertIn("【节奏 Agent 判出的玩家意图】", content)
        self.assertIn("取出引气诀翻看研习", content)
        # 旧事件仅作背景，且明令不得续写。
        self.assertIn("仅作背景，不得续写", content)
        self.assertIn("而不是延续刚结束事件的冲突", content)
        # user 内容仍带主角成长；记忆与稳定世界已迁入 system 内容。
        self.assertIn("刚初悟引气门径", content)
        self.assertNotIn("青溪镇散修", content)
        self.assertNotIn("【稳定世界", content)

    def test_viewpoint_updates_keep_only_eight_newest_facts(self):
        base = "# 主角视角\n\n## 主角位置\n白石村。"
        existing = "\n".join(
            f"- 第 {turn} 回合：认知 {turn}" for turn in range(1, 11)
        )
        viewpoint = base + "\n\n## 正文后新增认知\n" + existing

        merged = game._merge_viewpoint_updates(viewpoint, ["认知 11"], 11)
        update_lines = [line for line in merged.splitlines() if line.startswith("- 第 ")]

        self.assertEqual(len(update_lines), 8)
        self.assertNotIn("认知 1\n", merged)
        self.assertNotIn("认知 2\n", merged)
        self.assertNotIn("认知 3\n", merged)
        self.assertIn("认知 4", merged)
        self.assertIn("认知 11", merged)
        self.assertIn("## 主角位置\n白石村。", merged)

    def test_missing_viewpoint_does_not_make_active_event_new(self):
        director = _director(status="active")
        director["event"]["viewpoint_model"] = ""

        self.assertTrue(game._event_needs_foundation(director))
        self.assertFalse(game._event_requires_new(director))

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

    def test_triggered_payoff_retires_when_agent_repeats_it(self):
        triggered = {
            "id": "payoff-triggered",
            "desc": "通过「破庙道人遗骨」获得「引气诀」",
            "trigger": "检查遗骨",
            "status": "triggered",
            "created_turn": 4,
            "triggered_turn": 5,
            "binding": {
                "opportunity_id": "ruined_temple_bones",
                "reward_id": "yin_qi_jue",
                "reward_kind": "art",
            },
        }

        current, last = game._reconcile_payoff_state(
            {"payoff_state": triggered},
            {"desc": triggered["desc"], "trigger": triggered["trigger"]},
            6,
            _context(),
        )

        self.assertIsNone(current)
        self.assertEqual(last, triggered)

    def test_triggered_payoff_can_be_replaced_by_new_bound_payoff(self):
        triggered = {
            "id": "payoff-triggered",
            "desc": "通过「破庙道人遗骨」获得「引气诀」",
            "trigger": "检查遗骨",
            "status": "triggered",
            "created_turn": 4,
            "binding": {
                "opportunity_id": "ruined_temple_bones",
                "reward_id": "yin_qi_jue",
                "reward_kind": "art",
            },
        }
        context = _context()
        context["reward_candidates"].append({
            "id": "iron_bone", "name": "铁骨功", "reward_kind": "art",
        })

        current, last = game._reconcile_payoff_state(
            {"payoff_state": triggered},
            {"desc": "通过青溪镇武馆获得铁骨功", "trigger": "前往武馆"},
            6,
            context,
        )

        self.assertEqual(current["status"], "pending")
        self.assertEqual(current["binding"]["reward_id"], "iron_bone")
        self.assertEqual(last, triggered)

    def test_two_field_payoff_binds_existing_opportunity_and_reward(self):
        payoff, _ = game._reconcile_payoff_state({}, {
            "desc": "通过「破庙道人遗骨」获得「引气诀」",
            "trigger": "亲自检查破庙道人遗骨中的旧物",
        }, 4, _context())
        facts = game._payoff_selected_facts(payoff, _context())

        self.assertEqual(payoff["binding"]["opportunity_id"], "ruined_temple_bones")
        self.assertEqual(payoff["binding"]["reward_id"], "yin_qi_jue")
        self.assertEqual({row["id"] for row in facts}, {"ruined_temple_bones", "yin_qi_jue"})

    def test_reward_only_payoff_is_accepted_without_opportunity(self):
        context = _context()
        context["reward_candidates"] = [{
            "id": "iron_bone", "name": "铁骨功", "reward_kind": "art",
        }]
        context["opportunities"] = []
        payoff, _ = game._reconcile_payoff_state({}, {
            "desc": "通过青溪镇武馆获得铁骨功",
            "trigger": "前往青溪镇武馆求取炼体功法",
        }, 4, context)

        self.assertEqual(payoff["binding"]["reward_id"], "iron_bone")
        self.assertNotIn("opportunity_id", payoff["binding"])

    def test_reward_only_payoff_uses_last_standard_reward_in_trade_description(self):
        context = _context()
        context["reward_candidates"] = [
            {"id": "yin_qi_jue", "name": "引气诀", "reward_kind": "art"},
            {"id": "iron_bone", "name": "铁骨功", "reward_kind": "art"},
        ]
        context["opportunities"] = []
        payoff, _ = game._reconcile_payoff_state({}, {
            "desc": "通过引气诀获得铁骨功，在青溪镇武馆完成交换",
            "trigger": "前往青溪镇武馆求取炼体功法",
        }, 4, context)

        self.assertEqual(payoff["binding"]["reward_id"], "iron_bone")

    def test_invalid_payoff_retry_receives_failed_output_and_standard_names(self):
        messages = game._director_payoff_retry_messages(
            {"turns": 3, "character_state": {}, "transcript": []},
            "去破庙查看",
            _context(),
            {},
            [],
            {"desc": "通过青溪镇武馆获得铁骨功", "trigger": "前往武馆"},
        )

        feedback = messages[-1]["content"]
        self.assertIn("通过青溪镇武馆获得铁骨功", feedback)
        self.assertIn("破庙道人遗骨", feedback)
        self.assertIn("引气诀", feedback)
        self.assertIn("必须逐字包含一个标准奖励名", feedback)

    def test_invalid_payoff_is_retried_before_director_skeleton(self):
        calls = []

        async def fake_complete(*args, **kwargs):
            request_type = kwargs["request_type"]
            calls.append(request_type)
            if request_type == "director_payoff":
                return json.dumps({
                    "desc": "通过青溪镇武馆获得铁骨功",
                    "trigger": "前往青溪镇武馆",
                }, ensure_ascii=False)
            if request_type == "director_payoff_retry":
                self.assertIn("青溪镇武馆", args[0][-1]["content"])
                self.assertIn("破庙道人遗骨", args[0][-1]["content"])
                return json.dumps({
                    "desc": "通过「破庙道人遗骨」获得「引气诀」",
                    "trigger": "检查破庙石像后的遗骨",
                }, ensure_ascii=False)
            if request_type == "director_pacing":
                return json.dumps({"intent": {"key": "调查", "same_as_previous": False}, "resolved": True})
            if request_type == "director_progression":
                return json.dumps({"direction": "调查破庙", "ended": False})
            if request_type == "director_hook":
                return json.dumps({"goal": "检查破庙石像后的遗骨"})
            self.assertEqual(request_type, "director_skeleton")
            return json.dumps({
                "turn_objective": "调查破庙",
                "beats": ["抵达破庙", "发现石像后的异常"],
                "scene": "破庙",
            }, ensure_ascii=False)

        state = {
            "turns": 0,
            "transcript": [],
            "character_state": {},
            "director_state": _director(),
        }
        with patch.object(game, "complete_chat", fake_complete):
            planned = asyncio.run(game._plan_director_turn(state, "去破庙查看", _context()))

        self.assertEqual(set(calls[:2]), {"director_pacing", "director_payoff"})
        self.assertEqual(calls[2], "director_payoff_retry")
        self.assertEqual(planned["current_plan"]["payoff"]["binding"]["opportunity_id"], "ruined_temple_bones")
        self.assertEqual(planned["agent_outputs"]["payoff"]["retry_output"]["desc"], "通过「破庙道人遗骨」获得「引气诀」")

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
        self.assertTrue(planned["current_plan"]["intent_resolved"])
        self.assertEqual(planned["current_plan"]["event_action"], "continue")
        self.assertFalse(planned["current_plan"]["event_ended"])
        self.assertEqual(planned["event"]["id"], "event-1")

    def test_pacing_fallback_only_settles_the_current_intent(self):
        pacing = game._fallback_director_pacing(
            _director(), "不要查看埋好的牌子，回家假装没事"
        )

        self.assertEqual(pacing, {
            "intent": {
                "key": "不要查看埋好的牌子，回家假装没事",
                "same_as_previous": False,
            },
            "resolved": True,
        })

    def test_progression_returns_next_direction_and_end_marker(self):
        event = _event(status="active")

        ongoing = game._sanitize_progression_decision({
            "direction": "让赵横因粮袋被截获而暴露黑风寨的补给缺口",
            "ended": False,
        }, event)
        ending = game._sanitize_progression_decision({
            "direction": "让赵横带领山匪撤离山路并恢复白石村通行",
            "ended": True,
        }, event)

        self.assertIn("补给缺口", ongoing["direction"])
        self.assertFalse(ongoing["ended"])
        self.assertEqual(ending, {
            "direction": "让赵横带领山匪撤离山路并恢复白石村通行",
            "ended": True,
        })

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

    def test_triggered_bound_payoff_moves_to_last_on_migration(self):
        triggered = {
            "id": "legacy-triggered",
            "desc": "通过「破庙道人遗骨」获得「引气诀」",
            "trigger": "检查遗骨",
            "status": "triggered",
            "binding": {
                "opportunity_id": "ruined_temple_bones",
                "reward_id": "yin_qi_jue",
                "reward_kind": "art",
            },
        }
        state = game._dynamic_director_state({
            "event": None,
            "current_plan": None,
            "payoff_state": triggered,
        })

        self.assertIsNone(state["payoff_state"])
        self.assertEqual(state["last_payoff"], triggered)

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
        self.assertIn("不得只复述玩家输入的动作或手段", pacing_messages[0]["content"])
        self.assertIn("摆脱追兵", pacing_messages[0]["content"])
        self.assertTrue(payoff_messages[-1]["content"].rstrip().endswith("【玩家本轮行动】\n回家"))
        self.assertTrue(pacing_messages[-1]["content"].rstrip().endswith("【玩家本轮行动】\n回家"))

    def test_hook_and_story_are_bound_to_event_benefit_path(self):
        director = _director()
        director["current_plan"] = {
            **_plan(),
            "hook": {"goal": "向赵横的粮草押运者查明补给路线"},
            "action_goal": "向赵横的粮草押运者查明补给路线",
        }

        hook_messages = game._director_hook_messages(
            {"turns": 1, "transcript": []},
            "观察山路",
            director,
            None,
            _context(),
        )
        rendered = game._render_director_plan(director, _context())

        self.assertIn("通往当前事件 benefit", hook_messages[0]["content"])
        self.assertIn("意图完成后的新局面", hook_messages[0]["content"])
        self.assertIn("不得依据意图执行前的旧位置", hook_messages[0]["content"])
        self.assertIn("本轮将完整落实的玩家意图", hook_messages[-1]["content"])
        self.assertIn("本轮意图完成后的预计结果", hook_messages[-1]["content"])
        self.assertIn("恢复白石村通往县城的道路", hook_messages[-1]["content"])
        self.assertIn("向赵横的粮草押运者查明补给路线", rendered)
        self.assertIn("钩子收益方向", rendered)
        self.assertIn("恢复白石村通往县城的道路", rendered)
        self.assertIn("为什么这一步能让自己更接近 benefit", rendered)

    def test_pacing_runs_before_progression_and_binds_its_direction(self):
        payoff_started = asyncio.Event()
        calls = []

        async def fake_complete(*args, **kwargs):
            request_type = kwargs["request_type"]
            calls.append(request_type)
            if request_type == "director_progression":
                self.assertIn("节奏 Agent的玩家意图结算要求", args[0][-1]["content"])
                self.assertIn('\"resolved\":true', args[0][-1]["content"])
                self.assertIn("必须先完整落实 intent.key", args[0][0]["content"])
                self.assertIn("不能只写躲藏成功", args[0][0]["content"])
                return json.dumps({
                    "direction": "迫使赵横暴露山匪封路所依赖的补给位置",
                    "ended": False,
                }, ensure_ascii=False)
            if request_type == "director_pacing":
                await asyncio.wait_for(payoff_started.wait(), 0.2)
                return json.dumps({
                    "intent": {"key": "迎战山匪", "same_as_previous": False},
                    "resolved": True,
                }, ensure_ascii=False)
            if request_type == "director_payoff":
                payoff_started.set()
                return json.dumps({
                    "desc": "通过「破庙道人遗骨」获得「引气诀」",
                    "trigger": "玩家亲自检查破庙道人遗骨中的旧物",
                }, ensure_ascii=False)
            if request_type == "director_hook":
                return json.dumps({
                    "goal": "沿山路追查赵横留下的粮袋痕迹",
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
        self.assertEqual(calls[2], "director_progression")
        self.assertEqual(calls[3:], ["director_hook", "director_skeleton"])
        self.assertEqual(planned["current_plan"]["turn_objective"], "本轮完成一次明确攻防")
        self.assertEqual(
            planned["current_plan"]["action_goal"],
            "沿山路追查赵横留下的粮袋痕迹",
        )
        self.assertEqual(planned["agent_outputs"]["director"]["output"]["action_goal"], (
            "沿山路追查赵横留下的粮袋痕迹"
        ))
        self.assertEqual(
            planned["agent_outputs"]["director"]["output"]["must_not"][0],
            "不得在本轮正文中替玩家执行下一步行动方向：沿山路追查赵横留下的粮袋痕迹",
        )
        self.assertIn("不得在本轮正文中替玩家执行", (
            planned["current_plan"]["must_not"][0]
        ))
        self.assertEqual(
            planned["current_plan"]["payoff"]["binding"]["reward_id"], "yin_qi_jue"
        )
        self.assertNotIn("current_goal", planned["current_plan"])
        self.assertEqual(planned["event"]["id"], "event-1")
        self.assertEqual(planned["event"]["benefit"], "恢复白石村通往县城的道路")
        self.assertEqual(
            planned["current_plan"]["progression_direction"],
            "迫使赵横暴露山匪封路所依赖的补给位置",
        )
        self.assertTrue(planned["current_plan"]["intent_resolved"])
        self.assertFalse(planned["current_plan"]["event_ended"])

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

        self.assertIn('"resolved":true', messages[-1]["content"])
        self.assertIn('"ended":false', messages[-1]["content"])
        self.assertNotIn('"event_action"', messages[-1]["content"])
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
                    return json.dumps({
                        "title": "白石村采药客失踪",
                        "core": core,
                        "benefit": "找到周济川后获得青屏山安全采药路线",
                        "end_condition": "周济川返回白石村，或周济川的明确下落得到确认",
                    }, ensure_ascii=False)
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
                    self.assertIn("director_viewpoint", calls)
                    self.assertEqual(first["agent_outputs"]["causal"]["source"], "pending")
                    await causal_started.wait()
                    await asyncio.sleep(0.01)
                    causal_release.set()
                    await game._CAUSAL_TASKS[first["event"]["id"]]
                    second = await game._ensure_event_foundation(state, "观察四周", _context(), [])
                self.assertEqual(set(calls), {
                    "director_event", "director_viewpoint", "director_causal",
                })
                self.assertEqual(len(calls), 3)
                self.assertEqual(second["event"]["id"], first["event"]["id"])
                self.assertEqual(
                    second["event"]["end_condition"],
                    "周济川返回白石村，或周济川的明确下落得到确认",
                )
                self.assertIn("山路塌方", second["event"]["causal_model"])
                self.assertEqual(second["agent_outputs"]["causal"]["source"], "llm")
                self.assertIsNone(first["hook_state"])
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
            self.assertIsNone(director["payoff_state"])
            self.assertEqual(director["last_payoff"]["status"], "triggered")
            self.assertEqual(director["last_payoff"]["triggered_turn"], 4)
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

    def test_audit_end_condition_restarts_progression_as_ended(self):
        sid = "audit-event-end-test"
        plan = {
            "plan_id": "plan-end-1",
            "event_id": "event-1",
            "turn_mode": "progress",
            "event_ended": False,
            "turn_objective": "确认来者离开",
            "intent": {"key": "观察", "same_as_previous": False},
            "beats": ["确认来者离开"],
        }
        game._CACHE[sid] = {
            "session_id": sid,
            "turns": 4,
            "transcript": [{"role": "narration", "text": "来者已经离开。"}],
            "director_state": {
                "event": _event(status="active", turns=4),
                "current_plan": plan,
                "agent_outputs": {},
            },
        }
        calls = []

        async def fake_complete(*args, **kwargs):
            calls.append(kwargs["request_type"])
            if kwargs["request_type"] == "director_audit":
                return json.dumps({
                    "fulfilled": True,
                    "payoff_triggered": False,
                    "event_end_reached": True,
                    "evidence": "正文确认来者已经离开，事件结束条件已满足。",
                    "viewpoint_updates": [],
                    "violations": [],
                    "note": "",
                }, ensure_ascii=False)
            self.assertEqual(kwargs["request_type"], "director_progression")
            self.assertIn("ended", args[0][-1]["content"])
            return json.dumps({"direction": "收束当前事件", "ended": False}, ensure_ascii=False)

        try:
            with patch.object(game, "complete_chat", fake_complete), patch.object(
                game.store, "save_director_state"
            ), patch.object(game.store, "save_opportunity_reward_binding"):
                asyncio.run(game._run_director_audit(
                    sid, "观察", "来者已经离开。", 4, plan
                ))
            director = game._CACHE[sid]["director_state"]
            self.assertEqual(calls, ["director_audit", "director_progression"])
            self.assertTrue(director["current_plan"]["event_ended"])
            self.assertEqual(director["current_plan"]["event_action"], "resolve")
            self.assertEqual(director["event"]["status"], "resolved")
            self.assertTrue(director["last_audit"]["event_end_reached"])
            self.assertTrue(director["last_audit"]["progression_after_event_end"]["ended"])
        finally:
            game._CACHE.pop(sid, None)

    def test_audit_conflict_keeps_progression_end_decision(self):
        sid = "audit-end-conflict-test"
        plan = {
            "plan_id": "plan-end-conflict-1",
            "event_id": "event-1",
            "turn_mode": "resolve",
            "event_ended": True,
            "event_action": "resolve",
            "turn_objective": "完成潜入并结束当前事件",
        }
        game._CACHE[sid] = {
            "session_id": sid,
            "turns": 73,
            "transcript": [{"role": "narration", "text": "主角已经潜入坳口内部。"}],
            "director_state": {
                "event": _event(status="resolved", turns=1),
                "current_plan": plan,
                "agent_outputs": {},
            },
        }

        async def fake_complete(*args, **kwargs):
            self.assertEqual(kwargs["request_type"], "director_audit")
            return json.dumps({
                "fulfilled": True,
                "payoff_triggered": False,
                "event_end_reached": False,
                "evidence": "正文完成潜入，但未观察到关键人物。",
                "viewpoint_updates": [],
                "violations": [],
                "note": "审计认为结束条件尚未满足。",
            }, ensure_ascii=False)

        try:
            with patch.object(game, "complete_chat", fake_complete), patch.object(
                game.store, "save_director_state"
            ), patch.object(game.store, "save_opportunity_reward_binding"):
                asyncio.run(game._run_director_audit(
                    sid, "继续潜入", "主角钻过缺口进入坳口内部。", 73, plan
                ))
            audit = game._CACHE[sid]["director_state"]["last_audit"]
            self.assertTrue(audit["event_end_reached"])
            self.assertFalse(audit["agent_event_end_reached"])
            self.assertEqual(audit["event_end_source"], "progression")
            self.assertEqual(
                game._CACHE[sid]["director_state"]["event"]["status"], "resolved"
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
