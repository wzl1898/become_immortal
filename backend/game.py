"""游戏会话状态管理（持久化版）。

内存里缓存活跃存档，SQLite 落盘。每手结束 write-through 写库，
重启后可从库里读档继续。

每个存档维护两份数据：
- messages   : 喂给 LLM 的消息（system + user/assistant，按 MAX_TURNS 截断）
- transcript : 展示用剧情块列表 [{role: narration|player, text}]，只增不删
"""

import re
import time

import store
from prompts import INQUIRY_SYSTEM_PROMPT, OPENING_PROMPT, SYSTEM_PROMPT

# save_id -> {"messages": list[dict], "transcript": list[dict], "turns": int, "lore": list[dict]}
_CACHE: dict[str, dict] = {}

# 保留的历史轮数（system 之外），防止上下文无限增长
MAX_TURNS = 40

# 见闻录注入后续生成时的规模上限：只取最近 N 条，且总长截断到约 M 字
LORE_INJECT_MAX_ITEMS = 12
LORE_INJECT_MAX_CHARS = 1500

# 喂给问询的"当前情境"取最近多少条 narration 的正文
INQUIRY_SCENE_TURNS = 3

DEFAULT_NAME = "无名修士"


def init() -> None:
    store.init()


def create_session(name: str = DEFAULT_NAME) -> str:
    """新建一局并落库，返回 save_id。"""
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    sid = store.create(name, messages)
    _CACHE[sid] = {"messages": messages, "transcript": [], "turns": 0, "lore": []}
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
        "lore": data.get("lore", []),
    }
    return _CACHE[session_id]


def get_transcript(session_id: str) -> list[dict] | None:
    """读档时取展示用的完整剧情。"""
    state = _get(session_id)
    return None if state is None else state["transcript"]


def messages_for_opening(session_id: str) -> list[dict]:
    """构造用于生成开场的消息（不落库，落库由 commit 完成）。

    开场一般无既往见闻/物件，但读档到空局再开场时可能已有见闻，一并注入。
    """
    state = _get(session_id)
    inject = _injection(state)
    content = f"{inject}{OPENING_PROMPT}" if inject else OPENING_PROMPT
    return state["messages"] + [{"role": "user", "content": content}]


_STATUS_RE = re.compile(r"《状态》(.*?)《/状态》", re.S)
_OBJECTS_RE = re.compile(r"《物件》(.*?)《/物件》", re.S)
_HINT_RE = re.compile(r"〔.*?〕", re.S)


def _narration_body(text: str) -> str:
    """剥掉《状态》《物件》块与〔灵光提示〕，只留纯叙事正文。"""
    text = _STATUS_RE.sub("", text)
    text = _OBJECTS_RE.sub("", text)
    text = _HINT_RE.sub("", text)
    return text.strip()


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


def _lore_dossier(lore: list[dict]) -> str:
    """把见闻录拼成"既定见闻"约束串，注入后续生成，防止情节与已答背景矛盾。

    见闻是玩家问出、并已锚定为设定的背景。取最近若干条，总长截断防膨胀。
    空则返回 ""。
    """
    if not lore:
        return ""
    items = lore[-LORE_INJECT_MAX_ITEMS:]
    lines: list[str] = []
    used = 0
    # 从最近往前取，直到字数预算用尽，再恢复时间顺序
    for entry in reversed(items):
        q = (entry.get("q") or "").strip()
        a = (entry.get("a") or "").strip()
        if not a:
            continue
        line = f"- 问：{q}　答：{a}"
        if used + len(line) > LORE_INJECT_MAX_CHARS and lines:
            break
        lines.append(line)
        used += len(line)
    if not lines:
        return ""
    lines.reverse()
    body = "\n".join(lines)
    return (
        "【既定见闻（主角已问明的背景，须与之一致，不可矛盾）】\n"
        f"{body}\n"
        "以上背景一经答明即为设定，后续剧情须与之吻合。\n\n"
    )


def _injection(state: dict) -> str:
    """行动/开场前注入的约束串：既定物件档案 + 既定见闻。"""
    return _dossier_from_transcript(state["transcript"]) + _lore_dossier(state["lore"])


def messages_for_action(session_id: str, action: str) -> list[dict]:
    """构造用于响应玩家行动的消息。

    在玩家行动前注入"既定物件档案"与"既定见闻"，让关键物件属性与已问明的
    背景即使在上下文被 MAX_TURNS 截断后也不丢失，抑制跨回合漂移与矛盾。注入串
    只随本次请求发送，不写入历史（落库仍是玩家原始行动）。
    """
    state = _get(session_id)
    inject = _injection(state)
    content = f"{inject}{action}" if inject else action
    return state["messages"] + [{"role": "user", "content": content}]


def _recent_scene(transcript: list[dict]) -> str:
    """取最近若干条 narration 的纯正文，作为问询的"当前情境"。"""
    bodies: list[str] = []
    for blk in reversed(transcript):
        if blk.get("role") == "narration":
            body = _narration_body(blk.get("text", ""))
            if body:
                bodies.append(body)
            if len(bodies) >= INQUIRY_SCENE_TURNS:
                break
    bodies.reverse()
    return "\n\n".join(bodies)


def messages_for_inquiry(session_id: str, question: str) -> list[dict]:
    """构造用于"见闻"问询的独立消息数组（不复用 GM 历史）。

    只给问询引擎：当前情境（最近情节正文）+ 既往见闻 + 玩家的问题，
    让它基于"主角此刻理应知道的见识"作答，且与已发生剧情、既往见闻一致。
    """
    state = _get(session_id)
    scene = _recent_scene(state["transcript"])
    lore = _lore_dossier(state["lore"])
    parts = []
    if scene:
        parts.append(f"【当前情境（主角所处的最近情节）】\n{scene}")
    if lore:
        parts.append(lore.rstrip())
    parts.append(f"【主角想打听的】\n{question}")
    user_content = "\n\n".join(parts)
    return [
        {"role": "system", "content": INQUIRY_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


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


# ---- 见闻录（问询旁路）----

def get_lore(session_id: str) -> list[dict] | None:
    state = _get(session_id)
    return None if state is None else state["lore"]


def commit_lore(session_id: str, question: str, answer: str) -> None:
    """把一次问答追加进见闻录并落盘（不碰 messages/transcript）。"""
    state = _get(session_id)
    if state is None:
        return
    state["lore"].append({"q": question, "a": answer, "ts": time.time()})
    store.save_lore(session_id, state["lore"])


def delete_lore(session_id: str, index: int) -> list[dict] | None:
    """按下标删除一条见闻，落盘并返回新列表；下标越界或存档不存在返回 None。"""
    state = _get(session_id)
    if state is None:
        return None
    lore = state["lore"]
    if not (0 <= index < len(lore)):
        return None
    lore.pop(index)
    store.save_lore(session_id, lore)
    return lore


# ---- 存档管理（转发到 store）----

def list_saves() -> list[dict]:
    return store.list_saves()


def rename(session_id: str, name: str) -> bool:
    return store.rename(session_id, name)


def delete(session_id: str) -> bool:
    _CACHE.pop(session_id, None)
    return store.delete(session_id)
