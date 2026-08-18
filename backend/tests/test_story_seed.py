import asyncio
import hashlib
import json
import unittest
from unittest.mock import patch

import game


def _stable(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _seed_state():
    outputs = {
        "event": {
            "source": "llm",
            "model": "seed-model",
            "fallback_reason": "",
            "output": {"title": "黑风踪至"},
        },
        "pacing": {
            "source": "llm",
            "model": "seed-model",
            "fallback_reason": "",
            "output": {"intent": "逃离林隙"},
        },
    }
    manifest = [
        {
            "name": name,
            "source": value["source"],
            "model": value["model"],
            "sha256": hashlib.sha256(_stable(value).encode()).hexdigest(),
        }
        for name, value in sorted(outputs.items())
    ]
    return {
        "session_id": "seed-test",
        "director_state": {
            "story_seed": {
                "source": {"session_id": "source", "turn": 11},
                "agent_outputs": outputs,
                "agent_output_manifest": manifest,
                "integrity": "verified",
                "consumed_by": [],
            }
        },
    }


class StorySeedTests(unittest.TestCase):
    def tearDown(self):
        game._CACHE.pop("seed-test", None)

    def test_injects_every_raw_output_and_records_consumer(self):
        state = _seed_state()
        game._CACHE["seed-test"] = state
        messages = game._inject_story_seed_messages(
            [{"role": "system", "content": "director"}],
            "seed-test",
            "director_progression",
        )
        rendered = "\n".join(message["content"] for message in messages)
        self.assertIn(game.STORY_SEED_MARKER, rendered)
        self.assertIn("黑风踪至", rendered)
        self.assertIn("逃离林隙", rendered)
        self.assertEqual(
            state["director_state"]["story_seed"]["consumed_by"],
            ["director_progression"],
        )

    def test_director_and_narrative_consumers_share_verified_seed(self):
        state = _seed_state()
        game._CACHE["seed-test"] = state
        captured = []

        async def fake_complete(messages, **kwargs):
            captured.append((kwargs["request_type"], messages))
            return "{}"

        with patch.object(game, "complete_chat", fake_complete):
            asyncio.run(game._call_director_agent(
                [{"role": "system", "content": "director"}],
                "director_hook", 20, "seed-test",
            ))
        narrative = game._inject_story_seed_messages(
            [{"role": "system", "content": "narrative"}],
            "seed-test", "narrative",
        )
        self.assertIn(game.STORY_SEED_MARKER, captured[0][1][1]["content"])
        self.assertIn(game.STORY_SEED_MARKER, narrative[1]["content"])
        self.assertEqual(
            state["director_state"]["story_seed"]["consumed_by"],
            ["director_hook", "narrative"],
        )

    def test_hash_mismatch_fails_before_call(self):
        state = _seed_state()
        state["director_state"]["story_seed"]["agent_outputs"]["event"]["output"]["title"] = "被篡改"
        game._CACHE["seed-test"] = state
        with self.assertRaisesRegex(ValueError, "哈希不一致"):
            game._inject_story_seed_messages([], "seed-test", "narrative")

    def test_dynamic_state_keeps_seed_separate_from_new_agent_outputs(self):
        state = _seed_state()["director_state"]
        normalized = game._dynamic_director_state(state)
        normalized["agent_outputs"] = {"hook": {"output": "new"}}
        self.assertEqual(
            set(normalized["story_seed"]["agent_outputs"]),
            {"event", "pacing"},
        )
        self.assertNotIn("story_seed", normalized["agent_outputs"])

    def test_preserve_seed_merges_consumers_after_state_rebuild(self):
        state = _seed_state()
        state["_story_seed_consumed_by"] = ["director_pacing", "narrative"]
        rebuilt = game._preserve_story_seed(state, {"agent_outputs": {}})
        self.assertEqual(
            rebuilt["story_seed"]["consumed_by"],
            ["director_pacing", "narrative"],
        )


if __name__ == "__main__":
    unittest.main()
