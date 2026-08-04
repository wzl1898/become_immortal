import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import store


class LLMMetricStoreTests(unittest.TestCase):
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
