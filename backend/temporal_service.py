"""Temporal client lifecycle, workflow start/cancel, and start reconciliation."""

from __future__ import annotations

import asyncio
import logging
import os
import time

from temporalio.client import Client, WorkflowHandle
from temporalio.exceptions import WorkflowAlreadyStartedError

import run_store
from temporal_workflows import DebugRerunWorkflow, TurnWorkflow


_LOG = logging.getLogger(__name__)
_client: Client | None = None
_connect_lock = asyncio.Lock()
_reconciler: asyncio.Task | None = None


class TemporalUnavailable(RuntimeError):
    pass


def enabled() -> bool:
    return os.getenv("TEMPORAL_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}


def task_queue() -> str:
    return os.getenv("TEMPORAL_TASK_QUEUE", "become-immortal-turns")


async def client(*, required: bool = True) -> Client | None:
    global _client
    if not enabled():
        if required:
            raise TemporalUnavailable("Temporal 未启用")
        return None
    if _client is not None:
        return _client
    async with _connect_lock:
        if _client is not None:
            return _client
        try:
            _client = await asyncio.wait_for(
                Client.connect(
                    os.getenv("TEMPORAL_ADDRESS", "127.0.0.1:7233"),
                    namespace=os.getenv("TEMPORAL_NAMESPACE", "default"),
                ),
                timeout=5,
            )
        except Exception as exc:
            if required:
                raise TemporalUnavailable(f"Temporal 不可用：{exc}") from exc
            _LOG.warning("Temporal unavailable during startup: %s", exc)
            return None
    return _client


async def start_run(run: dict) -> WorkflowHandle:
    temporal = await client(required=True)
    workflow = DebugRerunWorkflow.run if run["kind"] == "debug" else TurnWorkflow.run
    try:
        handle = await temporal.start_workflow(
            workflow,
            run["id"],
            id=run["workflow_id"],
            task_queue=task_queue(),
        )
    except WorkflowAlreadyStartedError:
        handle = temporal.get_workflow_handle(run["workflow_id"])
    await run_store.mark_run_started(
        run["id"], getattr(handle, "first_execution_run_id", None)
    )
    return handle


async def cancel_run(run: dict) -> None:
    temporal = await client(required=False)
    if temporal is None:
        return
    handle = temporal.get_workflow_handle(
        run["workflow_id"], run_id=run.get("workflow_run_id")
    )
    try:
        await handle.cancel()
    except Exception:
        # The durable DB fence is authoritative; a missing/already-terminal
        # workflow cannot make the canceled run commit.
        _LOG.exception("Temporal cancel failed for workflow %s", run["workflow_id"])


async def wait_for_activity_drain(run: dict, *, timeout: float = 10.0) -> bool:
    """Wait for canceled activities to acknowledge before deleting their ledger."""
    temporal = await client(required=False)
    if temporal is None:
        return False
    handle = temporal.get_workflow_handle(
        run["workflow_id"], run_id=run.get("workflow_run_id")
    )
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        try:
            description = await handle.describe()
            if not description.raw_description.pending_activities:
                return True
        except Exception:
            _LOG.exception("Temporal activity drain check failed for %s", run["workflow_id"])
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _LOG.warning("Temporal activities did not drain for %s", run["workflow_id"])
            return False
        await asyncio.sleep(min(0.1, remaining))


async def _reconcile_loop() -> None:
    while True:
        try:
            await asyncio.sleep(5)
            for run in await run_store.starting_runs(time.time() - 3):
                try:
                    await start_run(run)
                except TemporalUnavailable:
                    break
                except Exception:
                    _LOG.exception("failed to reconcile starting run %s", run["id"])
        except asyncio.CancelledError:
            return
        except Exception:
            _LOG.exception("Temporal start reconciler failed")


async def startup() -> None:
    global _reconciler
    if not enabled():
        return
    await run_store.init()
    await client(required=False)
    if _reconciler is None or _reconciler.done():
        _reconciler = asyncio.create_task(_reconcile_loop(), name="temporal-start-reconciler")


async def shutdown() -> None:
    global _reconciler, _client
    if _reconciler is not None:
        _reconciler.cancel()
        await asyncio.gather(_reconciler, return_exceptions=True)
        _reconciler = None
    _client = None
    await run_store.close()
