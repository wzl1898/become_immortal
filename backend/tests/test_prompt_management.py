import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import game
import main
import prompts
import store


class PromptManagementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_patch = patch.object(store, "DATA_DIR", self.tmp.name)
        self.db_patch = patch.object(
            store, "DB_PATH", os.path.join(self.tmp.name, "saves.db")
        )
        self.data_patch.start()
        self.db_patch.start()
        prompts._reset_active_templates()
        game.init()
        self.client = TestClient(main.app)
        self.headers = {"X-User-ID": "prompt-admin"}

    def tearDown(self):
        self.client.close()
        prompts._reset_active_templates()
        self.db_patch.stop()
        self.data_patch.stop()
        self.tmp.cleanup()

    def test_file_templates_are_seeded_as_production_version_one(self):
        response = self.client.get("/api/prompts", headers=self.headers)
        self.assertEqual(200, response.status_code)
        items = response.json()["prompts"]
        self.assertEqual(len(prompts._TEMPLATES), len(items))
        opening = next(
            item for item in items if item["name"] == "engine/narrative/opening"
        )
        self.assertEqual(1, opening["production_version"])
        self.assertEqual(1, opening["version_count"])
        self.assertEqual("file", opening["source"])

    def test_catalog_groups_templates_under_eighteen_active_agents(self):
        items = self.client.get("/api/prompts", headers=self.headers).json()["prompts"]
        agent_groups = {
            item["group_key"]
            for item in items
            if item["catalog_scope"] == "agent"
        }
        self.assertEqual(set(prompts._AGENT_GROUPS), agent_groups)
        self.assertEqual(18, len(agent_groups))
        for item in items:
            with self.subTest(prompt=item["name"]):
                self.assertTrue(item["group_key"])
                self.assertTrue(item["group_label"])
                self.assertTrue(item["template_path"])
                self.assertTrue(item["template_label"])

        scopes = {item["name"]: item["catalog_scope"] for item in items}
        for prefix in ("constraints/", "shared/", "legacy/"):
            self.assertTrue(
                all(
                    scopes[name] == "support"
                    for name in scopes
                    if name.startswith(prefix)
                )
            )

    def test_custom_prompt_has_catalog_metadata(self):
        created = self.client.post(
            "/api/prompts",
            headers=self.headers,
            json={"name": "custom/example", "content": "Example"},
        ).json()["prompt"]
        self.assertEqual("custom", created["catalog_scope"])
        self.assertEqual("custom", created["group_key"])
        self.assertEqual("自建提示词", created["group_label"])

    def test_every_existing_template_can_create_a_new_version(self):
        for name, template in prompts._TEMPLATES.items():
            with self.subTest(prompt=name):
                result = prompts.create_version(
                    name, f"{template.template}\n", "prompt-admin"
                )
                self.assertIsNotNone(result)
                self.assertEqual(2, result["versions"][0]["version"])
                self.assertEqual(1, result["production_version"])

    def test_new_version_only_changes_rendering_after_production_promotion(self):
        created = self.client.post(
            "/api/prompts",
            headers=self.headers,
            json={
                "name": "custom/greeting",
                "description": "Greeting contract",
                "content": "Hello ${name}",
                "make_production": True,
            },
        )
        self.assertEqual(200, created.status_code)
        self.assertEqual("Hello Lin", prompts.render_prompt("custom/greeting", name="Lin"))

        drafted = self.client.post(
            "/api/prompts/versions",
            headers=self.headers,
            json={"name": "custom/greeting", "content": "Welcome ${name}"},
        )
        self.assertEqual(200, drafted.status_code)
        self.assertEqual(2, drafted.json()["prompt"]["versions"][0]["version"])
        self.assertEqual("Hello Lin", prompts.render_prompt("custom/greeting", name="Lin"))

        promoted = self.client.post(
            "/api/prompts/production",
            headers=self.headers,
            json={"name": "custom/greeting", "version": 2},
        )
        self.assertEqual(200, promoted.status_code)
        self.assertEqual(2, promoted.json()["prompt"]["production_version"])
        self.assertEqual("Welcome Lin", prompts.render_prompt("custom/greeting", name="Lin"))

        detail = self.client.get(
            "/api/prompts/detail?name=custom%2Fgreeting", headers=self.headers
        ).json()["prompt"]
        self.assertEqual("Hello ${name}", detail["versions"][1]["content"])
        self.assertEqual(["production"], detail["versions"][0]["labels"])

    def test_incompatible_variable_contract_cannot_be_production(self):
        self.client.post(
            "/api/prompts",
            headers=self.headers,
            json={
                "name": "custom/contract",
                "content": "First ${name}",
                "make_production": True,
            },
        )
        drafted = self.client.post(
            "/api/prompts/versions",
            headers=self.headers,
            json={"name": "custom/contract", "content": "Second ${other}"},
        )
        self.assertEqual(200, drafted.status_code)

        promoted = self.client.post(
            "/api/prompts/production",
            headers=self.headers,
            json={"name": "custom/contract", "version": 2},
        )
        self.assertEqual(400, promoted.status_code)
        self.assertIn("变量", promoted.json()["detail"])
        self.assertEqual("First Lin", prompts.render_prompt("custom/contract", name="Lin"))

    def test_rejects_duplicate_names_and_invalid_template_syntax(self):
        body = {"name": "custom/duplicate", "content": "ok"}
        self.assertEqual(
            200,
            self.client.post("/api/prompts", headers=self.headers, json=body).status_code,
        )
        duplicate = self.client.post("/api/prompts", headers=self.headers, json=body)
        self.assertEqual(400, duplicate.status_code)
        invalid = self.client.post(
            "/api/prompts",
            headers=self.headers,
            json={"name": "custom/invalid", "content": "bad $ value"},
        )
        self.assertEqual(400, invalid.status_code)


if __name__ == "__main__":
    unittest.main()
