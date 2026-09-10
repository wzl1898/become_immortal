"""Agent-friendly command line for the same operations used by the web UI."""

import argparse
import contextlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

if __package__:
    from .cli_client import CLIError, Client
else:
    from cli_client import CLIError, Client

ROOT = Path(__file__).resolve().parent.parent

# Canonical public operations. Legacy aliases remain available through api request.
ENDPOINTS = {
    ("cards", "list"): ("GET", "/api/story-cards"),
    ("saves", "list"): ("GET", "/api/saves"),
    ("saves", "create"): ("POST", "/api/new"),
    ("saves", "load"): ("GET", "/api/load"),
    ("saves", "rename"): ("POST", "/api/rename"),
    ("saves", "delete"): ("POST", "/api/delete"),
    ("play", "opening"): ("GET", "/api/opening"),
    ("play", "action"): ("POST", "/api/action"),
    ("play", "inquiry"): ("POST", "/api/inquiry"),
    ("state", "show"): ("GET", "/api/character-state"),
    ("state", "reconcile"): ("POST", "/api/reconcile-character-state"),
    ("world", "show"): ("GET", "/api/world-state"),
    ("memory", "list"): ("GET", "/api/world-memory"),
    ("memory", "delete"): ("POST", "/api/world-memory/delete"),
    ("director", "show"): ("GET", "/api/director"),
    ("traces",): ("GET", "/api/agent-traces"),
    ("metrics",): ("GET", "/api/llm-metrics"),
    ("tokens",): ("GET", "/api/agent-token-stats"),
    ("jobs", "show"): ("GET", "/api/jobs"),
}
API_ALIASES = {
    ("GET", "/api/action"),
    ("GET", "/api/inquiry"),
    ("GET", "/api/lore"),
    ("POST", "/api/lore/delete"),
}


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise CLIError(message)


def positive(value):
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("必须是正数") from exc
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("必须是有限正数")
    return number


def nonnegative(value):
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("必须是非负整数") from exc
    if number < 0:
        raise argparse.ArgumentTypeError("必须是非负整数")
    return number


def parser():
    common = Parser(add_help=False)
    for flags, kwargs in [
        (("--url",), {"help": "服务地址，默认 STORY_URL 或 http://127.0.0.1:8888"}),
        (("--user",), {"help": "userid，默认 STORY_USER_ID 或 default"}),
        (("--timeout",), {"type": positive, "help": "HTTP 读取超时秒数，默认 120"}),
        (("--pretty",), {"action": "store_true", "help": "缩进 JSON 输出"}),
        (("--output", "-o"), {"help": "输出文件；默认 stdout"}),
    ]:
        common.add_argument(*flags, default=argparse.SUPPRESS, **kwargs)
    root = Parser(
        prog="story",
        description="文字冒险项目 CLI：默认输出 JSON，无交互确认。",
        parents=[common],
    )
    groups = {(): root.add_subparsers(dest="command", required=True)}

    def leaf(path, help):
        parent = ()
        for part in path[:-1]:
            key = (*parent, part)
            if key not in groups:
                group = groups[parent].add_parser(part, parents=[common])
                groups[key] = group.add_subparsers(required=True)
            parent = key
        command = groups[parent].add_parser(
            path[-1], help=help, description=help, parents=[common]
        )
        command.set_defaults(operation=path)
        return command

    help_text = {
        ("cards", "list"): "列出服务端可选故事卡",
        ("saves", "list"): "列出当前用户的存档",
        ("saves", "create"): "创建空存档，不自动调用模型",
        ("saves", "load"): "读取完整剧情、状态、物品、记忆和导演状态",
        ("saves", "rename"): "修改存档名",
        ("saves", "delete"): "永久删除指定用户的指定存档",
        ("play", "opening"): "生成并保存开场",
        ("play", "action"): "提交行动，生成并保存下一轮剧情",
        ("play", "inquiry"): "打听世界记忆，不推进剧情",
        ("state", "show"): "读取主角状态",
        ("state", "reconcile"): "调用模型，从最新正文校准状态",
        ("world", "show"): "读取位置、时间和已知世界信息",
        ("memory", "list"): "读取世界记忆",
        ("memory", "delete"): "按当前列表下标删除一条世界记忆",
        ("director", "show"): "读取引导、观察、事件、因果、钩子、爽点与审计状态",
        ("traces",): "读取 Agent 输入、输出、状态及增量游标",
        ("metrics",): "读取最近模型请求、耗时和状态",
        ("tokens",): "读取各 Agent 的 token 汇总",
        ("jobs", "show"): "读取当前服务进程内的后台任务",
    }
    for operation in ENDPOINTS:
        command = leaf(operation, help_text[operation])
        if operation not in {("cards", "list"), ("saves", "list"), ("saves", "create")}:
            command.add_argument("sid", help="存档 ID")
        if operation == ("saves", "create"):
            command.add_argument("--card", default="xiuxian", help="故事卡 ID")
            command.add_argument("--name")
        elif operation == ("saves", "rename"):
            command.add_argument("name")
        elif operation == ("memory", "delete"):
            command.add_argument("index", type=nonnegative)
        elif operation[0] == "play":
            if operation[1] != "opening":
                command.add_argument(
                    "text", nargs="?", help="行动/问题；与 --file 二选一"
                )
                command.add_argument("--file", help="UTF-8 输入文件；- 表示 stdin")
            command.add_argument(
                "--stream", action="store_true", help="实时输出 SSE 对应的 JSON Lines"
            )
            command.add_argument(
                "--wait", action="store_true", help="完成生成后等待后台任务结束"
            )
            command.add_argument("--wait-timeout", type=positive, default=120)
        elif operation == ("traces",):
            command.add_argument("--turn", type=nonnegative)
            command.add_argument("--limit", type=nonnegative, default=100)
            command.add_argument("--content", action="store_true")
            command.add_argument("--after", type=float)
        elif operation == ("metrics",):
            command.add_argument("--limit", type=nonnegative, default=30)

    leaf(("health",), "检查服务及用户标识，最多等待 8 秒")
    command = leaf(("jobs", "wait"), "等待当前存档的后台任务结束；idle 不等于业务成功")
    command.add_argument("sid")
    command.add_argument("--wait-timeout", type=positive, default=120)
    command.add_argument("--interval", type=positive, default=0.25)
    command = leaf(("state", "inventory"), "读取物品影子库，包含冷物品")
    command.add_argument("sid")
    command = leaf(("saves", "export"), "导出剧情、状态、记忆、导演信息和诊断数据")
    command.add_argument("sid")
    command.add_argument("--trace-limit", type=nonnegative, default=500)
    command = leaf(("cards", "show"), "读取本地完整故事卡（包含隐藏设定）")
    command.add_argument("card")
    command = leaf(("cards", "validate"), "校验本地全部故事卡或指定 card.json")
    command.add_argument("path", nargs="?")
    leaf(("prompts", "list"), "列出本地通用模板及必需变量")
    command = leaf(("prompts", "show"), "读取本地模板原文及变量")
    command.add_argument("template")
    command = leaf(("prompts", "render"), "严格渲染模板，或组合指定故事卡的系统提示词")
    command.add_argument("template")
    inputs = command.add_mutually_exclusive_group()
    inputs.add_argument("--vars", help="变量 JSON 对象")
    inputs.add_argument("--vars-file", help="变量 JSON 文件；- 表示 stdin")
    command.add_argument("--system", action="store_true")
    command.add_argument("--card", default="xiuxian")
    leaf(("api", "schema"), "读取 OpenAPI 接口契约")
    command = leaf(
        ("api", "request"), "直接调用 JSON 接口，包括兼容路由；SSE 请用 play"
    )
    command.add_argument("method", choices=["GET", "POST", "PUT", "PATCH", "DELETE"])
    command.add_argument("path")
    inputs = command.add_mutually_exclusive_group()
    inputs.add_argument("--body", help="JSON 请求体")
    inputs.add_argument("--body-file", help="JSON 文件；- 表示 stdin")
    command.add_argument("--query", action="append", default=[], metavar="KEY=VALUE")
    command = leaf(("run",), "顺序运行 JSON 测试场景，等待后台任务并在首个失败处停止")
    command.add_argument("file", help="场景文件；- 表示 stdin")
    command.add_argument("--wait-timeout", type=positive, default=120)
    command = leaf(("serve",), "前台启动无 reload 后端；Ctrl+C 停止，默认端口 8888")
    command.add_argument("--host", default="127.0.0.1")
    command.add_argument("--port", type=int, default=8888)
    command.add_argument("--data-dir", help="隔离存档目录，不使用真实 saves.db")
    command.add_argument("--no-embed", action="store_true")
    command = leaf(("test",), "运行项目 unittest 测试，失败返回非零退出码")
    command.add_argument("--pattern", default="test_*.py")
    return root


def read_text(path):
    return sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")


def read_json(text=None, path=None):
    return json.loads(read_text(path) if path is not None else text or "{}")


def input_text(args):
    if bool(args.text is not None) == bool(args.file is not None):
        raise CLIError("请提供文本或 --file，且只能提供一个")
    text = read_text(args.file) if args.file is not None else args.text
    if not text.strip():
        raise CLIError("输入不能为空")
    return text


def snapshot(client, sid, limit=500):
    return {
        **client.request("GET", "/api/load", params={"sid": sid}),
        "traces": client.request(
            "GET",
            "/api/agent-traces",
            params={"sid": sid, "include_content": True, "limit": limit},
        ),
        "metrics": client.request(
            "GET", "/api/llm-metrics", params={"sid": sid, "limit": limit}
        ),
        "tokens": client.request("GET", "/api/agent-token-stats", params={"sid": sid}),
        "jobs": client.request("GET", "/api/jobs", params={"sid": sid}),
    }


def run_scenario(client, args):
    spec = read_json(path=args.file)
    if not isinstance(spec, dict) or not isinstance(spec.get("steps"), list):
        raise CLIError("场景必须是包含 steps 数组的 JSON 对象")
    if bool(spec.get("sid")) == bool(spec.get("card")):
        raise CLIError("场景必须指定 sid 或 card，且只能指定一个")
    steps = spec["steps"]
    allowed = {"opening", "action", "inquiry", "reconcile", "checkpoint", "assert"}
    # Validate the whole script before creating a save or spending model tokens.
    for step in steps:
        if not isinstance(step, dict) or step.get("command") not in allowed:
            raise CLIError("场景包含无效 command")
        if step["command"] in {"action", "inquiry"} and (
            not isinstance(step.get("text"), str) or not step["text"].strip()
        ):
            raise CLIError("action/inquiry 步骤需要非空 text")
        if step["command"] == "assert":
            if not isinstance(step.get("path"), str) or not step["path"].startswith(
                "/"
            ):
                raise CLIError("assert.path 必须为 JSON Pointer，例如 /story_card/id")
            if sum(key in step for key in ("equals", "contains", "exists")) != 1:
                raise CLIError("assert 必须且只能指定 equals、contains 或 exists")
            if "exists" in step and not isinstance(step["exists"], bool):
                raise CLIError("exists 必须为布尔值")
    sid = spec.get("sid")
    if not sid:
        sid = client.request(
            "POST",
            "/api/new",
            body={"story_card_id": spec["card"], "name": spec.get("name")},
        )["session_id"]
    report = {"session_id": sid, "ok": False, "steps": []}
    try:
        for index, step in enumerate(steps):
            command = step["command"]
            if command in {"opening", "action", "inquiry"}:
                method, path = ENDPOINTS[("play", command)]
                body = {
                    "sid": sid,
                    "q" if command == "inquiry" else "text": step.get("text", ""),
                }
                result = client.generate(
                    method, path, sid=sid, body=body if method == "POST" else None
                )
                client.wait(sid, args.wait_timeout)
            elif command == "reconcile":
                result = client.request(
                    "POST", "/api/reconcile-character-state", body={"sid": sid}
                )
            else:
                client.wait(sid, args.wait_timeout)
                result = client.request("GET", "/api/load", params={"sid": sid})
                if command == "assert":
                    check_assertion(result, step)
                    result = {"passed": True, "path": step["path"]}
            report["steps"].append(
                {"index": index, "command": command, "result": result}
            )
        report["snapshot"] = snapshot(client, sid)
        report["ok"] = True
        return report
    except CLIError as exc:
        report["failed_step"] = len(report["steps"])
        exc.payload["report"] = report
        raise


def check_assertion(document, step):
    value, exists = document, True
    try:
        for token in step["path"].split("/")[1:]:
            token = token.replace("~1", "/").replace("~0", "~")
            if isinstance(value, list):
                if not token.isdecimal():
                    raise KeyError(token)
                value = value[int(token)]
            else:
                value = value[token]
    except (KeyError, IndexError, TypeError):
        value, exists = None, False
    if "exists" in step:
        passed = exists == step["exists"]
    elif "equals" in step:
        passed = exists and value == step["equals"]
    else:
        try:
            passed = exists and step["contains"] in value
        except TypeError:
            passed = False
    if not passed:
        raise CLIError(
            "场景断言失败", 7, "assertion", assertion=step, actual=value, exists=exists
        )


def local_command(args):
    operation = args.operation
    # Imports are local: talking to a remote service does not open a local DB or
    # require the local model configuration or story-card catalog to be valid.
    if str(ROOT / "backend") not in sys.path:
        sys.path.insert(0, str(ROOT / "backend"))
    if operation[0] == "cards":
        import story_cards

        if operation[1] == "show":
            return story_cards.get(args.card)
        cards = (
            [story_cards.load_card(Path(args.path))]
            if args.path
            else list(story_cards._CARDS.values())
        )
        return {
            "valid": True,
            "cards": [{"id": card["id"], "version": card["version"]} for card in cards],
        }
    if operation[0] == "prompts":
        import prompts

        if operation[1] == "list":
            return {
                "templates": [
                    {"name": name, "variables": sorted(prompts._VARIABLES[name])}
                    for name in sorted(prompts._TEMPLATES)
                ]
            }
        if args.template not in prompts._TEMPLATES:
            raise CLIError(f"模板不存在：{args.template}")
        if operation[1] == "show":
            return {
                "name": args.template,
                "text": prompts._TEMPLATES[args.template].template,
                "variables": sorted(prompts._VARIABLES[args.template]),
            }
        variables = read_json(args.vars, args.vars_file)
        if not isinstance(variables, dict):
            raise CLIError("模板变量必须是 JSON 对象")
        if args.system:
            if variables.keys() - {"context"}:
                raise CLIError("--system 只接受 context 变量，其余槽位由故事卡组合")
            text = prompts.render_system_prompt(
                args.template, card=prompts.story_cards.get(args.card), **variables
            )
        else:
            expected = prompts._VARIABLES[args.template]
            if variables.keys() != expected:
                raise CLIError(
                    f"模板变量不匹配：missing={sorted(expected - variables.keys())}, unexpected={sorted(variables.keys() - expected)}"
                )
            text = prompts.render_prompt(args.template, **variables)
        return {"name": args.template, "text": text}
    if operation == ("test",):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "unittest",
                "discover",
                "-s",
                "backend/tests",
                "-t",
                "backend",
                "-p",
                args.pattern,
            ],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        sys.stderr.write(result.stdout + result.stderr)
        if result.returncode:
            raise CLIError("项目测试失败", 7, "tests", returncode=result.returncode)
        return {"ok": True, "pattern": args.pattern}
    if operation == ("serve",):
        if not 1 <= args.port <= 65535:
            raise CLIError("端口必须为 1–65535")
        if args.data_dir:
            os.environ["STORY_DATA_DIR"] = str(Path(args.data_dir).resolve())
        if args.no_embed:
            os.environ["EMBED_ENABLED"] = "0"
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
        for key in (
            "ALL_PROXY",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "all_proxy",
            "http_proxy",
            "https_proxy",
        ):
            if key in os.environ:
                os.environ[key] = os.environ[key].replace("socks5h://", "socks5://")
        import uvicorn

        # Keep stdout exclusively for CLI JSON; application logs go to stderr.
        with contextlib.redirect_stdout(sys.stderr):
            uvicorn.run(
                "main:app", host=args.host, port=args.port, timeout_graceful_shutdown=3
            )
        return {"stopped": True}
    raise CLIError("未知本地命令")


def remote_command(client, args, emit):
    operation = args.operation
    if operation == ("health",):
        data = client.request("GET", "/api/story-cards", timeout=min(8, args.timeout))
        return {
            "ok": True,
            "url": args.url,
            "user": args.user,
            "story_cards": len(data["story_cards"]),
        }
    if operation == ("jobs", "wait"):
        return client.wait(args.sid, args.wait_timeout, args.interval)
    if operation == ("saves", "export"):
        return snapshot(client, args.sid, args.trace_limit)
    if operation == ("state", "inventory"):
        return {
            "inventory": client.request("GET", "/api/load", params={"sid": args.sid})[
                "inventory"
            ]
        }
    if operation == ("run",):
        return run_scenario(client, args)
    if operation == ("api", "schema"):
        return client.request("GET", "/openapi.json")
    if operation == ("api", "request"):
        if not args.path.startswith("/api/") or urlsplit(args.path).netloc:
            raise CLIError("path 必须为 /api/ 开头的相对接口路径")
        if urlsplit(args.path).path in {"/api/opening", "/api/action", "/api/inquiry"}:
            raise CLIError(
                "生成接口返回 SSE，请使用对应的 play opening/action/inquiry 命令"
            )
        query = []
        for pair in args.query:
            key, sep, value = pair.partition("=")
            if not sep or not key:
                raise CLIError("--query 格式为 KEY=VALUE")
            query.append((key, value))
        body = (
            read_json(args.body, args.body_file)
            if args.body is not None or args.body_file is not None
            else None
        )
        return client.request(args.method, args.path, params=query, body=body)
    method, path = ENDPOINTS[operation]
    params = {"sid": args.sid} if hasattr(args, "sid") else {}
    body = None
    if operation == ("saves", "create"):
        body = {"story_card_id": args.card, "name": args.name}
    elif method == "POST":
        body = {"sid": args.sid}
        if operation == ("saves", "rename"):
            body["name"] = args.name
        elif operation == ("memory", "delete"):
            body["index"] = args.index
    if operation[0] == "play":
        if operation[1] != "opening":
            body["q" if operation[1] == "inquiry" else "text"] = input_text(args)
        result = client.generate(
            method, path, sid=args.sid, body=body, emit=emit if args.stream else None
        )
        if args.wait:
            result["jobs"] = client.wait(args.sid, args.wait_timeout)
            if args.stream:
                emit({"event": "idle", "data": result["jobs"]})
        return None if args.stream else result
    if operation == ("traces",):
        params.update(limit=args.limit, include_content=args.content)
        if args.turn is not None:
            params["turn"] = args.turn
        if args.after is not None:
            if not math.isfinite(args.after) or args.after < 0:
                raise CLIError("--after 必须为非负有限时间戳")
            params["updated_after"] = args.after
    elif operation == ("metrics",):
        params["limit"] = args.limit
    return client.request(
        method, path, params=params if method == "GET" else None, body=body
    )


def main(argv=None):
    args, output, output_path = None, None, None
    try:
        args = parser().parse_args(argv)
        args.url = getattr(args, "url", os.getenv("STORY_URL", "http://127.0.0.1:8888"))
        args.user = getattr(args, "user", os.getenv("STORY_USER_ID", "default"))
        args.timeout = getattr(args, "timeout", 120)
        url = urlsplit(args.url)
        if (
            url.scheme not in {"http", "https"}
            or not url.netloc
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise CLIError("--url 必须是无账号密码、查询参数及片段的 HTTP(S) 地址")
        if not re.fullmatch(r"[A-Za-z0-9._%~-]{1,64}", args.user):
            raise CLIError("--user 必须为 1–64 位字母、数字或 . _ % ~ -")
        if getattr(args, "output", None):
            output_path = Path(args.output).resolve()
            if output_path.is_dir():
                raise CLIError("--output 必须指向文件")
            output = tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=output_path.parent,
                prefix=".story-output-",
                delete=False,
            )
        sink = output or sys.stdout

        def emit(frame):
            print(json.dumps(frame, ensure_ascii=False), file=sink, flush=True)

        local = args.operation[0] in {"prompts", "serve", "test"} or args.operation in {
            ("cards", "show"),
            ("cards", "validate"),
        }
        if local:
            result = local_command(args)
        else:
            client = Client(args.url, args.user, args.timeout)
            try:
                result = remote_command(client, args, emit)
            finally:
                client.close()
        if result is not None:
            print(
                json.dumps(
                    result,
                    ensure_ascii=False,
                    indent=2 if getattr(args, "pretty", False) else None,
                ),
                file=sink,
            )
        return 0
    except (CLIError, ValueError, OSError) as exc:
        if not isinstance(exc, CLIError):
            exc = CLIError(str(exc), 2, "input")
        error = json.dumps({"error": exc.payload}, ensure_ascii=False)
        print(error, file=sys.stderr)
        if output:
            print(error, file=output)
        return exc.code
    except KeyboardInterrupt:
        print(
            json.dumps(
                {
                    "error": {
                        "type": "interrupted",
                        "message": "用户中断；请读档确认已提交结果",
                    }
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 130
    finally:
        if output:
            output.close()
            os.replace(output.name, output_path)


if __name__ == "__main__":
    raise SystemExit(main())
