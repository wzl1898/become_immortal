"""Observe session background work without changing scheduling or Agent decisions."""

import asyncio
import time

_TASKS: dict[asyncio.Task, dict] = {}


def track(task: asyncio.Task, session_id: str | None, kind: str) -> asyncio.Task:
    if session_id:
        _TASKS[task] = {
            "session_id": session_id,
            "kind": kind,
            "started_at": time.time(),
        }
        task.add_done_callback(lambda done: _TASKS.pop(done, None))
    return task


def status(session_id: str) -> dict:
    jobs = [
        dict(info)
        for task, info in _TASKS.items()
        if info["session_id"] == session_id and not task.done()
    ]
    return {"session_id": session_id, "idle": not jobs, "pending": jobs}
