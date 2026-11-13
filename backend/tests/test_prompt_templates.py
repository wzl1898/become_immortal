import ast
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from string import Template
from unittest.mock import patch

import game
import prompts


class PromptTemplateTests(unittest.TestCase):
    def test_integrity_rules_are_scoped_to_each_risky_agent_output(self):
        checks = {
            "guidance/stage/system": "goal 涉及稳定世界已有的功法",
            "guidance/conflict/system": "conflict_seed 可以围绕稳定世界中的物品",
            "engine/event/system": "core、benefit 和 end_condition 涉及稳定世界",
            "engine/causal/system": "幕后事实不得把它解释成残片",
            "engine/viewpoint/system": "当前可感知事实只能记录物件",
            "engine/pacing/system": "intent.key 只概括玩家想达成的结果",
            "engine/progression/system": "direction 可以决定玩家是否取得 benefit",
            "engine/hook/system": "goal 指向固定功法",
            "engine/payoff/system": "reward_candidates 中的 name、reward_kind 和 summary",
            "engine/skeleton/system": "turn_objective 和 beats 可以安排固定奖励",
            "engine/narrative/system": "正文、《状态》和《物件》均不得把它写成残片",
            "engine/audit/system": "必须记入 violations",
            "memory/extract/system": "不得提取为 state、event、别名或物品属性",
            "memory/inquiry/system": "answer 必须忽略该污染描述",
            "observation/character/system": "reason、attitude 和 goal 只能建立人物关系",
            "observation/state/system": "不属于主角状态变化",
            "legacy/director/system": "payoff、trigger 和 guidance 涉及故事卡已有的功法",
        }
        for name, rule in checks.items():
            with self.subTest(prompt=name):
                self.assertIn(rule, prompts.render_system_prompt(name))

    def test_read_only_observers_do_not_receive_unrelated_integrity_rules(self):
        self.assertNotIn("shared/world_integrity", prompts._TEMPLATES)
        for name in (
            "observation/conflict/system",
            "observation/location/system",
        ):
            with self.subTest(prompt=name):
                self.assertNotIn("残篇", prompts.render_prompt(name))

    def test_json_and_template_looking_input_are_preserved_verbatim(self):
        action = '检查 ${cultivation_rules} 和 {"goal": "开门"}，价格 $5；{{ untouched }}'
        self.assertEqual(
            prompts.render_prompt("engine/narrative/action", action=action),
            "【玩家原始行动】\n" + action,
        )

    def test_missing_and_extra_variables_fail_with_template_name(self):
        for variables in ({}, {"action": "观察", "typo": "多余"}):
            with self.subTest(variables=variables):
                with self.assertRaisesRegex(ValueError, "engine/narrative/action.md"):
                    prompts.render_prompt("engine/narrative/action", **variables)

    def test_missing_file_does_not_fall_back_or_read_arbitrary_paths(self):
        for name in ("missing", "../../README", str(Path(__file__).resolve())):
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, "Unknown prompt template"):
                    prompts.render_prompt(name)

    def test_every_direct_render_call_matches_the_template_variables(self):
        # Verify rarely exercised fallback/optional branches without making LLM calls.
        for filename in ("game.py", "constraints.py", "prompts.py"):
            path = prompts.TEMPLATE_DIR.parent / filename
            for call in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
                    continue
                if call.func.id != "render_prompt" or not call.args:
                    continue
                if not isinstance(call.args[0], ast.Constant):
                    continue
                name = call.args[0].value
                variables = {keyword.arg: "sample" for keyword in call.keywords}
                with self.subTest(file=filename, line=call.lineno, template=name):
                    prompts.render_prompt(name, **variables)

    def test_event_context_uses_a_slot_instead_of_a_heading_match(self):
        name = "engine/event/system"
        template = Template(prompts._TEMPLATES[name].template.replace("# 输出", "# 返回格式"))
        with patch.dict(prompts._TEMPLATES, {name: template}):
            result = game._director_event_system_prompt(
                {"location": "白石村"},
                [{"text": "追兵已离开。"}],
                {"conflict_seed": "新的冲突"},
            )
        self.assertLess(result.index("【稳定世界】"), result.index("【近期世界记忆】"))
        self.assertLess(result.index("【引导层事件引导】"), result.index("# 返回格式"))
        self.assertIn(prompts.CULTIVATION_SYSTEM_APPENDIX, result)

    def test_causal_context_remains_before_shared_cultivation_rules(self):
        result = game._director_causal_messages(
            {"core": "村口争执"}, {"location": "白石村"}, [], {}, ""
        )[0]["content"]
        self.assertLess(result.index("【稳定世界】"), result.index("# 固定修炼体系"))
        self.assertIn(prompts.CULTIVATION_SYSTEM_APPENDIX, result)

    def test_template_lookup_does_not_depend_on_working_directory(self):
        backend = str(prompts.TEMPLATE_DIR.parent)
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "import sys; sys.path.insert(0, sys.argv[1]); "
                        "import prompts; print(prompts.render_prompt('engine/narrative/action', action='观察'))"
                    ),
                    backend,
                ],
                cwd=directory,
                capture_output=True,
                text=True,
                timeout=8,
                check=True,
            )
        self.assertEqual(result.stdout, "【玩家原始行动】\n观察\n")
