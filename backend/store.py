"""SQLite 持久化层：存档的增删改查。

一个存档(save) = 一整局游戏，包含：
- messages   : 喂给 LLM 的消息数组（会按轮数截断，省 token）
- transcript : 展示用的完整剧情，只增不删（读档时重放全程）
- world_memory : 长期世界记忆（剧情事实、问询、人物、地点、物品等）

单机单进程使用，每次操作开独立连接，简单可靠。
"""

import json
import os
import sqlite3
import time
import uuid

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
DB_PATH = os.path.join(DATA_DIR, "saves.db")


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
                name        TEXT NOT NULL,
                messages    TEXT NOT NULL,   -- JSON: list[dict]
                transcript  TEXT NOT NULL,   -- JSON: list[dict{role,text}]
                turns       INTEGER NOT NULL DEFAULT 0,
                lore        TEXT NOT NULL DEFAULT '[]',  -- JSON: list[dict{q,a,ts}]，见闻录
                world_memory TEXT NOT NULL DEFAULT '[]', -- JSON: list[dict]，长期世界记忆
                inventory   TEXT NOT NULL DEFAULT '[]',  -- JSON: list[dict{id,name,attrs,kind,whereabouts,last_turn}]，物品影子库
                director_state TEXT NOT NULL DEFAULT '{}', -- JSON: dict，导演模块状态（当前爽点/留白期等）
                created_at  REAL NOT NULL,
                updated_at  REAL NOT NULL
            )
            """
        )
        # 老库迁移：改动前建的表没有 lore/inventory 列，幂等补上
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(saves)")}
        if "lore" not in cols:
            conn.execute("ALTER TABLE saves ADD COLUMN lore TEXT NOT NULL DEFAULT '[]'")
        if "inventory" not in cols:
            conn.execute("ALTER TABLE saves ADD COLUMN inventory TEXT NOT NULL DEFAULT '[]'")
        if "world_memory" not in cols:
            conn.execute("ALTER TABLE saves ADD COLUMN world_memory TEXT NOT NULL DEFAULT '[]'")
        if "director_state" not in cols:
            conn.execute("ALTER TABLE saves ADD COLUMN director_state TEXT NOT NULL DEFAULT '{}'")
        _migrate_lore_to_world_memory(conn)


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


def create(name: str, messages: list[dict]) -> str:
    """新建存档，返回 save_id。"""
    sid = uuid.uuid4().hex
    now = time.time()
    with _conn() as conn:
        conn.execute(
            "INSERT INTO saves (id, name, messages, transcript, turns, lore, inventory, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 0, '[]', '[]', ?, ?)",
            (sid, name, json.dumps(messages, ensure_ascii=False), "[]", now, now),
        )
    return sid


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


def load(sid: str) -> dict | None:
    """读取单个存档的完整数据；不存在返回 None。"""
    with _conn() as conn:
        row = conn.execute("SELECT * FROM saves WHERE id=?", (sid,)).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"],
        "name": row["name"],
        "messages": json.loads(row["messages"]),
        "transcript": json.loads(row["transcript"]),
        "turns": row["turns"],
        "lore": json.loads(row["lore"] or "[]"),
        "world_memory": json.loads(row["world_memory"] or "[]"),
        "inventory": json.loads(row["inventory"] or "[]"),
        "director_state": json.loads(row["director_state"] or "{}"),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def exists(sid: str) -> bool:
    with _conn() as conn:
        row = conn.execute("SELECT 1 FROM saves WHERE id=?", (sid,)).fetchone()
    return row is not None


def list_saves() -> list[dict]:
    """列出所有存档的摘要（不含完整 messages/transcript），按最近更新排序。"""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, name, turns, transcript, created_at, updated_at "
            "FROM saves ORDER BY updated_at DESC"
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
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
            }
        )
    return result


def rename(sid: str, name: str) -> bool:
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE saves SET name=?, updated_at=? WHERE id=?",
            (name, time.time(), sid),
        )
    return cur.rowcount > 0


def delete(sid: str) -> bool:
    with _conn() as conn:
        cur = conn.execute("DELETE FROM saves WHERE id=?", (sid,))
    return cur.rowcount > 0
