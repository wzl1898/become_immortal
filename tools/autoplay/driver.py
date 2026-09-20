"""HTTP/SSE 驱动层。

直接复用 backend/cli_client.py 的 Client：它明确声明为纯传输层（不 import game 或
store），已经处理了 SSE 分帧、退出码语义和代理绕过。这里只做薄封装，把 CLI 的
CLIError 转成本框架的 DriverError，并补上自动游玩需要的便捷方法。
"""

from __future__ import annotations

import sys
from pathlib import Path

# cli_client 位于 backend/ 下，不在包路径里；显式加入以便复用而不复制代码。
_BACKEND = Path(__file__).resolve().parents[2] / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import cli_client  # noqa: E402


class DriverError(Exception):
    """驱动服务时的失败：网络、HTTP、协议或生成错误。"""

    def __init__(self, message: str, *, kind: str = "driver", code: int = 1):
        super().__init__(message)
        self.kind = kind
        self.code = code


class Driver:
    def __init__(self, base_url: str, user_id: str, timeout: int = 120):
        self.base_url = base_url
        self.user_id = user_id
        try:
            self.client = cli_client.Client(base_url, user_id, timeout=timeout)
        except cli_client.CLIError as exc:
            raise DriverError(str(exc), kind=exc.payload.get("type", "usage")) from exc

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> "Driver":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def _call(self, method: str, path: str, **kwargs):
        try:
            return self.client.request(method, path, **kwargs)
        except cli_client.CLIError as exc:
            raise DriverError(str(exc), kind=exc.payload.get("type", "http")) from exc

    # ---- 只读接口 ----

    def health(self) -> dict:
        """探测服务与 userid；沿用 CLI 的 8 秒上限。"""
        return self._call("GET", "/api/story-cards", timeout=8)

    def create_save(self, card: str, name: str | None) -> str:
        data = self._call(
            "POST", "/api/new", body={"story_card_id": card, "name": name}
        )
        sid = data.get("session_id")
        if not sid:
            raise DriverError("服务未返回 session_id", kind="protocol")
        return sid

    def load(self, sid: str) -> dict:
        return self._call("GET", "/api/load", params={"sid": sid})

    def jobs(self, sid: str) -> dict:
        return self._call("GET", "/api/jobs", params={"sid": sid}, timeout=8)

    def traces(self, sid: str, turn: int, limit: int = 200) -> list[dict]:
        data = self._call(
            "GET",
            "/api/agent-traces",
            params={
                "sid": sid,
                "turn": turn,
                "include_content": True,
                "limit": limit,
            },
        )
        return data.get("traces") or []

    def token_stats(self, sid: str) -> dict:
        return self._call("GET", "/api/agent-token-stats", params={"sid": sid})

    # ---- 生成接口 ----

    def opening(self, sid: str) -> dict:
        return self._generate("GET", "/api/opening", sid, None)

    def action(self, sid: str, text: str) -> dict:
        return self._generate("POST", "/api/action", sid, {"sid": sid, "text": text})

    def _generate(self, method: str, path: str, sid: str, body) -> dict:
        try:
            return self.client.generate(method, path, sid=sid, body=body)
        except cli_client.CLIError as exc:
            raise DriverError(
                str(exc), kind=exc.payload.get("type", "generation"), code=exc.code
            ) from exc

    def wait_idle(self, sid: str, timeout: int, interval: float = 0.25) -> dict:
        """等后台任务结束。

        必须在抓 trace 之前调用：store.reap_stale_agent_traces 会把超过 180 秒仍为
        running 的 trace 标成 timeout，不等就会把正常调用误读成失败。
        """
        try:
            return self.client.wait(sid, timeout, interval)
        except cli_client.CLIError as exc:
            raise DriverError(
                str(exc), kind=exc.payload.get("type", "wait_timeout"), code=exc.code
            ) from exc


def latest_narration(transcript: list[dict]) -> str:
    """取 transcript 里最后一条 narration。

    不能简单取 transcript[-1]：最后一块可能是 player 行动或 hint。
    """
    for block in reversed(transcript or []):
        if block.get("role") == "narration":
            text = (block.get("text") or "").strip()
            if text:
                return text
    return ""


def latest_player_action(transcript: list[dict]) -> str:
    for block in reversed(transcript or []):
        if block.get("role") == "player":
            text = (block.get("text") or "").strip()
            if text:
                return text
    return ""
