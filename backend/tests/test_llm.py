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

    def __init__(self, **kwargs):
        pass

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


if __name__ == "__main__":
    unittest.main()
