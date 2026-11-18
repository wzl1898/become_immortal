"""Durable execution ledger and save/run concurrency rules."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
import uuid
from typing import Any, Iterable

from sqlalchemy import and_, delete, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

import db
import constraints
import store


ACTIVE_STATUSES = {"starting", "running", "canceling"}
TERMINAL_STATUSES = {
    "succeeded",
    "failed",
    "canceled",
    "commit_conflict",
}

TURN_NODE_KEYS = (
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
    "narrative",
    "location_reconcile",
    "character_reconcile",
    "observer",
    "audit",
    "memory",
    "next_event",
    "commit",
)

TRACE_NODE_MAP = {
    "character_setting": "character_setting",
    "guidance_conflict": "guidance",
    "conflict_guidance": "guidance",
    "stage_guidance": "stage",
    "director_stage": "stage",
    "director_event": "event",
    "director_viewpoint": "viewpoint",
    "director_causal": "causal",
    "director_pacing": "pacing",
    "director_progression": "progression",
    "director_hook": "hook",
    "director_skeleton": "skeleton",
    "opening": "narrative",
    "narrative": "narrative",
    "location_state": "location_reconcile",
    "state_reconcile": "character_reconcile",
    "narrative_observer": "observer",
    "director_audit": "audit",
    "memory_extract": "memory",
    "next_event": "next_event",
}


class RunStoreError(RuntimeError):
    code = "run_store_error"


class SaveNotFound(RunStoreError):
    code = "save_not_found"


class RunNotFound(RunStoreError):
    code = "run_not_found"


class TurnConflict(RunStoreError):
    code = "turn_conflict"


class ActiveRunConflict(RunStoreError):
    code = "active_run"


class AlreadyCommitted(RunStoreError):
    code = "already_committed"


class CommitConflict(RunStoreError):
    code = "commit_conflict"


_engine: AsyncEngine | None = None


def engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = db.make_async_engine()
    return _engine


async def init() -> None:
    async with engine().begin() as connection:
        await connection.run_sync(db.metadata.create_all)


async def close() -> None:
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None


def _now() -> float:
    return time.time()


def _hash_action(action: str | None) -> str:
    return hashlib.sha256((action or "").encode("utf-8")).hexdigest()


def _revision() -> str:
    configured = os.getenv("CODE_REVISION", "").strip()
    if configured:
        return configured
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=os.path.dirname(os.path.dirname(__file__)),
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=2,
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _prompt_bundle_version() -> str:
    return os.getenv("PROMPT_BUNDLE_VERSION", _revision())


def _row(row) -> dict | None:
    return dict(row._mapping) if row is not None else None


async def _append_event_tx(
    connection: AsyncConnection,
    run_id: str,
    event_type: str,
    payload: dict,
    *,
    node_id: str | None = None,
    attempt_id: str | None = None,
) -> int:
    sequence = (
        await connection.execute(
            update(db.execution_runs)
            .where(db.execution_runs.c.id == run_id)
            .values(
                event_sequence=db.execution_runs.c.event_sequence + 1,
                updated_at=_now(),
            )
            .returning(db.execution_runs.c.event_sequence)
        )
    ).scalar_one()
    await connection.execute(
        insert(db.execution_events).values(
            run_id=run_id,
            sequence=sequence,
            node_id=node_id,
            attempt_id=attempt_id,
            event_type=event_type,
            payload=payload,
            created_at=_now(),
        )
    )
    return int(sequence)


async def append_event(
    run_id: str,
    event_type: str,
    payload: dict,
    *,
    node_id: str | None = None,
    attempt_id: str | None = None,
) -> int:
    async with engine().begin() as connection:
        run = _row(
            (
                await connection.execute(
                    select(db.execution_runs)
                    .where(db.execution_runs.c.id == run_id)
                    .with_for_update()
                )
            ).first()
        )
        if run is None:
            raise RunNotFound("执行不存在")
        if run["status"] not in {"starting", "running"} or run.get("cancel_requested_at") is not None:
            return int(run["event_sequence"])
        return await _append_event_tx(
            connection,
            run_id,
            event_type,
            payload,
            node_id=node_id,
            attempt_id=attempt_id,
        )


async def reserve_primary_run(
    sid: str,
    *,
    input_kind: str,
    action: str | None,
    client_request_id: str | None,
    base_turn: int | None,
    base_version: int | None,
) -> tuple[dict, bool]:
    """Reserve a turn or return the idempotently existing request."""
    client_request_id = (client_request_id or "").strip() or None
    async with engine().begin() as connection:
        if client_request_id:
            existing = (
                await connection.execute(
                    select(db.execution_runs).where(
                        and_(
                            db.execution_runs.c.sid == sid,
                            db.execution_runs.c.client_request_id == client_request_id,
                        )
                    )
                )
            ).first()
            if existing is not None:
                run = _row(existing)
                if run["action_hash"] != _hash_action(action):
                    raise TurnConflict("client_request_id 已被不同请求使用")
                return run, False

        save_row = (
            await connection.execute(
                select(db.saves).where(db.saves.c.id == sid).with_for_update()
            )
        ).first()
        if save_row is None:
            raise SaveNotFound("存档不存在")
        save = _row(save_row)
        current_turn = int(save["turns"])
        current_version = int(save["version"])
        if base_turn is not None and int(base_turn) != current_turn:
            raise TurnConflict(f"存档回合已变化：expected={base_turn}, actual={current_turn}")
        if base_version is not None and int(base_version) != current_version:
            raise TurnConflict(
                f"存档版本已变化：expected={base_version}, actual={current_version}"
            )
        if save.get("active_run_id"):
            raise ActiveRunConflict(str(save["active_run_id"]))
        if input_kind == "opening" and current_turn != 0:
            raise TurnConflict("开场只能在空存档上生成")

        run_id = uuid.uuid4().hex
        target_turn = current_turn + 1
        workflow_id = f"turn:{sid}:{target_turn}:{run_id}"
        now = _now()
        values = {
            "id": run_id,
            "sid": sid,
            "kind": "primary",
            "input_kind": input_kind,
            "action": action,
            "action_hash": _hash_action(action),
            "client_request_id": client_request_id,
            "request_snapshot": {
                "input_kind": input_kind,
                "action": action,
                "base_turn": current_turn,
                "base_version": current_version,
            },
            "base_turn": current_turn,
            "base_version": current_version,
            "target_turn": target_turn,
            "workflow_id": workflow_id,
            "status": "starting",
            "degraded": False,
            "event_sequence": 0,
            "code_revision": _revision(),
            "prompt_bundle_version": _prompt_bundle_version(),
            "created_at": now,
            "updated_at": now,
        }
        await connection.execute(insert(db.execution_runs).values(**values))
        await connection.execute(
            update(db.saves)
            .where(db.saves.c.id == sid)
            .values(active_run_id=run_id, updated_at=now)
        )
        await _create_nodes_tx(connection, run_id, TURN_NODE_KEYS)
        await _append_event_tx(
            connection,
            run_id,
            "run_started",
            {
                "run_id": run_id,
                "sid": sid,
                "target_turn": target_turn,
                "input_kind": input_kind,
            },
        )
        return {**values, "event_sequence": 1}, True


async def _create_nodes_tx(
    connection: AsyncConnection,
    run_id: str,
    node_keys: Iterable[str],
) -> list[dict]:
    now = _now()
    rows = []
    previous_id = None
    revision = _revision()
    prompt_version = _prompt_bundle_version()
    for ordinal, node_key in enumerate(node_keys):
        node_id = uuid.uuid4().hex
        row = {
            "id": node_id,
            "run_id": run_id,
            "node_key": node_key,
            "ordinal": ordinal,
            "status": "pending",
            "schema_version": "1",
            "code_revision": revision,
            "prompt_bundle_version": prompt_version,
            "created_at": now,
            "updated_at": now,
        }
        await connection.execute(insert(db.execution_nodes).values(**row))
        if previous_id:
            await connection.execute(
                insert(db.execution_node_edges).values(
                    run_id=run_id,
                    from_node_id=previous_id,
                    to_node_id=node_id,
                    edge_kind="depends_on",
                )
            )
        previous_id = node_id
        rows.append(row)
    return rows


async def get_run(run_id: str, *, user_id: str | None = None) -> dict | None:
    statement = (
        select(db.execution_runs)
        .join(db.saves, db.saves.c.id == db.execution_runs.c.sid)
        .where(db.execution_runs.c.id == run_id)
    )
    if user_id is not None:
        statement = statement.where(db.saves.c.user_id == user_id)
    async with engine().connect() as connection:
        return _row((await connection.execute(statement)).first())


async def list_runs(
    sid: str,
    *,
    user_id: str,
    limit: int = 50,
) -> list[dict]:
    async with engine().connect() as connection:
        rows = (
            await connection.execute(
                select(db.execution_runs)
                .join(db.saves, db.saves.c.id == db.execution_runs.c.sid)
                .where(
                    and_(db.execution_runs.c.sid == sid, db.saves.c.user_id == user_id)
                )
                .order_by(db.execution_runs.c.created_at.desc())
                .limit(max(1, min(int(limit), 200)))
            )
        ).all()
    return [_row(row) for row in rows]


async def list_nodes(run_id: str) -> list[dict]:
    async with engine().connect() as connection:
        rows = (
            await connection.execute(
                select(db.execution_nodes)
                .where(db.execution_nodes.c.run_id == run_id)
                .order_by(db.execution_nodes.c.ordinal)
            )
        ).all()
    return [_row(row) for row in rows]


async def list_nodes_with_attempts(run_id: str) -> list[dict]:
    nodes = await list_nodes(run_id)
    if not nodes:
        return []
    async with engine().connect() as connection:
        rows = (
            await connection.execute(
                select(db.llm_attempts)
                .join(db.execution_nodes, db.execution_nodes.c.id == db.llm_attempts.c.node_id)
                .where(db.execution_nodes.c.run_id == run_id)
                .order_by(db.execution_nodes.c.ordinal, db.llm_attempts.c.attempt)
            )
        ).all()
    attempts_by_node: dict[str, list[dict]] = {}
    for row in rows:
        attempt = _row(row)
        attempts_by_node.setdefault(attempt["node_id"], []).append(attempt)
    return [
        {**node, "attempts": attempts_by_node.get(node["id"], [])}
        for node in nodes
    ]


async def get_node(run_id: str, node_key: str) -> dict | None:
    async with engine().connect() as connection:
        return _row(
            (
                await connection.execute(
                    select(db.execution_nodes).where(
                        and_(
                            db.execution_nodes.c.run_id == run_id,
                            db.execution_nodes.c.node_key == node_key,
                        )
                    )
                )
            ).first()
        )


async def mark_run_started(run_id: str, workflow_run_id: str | None = None) -> None:
    now = _now()
    async with engine().begin() as connection:
        await connection.execute(
            update(db.execution_runs)
            .where(
                and_(
                    db.execution_runs.c.id == run_id,
                    db.execution_runs.c.status == "starting",
                )
            )
            .values(
                status="running",
                workflow_run_id=workflow_run_id,
                started_at=now,
                updated_at=now,
            )
        )


async def mark_node_started(run_id: str, node_key: str) -> dict:
    now = _now()
    async with engine().begin() as connection:
        run = _row(
            (
                await connection.execute(
                    select(db.execution_runs)
                    .where(db.execution_runs.c.id == run_id)
                    .with_for_update()
                )
            ).first()
        )
        if run is None:
            raise RunNotFound("执行不存在")
        row = (
            await connection.execute(
                select(db.execution_nodes)
                .where(
                    and_(
                        db.execution_nodes.c.run_id == run_id,
                        db.execution_nodes.c.node_key == node_key,
                    )
                )
                .with_for_update()
            )
        ).first()
        if row is None:
            raise RunNotFound(f"节点不存在：{node_key}")
        node = _row(row)
        if run["status"] not in {"starting", "running"} or run.get("cancel_requested_at") is not None:
            return node
        if node["status"] == "completed":
            return node
        await connection.execute(
            update(db.execution_nodes)
            .where(db.execution_nodes.c.id == node["id"])
            .values(status="running", started_at=node.get("started_at") or now, updated_at=now)
        )
        await _append_event_tx(
            connection,
            run_id,
            "stage",
            {"key": node_key, "label": node_key},
            node_id=node["id"],
        )
        return {**node, "status": "running", "started_at": now}


async def complete_node(
    run_id: str,
    node_key: str,
    normalized_output: Any,
    *,
    degraded: bool = False,
) -> None:
    now = _now()
    async with engine().begin() as connection:
        run = _row(
            (
                await connection.execute(
                    select(db.execution_runs)
                    .where(db.execution_runs.c.id == run_id)
                    .with_for_update()
                )
            ).first()
        )
        if run is None:
            raise RunNotFound("执行不存在")
        if run["status"] not in {"starting", "running"} or run.get("cancel_requested_at") is not None:
            return
        node = _row(
            (
                await connection.execute(
                    select(db.execution_nodes).where(
                        and_(
                            db.execution_nodes.c.run_id == run_id,
                            db.execution_nodes.c.node_key == node_key,
                        )
                    )
                )
            ).first()
        )
        if node is None:
            raise RunNotFound(f"节点不存在：{node_key}")
        await connection.execute(
            update(db.execution_nodes)
            .where(db.execution_nodes.c.id == node["id"])
            .values(
                status="completed",
                normalized_output=normalized_output,
                completed_at=now,
                updated_at=now,
            )
        )
        if degraded:
            await connection.execute(
                update(db.execution_runs)
                .where(db.execution_runs.c.id == run_id)
                .values(degraded=True, updated_at=now)
            )
        await _append_event_tx(
            connection,
            run_id,
            "node_completed",
            {"node_key": node_key, "degraded": degraded},
            node_id=node["id"],
        )


async def save_snapshot(
    run_id: str,
    node_key: str,
    phase: str,
    state: dict,
) -> str:
    canonical = json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    now = _now()
    snapshot_id = uuid.uuid4().hex
    async with engine().begin() as connection:
        node_id = (
            await connection.execute(
                select(db.execution_nodes.c.id).where(
                    and_(
                        db.execution_nodes.c.run_id == run_id,
                        db.execution_nodes.c.node_key == node_key,
                    )
                )
            )
        ).scalar_one()
        existing_id = (
            await connection.execute(
                select(db.state_snapshots.c.id).where(
                    and_(
                        db.state_snapshots.c.node_id == node_id,
                        db.state_snapshots.c.phase == phase,
                    )
                )
            )
        ).scalar_one_or_none()
        values = {
            "state": state,
            "content_hash": digest,
            "schema_version": "1",
            "code_revision": _revision(),
            "prompt_bundle_version": _prompt_bundle_version(),
            "created_at": now,
        }
        if existing_id:
            await connection.execute(
                update(db.state_snapshots)
                .where(db.state_snapshots.c.id == existing_id)
                .values(**values)
            )
            return str(existing_id)
        await connection.execute(
            insert(db.state_snapshots).values(
                id=snapshot_id,
                node_id=node_id,
                phase=phase,
                **values,
            )
        )
    return snapshot_id


async def get_snapshot(run_id: str, node_key: str, phase: str) -> dict | None:
    async with engine().connect() as connection:
        row = (
            await connection.execute(
                select(db.state_snapshots)
                .join(db.execution_nodes, db.execution_nodes.c.id == db.state_snapshots.c.node_id)
                .where(
                    and_(
                        db.execution_nodes.c.run_id == run_id,
                        db.execution_nodes.c.node_key == node_key,
                        db.state_snapshots.c.phase == phase,
                    )
                )
            )
        ).first()
    return _row(row)


async def record_attempt(
    run_id: str,
    node_key: str,
    *,
    attempt: int,
    activity_id: str,
    activity_attempt: int,
    request_snapshot: dict,
    status: str = "running",
) -> dict:
    now = _now()
    async with engine().begin() as connection:
        run = _row(
            (
                await connection.execute(
                    select(db.execution_runs)
                    .where(db.execution_runs.c.id == run_id)
                    .with_for_update()
                )
            ).first()
        )
        if run is None:
            raise RunNotFound("执行不存在")
        node_id = (
            await connection.execute(
                select(db.execution_nodes.c.id).where(
                    and_(
                        db.execution_nodes.c.run_id == run_id,
                        db.execution_nodes.c.node_key == node_key,
                    )
                )
            )
        ).scalar_one()
        existing = (
            await connection.execute(
                select(db.llm_attempts).where(
                    and_(
                        db.llm_attempts.c.node_id == node_id,
                        db.llm_attempts.c.activity_id == activity_id,
                        db.llm_attempts.c.activity_attempt == activity_attempt,
                    )
                )
            )
        ).first()
        if existing:
            return _row(existing)
        attempt_id = uuid.uuid4().hex
        values = {
            "id": attempt_id,
            "node_id": node_id,
            "attempt": attempt,
            "activity_id": activity_id,
            "activity_attempt": activity_attempt,
            "request_snapshot": request_snapshot,
            "status": status,
            "created_at": now,
        }
        await connection.execute(insert(db.llm_attempts).values(**values))
        if run["status"] in {"starting", "running"} and run.get("cancel_requested_at") is None:
            await _append_event_tx(
                connection,
                run_id,
                "attempt_started",
                {"node_key": node_key, "attempt": attempt},
                node_id=node_id,
                attempt_id=attempt_id,
            )
        return values


async def finish_attempt(
    attempt_id: str,
    *,
    status: str,
    raw_output: str = "",
    normalized_output: Any = None,
    error: dict | None = None,
    duration_ms: int | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    total_tokens: int | None = None,
) -> bool:
    """Finish an attempt and first-success-CAS it onto its logical node."""
    now = _now()
    async with engine().begin() as connection:
        attempt_row = (
            await connection.execute(
                select(db.llm_attempts).where(db.llm_attempts.c.id == attempt_id).with_for_update()
            )
        ).first()
        if attempt_row is None:
            return False
        attempt = _row(attempt_row)
        await connection.execute(
            update(db.llm_attempts)
            .where(db.llm_attempts.c.id == attempt_id)
            .values(
                status=status,
                raw_output=raw_output,
                normalized_output=normalized_output,
                error=error,
                duration_ms=duration_ms,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                completed_at=now,
            )
        )
        if status != "success":
            return False
        result = await connection.execute(
            update(db.execution_nodes)
            .where(
                and_(
                    db.execution_nodes.c.id == attempt["node_id"],
                    db.execution_nodes.c.result_attempt_id.is_(None),
                )
            )
            .values(
                result_attempt_id=attempt_id,
                normalized_output=normalized_output,
                updated_at=now,
            )
        )
        return bool(result.rowcount)


async def list_events(run_id: str, after: int = 0, limit: int = 500) -> list[dict]:
    async with engine().connect() as connection:
        rows = (
            await connection.execute(
                select(db.execution_events)
                .where(
                    and_(
                        db.execution_events.c.run_id == run_id,
                        db.execution_events.c.sequence > max(0, int(after)),
                    )
                )
                .order_by(db.execution_events.c.sequence)
                .limit(max(1, min(int(limit), 2000)))
            )
        ).all()
    return [_row(row) for row in rows]


async def cancel_run(run_id: str, *, user_id: str) -> dict:
    """Persist the cancellation fence and release the turn before RPC cancel."""
    now = _now()
    async with engine().begin() as connection:
        run_row = (
            await connection.execute(
                select(db.execution_runs)
                .join(db.saves, db.saves.c.id == db.execution_runs.c.sid)
                .where(
                    and_(
                        db.execution_runs.c.id == run_id,
                        db.saves.c.user_id == user_id,
                    )
                )
                .with_for_update()
            )
        ).first()
        if run_row is None:
            raise RunNotFound("执行不存在")
        run = _row(run_row)
        save_row = (
            await connection.execute(
                select(db.saves).where(db.saves.c.id == run["sid"]).with_for_update()
            )
        ).first()
        if run["status"] == "succeeded":
            raise AlreadyCommitted("回合已提交，不能取消")
        if run["status"] in {"failed", "canceled", "commit_conflict"}:
            return run
        await connection.execute(
            update(db.execution_runs)
            .where(db.execution_runs.c.id == run_id)
            .values(status="canceling", cancel_requested_at=now, updated_at=now)
        )
        await connection.execute(
            update(db.execution_nodes)
            .where(
                and_(
                    db.execution_nodes.c.run_id == run_id,
                    db.execution_nodes.c.status.in_(("pending", "running")),
                )
            )
            .values(status="canceled", completed_at=now, updated_at=now)
        )
        if save_row is not None and _row(save_row).get("active_run_id") == run_id:
            await connection.execute(
                update(db.saves)
                .where(db.saves.c.id == run["sid"])
                .values(active_run_id=None, updated_at=now)
            )
        await _append_event_tx(
            connection,
            run_id,
            "canceled",
            {"run_id": run_id, "action": run.get("action") or ""},
        )
        return {**run, "status": "canceling", "cancel_requested_at": now}


async def finalize_canceled(run_id: str) -> None:
    now = _now()
    async with engine().begin() as connection:
        await connection.execute(
            update(db.execution_runs)
            .where(
                and_(
                    db.execution_runs.c.id == run_id,
                    db.execution_runs.c.status == "canceling",
                )
            )
            .values(status="canceled", completed_at=now, updated_at=now)
        )


async def fail_run(run_id: str, error: dict) -> None:
    now = _now()
    async with engine().begin() as connection:
        run_row = (
            await connection.execute(
                select(db.execution_runs).where(db.execution_runs.c.id == run_id).with_for_update()
            )
        ).first()
        if run_row is None:
            return
        run = _row(run_row)
        if run["status"] in TERMINAL_STATUSES:
            return
        await connection.execute(
            update(db.execution_runs)
            .where(db.execution_runs.c.id == run_id)
            .values(status="failed", error=error, completed_at=now, updated_at=now)
        )
        await connection.execute(
            update(db.saves)
            .where(
                and_(db.saves.c.id == run["sid"], db.saves.c.active_run_id == run_id)
            )
            .values(active_run_id=None, updated_at=now)
        )
        await _append_event_tx(connection, run_id, "error", error)


async def commit_shadow(run_id: str, shadow_sid: str) -> dict:
    """CAS the complete branch save into the canonical save in one transaction."""
    now = _now()
    world_state = constraints.get_world_state(shadow_sid) or {}
    canonical_columns = (
        "messages",
        "transcript",
        "lore",
        "character_state",
        "world_memory",
        "world_entities",
        "inventory",
        "director_state",
        "stage_summary",
        "summary_turn",
        "story_card",
    )
    async with engine().begin() as connection:
        run_row = (
            await connection.execute(
                select(db.execution_runs).where(db.execution_runs.c.id == run_id).with_for_update()
            )
        ).first()
        if run_row is None:
            raise RunNotFound("执行不存在")
        run = _row(run_row)
        save_row = (
            await connection.execute(
                select(db.saves).where(db.saves.c.id == run["sid"]).with_for_update()
            )
        ).first()
        shadow_row = (
            await connection.execute(select(db.saves).where(db.saves.c.id == shadow_sid))
        ).first()
        if save_row is None or shadow_row is None:
            raise CommitConflict("正式存档或分支状态不存在")
        save = _row(save_row)
        shadow = _row(shadow_row)
        valid = (
            run["kind"] == "primary"
            and run.get("cancel_requested_at") is None
            and run["status"] in {"running", "starting"}
            and save.get("active_run_id") == run_id
            and int(save["version"]) == int(run["base_version"])
            and int(save["turns"]) == int(run["base_turn"])
            and int(shadow["turns"]) == int(run["target_turn"])
        )
        if not valid:
            await connection.execute(
                update(db.execution_runs)
                .where(db.execution_runs.c.id == run_id)
                .values(
                    status="commit_conflict",
                    error={"message": "save CAS rejected"},
                    completed_at=now,
                    updated_at=now,
                )
            )
            raise CommitConflict("存档已变化、执行已取消或不再拥有活动回合")

        await connection.execute(
            update(db.saves)
            .where(db.saves.c.id == run["sid"])
            .values(
                **{column: shadow[column] for column in canonical_columns},
                turns=run["target_turn"],
                version=int(save["version"]) + 1,
                active_run_id=None,
                updated_at=now,
            )
        )
        await _copy_shadow_tables(connection, shadow_sid, run["sid"])
        await connection.execute(
            update(db.execution_runs)
            .where(db.execution_runs.c.id == run_id)
            .values(status="succeeded", completed_at=now, updated_at=now)
        )
        await connection.execute(
            update(db.execution_nodes)
            .where(
                and_(
                    db.execution_nodes.c.run_id == run_id,
                    db.execution_nodes.c.node_key == "commit",
                )
            )
            .values(
                status="completed",
                normalized_output={"version": int(save["version"]) + 1},
                completed_at=now,
                updated_at=now,
            )
        )
        payload = {
            "run_id": run_id,
            "turn": run["target_turn"],
            "version": int(save["version"]) + 1,
            "character_state": json.loads(shadow["character_state"] or "{}"),
            "inventory": json.loads(shadow["inventory"] or "[]"),
            "director_state": json.loads(shadow["director_state"] or "{}"),
            "world_state": world_state,
        }
        await _append_event_tx(connection, run_id, "done", payload)
        return payload


async def complete_debug_run(run_id: str, payload: dict) -> None:
    now = _now()
    async with engine().begin() as connection:
        run = _row(
            (
                await connection.execute(
                    select(db.execution_runs).where(db.execution_runs.c.id == run_id).with_for_update()
                )
            ).first()
        )
        if run is None:
            raise RunNotFound("执行不存在")
        if run["kind"] != "debug":
            raise TurnConflict("只能以 debug 方式完成调试分支")
        await connection.execute(
            update(db.execution_runs)
            .where(db.execution_runs.c.id == run_id)
            .values(status="succeeded", completed_at=now, updated_at=now)
        )
        await connection.execute(
            update(db.execution_nodes)
            .where(
                and_(
                    db.execution_nodes.c.run_id == run_id,
                    db.execution_nodes.c.node_key == "commit",
                )
            )
            .values(
                status="completed",
                normalized_output={"debug_only": True},
                completed_at=now,
                updated_at=now,
            )
        )
        await _append_event_tx(
            connection,
            run_id,
            "done",
            {**payload, "run_id": run_id, "debug_only": True},
        )


async def _copy_shadow_tables(
    connection: AsyncConnection, shadow_sid: str, canonical_sid: str
) -> None:
    for table in (
        db.save_player_location,
        db.save_world_time,
        db.save_player_knowledge,
        db.save_opportunity_states,
        db.save_realm_states,
        db.save_opportunity_rewards,
    ):
        rows = (
            await connection.execute(select(table).where(table.c.save_id == shadow_sid))
        ).mappings().all()
        await connection.execute(delete(table).where(table.c.save_id == canonical_sid))
        for source in rows:
            values = dict(source)
            values["save_id"] = canonical_sid
            if table is db.save_player_knowledge:
                values.pop("id", None)
            if table is db.save_opportunity_rewards:
                suffix = f":{shadow_sid}"
                if str(values["payoff_id"]).endswith(suffix):
                    values["payoff_id"] = str(values["payoff_id"])[: -len(suffix)]
            await connection.execute(insert(table).values(**values))

    # Diagnostics are append-only. Preserve the real provider calls made on
    # the isolated save so the existing trace/metrics APIs remain complete;
    # the execution ledger keeps the same calls associated with their nodes.
    for table in (db.llm_request_metrics, db.agent_traces):
        rows = (
            await connection.execute(select(table).where(table.c.save_id == shadow_sid))
        ).mappings().all()
        for source in rows:
            values = dict(source)
            values.pop("id", None)
            values["save_id"] = canonical_sid
            await connection.execute(insert(table).values(**values))


async def active_run_summary(sid: str, *, user_id: str) -> dict | None:
    async with engine().connect() as connection:
        row = (
            await connection.execute(
                select(db.execution_runs)
                .join(db.saves, db.saves.c.active_run_id == db.execution_runs.c.id)
                .where(and_(db.saves.c.id == sid, db.saves.c.user_id == user_id))
            )
        ).first()
    return _row(row)


async def inflight_runs(sid: str, *, user_id: str) -> list[dict]:
    """Return executions that may still have a Worker activity in flight."""
    async with engine().connect() as connection:
        rows = (
            await connection.execute(
                select(db.execution_runs)
                .join(db.saves, db.saves.c.id == db.execution_runs.c.sid)
                .where(
                    and_(
                        db.execution_runs.c.sid == sid,
                        db.saves.c.user_id == user_id,
                        db.execution_runs.c.status.in_(("starting", "running", "canceling")),
                    )
                )
                .order_by(db.execution_runs.c.created_at)
            )
        ).all()
    return [_row(row) for row in rows]


async def starting_runs(older_than: float) -> list[dict]:
    async with engine().connect() as connection:
        rows = (
            await connection.execute(
                select(db.execution_runs).where(
                    and_(
                        db.execution_runs.c.status == "starting",
                        db.execution_runs.c.updated_at < older_than,
                    )
                )
            )
        ).all()
    return [_row(row) for row in rows]


async def create_debug_run(
    source_run_id: str,
    node_key: str,
    *,
    mode: str,
    user_id: str,
) -> dict:
    if mode not in {"original", "current"}:
        raise TurnConflict("rerun mode 必须是 original 或 current")
    now = _now()
    async with engine().begin() as connection:
        source_row = (
            await connection.execute(
                select(db.execution_runs)
                .join(db.saves, db.saves.c.id == db.execution_runs.c.sid)
                .where(
                    and_(
                        db.execution_runs.c.id == source_run_id,
                        db.saves.c.user_id == user_id,
                    )
                )
            )
        ).first()
        if source_row is None:
            raise RunNotFound("源执行不存在")
        source = _row(source_row)
        if source["kind"] == "legacy":
            raise TurnConflict("legacy 执行缺少状态快照，不能分叉")
        source_nodes = (
            await connection.execute(
                select(db.execution_nodes)
                .where(db.execution_nodes.c.run_id == source_run_id)
                .order_by(db.execution_nodes.c.ordinal)
            )
        ).all()
        nodes = [_row(row) for row in source_nodes]
        fork = next((node for node in nodes if node["node_key"] == node_key), None)
        if fork is None:
            raise RunNotFound("分叉节点不存在")
        snapshot = (
            await connection.execute(
                select(db.state_snapshots).where(
                    and_(
                        db.state_snapshots.c.node_id == fork["id"],
                        db.state_snapshots.c.phase == "before",
                    )
                )
            )
        ).first()
        if snapshot is None:
            raise TurnConflict("目标节点没有 before 快照")

        run_id = uuid.uuid4().hex
        workflow_id = f"debug:{source['sid']}:{source['target_turn']}:{run_id}"
        values = {
            "id": run_id,
            "sid": source["sid"],
            "kind": "debug",
            "input_kind": source["input_kind"],
            "action": source["action"],
            "action_hash": source["action_hash"],
            "request_snapshot": source["request_snapshot"],
            "base_turn": source["base_turn"],
            "base_version": source["base_version"],
            "target_turn": source["target_turn"],
            "parent_run_id": source_run_id,
            "fork_node_id": fork["id"],
            "rerun_mode": mode,
            "workflow_id": workflow_id,
            "status": "starting",
            "degraded": False,
            "event_sequence": 0,
            "code_revision": _revision(),
            "prompt_bundle_version": _prompt_bundle_version(),
            "created_at": now,
            "updated_at": now,
        }
        await connection.execute(insert(db.execution_runs).values(**values))
        previous_id = None
        for source_node in nodes:
            node_id = uuid.uuid4().hex
            reused = source_node["ordinal"] < fork["ordinal"]
            await connection.execute(
                insert(db.execution_nodes).values(
                    id=node_id,
                    run_id=run_id,
                    node_key=source_node["node_key"],
                    ordinal=source_node["ordinal"],
                    source_node_id=source_node["id"] if reused else None,
                    status="reused" if reused else "pending",
                    normalized_output=source_node["normalized_output"] if reused else None,
                    schema_version="1",
                    code_revision=_revision(),
                    prompt_bundle_version=_prompt_bundle_version(),
                    created_at=now,
                    updated_at=now,
                )
            )
            if previous_id:
                await connection.execute(
                    insert(db.execution_node_edges).values(
                        run_id=run_id,
                        from_node_id=previous_id,
                        to_node_id=node_id,
                        edge_kind="reused" if reused else "depends_on",
                    )
                )
            previous_id = node_id
        await _append_event_tx(
            connection,
            run_id,
            "run_started",
            {"run_id": run_id, "kind": "debug", "source_run_id": source_run_id},
        )
        return {**values, "event_sequence": 1}
