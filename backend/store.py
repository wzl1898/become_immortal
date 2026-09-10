"""SQLite 持久化层：存档的增删改查。

一个存档(save) = 一整局游戏，包含：
- messages   : 喂给 LLM 的消息数组（会按轮数截断，省 token）
- transcript : 展示用的完整剧情，只增不删（读档时重放全程）
- character_state : 主角当前状态快照（从最新《状态》面板解析）
- world_memory : 长期世界记忆（剧情事实、问询、人物、地点、物品等）

单机单进程使用，每次操作开独立连接，简单可靠。
"""

import json
import os
import re
import sqlite3
import time
import uuid

import story_cards

DATA_DIR = os.path.abspath(
    os.getenv("STORY_DATA_DIR") or os.path.join(os.path.dirname(__file__), "data")
)
DB_PATH = os.path.join(DATA_DIR, "saves.db")
DEFAULT_USER_ID = "default"
DEFAULT_WORLD_SEASON = story_cards.get()["initial"]["time"]["season"]
DEFAULT_CALENDAR_LABEL = story_cards.get()["initial"]["time"]["calendar_label"]


def _conn() -> sqlite3.Connection:
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    """建表（幂等），并对老库补齐新列。"""
    with _conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS saves (
                id          TEXT PRIMARY KEY,
                user_id     TEXT NOT NULL DEFAULT 'default',
                name        TEXT NOT NULL,
                messages    TEXT NOT NULL,   -- JSON: list[dict]
                transcript  TEXT NOT NULL,   -- JSON: list[dict{role,text}]
                turns       INTEGER NOT NULL DEFAULT 0,
                lore        TEXT NOT NULL DEFAULT '[]',  -- JSON: list[dict{q,a,ts}]，见闻录
                character_state TEXT NOT NULL DEFAULT '{}', -- JSON: dict，主角当前状态
                world_memory TEXT NOT NULL DEFAULT '[]', -- JSON: list[dict]，长期世界记忆
                world_entities TEXT NOT NULL DEFAULT '{}', -- JSON: dict，规范实体表 canonical_id->{name,aliases,identity}
                inventory   TEXT NOT NULL DEFAULT '[]',  -- JSON: list[dict{id,name,attrs,kind,whereabouts,last_turn}]，物品影子库
                director_state TEXT NOT NULL DEFAULT '{}', -- JSON: dict，导演模块状态（当前爽点/留白期等）
                stage_summary TEXT NOT NULL DEFAULT '', -- 低频更新的历史阶段摘要
                summary_turn INTEGER NOT NULL DEFAULT 0,
                created_at  REAL NOT NULL,
                updated_at  REAL NOT NULL
            )
            """
        )
        # 老库迁移：改动前建的表没有 lore/inventory 列，幂等补上
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(saves)")}
        if "user_id" not in cols:
            conn.execute(
                "ALTER TABLE saves ADD COLUMN user_id TEXT NOT NULL DEFAULT 'default'"
            )
        if "lore" not in cols:
            conn.execute("ALTER TABLE saves ADD COLUMN lore TEXT NOT NULL DEFAULT '[]'")
        if "inventory" not in cols:
            conn.execute("ALTER TABLE saves ADD COLUMN inventory TEXT NOT NULL DEFAULT '[]'")
        if "character_state" not in cols:
            conn.execute("ALTER TABLE saves ADD COLUMN character_state TEXT NOT NULL DEFAULT '{}'")
        if "world_memory" not in cols:
            conn.execute("ALTER TABLE saves ADD COLUMN world_memory TEXT NOT NULL DEFAULT '[]'")
        if "world_entities" not in cols:
            conn.execute("ALTER TABLE saves ADD COLUMN world_entities TEXT NOT NULL DEFAULT '{}'")
        if "director_state" not in cols:
            conn.execute("ALTER TABLE saves ADD COLUMN director_state TEXT NOT NULL DEFAULT '{}'")
        if "stage_summary" not in cols:
            conn.execute("ALTER TABLE saves ADD COLUMN stage_summary TEXT NOT NULL DEFAULT ''")
        if "summary_turn" not in cols:
            conn.execute("ALTER TABLE saves ADD COLUMN summary_turn INTEGER NOT NULL DEFAULT 0")
        if "story_card" not in cols:
            conn.execute(
                "ALTER TABLE saves ADD COLUMN story_card TEXT NOT NULL DEFAULT '{}'"
            )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_saves_user_updated "
            "ON saves(user_id, updated_at DESC)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS llm_request_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                save_id TEXT,
                request_type TEXT NOT NULL,
                protocol TEXT NOT NULL,
                model TEXT NOT NULL,
                status TEXT NOT NULL,
                duration_ms INTEGER NOT NULL,
                input_chars INTEGER NOT NULL,
                output_chars INTEGER NOT NULL,
                prompt_tokens INTEGER,
                completion_tokens INTEGER,
                total_tokens INTEGER,
                cache_hit_tokens INTEGER,
                cache_miss_tokens INTEGER,
                error_type TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_traces (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                save_id TEXT,
                turn INTEGER,
                agent_type TEXT NOT NULL,
                protocol TEXT NOT NULL,
                model TEXT NOT NULL,
                stream INTEGER NOT NULL DEFAULT 0,
                input_messages TEXT NOT NULL DEFAULT '[]',
                raw_output TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                duration_ms INTEGER NOT NULL,
                error_type TEXT NOT NULL DEFAULT '',
                error_message TEXT NOT NULL DEFAULT '',
                input_tokens INTEGER,
                output_tokens INTEGER,
                total_tokens INTEGER,
                cache_hit_tokens INTEGER,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        trace_cols = {row["name"] for row in conn.execute("PRAGMA table_info(agent_traces)")}
        if "updated_at" not in trace_cols:
            conn.execute("ALTER TABLE agent_traces ADD COLUMN updated_at REAL NOT NULL DEFAULT 0")
            conn.execute("UPDATE agent_traces SET updated_at=created_at WHERE updated_at=0")
        for column in ("input_tokens", "output_tokens", "total_tokens", "cache_hit_tokens"):
            if column not in trace_cols:
                conn.execute(f"ALTER TABLE agent_traces ADD COLUMN {column} INTEGER")
        metric_cols = {row["name"] for row in conn.execute("PRAGMA table_info(llm_request_metrics)")}
        if "total_tokens" not in metric_cols:
            conn.execute("ALTER TABLE llm_request_metrics ADD COLUMN total_tokens INTEGER")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_agent_traces_save_turn "
            "ON agent_traces(save_id, turn, id)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS save_opportunity_rewards (
                payoff_id       TEXT PRIMARY KEY,
                save_id         TEXT NOT NULL,
                opportunity_id  TEXT NOT NULL,
                reward_kind     TEXT NOT NULL,
                reward_id       TEXT NOT NULL,
                status          TEXT NOT NULL DEFAULT 'pending',
                created_turn    INTEGER NOT NULL,
                triggered_turn  INTEGER,
                updated_at      REAL NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_save_opportunity_rewards_save "
            "ON save_opportunity_rewards(save_id, status)"
        )
        _init_world_tables(conn)
        _migrate_story_cards(conn)
        _migrate_lore_to_world_memory(conn)
        _migrate_character_state(conn)


_STATUS_RE = re.compile(r"《状态》(.*?)《/状态》", re.S)


def _parse_character_state(
    status_text: str, turn: int, updated_at: float, card: dict | None = None
) -> dict:
    state = {}
    for line in status_text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^(.+?)[：:]\s*(.*)$", line)
        if not m:
            continue
        key = {label: key for key, label in story_cards.field_labels(card)}.get(
            m.group(1).strip()
        )
        if key:
            state[key] = m.group(2).strip()
    if not state:
        return {}
    state["turn"] = turn
    state["updated_at"] = updated_at
    return state


def _latest_character_state(
    transcript: list[dict], turns: int, updated_at: float, card: dict | None = None
) -> dict:
    for blk in reversed(transcript):
        if blk.get("role") != "narration":
            continue
        match = _STATUS_RE.search(blk.get("text", ""))
        if match:
            return _parse_character_state(match.group(1), turns, updated_at, card)
    return {}


def _migrate_character_state(conn: sqlite3.Connection) -> None:
    """从旧 transcript 的最后一个状态面板回填主角状态；已有值不覆盖。"""
    rows = conn.execute(
        "SELECT id, transcript, turns, updated_at, character_state, story_card FROM saves "
        "WHERE transcript IS NOT NULL AND transcript != '[]'"
    ).fetchall()
    for row in rows:
        try:
            existing = json.loads(row["character_state"] or "{}")
            transcript = json.loads(row["transcript"] or "[]")
        except json.JSONDecodeError:
            continue
        if existing:
            continue
        character_state = _latest_character_state(
            transcript,
            int(row["turns"] or 0),
            float(row["updated_at"] or time.time()),
            json.loads(row["story_card"]),
        )
        if character_state:
            conn.execute(
                "UPDATE saves SET character_state=? WHERE id=?",
                (json.dumps(character_state, ensure_ascii=False), row["id"]),
            )


def _migrate_lore_to_world_memory(conn: sqlite3.Connection) -> None:
    """把旧见闻录迁移成 qa 类型世界记忆；已迁移过的存档不重复写。"""
    rows = conn.execute(
        "SELECT id, turns, lore, world_memory FROM saves WHERE lore IS NOT NULL AND lore != '[]'"
    ).fetchall()
    for row in rows:
        try:
            existing = json.loads(row["world_memory"] or "[]")
            lore = json.loads(row["lore"] or "[]")
        except json.JSONDecodeError:
            continue
        if existing or not lore:
            continue
        migrated = []
        for entry in lore:
            q = (entry.get("q") or "").strip()
            a = (entry.get("a") or "").strip()
            if not q and not a:
                continue
            try:
                ts = float(entry.get("ts") or time.time())
            except (TypeError, ValueError):
                ts = time.time()
            migrated.append({
                "id": uuid.uuid4().hex,
                "type": "qa",
                "text": f"问：{q}　答：{a}" if q else a,
                "entities": [],
                "turn": row["turns"],
                "importance": 0.7,
                "source": "inquiry_migration",
                "q": q,
                "a": a,
                "ts": ts,
            })
        if migrated:
            conn.execute(
                "UPDATE saves SET world_memory=? WHERE id=?",
                (json.dumps(migrated, ensure_ascii=False), row["id"]),
            )


def create(
    name: str,
    messages: list[dict],
    user_id: str = DEFAULT_USER_ID,
    *,
    story_card: dict | None = None,
) -> str:
    """Create a save with a validated world snapshot, before any model call."""
    card = story_card if story_card is not None else story_cards.get()
    story_cards.validate(card)
    sid = uuid.uuid4().hex
    now = time.time()
    character = {field["key"]: field["initial"] for field in card["status_fields"]}
    with _conn() as conn:
        conn.execute(
            "INSERT INTO saves (id, user_id, name, messages, transcript, turns, lore, inventory, character_state, story_card, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, '[]', 0, '[]', ?, ?, ?, ?, ?)",
            (
                sid,
                user_id,
                name,
                json.dumps(messages, ensure_ascii=False),
                json.dumps(story_cards.initial_inventory(card), ensure_ascii=False),
                json.dumps(character, ensure_ascii=False),
                json.dumps(card, ensure_ascii=False),
                now,
                now,
            ),
        )
        _ensure_default_save_world_state(conn, sid)
    return sid


# Legacy world tables are kept for database compatibility. New saves read only
# their own card snapshot, so cards may safely reuse entity IDs.
_WORLD_TABLES = {
    "regions": ("world_regions", "id name role summary"),
    "locations": ("world_locations", "id region_id name kind parent_id summary"),
    "routes": (
        "world_routes",
        "id from_location_id to_location_id name difficulty risk summary",
    ),
    "factions": ("world_factions", "id name kind region_id summary"),
    "rewards": (
        "world_arts",
        "id name rank category primary_element realm_cap summary visibility source_location_id source_label",
    ),
    "opportunities": (
        "world_opportunities",
        "id name location_id kind clue danger default_state",
    ),
    "special_locations": (
        "world_realms",
        "id name entrance_location_id kind opening_rule entry_limit",
    ),
    "power_levels": (
        "world_cultivation_demographics",
        "id name sort_order rarity prevalence npc_rule",
    ),
}


def _init_world_tables(conn: sqlite3.Connection) -> None:
    """固定世界表与每档世界状态表。world_* 是事实，save_* 是玩家视野/状态。"""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS world_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS world_regions (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            role TEXT NOT NULL,
            summary TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS world_locations (
            id TEXT PRIMARY KEY,
            region_id TEXT NOT NULL,
            name TEXT NOT NULL UNIQUE,
            kind TEXT NOT NULL,
            parent_id TEXT,
            summary TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS world_routes (
            id TEXT PRIMARY KEY,
            from_location_id TEXT NOT NULL,
            to_location_id TEXT NOT NULL,
            name TEXT NOT NULL,
            difficulty TEXT NOT NULL,
            risk TEXT NOT NULL,
            summary TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS world_factions (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            kind TEXT NOT NULL,
            region_id TEXT NOT NULL,
            summary TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS world_arts (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            rank TEXT NOT NULL,
            category TEXT NOT NULL,
            primary_element TEXT NOT NULL,
            realm_cap TEXT NOT NULL,
            summary TEXT NOT NULL,
            visibility TEXT NOT NULL,
            source_location_id TEXT,
            source_label TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS world_opportunities (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            location_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            clue TEXT NOT NULL,
            danger TEXT NOT NULL,
            default_state TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS world_realms (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            entrance_location_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            opening_rule TEXT NOT NULL,
            entry_limit TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS world_cultivation_demographics (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            sort_order INTEGER NOT NULL,
            rarity TEXT NOT NULL,
            prevalence TEXT NOT NULL,
            npc_rule TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS save_player_location (
            save_id TEXT PRIMARY KEY,
            region_id TEXT NOT NULL,
            location_id TEXT NOT NULL,
            site_name TEXT NOT NULL DEFAULT '',
            location_state TEXT NOT NULL DEFAULT '安全',
            intended_destination_id TEXT,
            lost_risk TEXT NOT NULL DEFAULT '无',
            updated_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS save_world_time (
            save_id TEXT PRIMARY KEY,
            day INTEGER NOT NULL DEFAULT 1,
            minute_of_day INTEGER NOT NULL DEFAULT 930,
            season TEXT NOT NULL DEFAULT '',
            calendar_label TEXT NOT NULL DEFAULT '',
            updated_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS save_player_knowledge (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            save_id TEXT NOT NULL,
            knowledge_type TEXT NOT NULL,
            target_id TEXT NOT NULL,
            status TEXT NOT NULL,
            reliability TEXT NOT NULL DEFAULT 'medium',
            source TEXT NOT NULL DEFAULT '',
            detail_level TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            UNIQUE(save_id, knowledge_type, target_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS save_opportunity_states (
            save_id TEXT NOT NULL,
            opportunity_id TEXT NOT NULL,
            state TEXT NOT NULL,
            notes TEXT NOT NULL DEFAULT '',
            updated_at REAL NOT NULL,
            PRIMARY KEY(save_id, opportunity_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS save_realm_states (
            save_id TEXT NOT NULL,
            realm_id TEXT NOT NULL,
            state TEXT NOT NULL,
            notes TEXT NOT NULL DEFAULT '',
            updated_at REAL NOT NULL,
            PRIMARY KEY(save_id, realm_id)
        )
        """
    )
    _seed_world(conn)


def _seed_world(conn: sqlite3.Connection) -> None:
    card = story_cards.get()
    for collection, (table, columns) in _WORLD_TABLES.items():
        fields = columns.split()
        conn.executemany(
            f"INSERT OR IGNORE INTO {table} ({', '.join(fields)}) VALUES ({', '.join('?' for _ in fields)})",
            [
                tuple(row.get(key) for key in fields)
                for row in card["world"][collection]
            ],
        )
    conn.execute(
        "INSERT OR IGNORE INTO world_meta (key, value) VALUES ('world_name', ?)",
        (card["world"]["name"],),
    )
    conn.execute(
        "INSERT OR IGNORE INTO world_meta (key, value) VALUES ('world_version', ?)",
        (str(card["version"]),),
    )


def _migrate_story_cards(conn: sqlite3.Connection) -> None:
    """Pin old saves to their existing world without touching story/state data."""
    rows = conn.execute("SELECT id FROM saves WHERE story_card='{}'").fetchall()
    if not rows:
        return
    card = story_cards.get()
    for collection, (table, _columns) in _WORLD_TABLES.items():
        card["world"][collection] = [
            dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")
        ]
    for reward in card["world"]["rewards"]:
        reward["reward_kind"] = "art"
    world_name = conn.execute(
        "SELECT value FROM world_meta WHERE key='world_name'"
    ).fetchone()
    if world_name:
        card["world"]["name"] = world_name["value"]
    snapshot = json.dumps(card, ensure_ascii=False)
    conn.executemany(
        "UPDATE saves SET story_card=? WHERE id=?",
        [(snapshot, row["id"]) for row in rows],
    )


def _card_in_connection(conn: sqlite3.Connection, sid: str) -> dict | None:
    row = conn.execute("SELECT * FROM saves WHERE id=?", (sid,)).fetchone()
    if row is None:
        return None
    return (
        json.loads(row["story_card"])
        if "story_card" in row.keys()
        else story_cards.get()
    )


def get_story_card(sid: str) -> dict | None:
    with _conn() as conn:
        return _card_in_connection(conn, sid)


def _ensure_default_save_world_state(conn: sqlite3.Connection, sid: str) -> None:
    card = _card_in_connection(conn, sid)
    if card is None:
        return
    initial = card["initial"]
    loc = initial["location"]
    now = time.time()
    conn.execute(
        "INSERT OR IGNORE INTO save_player_location "
        "(save_id, region_id, location_id, site_name, location_state, intended_destination_id, lost_risk, updated_at) "
        "VALUES (?, ?, ?, ?, ?, NULL, ?, ?)",
        (
            sid,
            loc["region_id"],
            loc["location_id"],
            loc.get("site_name", ""),
            loc.get("location_state", "安全"),
            loc.get("lost_risk", "无"),
            now,
        ),
    )
    clock = initial["time"]
    save_row = conn.execute("SELECT turns FROM saves WHERE id=?", (sid,)).fetchone()
    elapsed_turns = max(0, int(save_row["turns"] or 0) - 1)
    initial_minute = min(1439, clock["minute_of_day"] + elapsed_turns * 15)
    conn.execute(
        "INSERT OR IGNORE INTO save_world_time "
        "(save_id, day, minute_of_day, season, calendar_label, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        (
            sid,
            clock["day"],
            initial_minute,
            clock["season"],
            clock["calendar_label"],
            now,
        ),
    )
    for row in initial["knowledge"]:
        conn.execute(
            "INSERT OR IGNORE INTO save_player_knowledge "
            "(save_id, knowledge_type, target_id, status, reliability, source, detail_level, notes, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, '', ?, ?, ?)",
            (
                sid,
                row["knowledge_type"],
                row["target_id"],
                row["status"],
                row.get("reliability", "medium"),
                row.get("source", ""),
                row.get("notes", ""),
                now,
                now,
            ),
        )
    for row in card["world"]["opportunities"]:
        conn.execute(
            "INSERT OR IGNORE INTO save_opportunity_states (save_id, opportunity_id, state, notes, updated_at) VALUES (?, ?, ?, '', ?)",
            (sid, row["id"], row.get("default_state", "unknown"), now),
        )
    for row in card["world"]["special_locations"]:
        conn.execute(
            "INSERT OR IGNORE INTO save_realm_states (save_id, realm_id, state, notes, updated_at) VALUES (?, ?, 'unknown', '', ?)",
            (sid, row["id"], now),
        )


def ensure_save_world_state(sid: str) -> None:
    """给老存档补默认位置/知识；新存档 create 时已做。"""
    with _conn() as conn:
        _ensure_default_save_world_state(conn, sid)


def world_snapshot(sid: str) -> dict | None:
    """Join this save's pinned world facts with only its own mutable state."""
    with _conn() as conn:
        card = _card_in_connection(conn, sid)
        if card is None:
            return None
        _ensure_default_save_world_state(conn, sid)
        location = dict(
            conn.execute(
                "SELECT * FROM save_player_location WHERE save_id=?", (sid,)
            ).fetchone()
        )
        world_time = dict(
            conn.execute(
                "SELECT * FROM save_world_time WHERE save_id=?", (sid,)
            ).fetchone()
        )
        knowledge = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM save_player_knowledge WHERE save_id=? ORDER BY knowledge_type, status, id",
                (sid,),
            )
        ]
        opportunity_states = {
            row["opportunity_id"]: row["state"]
            for row in conn.execute(
                "SELECT * FROM save_opportunity_states WHERE save_id=?", (sid,)
            )
        }
    world = card["world"]
    place = next(
        row for row in world["locations"] if row["id"] == location["location_id"]
    )
    region = next(row for row in world["regions"] if row["id"] == location["region_id"])
    location.update(
        region_name=region["name"],
        location_name=place["name"],
        location_kind=place.get("kind", "site"),
        location_summary=place.get("summary", ""),
    )
    rewards = [
        {
            "rank": "",
            "category": "",
            "primary_element": "",
            "realm_cap": "",
            "summary": "",
            "visibility": "public",
            "source_label": "",
            **row,
        }
        for row in world["rewards"]
    ]
    return {
        **world,
        "world_name": world["name"],
        "story_card": card,
        "location": location,
        "time": world_time,
        "knowledge": knowledge,
        # Legacy internal names keep the persistence/recall protocol compatible.
        "arts": rewards,
        "realms": world["special_locations"],
        "cultivation_demographics": world["power_levels"],
        "opportunities": [
            {
                **row,
                "save_state": opportunity_states.get(
                    row["id"], row.get("default_state", "unknown")
                ),
            }
            for row in world["opportunities"]
        ],
    }


def update_player_location(
    sid: str,
    *,
    region_id: str,
    location_id: str,
    site_name: str = "",
    location_state: str = "安全",
    intended_destination_id: str | None = None,
    lost_risk: str = "无",
) -> None:
    with _conn() as conn:
        conn.execute(
            """
            UPDATE save_player_location
            SET region_id=?, location_id=?, site_name=?, location_state=?,
                intended_destination_id=?, lost_risk=?, updated_at=?
            WHERE save_id=?
            """,
            (region_id, location_id, site_name, location_state, intended_destination_id, lost_risk, time.time(), sid),
        )


def advance_world_time(sid: str, minutes: int) -> dict:
    """Advance the persistent story clock; the clock can never move backward."""
    elapsed = max(0, int(minutes))
    with _conn() as conn:
        _ensure_default_save_world_state(conn, sid)
        row = conn.execute(
            "SELECT * FROM save_world_time WHERE save_id=?", (sid,)
        ).fetchone()
        total = (int(row["day"]) - 1) * 1440 + int(row["minute_of_day"]) + elapsed
        day, minute_of_day = divmod(total, 1440)
        conn.execute(
            "UPDATE save_world_time SET day=?, minute_of_day=?, updated_at=? WHERE save_id=?",
            (day + 1, minute_of_day, time.time(), sid),
        )
        updated = conn.execute(
            "SELECT * FROM save_world_time WHERE save_id=?", (sid,)
        ).fetchone()
    return dict(updated)


def set_intended_destination(sid: str, target_id: str | None) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE save_player_location SET intended_destination_id=?, updated_at=? WHERE save_id=?",
            (target_id, time.time(), sid),
        )


def upsert_knowledge(
    sid: str,
    knowledge_type: str,
    target_id: str,
    status: str,
    *,
    reliability: str = "medium",
    source: str = "",
    notes: str = "",
) -> None:
    now = time.time()
    rank = {"unknown": 0, "rumored": 1, "known": 2, "confirmed": 3}
    with _conn() as conn:
        row = conn.execute(
            """
            SELECT status FROM save_player_knowledge
            WHERE save_id=? AND knowledge_type=? AND target_id=?
            """,
            (sid, knowledge_type, target_id),
        ).fetchone()
        if row is not None and rank.get(row["status"], 0) >= rank.get(status, 0):
            return
        conn.execute(
            """
            INSERT INTO save_player_knowledge
            (save_id, knowledge_type, target_id, status, reliability, source, detail_level, notes, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, '', ?, ?, ?)
            ON CONFLICT(save_id, knowledge_type, target_id) DO UPDATE SET
                status=excluded.status,
                reliability=excluded.reliability,
                source=excluded.source,
                notes=excluded.notes,
                updated_at=excluded.updated_at
            """,
            (sid, knowledge_type, target_id, status, reliability, source, notes, now, now),
        )


def save_state(sid: str, messages: list[dict], transcript: list[dict], turns: int) -> None:
    """覆盖写入某存档的当前状态（每手落盘）。"""
    with _conn() as conn:
        conn.execute(
            "UPDATE saves SET messages=?, transcript=?, turns=?, updated_at=? WHERE id=?",
            (
                json.dumps(messages, ensure_ascii=False),
                json.dumps(transcript, ensure_ascii=False),
                turns,
                time.time(),
                sid,
            ),
        )


def save_stage_summary(sid: str, summary: str, summary_turn: int) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE saves SET stage_summary=?, summary_turn=? WHERE id=?",
            (summary, summary_turn, sid),
        )


def record_llm_request_metric(metric: dict) -> None:
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO llm_request_metrics (
                save_id, request_type, protocol, model, status, duration_ms,
                input_chars, output_chars, prompt_tokens, completion_tokens, total_tokens,
                cache_hit_tokens, cache_miss_tokens, error_type, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                metric.get("save_id"), metric.get("request_type") or "unknown",
                metric.get("protocol") or "", metric.get("model") or "",
                metric.get("status") or "error", int(metric.get("duration_ms") or 0),
                int(metric.get("input_chars") or 0), int(metric.get("output_chars") or 0),
                metric.get("prompt_tokens"), metric.get("completion_tokens"),
                metric.get("total_tokens"),
                metric.get("cache_hit_tokens"), metric.get("cache_miss_tokens"),
                metric.get("error_type") or "", time.time(),
            ),
        )


def list_llm_request_metrics(sid: str, limit: int = 30) -> list[dict] | None:
    """Return recent LLM requests for a save, newest first."""
    limit = max(1, min(int(limit), 50))
    with _conn() as conn:
        if conn.execute("SELECT 1 FROM saves WHERE id=?", (sid,)).fetchone() is None:
            return None
        rows = conn.execute(
            """
            SELECT id, request_type, protocol, model, status, duration_ms,
                   input_chars, output_chars, prompt_tokens, completion_tokens, total_tokens,
                   cache_hit_tokens, cache_miss_tokens, error_type, created_at
            FROM llm_request_metrics
            WHERE save_id=?
            ORDER BY id DESC
            LIMIT ?
            """,
            (sid, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def record_agent_trace(trace: dict) -> int:
    """Create an Agent trace; callers may later finish the same row."""
    now = float(trace.get("created_at") or time.time())
    with _conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO agent_traces (
                save_id, turn, agent_type, protocol, model, stream,
                input_messages, raw_output, status, duration_ms,
                error_type, error_message, input_tokens, output_tokens,
                total_tokens, cache_hit_tokens, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trace.get("save_id"),
                trace.get("turn"),
                trace.get("agent_type") or "unknown",
                trace.get("protocol") or "",
                trace.get("model") or "",
                int(bool(trace.get("stream"))),
                json.dumps(trace.get("input_messages") or [], ensure_ascii=False, default=str),
                str(trace.get("raw_output") or ""),
                trace.get("status") or "unknown",
                int(trace.get("duration_ms") or 0),
                trace.get("error_type") or "",
                trace.get("error_message") or "",
                trace.get("input_tokens"),
                trace.get("output_tokens"),
                trace.get("total_tokens"),
                trace.get("cache_hit_tokens"),
                now,
                now,
            ),
        )
    return int(cur.lastrowid)


def finish_agent_trace(trace_id: int, trace: dict) -> None:
    """Finish a running Agent trace without changing its identity."""
    with _conn() as conn:
        conn.execute(
            """
            UPDATE agent_traces
            SET raw_output=?, status=?, duration_ms=?, error_type=?,
                error_message=?, input_tokens=?, output_tokens=?,
                total_tokens=?, cache_hit_tokens=?, updated_at=?
            WHERE id=?
            """,
            (
                str(trace.get("raw_output") or ""),
                trace.get("status") or "unknown",
                int(trace.get("duration_ms") or 0),
                trace.get("error_type") or "",
                trace.get("error_message") or "",
                trace.get("input_tokens"),
                trace.get("output_tokens"),
                trace.get("total_tokens"),
                trace.get("cache_hit_tokens"),
                time.time(),
                int(trace_id),
            ),
        )


def list_agent_traces(
    sid: str, *, turn: int | None = None, limit: int = 100,
    include_content: bool = False, updated_after: float | None = None,
) -> list[dict] | None:
    """Read trace summaries, or complete payloads when explicitly requested."""
    limit = max(1, min(int(limit), 500))
    with _conn() as conn:
        if conn.execute("SELECT 1 FROM saves WHERE id=?", (sid,)).fetchone() is None:
            return None
        content_columns = ", input_messages, raw_output, error_message" if include_content else ""
        clauses = ["save_id=?"]
        params: list = [sid]
        if turn is not None:
            clauses.append("turn=?")
            params.append(int(turn))
        if updated_after is not None:
            clauses.append("updated_at>?")
            params.append(float(updated_after))
        params.append(limit)
        order_by = "updated_at ASC, id ASC" if updated_after is not None else "id ASC"
        rows = conn.execute(
            f"""
            SELECT id, save_id, turn, agent_type, protocol, model, stream,
                   status, duration_ms, error_type, created_at, updated_at,
                   input_tokens, output_tokens, total_tokens, cache_hit_tokens,
                   length(input_messages) AS input_chars, length(raw_output) AS output_chars
                   {content_columns}
            FROM agent_traces
            WHERE {' AND '.join(clauses)}
            ORDER BY {order_by}
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
    result = [dict(row) for row in rows]
    if include_content:
        for row in result:
            row["input_messages"] = json.loads(row["input_messages"] or "[]")
    return result


def get_agent_token_stats(sid: str) -> dict | None:
    """Aggregate persisted token usage for a save overall and by Agent type."""
    with _conn() as conn:
        if conn.execute("SELECT 1 FROM saves WHERE id=?", (sid,)).fetchone() is None:
            return None
        select = """
            COUNT(*) AS calls,
            COUNT(total_tokens) AS measured_calls,
            COALESCE(SUM(input_tokens), 0) AS input_tokens,
            COALESCE(SUM(output_tokens), 0) AS output_tokens,
            COALESCE(SUM(total_tokens), 0) AS total_tokens,
            COALESCE(SUM(cache_hit_tokens), 0) AS cache_hit_tokens
        """
        total = dict(conn.execute(
            f"SELECT {select} FROM agent_traces WHERE save_id=?",
            (sid,),
        ).fetchone())
        rows = conn.execute(
            f"""
            SELECT agent_type, {select}
            FROM agent_traces
            WHERE save_id=?
            GROUP BY agent_type
            ORDER BY total_tokens DESC, agent_type ASC
            """,
            (sid,),
        ).fetchall()

    def with_rate(row: dict) -> dict:
        input_tokens = int(row.get("input_tokens") or 0)
        cache_hit_tokens = int(row.get("cache_hit_tokens") or 0)
        return {
            **row,
            "cache_hit_rate": round(cache_hit_tokens / input_tokens, 4) if input_tokens else 0,
        }

    return {
        "total": with_rate(total),
        "by_agent": [with_rate(dict(row)) for row in rows],
    }


def reap_stale_agent_traces(sid: str, *, max_age_seconds: float = 180.0) -> int:
    """Mark abandoned running traces so they cannot remain live forever."""
    now = time.time()
    cutoff = now - max(30.0, float(max_age_seconds))
    with _conn() as conn:
        cur = conn.execute(
            """
            UPDATE agent_traces
            SET status='timeout', error_type='StaleTrace',
                error_message='trace 未在超时窗口内完成，已自动收尾',
                duration_ms=CAST((? - created_at) * 1000 AS INTEGER),
                updated_at=?
            WHERE save_id=? AND status='running' AND created_at<?
            """,
            (now, now, sid, cutoff),
        )
        return int(cur.rowcount or 0)


def save_lore(sid: str, lore: list[dict]) -> None:
    """只更新见闻录（问询旁路，不触发主状态落盘）。"""
    with _conn() as conn:
        conn.execute(
            "UPDATE saves SET lore=?, updated_at=? WHERE id=?",
            (json.dumps(lore, ensure_ascii=False), time.time(), sid),
        )


def save_world_memory(sid: str, world_memory: list[dict]) -> None:
    """只更新世界记忆。"""
    with _conn() as conn:
        conn.execute(
            "UPDATE saves SET world_memory=?, updated_at=? WHERE id=?",
            (json.dumps(world_memory, ensure_ascii=False), time.time(), sid),
        )


def save_character_state(sid: str, character_state: dict) -> None:
    """只更新主角当前状态快照。"""
    with _conn() as conn:
        conn.execute(
            "UPDATE saves SET character_state=?, updated_at=? WHERE id=?",
            (json.dumps(character_state, ensure_ascii=False), time.time(), sid),
        )


def append_world_memory(sid: str, items: list[dict]) -> list[dict] | None:
    """追加世界记忆并返回新列表；存档不存在返回 None。"""
    if not items:
        return load(sid)["world_memory"] if exists(sid) else None
    with _conn() as conn:
        row = conn.execute(
            "SELECT world_memory FROM saves WHERE id=?",
            (sid,),
        ).fetchone()
        if row is None:
            return None
        current = json.loads(row["world_memory"] or "[]")
        current.extend(items)
        conn.execute(
            "UPDATE saves SET world_memory=?, updated_at=? WHERE id=?",
            (json.dumps(current, ensure_ascii=False), time.time(), sid),
        )
        return current


def save_world_entities(sid: str, world_entities: dict) -> None:
    """只更新规范实体表。"""
    with _conn() as conn:
        conn.execute(
            "UPDATE saves SET world_entities=?, updated_at=? WHERE id=?",
            (json.dumps(world_entities, ensure_ascii=False), time.time(), sid),
        )


def _state_key(mem: dict) -> tuple | None:
    """状态型记忆的合并键：(type, canonical_id)。缺 canonical_id 或非状态型返回 None。"""
    if mem.get("scope") != "state":
        return None
    cid = mem.get("canonical_id")
    if not cid:
        return None
    return (mem.get("type"), cid)


def upsert_world_memory(sid: str, items: list[dict]) -> list[dict] | None:
    """写入世界记忆：事件型追加，状态型按 (type, canonical_id) 覆盖旧条。

    存档不存在返回 None；items 为空返回当前列表。同批多条命中同键时以最后一条为准。
    """
    if not items:
        return load(sid)["world_memory"] if exists(sid) else None
    with _conn() as conn:
        row = conn.execute(
            "SELECT world_memory FROM saves WHERE id=?",
            (sid,),
        ).fetchone()
        if row is None:
            return None
        current = json.loads(row["world_memory"] or "[]")
        # 建索引：状态键 -> 在 current 中的下标
        index: dict[tuple, int] = {}
        for i, m in enumerate(current):
            k = _state_key(m)
            if k is not None:
                index[k] = i
        for item in items:
            k = _state_key(item)
            if k is not None and k in index:
                current[index[k]] = item  # 覆盖旧状态（丢弃旧内容）
            else:
                current.append(item)
                if k is not None:
                    index[k] = len(current) - 1
        conn.execute(
            "UPDATE saves SET world_memory=?, updated_at=? WHERE id=?",
            (json.dumps(current, ensure_ascii=False), time.time(), sid),
        )
        return current


def save_inventory(sid: str, inventory: list[dict]) -> None:
    """只更新物品影子库。"""
    with _conn() as conn:
        conn.execute(
            "UPDATE saves SET inventory=?, updated_at=? WHERE id=?",
            (json.dumps(inventory, ensure_ascii=False), time.time(), sid),
        )


def save_director_state(sid: str, state: dict) -> None:
    """只更新导演模块状态。"""
    with _conn() as conn:
        conn.execute(
            "UPDATE saves SET director_state=?, updated_at=? WHERE id=?",
            (json.dumps(state, ensure_ascii=False), time.time(), sid),
        )


def save_opportunity_reward_binding(sid: str, payoff: dict | None) -> None:
    """Persist the director's save-specific opportunity-to-reward binding."""
    binding = payoff.get("binding") if isinstance(payoff, dict) else None
    required = ("opportunity_id", "reward_kind", "reward_id")
    if (
        not isinstance(binding, dict)
        or not payoff.get("id")
        or any(not binding.get(key) for key in required)
    ):
        return
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO save_opportunity_rewards (
                payoff_id, save_id, opportunity_id, reward_kind, reward_id,
                status, created_turn, triggered_turn, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(payoff_id) DO UPDATE SET
                status=excluded.status,
                triggered_turn=excluded.triggered_turn,
                updated_at=excluded.updated_at
            """,
            (
                payoff["id"], sid, binding.get("opportunity_id"),
                binding.get("reward_kind"), binding.get("reward_id"),
                payoff.get("status") or "pending", int(payoff.get("created_turn") or 0),
                payoff.get("triggered_turn"), time.time(),
            ),
        )


def list_opportunity_reward_bindings(sid: str) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT payoff_id, opportunity_id, reward_kind, reward_id, status,
                   created_turn, triggered_turn
            FROM save_opportunity_rewards
            WHERE save_id=?
            ORDER BY created_turn, payoff_id
            """,
            (sid,),
        ).fetchall()
    return [dict(row) for row in rows]


def load(sid: str) -> dict | None:
    """读取单个存档的完整数据；不存在返回 None。"""
    with _conn() as conn:
        row = conn.execute("SELECT * FROM saves WHERE id=?", (sid,)).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"],
        "name": row["name"],
        "story_card": json.loads(row["story_card"]),
        "messages": json.loads(row["messages"]),
        "transcript": json.loads(row["transcript"]),
        "turns": row["turns"],
        "lore": json.loads(row["lore"] or "[]"),
        "character_state": json.loads(row["character_state"] or "{}"),
        "world_memory": json.loads(row["world_memory"] or "[]"),
        "world_entities": json.loads(row["world_entities"] or "{}"),
        "inventory": json.loads(row["inventory"] or "[]"),
        "director_state": json.loads(row["director_state"] or "{}"),
        "stage_summary": row["stage_summary"] or "",
        "summary_turn": int(row["summary_turn"] or 0),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def exists(sid: str) -> bool:
    with _conn() as conn:
        row = conn.execute("SELECT 1 FROM saves WHERE id=?", (sid,)).fetchone()
    return row is not None


def owned_by(sid: str, user_id: str) -> bool:
    """Return whether a save belongs to the given local user."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM saves WHERE id=? AND user_id=?", (sid, user_id)
        ).fetchone()
    return row is not None


def list_saves(user_id: str = DEFAULT_USER_ID) -> list[dict]:
    """列出当前用户的存档摘要，按最近更新排序。"""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, name, turns, transcript, story_card, created_at, updated_at "
            "FROM saves WHERE user_id=? ORDER BY updated_at DESC",
            (user_id,),
        ).fetchall()
    result = []
    for r in rows:
        transcript = json.loads(r["transcript"])
        preview = ""
        for blk in reversed(transcript):
            if blk.get("role") == "narration":
                preview = blk.get("text", "")[:60]
                break
        result.append(
            {
                "id": r["id"],
                "name": r["name"],
                "turns": r["turns"],
                "preview": preview,
                "story_card": story_cards.public(json.loads(r["story_card"])),
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
            }
        )
    return result


def rename(sid: str, name: str, user_id: str = DEFAULT_USER_ID) -> bool:
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE saves SET name=?, updated_at=? WHERE id=? AND user_id=?",
            (name, time.time(), sid, user_id),
        )
    return cur.rowcount > 0


def delete(sid: str, user_id: str = DEFAULT_USER_ID) -> bool:
    with _conn() as conn:
        if conn.execute(
            "SELECT 1 FROM saves WHERE id=? AND user_id=?", (sid, user_id)
        ).fetchone() is None:
            return False
        conn.execute("DELETE FROM save_opportunity_rewards WHERE save_id=?", (sid,))
        conn.execute("DELETE FROM save_world_time WHERE save_id=?", (sid,))
        conn.execute("DELETE FROM llm_request_metrics WHERE save_id=?", (sid,))
        conn.execute("DELETE FROM agent_traces WHERE save_id=?", (sid,))
        cur = conn.execute(
            "DELETE FROM saves WHERE id=? AND user_id=?", (sid, user_id)
        )
    return cur.rowcount > 0
