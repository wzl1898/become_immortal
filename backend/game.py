"""游戏会话状态管理（持久化版）。

内存里缓存活跃存档，SQLite 落盘。每手结束 write-through 写库，
重启后可从库里读档继续。

每个存档维护两份数据：
- messages   : 喂给 LLM 的消息（system + user/assistant，按 MAX_TURNS 截断）
- transcript : 展示用剧情块列表 [{role: narration|player, text}]，只增不删
"""

import re

import store
from prompts import OPENING_PROMPT, SYSTEM_PROMPT

# save_id -> {"messages": list[dict], "transcript": list[dict], "turns": int}
_CACHE: dict[str, dict] = {}

# 保留的历史轮数（system 之外），防止上下文无限增长
MAX_TURNS = 40

DEFAULT_NAME = "无名修士"


def init() -> None:
    store.init()


def create_session(name: str = DEFAULT_NAME) -> str:
    """新建一局并落库，返回 save_id。"""
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    sid = store.create(name, messages)
    _CACHE[sid] = {"messages": messages, "transcript": [], "turns": 0}
    return sid


def exists(session_id: str) -> bool:
    return session_id in _CACHE or store.exists(session_id)


def _get(session_id: str) -> dict | None:
    """取活跃状态，缓存未命中则从库加载。"""
    if session_id in _CACHE:
        return _CACHE[session_id]
    data = store.load(session_id)
    if data is None:
        return None
    _CACHE[session_id] = {
        "messages": data["messages"],
        "transcript": data["transcript"],
        "turns": data["turns"],
    }
    return _CACHE[session_id]


def get_transcript(session_id: str) -> list[dict] | None:
    """读档时取展示用的完整剧情。"""
    state = _get(session_id)
    return None if state is None else state["transcript"]


def messages_for_opening(session_id: str) -> list[dict]:
    """构造用于生成开场的消息（不落库，落库由 commit 完成）。"""
    state = _get(session_id)
    return state["messages"] + [{"role": "user", "content": OPENING_PROMPT}]


_STATUS_RE = re.compile(r"《状态》(.*?)《/状态》", re.S)
_OBJECTS_RE = re.compile(r"《物件》(.*?)《/物件》", re.S)


def _latest_block(transcript: list[dict], pattern: "re.Pattern") -> str | None:
    """从最近一条 narration 里按 pattern 提取块内容；没有则 None。"""
    for blk in reversed(transcript):
        if blk.get("role") == "narration":
            m = pattern.search(blk.get("text", ""))
            if m:
                return m.group(1)
    return None


def _split_top_level(val: str) -> list[str]:
    """按顿号/逗号拆分，但忽略成对括号（全角/半角）内部的分隔符。"""
    parts, buf, depth = [], [], 0
    for ch in val:
        if ch in "（(":
            depth += 1
            buf.append(ch)
        elif ch in "）)":
            depth = max(0, depth - 1)
            buf.append(ch)
        elif ch in "、,，" and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf))
    return parts


def _dossier_from_transcript(transcript: list[dict]) -> str:
    """从完整 transcript（不受 MAX_TURNS 截断）里，取最近一次的关键物件，
    拼成"既定物件档案"约束串，抑制跨回合属性漂移。

    来源两处：
    - 《状态》面板的"资源""法宝"——主角**拥有**的关键物（只取带括号的条目/非空法宝）；
    - 《物件》块——主角**未拥有**但有剧情分量的关键物（整行留存，含归属）。

    寻常消耗品（无括号）与空块天然排除。都没有则返回 ""。
    """
    items: list[str] = []

    status = _latest_block(transcript, _STATUS_RE)
    if status:
        for line in status.splitlines():
            line = line.strip()
            m = re.match(r"^(资源|法宝)[：:]\s*(.*)$", line)
            if not m:
                continue
            field, val = m.group(1), m.group(2).strip()
            if not val or val in ("无", "暂无", "无。"):
                continue
            if field == "资源":
                # 只取带括号（固有属性）的条目。按顿号/逗号拆分，但括号内的
                # 分隔符不算（否则会把"（触之清凉，内藏硬物）"从中切断）。
                for part in _split_top_level(val):
                    part = part.strip()
                    if part and ("（" in part or "(" in part):
                        items.append(f"{part}（随身）")
            else:  # 法宝：整行留存
                items.append(f"法宝：{val}（随身）")

    objects = _latest_block(transcript, _OBJECTS_RE)
    if objects:
        for line in objects.splitlines():
            line = line.strip()
            if line:
                items.append(line)

    if not items:
        return ""
    lines = "\n".join(f"- {it}" for it in items)
    return (
        "【既定物件档案（须与之保持一致，括号内属性不可无故矛盾）】\n"
        f"{lines}\n"
        "以上物件的既定属性除非剧情明确改变，否则须原样沿用。\n\n"
    )


def messages_for_action(session_id: str, action: str) -> list[dict]:
    """构造用于响应玩家行动的消息。

    在玩家行动前注入"既定物件档案"，让关键物件的属性即使在上下文
    被 MAX_TURNS 截断后也不丢失，抑制跨回合属性漂移。档案只随本次
    请求发送，不写入历史（落库仍是玩家原始行动）。
    """
    state = _get(session_id)
    dossier = _dossier_from_transcript(state["transcript"])
    content = f"{dossier}{action}" if dossier else action
    return state["messages"] + [{"role": "user", "content": content}]


def commit(session_id: str, user_content: str | None, assistant_content: str) -> None:
    """把一轮对话写入会话历史 + transcript，并落盘。

    开场时 user_content 传 None：LLM 历史里放占位以保持交替，
    transcript 里则只记开场旁白（不显示占位）。
    """
    state = _get(session_id)
    messages = state["messages"]
    transcript = state["transcript"]

    if user_content is None:
        messages.append({"role": "user", "content": "（开始这一世）"})
    else:
        messages.append({"role": "user", "content": user_content})
        transcript.append({"role": "player", "text": user_content})
    messages.append({"role": "assistant", "content": assistant_content})
    transcript.append({"role": "narration", "text": assistant_content})

    state["turns"] += 1
    _trim(state)
    store.save_state(session_id, state["messages"], state["transcript"], state["turns"])


def _trim(state: dict) -> None:
    messages = state["messages"]
    system, rest = messages[0], messages[1:]
    if len(rest) > MAX_TURNS * 2:
        rest = rest[-MAX_TURNS * 2:]
    state["messages"] = [system] + rest


# ---- 存档管理（转发到 store）----

def list_saves() -> list[dict]:
    return store.list_saves()


def rename(session_id: str, name: str) -> bool:
    return store.rename(session_id, name)


def delete(session_id: str) -> bool:
    _CACHE.pop(session_id, None)
    return store.delete(session_id)
