import os
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


if __name__ == "__main__":
    unittest.main()
