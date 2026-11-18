"""Explicit, idempotent SQLite-to-PostgreSQL importer."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path

from sqlalchemy import Integer, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

import db


TABLE_ORDER = (
    "world_meta",
    "world_regions",
    "world_locations",
    "world_routes",
    "world_factions",
    "world_arts",
    "world_opportunities",
    "world_realms",
    "world_cultivation_demographics",
    "prompt_definitions",
    "prompt_versions",
    "saves",
    "save_player_location",
    "save_world_time",
    "save_player_knowledge",
    "save_opportunity_states",
    "save_realm_states",
    "save_opportunity_rewards",
    "llm_request_metrics",
    "agent_traces",
)


def _hash_rows(rows: list[dict]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        digest.update(
            json.dumps(
                row,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        )
        digest.update(b"\n")
    return digest.hexdigest()


def _source_rows(connection: sqlite3.Connection, table, available: set[str]) -> list[dict]:
    source_columns = {
        row["name"]
        for row in connection.execute(f'PRAGMA table_info("{table.name}")').fetchall()
    }
    columns = [column.name for column in table.columns if column.name in source_columns]
    if not columns:
        return []
    order = [column.name for column in table.primary_key.columns if column.name in source_columns]
    order_sql = f" ORDER BY {', '.join(order)}" if order else ""
    rows = connection.execute(
        f"SELECT {', '.join(columns)} FROM {table.name}{order_sql}"
    ).fetchall()
    return [{column: row[column] for column in columns} for row in rows]


def _table_report(source: sqlite3.Connection) -> tuple[dict, dict[str, list[dict]]]:
    available = {
        row["name"]
        for row in source.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    save_ids = {
        str(row["id"])
        for row in source.execute("SELECT id FROM saves").fetchall()
    } if "saves" in available else set()
    report, rows_by_table = {}, {}
    for name in TABLE_ORDER:
        if name not in available or name not in db.metadata.tables:
            continue
        raw_rows = _source_rows(source, db.metadata.tables[name], available)
        rows = [
            row
            for row in raw_rows
            if "save_id" not in row
            or row["save_id"] is None
            or str(row["save_id"]) in save_ids
        ]
        rows_by_table[name] = rows
        report[name] = {"rows": len(rows), "sha256": _hash_rows(rows)}
        skipped = len(raw_rows) - len(rows)
        if skipped:
            report[name]["skipped_orphans"] = skipped
    return report, rows_by_table


def _upsert_rows(connection, table, rows: list[dict]) -> None:
    if not rows:
        return
    primary_keys = [column.name for column in table.primary_key.columns]
    for offset in range(0, len(rows), 500):
        batch = rows[offset : offset + 500]
        statement = pg_insert(table).values(batch)
        update_columns = {
            key: getattr(statement.excluded, key)
            for key in batch[0]
            if key not in primary_keys
        }
        if primary_keys and update_columns:
            statement = statement.on_conflict_do_update(
                index_elements=primary_keys, set_=update_columns
            )
        elif primary_keys:
            statement = statement.on_conflict_do_nothing(index_elements=primary_keys)
        connection.execute(statement)


def _verify_table(connection, table, source_rows: list[dict]) -> dict:
    if not source_rows:
        return {"rows": 0, "sha256": _hash_rows([]), "matches": True}
    column_names = list(source_rows[0])
    columns = [table.c[name] for name in column_names]
    order = [table.c[column.name] for column in table.primary_key.columns]
    statement = select(*columns)
    if order:
        statement = statement.order_by(*order)
    target_rows = [dict(row._mapping) for row in connection.execute(statement).all()]
    # Limit verification to source primary keys so pre-existing unrelated rows
    # do not make a valid additive import fail verification.
    pk_names = [column.name for column in table.primary_key.columns]
    if pk_names:
        wanted = {tuple(row[name] for name in pk_names) for row in source_rows}
        target_rows = [
            row for row in target_rows
            if tuple(row[name] for name in pk_names) in wanted
        ]
    source_hash = _hash_rows(source_rows)
    target_hash = _hash_rows(target_rows)
    return {
        "rows": len(target_rows),
        "sha256": target_hash,
        "matches": len(target_rows) == len(source_rows) and target_hash == source_hash,
    }


def _reset_postgres_sequences(connection) -> None:
    """Advance sequences after preserving explicit SQLite integer IDs."""
    for table in db.metadata.tables.values():
        for column in table.primary_key.columns:
            if (
                not isinstance(column.type, Integer)
                or not column.autoincrement
                or len(table.primary_key.columns) != 1
            ):
                continue
            connection.execute(
                text(
                    f"SELECT setval(pg_get_serial_sequence('{table.name}', '{column.name}'), "
                    f"COALESCE(MAX({column.name}), 1), MAX({column.name}) IS NOT NULL) "
                    f"FROM {table.name}"
                )
            )


def _import_legacy_runs(connection) -> int:
    traces = connection.execute(
        select(db.agent_traces).where(db.agent_traces.c.save_id.is_not(None)).order_by(
            db.agent_traces.c.save_id, db.agent_traces.c.turn, db.agent_traces.c.id
        )
    ).mappings().all()
    groups: dict[tuple[str, int], list[dict]] = {}
    for trace in traces:
        key = (str(trace["save_id"]), int(trace["turn"] or 0))
        groups.setdefault(key, []).append(dict(trace))
    imported = 0
    for (sid, turn), items in groups.items():
        run_id = hashlib.sha256(f"legacy:{sid}:{turn}".encode()).hexdigest()[:32]
        now = max(float(item.get("updated_at") or item["created_at"]) for item in items)
        run_statement = pg_insert(db.execution_runs).values(
            id=run_id,
            sid=sid,
            kind="legacy",
            input_kind="legacy",
            action=None,
            action_hash=hashlib.sha256(b"").hexdigest(),
            request_snapshot={"legacy": True},
            base_turn=max(0, turn - 1),
            base_version=0,
            target_turn=turn,
            workflow_id=f"legacy:{sid}:{turn}",
            status="succeeded",
            degraded=False,
            event_sequence=0,
            code_revision="legacy",
            prompt_bundle_version="legacy",
            started_at=min(float(item["created_at"]) for item in items),
            completed_at=now,
            created_at=min(float(item["created_at"]) for item in items),
            updated_at=now,
        ).on_conflict_do_nothing(index_elements=["id"])
        connection.execute(run_statement)
        previous_id = None
        for ordinal, trace in enumerate(items):
            trace_id = int(trace["id"])
            node_id = hashlib.sha256(f"legacy-node:{trace_id}".encode()).hexdigest()[:32]
            node_key = f"{trace['agent_type']}:{trace_id}"[:64]
            connection.execute(
                pg_insert(db.execution_nodes)
                .values(
                    id=node_id,
                    run_id=run_id,
                    node_key=node_key,
                    ordinal=ordinal,
                    status="completed" if trace["status"] == "success" else "failed",
                    normalized_output={
                        "legacy_trace_id": trace_id,
                        "status": trace["status"],
                    },
                    schema_version="legacy",
                    code_revision="legacy",
                    prompt_bundle_version="legacy",
                    started_at=trace["created_at"],
                    completed_at=trace.get("updated_at") or trace["created_at"],
                    created_at=trace["created_at"],
                    updated_at=trace.get("updated_at") or trace["created_at"],
                )
                .on_conflict_do_nothing(index_elements=["id"])
            )
            attempt_id = hashlib.sha256(f"legacy-attempt:{trace_id}".encode()).hexdigest()[:32]
            connection.execute(
                pg_insert(db.llm_attempts)
                .values(
                    id=attempt_id,
                    node_id=node_id,
                    attempt=1,
                    activity_id=f"legacy:{trace_id}",
                    activity_attempt=1,
                    request_snapshot={
                        "messages": json.loads(trace.get("input_messages") or "[]"),
                        "protocol": trace.get("protocol") or "",
                        "model": trace.get("model") or "",
                    },
                    raw_output=trace.get("raw_output") or "",
                    status="success" if trace["status"] == "success" else "failed",
                    error=(
                        None
                        if trace["status"] == "success"
                        else {
                            "type": trace.get("error_type") or "LegacyError",
                            "message": trace.get("error_message") or "",
                        }
                    ),
                    input_tokens=trace.get("input_tokens"),
                    output_tokens=trace.get("output_tokens"),
                    total_tokens=trace.get("total_tokens"),
                    duration_ms=trace.get("duration_ms"),
                    created_at=trace["created_at"],
                    completed_at=trace.get("updated_at") or trace["created_at"],
                )
                .on_conflict_do_nothing(index_elements=["id"])
            )
            if previous_id:
                connection.execute(
                    pg_insert(db.execution_node_edges)
                    .values(
                        run_id=run_id,
                        from_node_id=previous_id,
                        to_node_id=node_id,
                        edge_kind="legacy_sequence",
                    )
                    .on_conflict_do_nothing()
                )
            previous_id = node_id
            imported += 1
    return imported


def run(path: Path, *, apply: bool) -> dict:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"SQLite 文件不存在：{path}")
    source = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    try:
        report, rows_by_table = _table_report(source)
    finally:
        source.close()
    result = {"source": str(path), "mode": "apply" if apply else "dry-run", "tables": report}
    if not apply:
        return result
    if not db.is_postgres():
        raise ValueError("--apply 要求 DATABASE_URL 指向 PostgreSQL")
    engine = db.make_sync_engine()
    db.metadata.create_all(engine)
    verification = {}
    with engine.begin() as connection:
        for name in TABLE_ORDER:
            rows = rows_by_table.get(name)
            if rows is not None:
                _upsert_rows(connection, db.metadata.tables[name], rows)
        legacy_traces = _import_legacy_runs(connection)
        _reset_postgres_sequences(connection)
        for name, rows in rows_by_table.items():
            verification[name] = _verify_table(connection, db.metadata.tables[name], rows)
    failed = [name for name, item in verification.items() if not item["matches"]]
    result.update(
        verification=verification,
        legacy_trace_nodes=legacy_traces,
        verified=not failed,
        verification_failures=failed,
        source_preserved=True,
    )
    if failed:
        raise RuntimeError(f"导入校验失败：{', '.join(failed)}")
    return result
