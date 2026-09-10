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
        self.assertTrue(result.endswith(prompts.CULTIVATION_SYSTEM_APPENDIX))

    def test_causal_context_remains_before_shared_cultivation_rules(self):
        result = game._director_causal_messages(
            {"core": "村口争执"}, {"location": "白石村"}, [], {}, ""
        )[0]["content"]
        self.assertLess(result.index("【稳定世界】"), result.index("# 固定修炼体系"))
        self.assertTrue(result.endswith(prompts.CULTIVATION_SYSTEM_APPENDIX))

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
