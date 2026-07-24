"""SQLite 持久化层：存档的增删改查。

一个存档(save) = 一整局游戏，包含：
- messages   : 喂给 LLM 的消息数组（会按轮数截断，省 token）
- transcript : 展示用的完整剧情，只增不删（读档时重放全程）

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
    """建表（幂等）。"""
    with _conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS saves (
                id          TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                messages    TEXT NOT NULL,   -- JSON: list[dict]
                transcript  TEXT NOT NULL,   -- JSON: list[dict{role,text}]
                turns       INTEGER NOT NULL DEFAULT 0,
                created_at  REAL NOT NULL,
                updated_at  REAL NOT NULL
            )
            """
        )


def create(name: str, messages: list[dict]) -> str:
    """新建存档，返回 save_id。"""
    sid = uuid.uuid4().hex
    now = time.time()
    with _conn() as conn:
        conn.execute(
            "INSERT INTO saves (id, name, messages, transcript, turns, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 0, ?, ?)",
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
