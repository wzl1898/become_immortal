import asyncio
import unittest
from unittest.mock import patch

import httpx

import llm


class _Response:
    status_code = 200

    def json(self):
        return {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1},
        }


class _RetryClient:
    attempts = 0
    timeout = None

    def __init__(self, **kwargs):
        type(self).timeout = kwargs.get("timeout")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, *args, **kwargs):
        type(self).attempts += 1
        if type(self).attempts < 3:
            raise httpx.ConnectError("temporary")
        return _Response()


class LLMRetryTests(unittest.TestCase):
    def test_complete_chat_records_full_trace_with_turn(self):
        config = llm.LLMConfig("openai", "https://example.test/v1", "key", "model", 5)
        messages = [{"role": "user", "content": "trace me"}]
        starts = []
        finishes = []
        with patch.object(llm, "_complete_openai", return_value=("raw result", {})), \
             patch.object(llm, "_record_metric"), \
             patch.object(llm, "_start_trace", side_effect=lambda trace: starts.append(trace) or 17), \
             patch.object(llm, "_finish_trace", side_effect=lambda trace_id, trace: finishes.append((trace_id, trace))):
            result = asyncio.run(llm.complete_chat(
                messages, config=config, request_type="director_test",
                session_id="save-1", turn=7,
            ))

        self.assertEqual(result, "raw result")
        self.assertEqual(starts[0]["turn"], 7)
        self.assertEqual(starts[0]["input_messages"], messages)
        self.assertEqual(finishes[0][0], 17)
        self.assertEqual(finishes[0][1]["raw_output"], "raw result")
        self.assertEqual(finishes[0][1]["status"], "success")

    def test_complete_chat_records_api_error_trace(self):
        config = llm.LLMConfig("openai", "https://example.test/v1", "key", "model", 5)
        finishes = []

        async def fail(*args, **kwargs):
            raise httpx.ConnectError("offline")

        with patch.object(llm, "_complete_openai", side_effect=fail), \
             patch.object(llm, "_record_metric"), \
             patch.object(llm, "_start_trace", return_value=18), \
             patch.object(llm, "_finish_trace", side_effect=lambda trace_id, trace: finishes.append((trace_id, trace))):
            with self.assertRaises(httpx.ConnectError):
                asyncio.run(llm.complete_chat(
                    [{"role": "user", "content": "fail"}], config=config,
                    request_type="director_test", session_id="save-1", turn=8,
                ))

        self.assertEqual(finishes[0][0], 18)
        self.assertEqual(finishes[0][1]["status"], "api_error")
        self.assertEqual(finishes[0][1]["error_type"], "ConnectError")
        self.assertEqual(finishes[0][1]["error_message"], "offline")

    def test_deepseek_v4_payload_disables_reasoning(self):
        payload = llm._openai_payload(
            [{"role": "user", "content": "hi"}],
            model="deepseek-v4-flash",
            temperature=0.2,
            max_tokens=100,
            stream=True,
        )

        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertTrue(payload["stream"])

    def test_other_openai_models_do_not_receive_deepseek_option(self):
        payload = llm._openai_payload(
            [], model="gpt-compatible", temperature=0.2, max_tokens=100, stream=False
        )

        self.assertNotIn("thinking", payload)

    def test_complete_openai_retries_connect_errors(self):
        _RetryClient.attempts = 0
        config = llm.LLMConfig("openai", "https://example.test/v1", "key", "model", 5)
        with patch.object(llm.httpx, "AsyncClient", _RetryClient), patch.object(
            llm, "CONNECT_RETRY_DELAYS", (0, 0)
        ):
            text, usage = asyncio.run(llm._complete_openai([], 0.1, 10, config))

        self.assertEqual(text, "ok")
        self.assertEqual(usage["completion_tokens"], 1)
        self.assertEqual(_RetryClient.attempts, 3)

    def test_complete_openai_can_disable_response_timeout(self):
        _RetryClient.attempts = 2
        config = llm.LLMConfig("openai", "https://example.test/v1", "key", "model", None)
        with patch.object(llm.httpx, "AsyncClient", _RetryClient):
            text, _ = asyncio.run(llm._complete_openai([], 0.1, 10, config))

        self.assertEqual(text, "ok")
        self.assertIsNone(_RetryClient.timeout.read)
        self.assertEqual(_RetryClient.timeout.connect, 15.0)


if __name__ == "__main__":
    unittest.main()
