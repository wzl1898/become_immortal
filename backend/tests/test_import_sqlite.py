import sqlite3
import unittest

import import_sqlite


class ImportSqliteTests(unittest.TestCase):
    def setUp(self):
        self.source = sqlite3.connect(":memory:")
        self.source.row_factory = sqlite3.Row
        self.source.executescript(
            """
            CREATE TABLE saves (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                name TEXT NOT NULL,
                messages TEXT NOT NULL,
                transcript TEXT NOT NULL,
                turns INTEGER NOT NULL,
                lore TEXT NOT NULL,
                character_state TEXT NOT NULL,
                world_memory TEXT NOT NULL,
                world_entities TEXT NOT NULL,
                inventory TEXT NOT NULL,
                director_state TEXT NOT NULL,
                stage_summary TEXT NOT NULL,
                summary_turn INTEGER NOT NULL,
                story_card TEXT NOT NULL,
                version INTEGER NOT NULL,
                active_run_id TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE save_player_location (
                save_id TEXT PRIMARY KEY,
                region_id TEXT NOT NULL,
                location_id TEXT NOT NULL,
                site_name TEXT NOT NULL,
                location_state TEXT NOT NULL,
                intended_destination_id TEXT,
                lost_risk TEXT NOT NULL,
                updated_at REAL NOT NULL
            );
            """
        )
        self.source.execute(
            "INSERT INTO saves VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "kept", "default", "save", "[]", "[]", 0, "[]", "{}",
                "[]", "{}", "[]", "{}", "", 0, "{}", 0, None, 1.0, 1.0,
            ),
        )
        self.source.executemany(
            "INSERT INTO save_player_location VALUES (?,?,?,?,?,?,?,?)",
            [
                ("kept", "r", "l", "", "safe", None, "none", 1.0),
                ("orphan", "r", "l", "", "safe", None, "none", 1.0),
            ],
        )

    def tearDown(self):
        self.source.close()

    def test_report_excludes_rows_whose_save_no_longer_exists(self):
        report, rows = import_sqlite._table_report(self.source)

        self.assertEqual([row["save_id"] for row in rows["save_player_location"]], ["kept"])
        self.assertEqual(report["save_player_location"]["rows"], 1)
        self.assertEqual(report["save_player_location"]["skipped_orphans"], 1)

    def test_sequence_reset_targets_only_single_integer_primary_keys(self):
        executed = []

        class Connection:
            def execute(self, statement):
                executed.append(str(statement))

        import_sqlite._reset_postgres_sequences(Connection())

        sql = "\n".join(executed)
        self.assertIn("llm_request_metrics", sql)
        self.assertIn("agent_traces", sql)
        self.assertIn("save_player_knowledge", sql)
        self.assertNotIn("execution_events", sql)
        self.assertNotIn("saves', 'id", sql)


if __name__ == "__main__":
    unittest.main()
