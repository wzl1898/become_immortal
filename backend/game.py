"""游戏会话状态管理。

内存态存储（进程内字典）。单机把玩足够；如需持久化可换 Redis/DB。
"""

import uuid

from prompts import OPENING_PROMPT, SYSTEM_PROMPT

# session_id -> messages(list[dict])
_SESSIONS: dict[str, list[dict]] = {}

# 保留的历史轮数（system 之外），防止上下文无限增长
MAX_TURNS = 40


def create_session() -> str:
    """新建一局，返回 session_id。"""
    sid = uuid.uuid4().hex
    _SESSIONS[sid] = [{"role": "system", "content": SYSTEM_PROMPT}]
    return sid


def exists(session_id: str) -> bool:
    return session_id in _SESSIONS


def messages_for_opening(session_id: str) -> list[dict]:
    """构造用于生成开场的消息（不落库，落库由 commit 完成）。"""
    return _SESSIONS[session_id] + [{"role": "user", "content": OPENING_PROMPT}]


def messages_for_action(session_id: str, action: str) -> list[dict]:
    """构造用于响应玩家行动的消息。"""
    user_turn = {"role": "user", "content": action}
    return _SESSIONS[session_id] + [user_turn]


def commit(session_id: str, user_content: str | None, assistant_content: str) -> None:
    """把一轮对话写入会话历史。

    开场时 user_content 传 None（不把开场指令暴露进可见历史，
    但需要一条占位以保持交替；这里用固定的旁白占位）。
    """
    history = _SESSIONS[session_id]
    if user_content is None:
        history.append({"role": "user", "content": "（开始这一世）"})
    else:
        history.append({"role": "user", "content": user_content})
    history.append({"role": "assistant", "content": assistant_content})
    _trim(session_id)


def _trim(session_id: str) -> None:
    history = _SESSIONS[session_id]
    system, rest = history[0], history[1:]
    # 每轮 2 条消息
    if len(rest) > MAX_TURNS * 2:
        rest = rest[-MAX_TURNS * 2:]
    _SESSIONS[session_id] = [system] + rest
