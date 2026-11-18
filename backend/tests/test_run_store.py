import asyncio
import os
import tempfile
import unittest
from pathlib import Path


class RunStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="story-run-store-")
        path = Path(self.temp.name) / "saves.db"
        os.environ["STORY_DATA_DIR"] = self.temp.name
        os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{path}"
        os.environ["EMBED_ENABLED"] = "0"

        import run_store
        import store

        await run_store.close()
        store.DATA_DIR = self.temp.name
        store.DB_PATH = str(path)
        store.init()
        await run_store.init()
        self.run_store = run_store
        self.store = store
        self.sid = store.create("test", [{"role": "system", "content": "rules"}])

    async def asyncTearDown(self):
        await self.run_store.close()
        self.temp.cleanup()
        os.environ.pop("DATABASE_URL", None)
        os.environ.pop("STORY_DATA_DIR", None)

    async def _reserve(self, request_id="request-1"):
        return await self.run_store.reserve_primary_run(
            self.sid,
            input_kind="opening",
            action=None,
            client_request_id=request_id,
            base_turn=0,
            base_version=0,
        )

    async def test_client_request_is_idempotent_and_nodes_are_durable(self):
        first, created = await self._reserve()
        second, created_again = await self._reserve()

        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(
            [node["node_key"] for node in await self.run_store.list_nodes(first["id"])],
            list(self.run_store.TURN_NODE_KEYS),
        )

    async def test_cancel_releases_turn_and_fences_old_commit(self):
        run, _ = await self._reserve()
        shadow = f"shadow:{run['id']}"
        self.store.clone_for_run(self.sid, shadow)
        branch = self.store.load(shadow)
        self.store.save_state(shadow, branch["messages"], branch["transcript"], 1)

        await self.run_store.cancel_run(run["id"], user_id="default")
        replacement, created = await self.run_store.reserve_primary_run(
            self.sid,
            input_kind="opening",
            action=None,
            client_request_id="replacement",
            base_turn=0,
            base_version=0,
        )

        self.assertTrue(created)
        self.assertNotEqual(run["id"], replacement["id"])
        with self.assertRaises(self.run_store.CommitConflict):
            await self.run_store.commit_shadow(run["id"], shadow)
        self.assertEqual(self.store.load(self.sid)["turns"], 0)

    async def test_commit_wins_then_cancel_returns_conflict(self):
        run, _ = await self._reserve()
        await self.run_store.mark_run_started(run["id"])
        shadow = f"shadow:{run['id']}"
        self.store.clone_for_run(self.sid, shadow)
        branch = self.store.load(shadow)
        transcript = [{"role": "narration", "text": "正文"}]
        self.store.save_state(shadow, branch["messages"], transcript, 1)

        result = await self.run_store.commit_shadow(run["id"], shadow)

        self.assertEqual(result["version"], 1)
        self.assertEqual(self.store.load(self.sid)["transcript"], transcript)
        self.assertEqual(result["world_state"]["location"]["location_name"], "白石村")
        with self.assertRaises(self.run_store.AlreadyCommitted):
            await self.run_store.cancel_run(run["id"], user_id="default")

    async def test_reload_replaces_api_cache_after_worker_commit(self):
        import game

        game._CACHE.pop(self.sid, None)
        self.assertEqual(game.get_transcript(self.sid), [])

        run, _ = await self._reserve("cache-refresh")
        await self.run_store.mark_run_started(run["id"])
        shadow = f"shadow:{run['id']}"
        self.store.clone_for_run(self.sid, shadow)
        branch = self.store.load(shadow)
        transcript = [{"role": "narration", "text": "第十九回合正文"}]
        self.store.save_state(shadow, branch["messages"], transcript, 1)
        self.store.save_director_state(shadow, {"event": {"title": "新事件"}})

        await self.run_store.commit_shadow(run["id"], shadow)

        # Simulate the API process: its old in-memory copy survives the Worker
        # transaction until a persisted read explicitly replaces it.
        self.assertEqual(game.get_transcript(self.sid), [])
        game.reload_persisted_state(self.sid)
        self.assertEqual(game.get_transcript(self.sid), transcript)
        self.assertEqual(game.get_director_state(self.sid)["event"]["title"], "新事件")

    async def test_commit_preserves_live_shadow_trace_identity(self):
        run, _ = await self._reserve()
        await self.run_store.mark_run_started(run["id"])
        shadow = f"shadow:{run['id']}"
        self.store.clone_for_run(self.sid, shadow)
        trace_id = self.store.record_agent_trace({
            "save_id": shadow,
            "turn": 1,
            "agent_type": "director_event",
            "protocol": "openai",
            "model": "test",
            "status": "success",
        })
        branch = self.store.load(shadow)
        self.store.save_state(shadow, branch["messages"], branch["transcript"], 1)

        await self.run_store.commit_shadow(run["id"], shadow)

        canonical = self.store.list_agent_traces(self.sid)
        self.assertEqual([trace["id"] for trace in canonical], [trace_id])
        self.assertEqual(self.store.list_agent_traces(shadow), [])

    async def test_cancellation_fences_late_activity_events_and_completion(self):
        run, _ = await self._reserve()
        await self.run_store.cancel_run(run["id"], user_id="default")
        events_before = await self.run_store.list_events(run["id"])

        await self.run_store.complete_node(
            run["id"], "character_setting", {"late": True}, degraded=True
        )
        await self.run_store.append_event(run["id"], "delta", {"text": "late"})

        events_after = await self.run_store.list_events(run["id"])
        node = await self.run_store.get_node(run["id"], "character_setting")
        persisted = await self.run_store.get_run(run["id"])
        self.assertEqual(events_after, events_before)
        self.assertEqual(node["status"], "canceled")
        self.assertFalse(persisted["degraded"])

    async def test_save_delete_cascades_execution_ledger(self):
        run, _ = await self._reserve()
        self.assertTrue(self.store.delete(self.sid, "default"))
        self.assertIsNone(await self.run_store.get_run(run["id"]))

    async def test_debug_run_never_occupies_or_commits_save(self):
        run, _ = await self._reserve()
        node = await self.run_store.get_node(run["id"], "narrative")
        await self.run_store.save_snapshot(
            run["id"], "narrative", "before", {"save": self.store.load(self.sid)}
        )
        await self.run_store.cancel_run(run["id"], user_id="default")
        await self.run_store.finalize_canceled(run["id"])
        debug = await self.run_store.create_debug_run(
            run["id"], "narrative", mode="current", user_id="default"
        )

        self.assertEqual(debug["kind"], "debug")
        self.assertIsNone(self.store.load(self.sid)["active_run_id"])
        await self.run_store.complete_debug_run(debug["id"], {"text": "experiment"})
        self.assertEqual(self.store.load(self.sid)["turns"], 0)


if __name__ == "__main__":
    unittest.main()
