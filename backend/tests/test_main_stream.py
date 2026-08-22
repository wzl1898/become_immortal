import asyncio
import json
import unittest
from unittest.mock import patch

import main


def _event_payload(raw: str) -> dict:
    data_line = next(line for line in raw.splitlines() if line.startswith("data:"))
    return json.loads(data_line.removeprefix("data:").strip())


class MainStreamTests(unittest.TestCase):
    def test_empty_model_output_is_not_committed(self):
        committed = []
        rolled_back = []

        async def empty_stream(*args, **kwargs):
            if False:
                yield ""

        async def collect():
            return [event async for event in main._stream(
                [], committed.append, request_type="opening", session_id="sid",
                on_error=lambda: rolled_back.append(True),
            )]

        with patch.object(main, "stream_chat", empty_stream):
            events = asyncio.run(collect())

        self.assertEqual(committed, [])
        self.assertEqual(rolled_back, [True])
        self.assertEqual(len(events), 1)
        self.assertIn("未保存", _event_payload(events[0])["message"])

    def test_nonempty_model_output_is_committed(self):
        committed = []

        async def text_stream(*args, **kwargs):
            yield "正文"

        async def collect():
            return [event async for event in main._stream(
                [], committed.append, request_type="opening", session_id="sid",
            )]

        with patch.object(main, "stream_chat", text_stream):
            events = asyncio.run(collect())

        self.assertEqual(committed, ["正文"])
        self.assertTrue(any(event.startswith("event: done") for event in events))


if __name__ == "__main__":
    unittest.main()
