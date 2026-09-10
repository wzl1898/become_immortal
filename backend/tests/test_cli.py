import asyncio
import contextlib
import io
import json
import os
from pathlib import Path
import socket
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx

import background
import cli
from cli_client import CLIError, Client, sse_events
import main

ROOT = Path(__file__).resolve().parents[2]


class CLITest(unittest.TestCase):
    def test_all_public_api_functions_have_commands(self):
        actual = {
            (method, route.path)
            for route in main.app.routes
            if getattr(route, "path", "").startswith("/api/")
            for method in route.methods
        }
        self.assertEqual(actual, set(cli.ENDPOINTS.values()) | cli.API_ALIASES)

    def test_global_options_work_before_or_after_subcommands(self):
        args = cli.parser().parse_args(
            ["--user", "alice", "saves", "--url", "http://localhost:8901", "list"]
        )
        self.assertEqual(args.user, "alice")
        self.assertEqual(args.url, "http://localhost:8901")
        args = cli.parser().parse_args(["saves", "list", "--user", "bob"])
        self.assertEqual(args.user, "bob")

    def test_sse_comments_multiline_and_unicode(self):
        frames = list(
            sse_events(
                [
                    ": heartbeat",
                    "",
                    "event: delta",
                    'data: {"text":',
                    'data: "你好"}',
                    "",
                    "event: done",
                    "data: {}",
                    "",
                ]
            )
        )
        self.assertEqual(
            frames,
            [
                {"event": "delta", "data": {"text": "你好"}},
                {"event": "done", "data": {}},
            ],
        )

    def test_stream_errors_and_truncated_stream_are_not_success(self):
        for content, kind in [
            ('event: delta\ndata: {"text":"部分内容"}\n\n', "incomplete_stream"),
            ('event: error\ndata: {"message":"模型失败"}\n\n', "generation"),
            ("event: done\ndata: invalid\n\n", "protocol"),
        ]:
            client = Client(
                "http://test",
                "alice",
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(
                        200, headers={"content-type": "text/event-stream"}, text=content
                    )
                ),
            )
            with self.assertRaises(CLIError) as caught:
                client.generate("GET", "/api/opening", sid="one")
            self.assertEqual(caught.exception.code, 5)
            self.assertEqual(caught.exception.payload["type"], kind)
            client.close()

    def test_http_and_connection_errors_have_distinct_exit_codes(self):
        for code in (400, 404, 500):
            client = Client(
                "http://test",
                "alice",
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(code, json={"detail": "失败"})
                ),
            )
            with self.assertRaises(CLIError) as caught:
                client.request("GET", "/api/load")
            self.assertEqual(caught.exception.code, 3)
            self.assertEqual(caught.exception.payload["status"], code)
            client.close()

        def disconnected(request):
            raise httpx.ConnectError("offline")

        client = Client(
            "http://test", "alice", transport=httpx.MockTransport(disconnected)
        )
        with self.assertRaises(CLIError) as caught:
            client.request("GET", "/api/saves")
        self.assertEqual(caught.exception.code, 4)
        client.close()

    def test_wait_is_bounded_and_keeps_last_pending_state(self):
        client = Client(
            "http://test",
            "alice",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, json={"idle": False, "pending": [{"kind": "memory_extract"}]}
                )
            ),
        )
        with self.assertRaises(CLIError) as caught:
            client.wait("sid", timeout=0.02, interval=0.005)
        self.assertEqual(caught.exception.code, 6)
        client.close()

    def test_background_tracking_is_session_scoped_and_includes_followups(self):
        async def check():
            gate = asyncio.Event()

            async def child():
                await gate.wait()

            async def parent():
                background.track(asyncio.create_task(child()), "alice", "followup")

            task = background.track(asyncio.create_task(parent()), "alice", "parent")
            self.assertFalse(background.status("alice")["idle"])
            self.assertTrue(background.status("bob")["idle"])
            await task
            self.assertEqual(
                background.status("alice")["pending"][0]["kind"], "followup"
            )
            gate.set()
            await asyncio.sleep(0)
            self.assertTrue(background.status("alice")["idle"])

        asyncio.run(check())

    def test_invalid_input_is_machine_readable_and_does_not_call_server(self):
        for arguments in (
            ["play", "action", "sid"],
            ["health", "--timeout", "nan"],
            ["health", "--user", "bad user"],
        ):
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(
                stderr
            ), patch.object(Client, "request") as request:
                code = cli.main(arguments)
            self.assertEqual(code, 2)
            self.assertEqual(stdout.getvalue(), "")
            self.assertIn("error", json.loads(stderr.getvalue()))
            request.assert_not_called()

    def test_json_pointer_assertions_support_lists_and_escaped_keys(self):
        value = {"a/b": [{"~key": "你好"}]}
        cli.check_assertion(value, {"path": "/a~1b/0/~0key", "equals": "你好"})
        cli.check_assertion(value, {"path": "/missing", "exists": False})
        with self.assertRaises(CLIError) as caught:
            cli.check_assertion(value, {"path": "/a~1b/-1/~0key", "exists": True})
        self.assertEqual(caught.exception.code, 7)

    def test_raw_generation_requests_are_rejected_before_sending(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(
            stderr
        ), patch.object(Client, "request") as request:
            code = cli.main(["api", "request", "GET", "/api/action?sid=one&text=test"])
        self.assertEqual(code, 2)
        request.assert_not_called()


class CLIIntegrationTest(unittest.TestCase):
    """Real CLI subprocesses -> TCP -> FastAPI -> engine -> isolated SQLite."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="story-cli-test-")
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        cls.url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        env = {
            **os.environ,
            "PYTHONPATH": str(ROOT / "backend"),
            "STORY_DATA_DIR": cls.tmp.name,
            "EMBED_ENABLED": "0",
        }
        cls.log = open(Path(cls.tmp.name) / "server.log", "w+")
        cls.server = subprocess.Popen(
            [
                sys.executable,
                str(ROOT / "backend/tests/cli_fixture_server.py"),
                "--fd",
                str(listener.fileno()),
            ],
            cwd=ROOT,
            env=env,
            pass_fds=(listener.fileno(),),
            stdout=cls.log,
            stderr=cls.log,
        )
        listener.close()
        try:
            with httpx.Client(trust_env=False, timeout=0.2) as client:
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    try:
                        if (
                            client.get(
                                cls.url + "/api/story-cards",
                                headers={"X-User-ID": "health"},
                            ).status_code
                            == 200
                        ):
                            return
                    except httpx.HTTPError:
                        pass
                    if cls.server.poll() is not None:
                        break
                    time.sleep(0.05)
            raise RuntimeError("isolated CLI test server did not start")
        except BaseException:
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.server.terminate()
        try:
            cls.server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.server.kill()
            cls.server.wait(timeout=5)
        cls.log.close()
        cls.tmp.cleanup()

    def command(self, *arguments, user="cli-test", input=None, expected=0):
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "backend/cli.py"),
                "--url",
                self.url,
                "--user",
                user,
                *arguments,
            ],
            cwd=self.tmp.name,
            input=input,
            text=True,
            capture_output=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, expected, result.stderr)
        if expected:
            return json.loads(result.stderr)
        return json.loads(result.stdout)

    def test_full_game_lifecycle_and_inspection_commands(self):
        self.assertTrue(self.command("health")["ok"])
        self.assertTrue(self.command("cards", "list")["story_cards"])
        sid = self.command(
            "saves", "create", "--card", "orbital", "--name", "CLI 科幻测试"
        )["session_id"]
        initial = self.command("saves", "load", sid)
        self.assertEqual(initial["story_card"]["id"], "orbital")
        self.assertEqual(initial["transcript"], [])
        opening = self.command("play", "opening", sid, "--wait")
        self.assertIn("负责人", opening["text"])
        self.assertTrue(opening["jobs"]["idle"])
        action = "核对记录\n保持原文中的 $() 与 `符号`"
        self.command("play", "action", sid, "--file", "-", "--wait", input=action)
        inquiry = self.command("play", "inquiry", sid, "负责人是谁？", "--wait")
        self.assertIn("负责人", inquiry["text"])
        self.command("state", "reconcile", sid)
        self.assertIn("energy", self.command("state", "show", sid)["character_state"])
        self.assertTrue(self.command("state", "inventory", sid)["inventory"])
        self.assertEqual(
            self.command("world", "show", sid)["world_state"]["location"][
                "location_name"
            ],
            "维修舱",
        )
        memory = self.command("memory", "list", sid)["world_memory"]
        self.assertTrue(memory)
        self.command("memory", "delete", sid, "0")
        self.assertEqual(self.command("director", "show", sid)["turns"], 2)
        self.assertIn("traces", self.command("traces", sid, "--turn", "2", "--content"))
        self.assertIn("requests", self.command("metrics", sid))
        self.command("tokens", sid)
        self.assertTrue(self.command("jobs", "wait", sid)["idle"])
        exported = self.command("saves", "export", sid)
        self.assertEqual(exported["transcript"][1]["text"], action)
        self.assertTrue(exported["jobs"]["idle"])
        self.command("saves", "rename", sid, "新的名字")
        renamed = next(
            row for row in self.command("saves", "list")["saves"] if row["id"] == sid
        )
        self.assertEqual(renamed["name"], "新的名字")
        self.command("saves", "load", sid, user="outsider", expected=3)
        self.command("jobs", "show", sid, user="outsider", expected=3)
        self.command("saves", "delete", sid)
        self.command("saves", "load", sid, expected=3)

    def test_streaming_jsonl_and_generation_failure_do_not_commit(self):
        sid = self.command("saves", "create")["session_id"]
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "backend/cli.py"),
                "--url",
                self.url,
                "--user",
                "cli-test",
                "play",
                "opening",
                sid,
                "--stream",
                "--wait",
            ],
            text=True,
            capture_output=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        frames = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertIn("delta", {frame["event"] for frame in frames})
        self.assertEqual(frames[-2]["event"], "done")
        self.assertEqual(frames[-1]["event"], "idle")
        before = self.command("saves", "load", sid)["transcript"]
        error = self.command("play", "action", sid, "FAIL_GENERATION", expected=5)
        self.assertEqual(error["error"]["type"], "generation")
        self.assertEqual(self.command("saves", "load", sid)["transcript"], before)

    def test_scenario_assertions_stop_before_later_actions(self):
        spec = {
            "card": "orbital",
            "steps": [
                {"command": "assert", "path": "/story_card/id", "equals": "orbital"},
                {"command": "opening"},
                {"command": "action", "text": "核对记录"},
                {
                    "command": "assert",
                    "path": "/transcript/1/text",
                    "equals": "核对记录",
                },
            ],
        }
        result = self.command("run", "-", input=json.dumps(spec))
        self.assertTrue(result["ok"])
        sid = result["session_id"]
        failure = {
            "sid": sid,
            "steps": [
                {"command": "assert", "path": "/story_card/id", "equals": "wrong"},
                {"command": "action", "text": "不得执行"},
            ],
        }
        error = self.command("run", "-", input=json.dumps(failure), expected=7)
        self.assertEqual(error["error"]["report"]["failed_step"], 0)
        self.assertEqual(self.command("director", "show", sid)["turns"], 2)

    def test_invalid_scenario_and_card_do_not_create_saves(self):
        before = self.command("saves", "list")["saves"]
        self.command("saves", "create", "--card", "missing", expected=3)
        invalid = {
            "card": "orbital",
            "steps": [{"command": "opening"}, {"command": "not-a-command"}],
        }
        self.command("run", "-", input=json.dumps(invalid), expected=2)
        self.assertEqual(self.command("saves", "list")["saves"], before)

    def test_offline_templates_cards_and_raw_api(self):
        self.assertTrue(self.command("cards", "validate")["valid"])
        self.assertEqual(self.command("cards", "show", "orbital")["id"], "orbital")
        self.assertTrue(self.command("prompts", "list")["templates"])
        template = self.command("prompts", "show", "shared/player_action")
        self.assertIn("action", template["variables"])
        rendered = self.command(
            "prompts",
            "render",
            "shared/player_action",
            "--vars-file",
            "-",
            input='{"action":"检查 ${literal}"}',
        )
        self.assertIn("检查 ${literal}", rendered["text"])
        system = self.command(
            "prompts",
            "render",
            "engine/narrative/system",
            "--system",
            "--card",
            "orbital",
        )
        self.assertIn("晨曦轨道站", system["text"])
        self.assertNotIn("灵力", system["text"])
        self.command("prompts", "render", "shared/player_action", expected=2)
        self.assertIn("/api/jobs", self.command("api", "schema")["paths"])
        self.assertIn("saves", self.command("api", "request", "GET", "/api/saves"))

    def test_output_replaces_input_after_render_and_invalid_cards_report_json(self):
        variables = Path(self.tmp.name) / "variables.json"
        variables.write_text('{"action":"保留输入"}', encoding="utf-8")
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "backend/cli.py"),
                "prompts",
                "render",
                "shared/player_action",
                "--vars-file",
                str(variables),
                "--output",
                str(variables),
            ],
            text=True,
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("保留输入", json.loads(variables.read_text())["text"])
        card_dir = Path(self.tmp.name) / "orbital"
        shutil.copytree(ROOT / "backend/story_cards/orbital", card_dir)
        card = json.loads((card_dir / "card.json").read_text())
        card["initial"]["location"] = None
        (card_dir / "card.json").write_text(json.dumps(card))
        self.command("cards", "validate", str(card_dir), expected=2)
        self.command(
            "prompts",
            "render",
            "shared/player_action",
            "--vars",
            '{"name":"wrong"}',
            expected=2,
        )


if __name__ == "__main__":
    unittest.main()
