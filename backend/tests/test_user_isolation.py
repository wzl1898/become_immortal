import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import game
import main
import store


class UserIsolationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(
            store, "DB_PATH", os.path.join(self.tmp.name, "test_saves.db")
        )
        self.db_patch.start()
        game._CACHE.clear()
        store.init()
        self.client = TestClient(main.app)

    def tearDown(self):
        self.client.close()
        game._CACHE.clear()
        self.db_patch.stop()
        self.tmp.cleanup()

    @staticmethod
    def headers(user_id):
        return {"X-User-ID": user_id}

    def test_users_only_list_their_own_saves(self):
        alice = self.client.post("/api/new", headers=self.headers("alice")).json()
        bob = self.client.post("/api/new", headers=self.headers("bob")).json()

        alice_saves = self.client.get(
            "/api/saves", headers=self.headers("alice")
        ).json()["saves"]
        bob_saves = self.client.get(
            "/api/saves", headers=self.headers("bob")
        ).json()["saves"]

        self.assertEqual([alice["session_id"]], [item["id"] for item in alice_saves])
        self.assertEqual([bob["session_id"]], [item["id"] for item in bob_saves])

    def test_foreign_user_cannot_read_mutate_or_stream_save(self):
        sid = self.client.post(
            "/api/new", headers=self.headers("alice")
        ).json()["session_id"]
        bob = self.headers("bob")

        self.assertEqual(404, self.client.get(f"/api/load?sid={sid}", headers=bob).status_code)
        self.assertEqual(
            404,
            self.client.post(
                "/api/rename", headers=bob, json={"sid": sid, "name": "stolen"}
            ).status_code,
        )
        self.assertEqual(
            404,
            self.client.post(
                "/api/action", headers=bob, json={"sid": sid, "text": "行动"}
            ).status_code,
        )
        self.assertEqual(
            404,
            self.client.post("/api/delete", headers=bob, json={"sid": sid}).status_code,
        )
        self.assertTrue(store.owned_by(sid, "alice"))

    def test_existing_saves_belong_to_default_user_after_migration(self):
        sid = store.create("旧存档", [])

        default_saves = self.client.get("/api/saves").json()["saves"]
        other_saves = self.client.get(
            "/api/saves", headers=self.headers("other")
        ).json()["saves"]

        self.assertIn(sid, [item["id"] for item in default_saves])
        self.assertEqual([], other_saves)


if __name__ == "__main__":
    unittest.main()
