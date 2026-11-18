"""HTTP/SSE transport for the headless CLI; no game or database imports."""

import json
import time

import httpx


class CLIError(Exception):
    def __init__(self, message, code=2, kind="usage", **details):
        super().__init__(message)
        self.code = code
        self.payload = {"type": kind, "message": message, **details}


def sse_events(lines):
    """Decode SSE frames, including comments, CRLF and multi-line data."""
    event, data, event_id = "message", [], None
    for line in lines:
        if not line:
            if data:
                yield _decode(event, data, event_id)
            event, data, event_id = "message", [], None
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "event":
            event = value
        elif field == "id":
            event_id = value
        elif field == "data":
            data.append(value)
    if data:
        # The caller still requires an explicit done event, even on clean EOF.
        yield _decode(event, data, event_id)


def _decode(event, lines, event_id=None):
    try:
        data = json.loads("\n".join(lines))
    except ValueError as exc:
        raise CLIError("SSE 返回了无效 JSON", 5, "protocol") from exc
    if not isinstance(data, dict):
        raise CLIError("SSE 数据必须为对象", 5, "protocol")
    frame = {"event": event, "data": data}
    if event_id is not None:
        frame["id"] = event_id
    return frame


class Client:
    def __init__(self, url, user, timeout=120, *, transport=None):
        self.timeout = timeout
        try:
            httpx.URL(url)
        except httpx.InvalidURL as exc:
            raise CLIError(str(exc)) from exc
        self.http = httpx.Client(
            base_url=url.rstrip("/"),
            headers={"X-User-ID": user},
            timeout=httpx.Timeout(timeout, connect=min(timeout, 8)),
            # Local tests must not travel through machine-wide proxy settings.
            trust_env=False,
            transport=transport,
        )

    def close(self):
        self.http.close()

    @staticmethod
    def _check(response):
        if response.is_success:
            return
        response.read()
        try:
            detail = response.json().get("detail", response.text)
        except (ValueError, AttributeError):
            detail = response.text
        raise CLIError(
            f"HTTP {response.status_code}: {detail}",
            3,
            "http",
            status=response.status_code,
        )

    def request(self, method, path, *, params=None, body=None, timeout=None):
        try:
            response = self.http.request(
                method,
                path,
                params=params,
                json=body,
                timeout=self.timeout if timeout is None else timeout,
            )
            self._check(response)
            try:
                return response.json()
            except ValueError as exc:
                raise CLIError("接口返回的内容不是 JSON", 5, "protocol") from exc
        except httpx.HTTPError as exc:
            raise CLIError(str(exc), 4, "transport") from exc

    def generate(self, method, path, *, sid, body=None, params=None, emit=None):
        full, run_id, last_event_id = [], None, None
        current_method, current_path, current_body = method, path, body
        for reconnect in range(4):
            headers = {"Last-Event-ID": last_event_id} if last_event_id else None
            try:
                with self.http.stream(
                    current_method,
                    current_path,
                    params=(params or {"sid": sid}) if current_method == "GET" and current_path == path else None,
                    json=current_body,
                    headers=headers,
                ) as response:
                    self._check(response)
                    if "text/event-stream" not in response.headers.get("content-type", ""):
                        raise CLIError("生成接口没有返回 SSE 流", 5, "protocol")
                    run_id = response.headers.get("X-Run-ID") or run_id
                    for frame in sse_events(response.iter_lines()):
                        event, data = frame["event"], frame["data"]
                        if frame.get("id"):
                            last_event_id = frame["id"]
                        if emit:
                            emit(frame)
                        if event == "delta":
                            text = data.get("text")
                            if not isinstance(text, str):
                                raise CLIError("delta 缺少文本", 5, "protocol")
                            full.append(text)
                        elif event == "reset":
                            full.clear()
                        elif event == "canceled":
                            raise CLIError("当前回合已取消", 5, "canceled", run_id=run_id)
                        elif event == "error":
                            raise CLIError(data.get("message", "生成失败"), 5, "generation")
                        elif event == "done":
                            return {
                                "session_id": sid,
                                "run_id": run_id,
                                "text": "".join(full),
                                "result": data,
                            }
            except httpx.HTTPError as exc:
                if not run_id or reconnect >= 3:
                    raise CLIError(str(exc), 4, "transport") from exc
            if not run_id:
                break
            current_method = "GET"
            current_path = f"/api/runs/{run_id}/events"
            current_body = None
        raise CLIError(
            "生成流在 done 事件前结束；可使用 runs show 检查持久执行状态",
            5,
            "incomplete_stream",
            run_id=run_id,
            last_event_id=last_event_id,
        )

    def wait(self, sid, timeout=120, interval=0.25):
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CLIError("等待后台任务超时", 6, "wait_timeout", session_id=sid)
            status = self.request(
                "GET", "/api/jobs", params={"sid": sid}, timeout=min(8, remaining)
            )
            if status["idle"]:
                return status
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CLIError(
                    "等待后台任务超时",
                    6,
                    "wait_timeout",
                    session_id=sid,
                    pending=status["pending"],
                )
            time.sleep(min(interval, remaining))
