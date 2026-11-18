"""Temporal activities for isolated turn branches."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import time
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

import game
import run_store
import store
from llm import stream_chat


PLANNING_NODES = (
    "character_setting",
    "guidance",
    "stage",
    "event",
    "viewpoint",
    "causal",
    "pacing",
    "progression",
    "hook",
    "skeleton",
)
POSTPROCESSING_NODES = (
    "location_reconcile",
    "character_reconcile",
    "observer",
    "audit",
    "memory",
    "next_event",
)


def shadow_id(run_id: str) -> str:
    return f"shadow:{run_id}"


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _branch_snapshot(sid: str, *, narrative: str | None = None) -> dict:
    snapshot = {
        "schema_version": "1",
        "save": store.load(sid),
        "world": store.world_snapshot(sid),
    }
    if narrative is not None:
        snapshot["narrative"] = narrative
    return _json_safe(snapshot)


@asynccontextmanager
async def _heartbeating(phase: str):
    """Heartbeat during provider silence and turn cancellation into task cancellation."""
    owner = asyncio.current_task()

    async def pulse() -> None:
        while True:
            activity.heartbeat({"phase": phase})
            if activity.is_cancelled() and owner is not None:
                owner.cancel()
                return
            await asyncio.sleep(0.5)

    heartbeat_task = asyncio.create_task(pulse())
    try:
        activity.heartbeat({"phase": phase})
        yield
    finally:
        heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task, return_exceptions=True)


async def _capture_traces(run: dict, sid: str) -> None:
    traces = store.list_agent_traces(
        sid,
        turn=int(run["target_turn"]),
        limit=500,
        include_content=True,
    ) or []
    for trace in traces:
        node_key = run_store.TRACE_NODE_MAP.get(str(trace.get("agent_type") or ""))
        if not node_key or node_key == "narrative":
            continue
        trace_id = int(trace["id"])
        attempt = await run_store.record_attempt(
            run["id"],
            node_key,
            attempt=trace_id,
            activity_id=f"trace:{sid}:{trace_id}",
            activity_attempt=1,
            request_snapshot={
                "messages": trace.get("input_messages") or [],
                "protocol": trace.get("protocol") or "",
                "model": trace.get("model") or "",
                "agent_type": trace.get("agent_type") or "",
            },
            status="running",
        )
        success = trace.get("status") == "success"
        await run_store.finish_attempt(
            attempt["id"],
            status="success" if success else "failed",
            raw_output=trace.get("raw_output") or "",
            error=(
                None
                if success
                else {
                    "type": trace.get("error_type") or "LLMError",
                    "message": trace.get("error_message") or "",
                }
            ),
            duration_ms=trace.get("duration_ms"),
            input_tokens=trace.get("input_tokens"),
            output_tokens=trace.get("output_tokens"),
            total_tokens=trace.get("total_tokens"),
        )


def _node_output(state: dict, node_key: str) -> tuple[dict, bool]:
    director = state.get("director_state") or {}
    outputs = director.get("agent_outputs") or {}
    output_key = {
        "guidance": "guidance",
        "skeleton": "director",
    }.get(node_key, node_key)
    value = outputs.get(output_key)
    if isinstance(value, dict):
        return _json_safe(value), value.get("source") == "fallback"
    if node_key == "character_setting":
        return _json_safe(director.get("character_setting") or {}), False
    return {"skipped": True}, False


@activity.defn
async def describe_run_activity(run_id: str) -> dict:
    run = await run_store.get_run(run_id)
    if run is None:
        raise ApplicationError("run not found", non_retryable=True)
    fork_key = None
    if run.get("fork_node_id"):
        nodes = await run_store.list_nodes(run_id)
        source = await run_store.list_nodes(run["parent_run_id"])
        fork_key = next(
            (node["node_key"] for node in source if node["id"] == run["fork_node_id"]),
            None,
        )
        if fork_key is None:
            fork_key = next(
                (node["node_key"] for node in nodes if node.get("source_node_id") is None),
                None,
            )
    return {
        "id": run_id,
        "kind": run["kind"],
        "fork_node_key": fork_key,
        "fork_ordinal": (
            run_store.TURN_NODE_KEYS.index(fork_key)
            if fork_key in run_store.TURN_NODE_KEYS
            else 0
        ),
        "target_turn": run["target_turn"],
    }


async def _restore_debug_branch(run: dict, sid: str) -> None:
    if run["kind"] != "debug":
        return
    source_nodes = await run_store.list_nodes(run["parent_run_id"])
    fork = next(
        (node for node in source_nodes if node["id"] == run.get("fork_node_id")),
        None,
    )
    if fork is None:
        raise ApplicationError("debug fork node is missing", non_retryable=True)
    snapshot = await run_store.get_snapshot(
        run["parent_run_id"], fork["node_key"], "before"
    )
    if snapshot is None:
        raise ApplicationError("debug fork snapshot is missing", non_retryable=True)
    store.restore_run_clone(sid, snapshot["state"])
    game._CACHE.pop(sid, None)


@activity.defn
async def initialize_debug_activity(run_id: str) -> dict:
    run = await run_store.get_run(run_id)
    if run is None or run["kind"] != "debug":
        raise ApplicationError("debug run not found", non_retryable=True)
    sid = shadow_id(run_id)
    store.delete_run_clone(sid)
    store.clone_for_run(run["sid"], sid)
    await _restore_debug_branch(run, sid)
    return {"initialized": True}


@activity.defn
async def prepare_turn_activity(run_id: str) -> dict:
    async with _heartbeating("prepare"):
        return await _prepare_turn(run_id)


async def _prepare_turn(run_id: str) -> dict:
    run = await run_store.get_run(run_id)
    if run is None:
        raise ApplicationError("run not found", non_retryable=True)
    skeleton = await run_store.get_node(run_id, "skeleton")
    if skeleton and skeleton["status"] == "completed":
        return {"prepared": True}

    sid = shadow_id(run_id)
    if activity.info().attempt > 1:
        game._CACHE.pop(sid, None)
        store.delete_run_clone(sid)
    store.clone_for_run(run["sid"], sid)
    await _restore_debug_branch(run, sid)
    game._CACHE.pop(sid, None)
    state = game._get(sid)
    if state is None:
        raise ApplicationError("unable to create branch save", non_retryable=True)

    before = _branch_snapshot(sid)
    for node_key in PLANNING_NODES:
        node = await run_store.get_node(run_id, node_key)
        if node and node["status"] == "reused":
            continue
        await run_store.mark_node_started(run_id, node_key)
        await run_store.save_snapshot(run_id, node_key, "before", before)

    try:
        if run["input_kind"] == "opening":
            messages = await game.prepare_opening(sid)
        else:
            messages = await game.prepare_action(sid, run.get("action") or "")
        await game.wait_for_preparation_tasks(sid)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        game.rollback_prepared_action(sid)
        raise ApplicationError(str(exc), type=type(exc).__name__) from exc

    # Planning mutates the branch cache. Persist it before another Worker can
    # execute the narrative/post-processing activities.
    state = game._get(sid) or state
    store.save_director_state(sid, state.get("director_state") or {})
    store.save_inventory(sid, state.get("inventory") or [])
    await _capture_traces(run, sid)
    after = _branch_snapshot(sid)
    for node_key in PLANNING_NODES:
        node = await run_store.get_node(run_id, node_key)
        if node and node["status"] == "reused":
            continue
        output, degraded = _node_output(state, node_key)
        if node_key == "skeleton":
            output = {**output, "messages": _json_safe(messages)}
        await run_store.save_snapshot(run_id, node_key, "after", after)
        await run_store.complete_node(
            run_id, node_key, output, degraded=degraded
        )
    return {"prepared": True}


@activity.defn
async def narrative_activity(run_id: str) -> dict:
    async with _heartbeating("narrative"):
        return await _run_narrative(run_id)


async def _run_narrative(run_id: str) -> dict:
    run = await run_store.get_run(run_id)
    if run is None:
        raise ApplicationError("run not found", non_retryable=True)
    node = await run_store.get_node(run_id, "narrative")
    if node and node["status"] == "completed":
        return {"completed": True}
    skeleton = await run_store.get_node(run_id, "skeleton")
    messages = ((skeleton or {}).get("normalized_output") or {}).get("messages")
    if not isinstance(messages, list):
        raise ApplicationError("planning result has no narrative messages", non_retryable=True)

    sid = shadow_id(run_id)
    await run_store.mark_node_started(run_id, "narrative")
    await run_store.save_snapshot(
        run_id, "narrative", "before", _branch_snapshot(sid)
    )
    info = activity.info()
    if info.attempt > 1:
        await run_store.append_event(
            run_id,
            "reset",
            {"attempt": info.attempt},
            node_id=node["id"] if node else None,
        )
    attempt = await run_store.record_attempt(
        run_id,
        "narrative",
        attempt=info.attempt,
        activity_id=info.activity_id,
        activity_attempt=info.attempt,
        request_snapshot={
            "messages": messages,
            "request_type": run["input_kind"] if run["input_kind"] == "opening" else "narrative",
        },
    )
    started = time.monotonic()
    pieces: list[str] = []
    try:
        async for piece in stream_chat(
            messages,
            request_type=("opening" if run["input_kind"] == "opening" else "narrative"),
            session_id=sid,
            turn=int(run["target_turn"]),
        ):
            if activity.is_cancelled():
                raise asyncio.CancelledError
            pieces.append(piece)
            activity.heartbeat(len(pieces), sum(map(len, pieces)))
            await run_store.append_event(
                run_id,
                "delta",
                {"text": piece, "attempt": info.attempt},
                node_id=node["id"] if node else None,
                attempt_id=attempt["id"],
            )
    except asyncio.CancelledError:
        await run_store.finish_attempt(
            attempt["id"],
            status="canceled",
            raw_output="".join(pieces),
            duration_ms=round((time.monotonic() - started) * 1000),
            error={"type": "CancelledError", "message": "activity canceled"},
        )
        raise
    except Exception as exc:
        await run_store.finish_attempt(
            attempt["id"],
            status="failed",
            raw_output="".join(pieces),
            duration_ms=round((time.monotonic() - started) * 1000),
            error={"type": type(exc).__name__, "message": str(exc)},
        )
        raise ApplicationError(str(exc), type=type(exc).__name__) from exc
    narrative = "".join(pieces)
    if not narrative.strip():
        await run_store.finish_attempt(
            attempt["id"],
            status="failed",
            raw_output=narrative,
            duration_ms=round((time.monotonic() - started) * 1000),
            error={"type": "EmptyNarrative", "message": "empty narrative"},
        )
        raise ApplicationError("模型没有返回可显示的正文")
    await run_store.finish_attempt(
        attempt["id"],
        status="success",
        raw_output=narrative,
        normalized_output={"text": narrative},
        duration_ms=round((time.monotonic() - started) * 1000),
    )
    await run_store.save_snapshot(
        run_id,
        "narrative",
        "after",
        _branch_snapshot(sid, narrative=narrative),
    )
    await run_store.complete_node(run_id, "narrative", {"text": narrative})
    return {"completed": True}


@activity.defn
async def postprocess_turn_activity(run_id: str) -> dict:
    async with _heartbeating("postprocess"):
        return await _postprocess_turn(run_id)


async def _postprocess_turn(run_id: str) -> dict:
    run = await run_store.get_run(run_id)
    if run is None:
        raise ApplicationError("run not found", non_retryable=True)
    observer = await run_store.get_node(run_id, "observer")
    if observer and observer["status"] == "completed":
        return {"completed": True}
    narrative_node = await run_store.get_node(run_id, "narrative")
    narrative = ((narrative_node or {}).get("normalized_output") or {}).get("text")
    if not isinstance(narrative, str) or not narrative.strip():
        raise ApplicationError("narrative result is missing", non_retryable=True)
    sid = shadow_id(run_id)
    game._CACHE.pop(sid, None)
    state = game._get(sid)
    if state is None:
        raise ApplicationError("branch save is missing", non_retryable=True)
    before = _branch_snapshot(sid, narrative=narrative)
    for node_key in POSTPROCESSING_NODES:
        node = await run_store.get_node(run_id, node_key)
        if node and node["status"] == "reused":
            continue
        await run_store.mark_node_started(run_id, node_key)
        await run_store.save_snapshot(run_id, node_key, "before", before)
    await run_store.append_event(
        run_id, "postprocessing", {"status": "started"}
    )
    try:
        if int(state.get("turns") or 0) < int(run["target_turn"]):
            await game.commit(
                sid,
                run.get("action") if run["input_kind"] == "action" else None,
                narrative,
                durable_postprocessing=True,
            )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise ApplicationError(str(exc), type=type(exc).__name__) from exc
    await _capture_traces(run, sid)
    state = game._get(sid) or {}
    after = _branch_snapshot(sid, narrative=narrative)
    outputs = (state.get("director_state") or {}).get("agent_outputs") or {}
    node_outputs = {
        "location_reconcile": {"world": after.get("world")},
        "character_reconcile": state.get("character_state") or {},
        "observer": outputs.get("observer") or {"preserved": True},
        "audit": outputs.get("audit") or {"preserved": True},
        "memory": {"count": len(state.get("world_memory") or [])},
        "next_event": outputs.get("next_event") or {"skipped": True},
    }
    for node_key in POSTPROCESSING_NODES:
        node = await run_store.get_node(run_id, node_key)
        if node and node["status"] == "reused":
            continue
        await run_store.save_snapshot(run_id, node_key, "after", after)
        await run_store.complete_node(run_id, node_key, _json_safe(node_outputs[node_key]))
    return {"completed": True}


@activity.defn
async def commit_turn_activity(run_id: str) -> dict:
    run = await run_store.get_run(run_id)
    if run is None:
        raise ApplicationError("run not found", non_retryable=True)
    sid = shadow_id(run_id)
    await run_store.mark_node_started(run_id, "commit")
    await run_store.save_snapshot(run_id, "commit", "before", _branch_snapshot(sid))
    if run["kind"] == "debug":
        payload = {
            "target_turn": run["target_turn"],
            "source_run_id": run.get("parent_run_id"),
        }
        await run_store.complete_debug_run(run_id, payload)
    else:
        payload = await run_store.commit_shadow(run_id, sid)
    game._CACHE.pop(sid, None)
    store.delete_run_clone(sid)
    return payload


@activity.defn
async def fail_turn_activity(args: dict) -> None:
    run_id = args["run_id"]
    sid = shadow_id(run_id)
    try:
        if store.exists(sid):
            await run_store.save_snapshot(
                run_id, "commit", "before", _branch_snapshot(sid)
            )
    except Exception:
        pass
    await run_store.fail_run(run_id, args.get("error") or {"message": "workflow failed"})
    game._CACHE.pop(sid, None)
    store.delete_run_clone(sid)


@activity.defn
async def finalize_cancel_activity(run_id: str) -> None:
    await run_store.finalize_canceled(run_id)
    sid = shadow_id(run_id)
    game._CACHE.pop(sid, None)
    store.delete_run_clone(sid)
