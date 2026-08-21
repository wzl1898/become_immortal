import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import store


class LLMMetricStoreTests(unittest.TestCase):
    def test_agent_traces_keep_complete_payloads_by_turn(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "saves.db")
            with patch.object(store, "DB_PATH", db_path):
                store.init()
                sid = store.create("test", [])
                trace_id = store.record_agent_trace({
                    "save_id": sid,
                    "turn": 5,
                    "agent_type": "director_skeleton",
                    "protocol": "openai",
                    "model": "test-model",
                    "stream": False,
                    "input_messages": [{"role": "user", "content": "完整输入"}],
                    "raw_output": '{"beats":["完整输出"]}',
                    "status": "success",
                    "duration_ms": 123,
                })

                summary = store.list_agent_traces(sid)
                complete = store.list_agent_traces(
                    sid, turn=5, include_content=True
                )

                self.assertEqual(summary[0]["id"], trace_id)
                self.assertNotIn("input_messages", summary[0])
                self.assertNotIn("raw_output", summary[0])
                self.assertEqual(complete[0]["input_messages"][0]["content"], "完整输入")
                self.assertIn("完整输出", complete[0]["raw_output"])
                self.assertGreater(complete[0]["updated_at"], 0)
                self.assertIsNone(store.list_agent_traces("missing"))

                self.assertTrue(store.delete(sid))
                with sqlite3.connect(db_path) as conn:
                    count = conn.execute(
                        "SELECT COUNT(*) FROM agent_traces WHERE save_id=?", (sid,)
                    ).fetchone()[0]
                self.assertEqual(count, 0)

    def test_agent_trace_lifecycle_and_incremental_cursor(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "saves.db")
            with patch.object(store, "DB_PATH", db_path):
                store.init()
                sid = store.create("test", [])
                trace_id = store.record_agent_trace({
                    "save_id": sid, "turn": 1, "agent_type": "opening",
                    "protocol": "openai", "model": "test",
                    "input_messages": [{"role": "user", "content": "开始"}],
                    "status": "running",
                })
                running = store.list_agent_traces(sid, include_content=True)[0]
                self.assertEqual(running["status"], "running")
                cursor = running["updated_at"]
                self.assertEqual(store.list_agent_traces(sid, updated_after=cursor), [])

                store.finish_agent_trace(trace_id, {
                    "raw_output": "剧情", "status": "success", "duration_ms": 42,
                })
                changed = store.list_agent_traces(
                    sid, include_content=True, updated_after=cursor
                )
                self.assertEqual(len(changed), 1)
                self.assertEqual(changed[0]["id"], trace_id)
                self.assertEqual(changed[0]["status"], "success")
                self.assertEqual(changed[0]["raw_output"], "剧情")

    def test_lists_recent_metrics_for_save_with_limit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "saves.db")
            with patch.object(store, "DB_PATH", db_path):
                store.init()
                sid = store.create("test", [])
                for duration in (100, 200, 300):
                    store.record_llm_request_metric({
                        "save_id": sid,
                        "request_type": "narrative",
                        "protocol": "openai",
                        "model": "test-model",
                        "status": "success",
                        "duration_ms": duration,
                    })

                rows = store.list_llm_request_metrics(sid, limit=2)

                self.assertEqual([row["duration_ms"] for row in rows], [300, 200])
                self.assertIsNone(store.list_llm_request_metrics("missing"))

    def test_persists_save_specific_opportunity_reward_binding(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "saves.db")
            with patch.object(store, "DB_PATH", db_path):
                store.init()
                sid = store.create("test", [])
                payoff = {
                    "id": "payoff-1",
                    "status": "pending",
                    "created_turn": 3,
                    "binding": {
                        "opportunity_id": "ruined_temple_bones",
                        "reward_kind": "art",
                        "reward_id": "yin_qi_jue",
                    },
                }
                store.save_opportunity_reward_binding(sid, payoff)
                payoff.update({"status": "triggered", "triggered_turn": 6})
                store.save_opportunity_reward_binding(sid, payoff)

                with sqlite3.connect(db_path) as conn:
                    row = conn.execute(
                        "SELECT * FROM save_opportunity_rewards WHERE payoff_id=?",
                        ("payoff-1",),
                    ).fetchone()

                self.assertEqual(row[1], sid)
                self.assertEqual(row[2:5], ("ruined_temple_bones", "art", "yin_qi_jue"))
                self.assertEqual(row[5], "triggered")
                self.assertEqual(row[7], 6)
                bindings = store.list_opportunity_reward_bindings(sid)
                self.assertEqual(bindings[0]["reward_id"], "yin_qi_jue")


if __name__ == "__main__":
    unittest.main()
