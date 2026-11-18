"""Database metadata and engine helpers.

The application keeps the historical save columns as JSON-encoded TEXT so the
SQLite importer can preserve bytes exactly.  New execution-ledger payloads use
JSONB on PostgreSQL (and SQLAlchemy JSON on SQLite tests).
"""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    text,
    event,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.engine import Engine, create_engine


metadata = MetaData()
JSON_PAYLOAD = JSON().with_variant(JSONB(), "postgresql")


def _table(name: str, *columns: Column, **kwargs) -> Table:
    return Table(name, metadata, *columns, **kwargs)


saves = _table(
    "saves",
    Column("id", String(64), primary_key=True),
    Column("user_id", String(64), nullable=False, server_default="default"),
    Column("name", Text, nullable=False),
    Column("messages", Text, nullable=False),
    Column("transcript", Text, nullable=False),
    Column("turns", Integer, nullable=False, server_default="0"),
    Column("lore", Text, nullable=False, server_default="[]"),
    Column("character_state", Text, nullable=False, server_default="{}"),
    Column("world_memory", Text, nullable=False, server_default="[]"),
    Column("world_entities", Text, nullable=False, server_default="{}"),
    Column("inventory", Text, nullable=False, server_default="[]"),
    Column("director_state", Text, nullable=False, server_default="{}"),
    Column("stage_summary", Text, nullable=False, server_default=""),
    Column("summary_turn", Integer, nullable=False, server_default="0"),
    Column("story_card", Text, nullable=False, server_default="{}"),
    Column("version", Integer, nullable=False, server_default="0"),
    Column("active_run_id", String(64)),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
)
Index("idx_saves_user_updated", saves.c.user_id, saves.c.updated_at.desc())


world_meta = _table(
    "world_meta",
    Column("key", Text, primary_key=True),
    Column("value", Text, nullable=False),
)
world_regions = _table(
    "world_regions",
    Column("id", Text, primary_key=True),
    Column("name", Text, nullable=False, unique=True),
    Column("role", Text, nullable=False),
    Column("summary", Text, nullable=False),
)
world_locations = _table(
    "world_locations",
    Column("id", Text, primary_key=True),
    Column("region_id", Text, nullable=False),
    Column("name", Text, nullable=False, unique=True),
    Column("kind", Text, nullable=False),
    Column("parent_id", Text),
    Column("summary", Text, nullable=False),
)
world_routes = _table(
    "world_routes",
    Column("id", Text, primary_key=True),
    Column("from_location_id", Text, nullable=False),
    Column("to_location_id", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("difficulty", Text, nullable=False),
    Column("risk", Text, nullable=False),
    Column("summary", Text, nullable=False),
)
world_factions = _table(
    "world_factions",
    Column("id", Text, primary_key=True),
    Column("name", Text, nullable=False, unique=True),
    Column("kind", Text, nullable=False),
    Column("region_id", Text, nullable=False),
    Column("summary", Text, nullable=False),
)
world_arts = _table(
    "world_arts",
    Column("id", Text, primary_key=True),
    Column("name", Text, nullable=False, unique=True),
    Column("rank", Text, nullable=False),
    Column("category", Text, nullable=False),
    Column("primary_element", Text, nullable=False),
    Column("realm_cap", Text, nullable=False),
    Column("summary", Text, nullable=False),
    Column("visibility", Text, nullable=False),
    Column("source_location_id", Text),
    Column("source_label", Text, nullable=False),
)
world_opportunities = _table(
    "world_opportunities",
    Column("id", Text, primary_key=True),
    Column("name", Text, nullable=False, unique=True),
    Column("location_id", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("clue", Text, nullable=False),
    Column("danger", Text, nullable=False),
    Column("default_state", Text, nullable=False),
)
world_realms = _table(
    "world_realms",
    Column("id", Text, primary_key=True),
    Column("name", Text, nullable=False, unique=True),
    Column("entrance_location_id", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("opening_rule", Text, nullable=False),
    Column("entry_limit", Text, nullable=False),
)
world_cultivation_demographics = _table(
    "world_cultivation_demographics",
    Column("id", Text, primary_key=True),
    Column("name", Text, nullable=False, unique=True),
    Column("sort_order", Integer, nullable=False),
    Column("rarity", Text, nullable=False),
    Column("prevalence", Text, nullable=False),
    Column("npc_rule", Text, nullable=False),
)


def _save_fk() -> ForeignKey:
    return ForeignKey("saves.id", ondelete="CASCADE")


save_player_location = _table(
    "save_player_location",
    Column("save_id", String(64), _save_fk(), primary_key=True),
    Column("region_id", Text, nullable=False),
    Column("location_id", Text, nullable=False),
    Column("site_name", Text, nullable=False, server_default=""),
    Column("location_state", Text, nullable=False, server_default="安全"),
    Column("intended_destination_id", Text),
    Column("lost_risk", Text, nullable=False, server_default="无"),
    Column("updated_at", Float, nullable=False),
)
save_world_time = _table(
    "save_world_time",
    Column("save_id", String(64), _save_fk(), primary_key=True),
    Column("day", Integer, nullable=False, server_default="1"),
    Column("minute_of_day", Integer, nullable=False, server_default="930"),
    Column("season", Text, nullable=False, server_default=""),
    Column("calendar_label", Text, nullable=False, server_default=""),
    Column("updated_at", Float, nullable=False),
)
save_player_knowledge = _table(
    "save_player_knowledge",
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("save_id", String(64), _save_fk(), nullable=False),
    Column("knowledge_type", Text, nullable=False),
    Column("target_id", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("reliability", Text, nullable=False, server_default="medium"),
    Column("source", Text, nullable=False, server_default=""),
    Column("detail_level", Text, nullable=False, server_default=""),
    Column("notes", Text, nullable=False, server_default=""),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    UniqueConstraint("save_id", "knowledge_type", "target_id"),
)
save_opportunity_states = _table(
    "save_opportunity_states",
    Column("save_id", String(64), _save_fk(), primary_key=True),
    Column("opportunity_id", Text, primary_key=True),
    Column("state", Text, nullable=False),
    Column("notes", Text, nullable=False, server_default=""),
    Column("updated_at", Float, nullable=False),
)
save_realm_states = _table(
    "save_realm_states",
    Column("save_id", String(64), _save_fk(), primary_key=True),
    Column("realm_id", Text, primary_key=True),
    Column("state", Text, nullable=False),
    Column("notes", Text, nullable=False, server_default=""),
    Column("updated_at", Float, nullable=False),
)
save_opportunity_rewards = _table(
    "save_opportunity_rewards",
    Column("payoff_id", Text, primary_key=True),
    Column("save_id", String(64), _save_fk(), nullable=False),
    Column("opportunity_id", Text, nullable=False),
    Column("reward_kind", Text, nullable=False),
    Column("reward_id", Text, nullable=False),
    Column("status", Text, nullable=False, server_default="pending"),
    Column("created_turn", Integer, nullable=False),
    Column("triggered_turn", Integer),
    Column("updated_at", Float, nullable=False),
)
Index(
    "idx_save_opportunity_rewards_save",
    save_opportunity_rewards.c.save_id,
    save_opportunity_rewards.c.status,
)


llm_request_metrics = _table(
    "llm_request_metrics",
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("save_id", String(64), _save_fk()),
    Column("request_type", Text, nullable=False),
    Column("protocol", Text, nullable=False),
    Column("model", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("duration_ms", Integer, nullable=False),
    Column("input_chars", Integer, nullable=False),
    Column("output_chars", Integer, nullable=False),
    Column("prompt_tokens", Integer),
    Column("completion_tokens", Integer),
    Column("total_tokens", Integer),
    Column("cache_hit_tokens", Integer),
    Column("cache_miss_tokens", Integer),
    Column("error_type", Text, nullable=False, server_default=""),
    Column("created_at", Float, nullable=False),
)
agent_traces = _table(
    "agent_traces",
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("save_id", String(64), _save_fk()),
    Column("turn", Integer),
    Column("agent_type", Text, nullable=False),
    Column("protocol", Text, nullable=False),
    Column("model", Text, nullable=False),
    Column("stream", Integer, nullable=False, server_default="0"),
    Column("input_messages", Text, nullable=False, server_default="[]"),
    Column("raw_output", Text, nullable=False, server_default=""),
    Column("status", Text, nullable=False),
    Column("duration_ms", Integer, nullable=False),
    Column("error_type", Text, nullable=False, server_default=""),
    Column("error_message", Text, nullable=False, server_default=""),
    Column("input_tokens", Integer),
    Column("output_tokens", Integer),
    Column("total_tokens", Integer),
    Column("cache_hit_tokens", Integer),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
)
Index("idx_agent_traces_save_turn", agent_traces.c.save_id, agent_traces.c.turn, agent_traces.c.id)

prompt_definitions = _table(
    "prompt_definitions",
    Column("name", Text, primary_key=True),
    Column("description", Text, nullable=False, server_default=""),
    Column("expected_variables", Text, nullable=False, server_default="[]"),
    Column("source", Text, nullable=False, server_default="custom"),
    Column("production_version", Integer),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
)
prompt_versions = _table(
    "prompt_versions",
    Column("prompt_name", Text, ForeignKey("prompt_definitions.name", ondelete="CASCADE"), primary_key=True),
    Column("version", Integer, primary_key=True),
    Column("content", Text, nullable=False),
    Column("variables", Text, nullable=False, server_default="[]"),
    Column("created_by", Text, nullable=False, server_default=""),
    Column("created_at", Float, nullable=False),
)
Index("idx_prompt_versions_created", prompt_versions.c.prompt_name, prompt_versions.c.created_at.desc())


execution_runs = _table(
    "execution_runs",
    Column("id", String(64), primary_key=True),
    Column("sid", String(64), _save_fk(), nullable=False),
    Column("kind", String(16), nullable=False, server_default="primary"),
    Column("input_kind", String(16), nullable=False),
    Column("action", Text),
    Column("action_hash", String(64), nullable=False),
    Column("client_request_id", String(128)),
    Column("request_snapshot", JSON_PAYLOAD, nullable=False, server_default=text("'{}'")),
    Column("base_turn", Integer, nullable=False),
    Column("base_version", Integer, nullable=False),
    Column("target_turn", Integer, nullable=False),
    Column("parent_run_id", String(64), ForeignKey("execution_runs.id", ondelete="SET NULL")),
    Column("fork_node_id", String(64)),
    Column("rerun_mode", String(24)),
    Column("workflow_id", String(255), nullable=False, unique=True),
    Column("workflow_run_id", String(255)),
    Column("status", String(32), nullable=False, server_default="starting"),
    Column("cancel_requested_at", Float),
    Column("degraded", Boolean, nullable=False, server_default=text("false")),
    Column("event_sequence", BigInteger, nullable=False, server_default="0"),
    Column("error", JSON_PAYLOAD),
    Column("code_revision", String(128), nullable=False, server_default="unknown"),
    Column("prompt_bundle_version", String(128), nullable=False, server_default="unknown"),
    Column("started_at", Float),
    Column("completed_at", Float),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    UniqueConstraint("sid", "client_request_id", name="uq_execution_runs_client_request"),
)
Index("idx_execution_runs_sid_created", execution_runs.c.sid, execution_runs.c.created_at.desc())
Index("idx_execution_runs_status", execution_runs.c.status, execution_runs.c.updated_at)
Index(
    "uq_execution_runs_primary_success",
    execution_runs.c.sid,
    execution_runs.c.target_turn,
    unique=True,
    postgresql_where=text("kind = 'primary' AND status = 'succeeded'"),
    sqlite_where=text("kind = 'primary' AND status = 'succeeded'"),
)

execution_nodes = _table(
    "execution_nodes",
    Column("id", String(64), primary_key=True),
    Column("run_id", String(64), ForeignKey("execution_runs.id", ondelete="CASCADE"), nullable=False),
    Column("node_key", String(64), nullable=False),
    Column("ordinal", Integer, nullable=False),
    Column("source_node_id", String(64), ForeignKey("execution_nodes.id", ondelete="SET NULL")),
    Column("status", String(32), nullable=False, server_default="pending"),
    Column("normalized_output", JSON_PAYLOAD),
    Column("result_attempt_id", String(64)),
    Column("schema_version", String(32), nullable=False, server_default="1"),
    Column("code_revision", String(128), nullable=False, server_default="unknown"),
    Column("prompt_bundle_version", String(128), nullable=False, server_default="unknown"),
    Column("started_at", Float),
    Column("completed_at", Float),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    UniqueConstraint("run_id", "node_key", name="uq_execution_nodes_run_key"),
)
Index("idx_execution_nodes_run_ordinal", execution_nodes.c.run_id, execution_nodes.c.ordinal)

execution_node_edges = _table(
    "execution_node_edges",
    Column("run_id", String(64), ForeignKey("execution_runs.id", ondelete="CASCADE"), primary_key=True),
    Column("from_node_id", String(64), ForeignKey("execution_nodes.id", ondelete="CASCADE"), primary_key=True),
    Column("to_node_id", String(64), ForeignKey("execution_nodes.id", ondelete="CASCADE"), primary_key=True),
    Column("edge_kind", String(24), nullable=False, server_default="depends_on"),
)

llm_attempts = _table(
    "llm_attempts",
    Column("id", String(64), primary_key=True),
    Column("node_id", String(64), ForeignKey("execution_nodes.id", ondelete="CASCADE"), nullable=False),
    Column("attempt", Integer, nullable=False),
    Column("activity_id", String(255), nullable=False),
    Column("activity_attempt", Integer, nullable=False),
    Column("provider_request_id", String(255)),
    Column("request_snapshot", JSON_PAYLOAD, nullable=False),
    Column("raw_output", Text, nullable=False, server_default=""),
    Column("normalized_output", JSON_PAYLOAD),
    Column("status", String(32), nullable=False),
    Column("error", JSON_PAYLOAD),
    Column("input_tokens", Integer),
    Column("output_tokens", Integer),
    Column("total_tokens", Integer),
    Column("duration_ms", Integer),
    Column("created_at", Float, nullable=False),
    Column("completed_at", Float),
    UniqueConstraint("node_id", "activity_id", "activity_attempt", name="uq_llm_attempt_activity"),
    UniqueConstraint("node_id", "attempt", name="uq_llm_attempt_number"),
)
Index("idx_llm_attempts_node", llm_attempts.c.node_id, llm_attempts.c.attempt)

state_snapshots = _table(
    "state_snapshots",
    Column("id", String(64), primary_key=True),
    Column("node_id", String(64), ForeignKey("execution_nodes.id", ondelete="CASCADE"), nullable=False),
    Column("phase", String(16), nullable=False),
    Column("state", JSON_PAYLOAD, nullable=False),
    Column("content_hash", String(64), nullable=False),
    Column("schema_version", String(32), nullable=False),
    Column("code_revision", String(128), nullable=False),
    Column("prompt_bundle_version", String(128), nullable=False),
    Column("created_at", Float, nullable=False),
    UniqueConstraint("node_id", "phase", name="uq_state_snapshots_node_phase"),
)

execution_events = _table(
    "execution_events",
    Column("run_id", String(64), ForeignKey("execution_runs.id", ondelete="CASCADE"), primary_key=True),
    Column("sequence", BigInteger, primary_key=True),
    Column("node_id", String(64), ForeignKey("execution_nodes.id", ondelete="SET NULL")),
    Column("attempt_id", String(64), ForeignKey("llm_attempts.id", ondelete="SET NULL")),
    Column("event_type", String(32), nullable=False),
    Column("payload", JSON_PAYLOAD, nullable=False),
    Column("created_at", Float, nullable=False),
)
Index("idx_execution_events_run_sequence", execution_events.c.run_id, execution_events.c.sequence)


def database_url() -> str:
    configured = os.getenv("DATABASE_URL", "").strip()
    if configured:
        return configured
    data_dir = Path(
        os.getenv("STORY_DATA_DIR") or Path(__file__).resolve().parent / "data"
    )
    return f"sqlite+aiosqlite:///{data_dir / 'saves.db'}"


def sync_database_url() -> str:
    url = database_url()
    if url.startswith("postgresql+asyncpg://"):
        return "postgresql+psycopg://" + url.removeprefix("postgresql+asyncpg://")
    if url.startswith("sqlite+aiosqlite://"):
        return "sqlite+pysqlite://" + url.removeprefix("sqlite+aiosqlite://")
    return url


def is_postgres() -> bool:
    return database_url().startswith("postgresql")


def make_async_engine(**kwargs) -> AsyncEngine:
    created = create_async_engine(database_url(), pool_pre_ping=True, **kwargs)
    if database_url().startswith("sqlite"):
        event.listen(created.sync_engine, "connect", _enable_sqlite_foreign_keys)
    return created


def make_sync_engine(**kwargs) -> Engine:
    created = create_engine(sync_database_url(), pool_pre_ping=True, **kwargs)
    if sync_database_url().startswith("sqlite"):
        event.listen(created, "connect", _enable_sqlite_foreign_keys)
    return created


def _enable_sqlite_foreign_keys(connection, _record) -> None:
    cursor = connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()
