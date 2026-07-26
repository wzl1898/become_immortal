"""游戏会话状态管理（持久化版）。

内存里缓存活跃存档，SQLite 落盘。每手结束 write-through 写库，
重启后可从库里读档继续。

每个存档维护三份核心数据：
- messages   : 喂给 LLM 的消息（system + user/assistant，按 MAX_TURNS 截断）
- transcript : 展示用剧情块列表 [{role: narration|player, text}]，只增不删
- character_state : 主角当前状态快照，按最新《状态》面板覆盖
- world_memory : 长期世界记忆，独立于短期上下文窗口，按需召回注入
"""

import asyncio
import json
import logging
import re
import time
import uuid

import embed
import store
from llm import complete_chat
from prompts import (
    DIRECTOR_SYSTEM_PROMPT,
    INQUIRY_SYSTEM_PROMPT,
    MEMORY_EXTRACT_SYSTEM_PROMPT,
    OPENING_PROMPT,
    SYSTEM_PROMPT,
)

# save_id -> {"messages": list[dict], "transcript": list[dict], "turns": int, "character_state": dict, "world_memory": list[dict]}
_CACHE: dict[str, dict] = {}
_LOG = logging.getLogger(__name__)

# 保留的历史轮数（system 之外），防止上下文无限增长
MAX_TURNS = 40

# 世界记忆注入后续生成时的规模上限。
WORLD_MEMORY_RECALL_TOP_K = 8
WORLD_MEMORY_RECALL_THRESHOLD = 0.32
WORLD_MEMORY_INJECT_MAX_CHARS = 2200
MEMORY_EXTRACT_MAX_ITEMS = 5

# 喂给问询的"当前情境"取最近多少条 narration 的正文
INQUIRY_SCENE_TURNS = 3

# ---- 物品冷热分离 ----
# 热窗口：物品最后一次在剧情正文里被提及后，往后多少回合内算"热"（每回合注入）。
# 连续 HOT_TURNS 回合正文都没提到 → 变冷、进折叠区、退出注入。
HOT_TURNS = 5
# 冷物品语义召回：命中数上限与相似度阈值（阈值实测微调）。
RECALL_TOP_K = 3
RECALL_THRESHOLD = 0.35

DEFAULT_NAME = "无名修士"

# ---- 导演模块（前瞻性调度：养一个爽点，指路不写戏，上膛不开枪）----
# 连续偏离多少轮就弃掉当前爽点、改跟玩家的路（实测微调）。
DIRECTOR_DRIFT_K = 3
# 爽点退场（兑现或废弃）后留白多少轮再孕育下一个。
DIRECTOR_COOLDOWN_TURNS = 3
# 注入给 GM 的导演块字数上限，防喧宾夺主。
DIRECTOR_INJECT_MAX_CHARS = 500


def init() -> None:
    store.init()


def create_session(name: str = DEFAULT_NAME) -> str:
    """新建一局并落库，返回 save_id。"""
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    sid = store.create(name, messages)
    _CACHE[sid] = {
        "messages": messages,
        "transcript": [],
        "turns": 0,
        "character_state": {},
        "world_memory": [],
        "inventory": [],
        "director_state": {},
        "_injected": [],  # 上一回合注入过（热+召回）的物品归一化名，供 _reconcile 判失去
    }
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
    # 老存档的 assistant 历史里堆着带灵光提示的旧回合；喂给模型前就地剥掉
    # （仅内存，不回写库——库里 messages/transcript 保持原样）。幂等安全。
    for m in data["messages"]:
        if m.get("role") == "assistant":
            m["content"] = _strip_hint(m["content"])
    _CACHE[session_id] = {
        "messages": data["messages"],
        "transcript": data["transcript"],
        "turns": data["turns"],
        "character_state": data.get("character_state", {}),
        "world_memory": data.get("world_memory", []),
        "inventory": data.get("inventory", []),
        "director_state": data.get("director_state", {}) or {},
        "_injected": [],
    }
    return _CACHE[session_id]


def get_transcript(session_id: str) -> list[dict] | None:
    """读档时取展示用的完整剧情。"""
    state = _get(session_id)
    return None if state is None else state["transcript"]


def messages_for_opening(session_id: str) -> list[dict]:
    """构造用于生成开场的消息（不落库，落库由 commit 完成）。

    开场一般无既往世界记忆/物件，但读档到空局再开场时可能已有世界记忆，一并注入。
    """
    state = _get(session_id)
    inject = _injection(state, None)
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


def _strip_hint(text: str) -> str:
    """剥掉末尾的〔灵光一现…〕提示块，保留正文与《状态》《物件》。

    专供喂给 LLM 的历史：灵光提示是给玩家的备选方向，不是已发生的剧情。
    若原样留在 assistant 历史里，模型会把它当作"自己说过的事实"回引——
    尤其玩家给模糊指令时，会就近把没被选中的选项/人物缝进正文当真事（幻觉）。
    只剥末尾那一处〔…〕（系统契约保证提示是最后一行且整段唯一），
    以防误删正文里偶发的全角括号内容。幂等：无提示时原样返回。
    """
    matches = list(_HINT_RE.finditer(text))
    if not matches:
        return text
    last = matches[-1]
    return (text[: last.start()] + text[last.end() :]).rstrip()


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


_STATE_FIELDS = {
    "境界": "realm",
    "气血": "health",
    "灵力": "spiritual_power",
    "修为": "cultivation",
    "状态": "condition",
    "资源": "resources",
    "法宝": "artifacts",
}

_STATE_LABELS = (
    ("realm", "境界"),
    ("health", "气血"),
    ("spiritual_power", "灵力"),
    ("cultivation", "修为"),
    ("condition", "状态"),
    ("resources", "资源"),
    ("artifacts", "法宝"),
)


def _parse_character_state(status_text: str | None, turn: int) -> dict:
    """从《状态》面板解析主角当前状态快照。解析失败返回空 dict。"""
    if not status_text:
        return {}
    character_state: dict = {}
    for line in status_text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^(.+?)[：:]\s*(.*)$", line)
        if not m:
            continue
        key = _STATE_FIELDS.get(m.group(1).strip())
        if key:
            character_state[key] = m.group(2).strip()
    if not character_state:
        return {}
    character_state["turn"] = turn
    character_state["updated_at"] = time.time()
    return character_state


def _character_state_dossier(character_state: dict) -> str:
    """把最新版主角状态拼成独立约束串。空则返回 ""。"""
    if not character_state:
        return ""
    lines = []
    for key, label in _STATE_LABELS:
        val = (character_state.get(key) or "").strip()
        if val:
            lines.append(f"{label}：{val}")
    if not lines:
        return ""
    body = "\n".join(lines)
    return (
        "【当前主角状态（最新版，以此为准）】\n"
        f"{body}\n"
        "【/当前主角状态】\n\n"
    )


# ---- 物品影子库：解析、冷热划分、召回、注入 ----

# 名称归一化：去掉括号（属性）、去掉 ——归属 后缀、去掉 ×N 数量、去空白，取主名。
_QTY_RE = re.compile(r"[×x]\s*\d+\s*$")


def _norm_name(raw: str) -> str:
    """把带属性/归属/数量的物件描述归一到"主名"，用于跨回合匹配与正文子串命中。"""
    s = raw.strip()
    # 先切掉 ——归属（《物件》行是 名称（属性）——归属）
    s = re.split(r"——|—{1,2}|--", s, maxsplit=1)[0]
    # 去掉第一个括号及其后所有内容（属性）
    for op in ("（", "("):
        idx = s.find(op)
        if idx != -1:
            s = s[:idx]
    s = _QTY_RE.sub("", s)
    return s.strip()


def _parse_panel_items(status: str | None, objects: str | None) -> list[dict]:
    """从本回合面板（《状态》资源/法宝 + 《物件》块）解析出带属性的物件。

    返回 [{name, attrs, kind, whereabouts}]：
    - 资源行里带括号的条目 → kind="资源"
    - 法宝行非空 → kind="法宝"，支持多个法宝用顿号/逗号分隔
    - 《物件》整行 → kind="物件"（attrs 取括号内容，name 取主名）
    寻常消耗品（无括号资源）不入库。
    """
    parsed: list[dict] = []

    def _attrs_of(text: str) -> str:
        for op, cl in (("（", "）"), ("(", ")")):
            i = text.find(op)
            if i != -1:
                j = text.find(cl, i)
                return text[i + 1: j if j != -1 else len(text)].strip()
        return ""

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
                for part in _split_top_level(val):
                    part = part.strip()
                    if part and ("（" in part or "(" in part):
                        parsed.append({
                            "name": _norm_name(part),
                            "attrs": _attrs_of(part),
                            "kind": "资源",
                            "whereabouts": "随身",
                        })
            else:
                for part in _split_top_level(val):
                    part = part.strip()
                    if part and part not in ("无", "暂无", "无。"):
                        parsed.append({
                            "name": _norm_name(part),
                            "attrs": _attrs_of(part),
                            "kind": "法宝",
                            "whereabouts": "随身",
                        })

    if objects:
        for line in objects.splitlines():
            line = line.strip()
            if line:
                chunks = re.split(r"——|—{1,2}|--", line, maxsplit=1)
                whereabouts = chunks[1].strip() if len(chunks) > 1 else ""
                parsed.append({
                    "name": _norm_name(line),
                    "attrs": _attrs_of(line),
                    "kind": "物件",
                    "whereabouts": whereabouts,
                })

    return parsed


def _reconcile_inventory(state: dict, narration: str) -> None:
    """把本回合面板解析回影子库：新物入库、已知物更新 kind/attrs、失去物移除。

    - last_turn 只由"正文子串命中"刷新（见 _touch_by_narration），此处新物落库时
      记一次 last_turn=当前turn（首次获得也算"用到"）。注入 ≠ 用到，故不在别处刷。
    - 失去判定：只针对"上回合注入过（热+召回）却在本回合面板消失"的物品，移除之；
      纯冷物品（本就不在场景/prompt）不受影响，绝不误删。
    """
    inv = state["inventory"]
    turn = state["turns"]  # commit 里已 +1，此处即本回合序号
    status = _STATUS_RE.search(narration)
    objects = _OBJECTS_RE.search(narration)
    parsed = _parse_panel_items(
        status.group(1) if status else None,
        objects.group(1) if objects else None,
    )

    by_name = {it["name"]: it for it in inv}
    present = set()
    for p in parsed:
        name = p["name"]
        if not name:
            continue
        present.add(name)
        cur = by_name.get(name)
        if cur is None:
            item = {
                "id": uuid.uuid4().hex,
                "name": name,
                "attrs": p["attrs"],
                "kind": p["kind"],
                "whereabouts": p.get("whereabouts", ""),
                "last_turn": turn,
            }
            inv.append(item)
            by_name[name] = item
        else:
            # 已知物：补全空属性、更新 kind（拥有关系可能迁移：物件→资源/法宝）
            if p["attrs"] and not cur.get("attrs"):
                cur["attrs"] = p["attrs"]
            if p["kind"]:
                cur["kind"] = p["kind"]
            if "whereabouts" in p:
                cur["whereabouts"] = p.get("whereabouts", "")

    # 失去：上回合注入过、本回合面板里没有了 → LLM 已从面板移除 → 移除出库
    injected = set(state.get("_injected") or [])
    if injected:
        lost = injected - present
        if lost:
            state["inventory"] = [it for it in inv if it["name"] not in lost]

    # 正文子串命中刷新 last_turn（含刚入库的新物再确认一次）
    _touch_by_narration(state, narration)


def _touch_by_narration(state: dict, narration: str) -> None:
    """保温信号：物品的归一化名若在本回合"正文"里被子串命中，则 last_turn=当前turn。

    只看剧情正文（不含面板/提示），因为"剧情在用它"才算热；被注入进 prompt 不算。
    """
    body = _narration_body(narration)
    if not body:
        return
    turn = state["turns"]
    for it in state["inventory"]:
        name = it.get("name") or ""
        if name and name in body:
            it["last_turn"] = turn


def _hot_cold(inv: list[dict], turn: int) -> tuple[list[dict], list[dict]]:
    """按最近相关回合划分热/冷：turn - last_turn < HOT_TURNS 为热，其余为冷。"""
    hot, cold = [], []
    for it in inv:
        if turn - int(it.get("last_turn", 0)) < HOT_TURNS:
            hot.append(it)
        else:
            cold.append(it)
    return hot, cold


def _cand_text(it: dict) -> str:
    """召回用的候选文本：名 + 属性 + 下落。"""
    attrs = it.get("attrs") or ""
    whereabouts = it.get("whereabouts") or ""
    return f"{it.get('name', '')}　{attrs}　{whereabouts}".strip()


def _recall_cold(state: dict, cold: list[dict], action: str) -> list[dict]:
    """冷变热：对玩家输入做语义召回，命中的冷物品 last_turn 刷新为当前turn（升回热）。

    embedding 不可用时 embed.recall 返回 []，等价于"不召回"。
    """
    if not cold or not action:
        return []
    idxs = embed.recall(action, [_cand_text(it) for it in cold],
                        top_k=RECALL_TOP_K, threshold=RECALL_THRESHOLD)
    recalled = [cold[i] for i in idxs]
    turn = state["turns"]
    for it in recalled:
        it["last_turn"] = turn
    return recalled


def _item_line(it: dict) -> str:
    """把一件物品拼成档案行：名称（属性） 类别/下落。"""
    name = it.get("name", "")
    attrs = it.get("attrs") or ""
    kind = it.get("kind") or ""
    whereabouts = it.get("whereabouts") or ""
    head = f"{name}（{attrs}）" if attrs else name
    meta = []
    if kind:
        meta.append(f"类别：{kind}")
    if whereabouts:
        meta.append(f"下落：{whereabouts}")
    return f"- {head}　{'　'.join(meta)}" if meta else f"- {head}"


def _inventory_dossier(active: list[dict]) -> str:
    """把当前相关物品（热+召回）拼成"当前物品档案"约束串。空则 ""。"""
    if not active:
        return ""
    lines = "\n".join(_item_line(it) for it in active)
    return (
        "【当前物品档案（仅以下为主角当前相关的持有/关注之物，须与之属性一致；"
        "勿凭空补列未在此的旧物）】\n"
        f"{lines}\n"
        "以上物件的既定属性除非剧情明确改变，否则须原样沿用。\n\n"
    )


def _memory_text(mem: dict) -> str:
    """召回/注入用的记忆文本。"""
    if mem.get("type") == "qa":
        q = (mem.get("q") or "").strip()
        a = (mem.get("a") or "").strip()
        return f"问：{q}　答：{a}" if q else a
    return (mem.get("text") or "").strip()


def _memory_candidate(mem: dict) -> str:
    entities = " ".join(str(e) for e in mem.get("entities") or [])
    return f"{mem.get('type', '')} {entities} {_memory_text(mem)}".strip()


def _num(val, default: float = 0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _fallback_memories(world_memory: list[dict]) -> list[dict]:
    """embedding 不可用时退回最近且较重要的记忆，保证长期事实不完全断供。"""
    items = sorted(
        world_memory,
        key=lambda m: (_num(m.get("importance")), _num(m.get("ts"))),
        reverse=True,
    )
    return items[:WORLD_MEMORY_RECALL_TOP_K]


def _recall_world_memory(state: dict, query: str) -> list[dict]:
    """按玩家输入与最近场景召回世界记忆。"""
    world_memory = state.get("world_memory") or []
    if not world_memory:
        return []
    candidates = [_memory_candidate(m) for m in world_memory]
    idxs = embed.recall(
        query,
        candidates,
        top_k=WORLD_MEMORY_RECALL_TOP_K,
        threshold=WORLD_MEMORY_RECALL_THRESHOLD,
    )
    if not idxs:
        return _fallback_memories(world_memory)
    return [world_memory[i] for i in idxs]


def _world_memory_dossier(items: list[dict]) -> str:
    """把召回到的长期记忆拼成约束串，注入后续生成。空则返回 ""。"""
    if not items:
        return ""
    lines: list[str] = []
    used = 0
    for entry in items:
        text = _memory_text(entry)
        if not text:
            continue
        kind = entry.get("type") or "plot"
        line = f"- [{kind}] {text}"
        if used + len(line) > WORLD_MEMORY_INJECT_MAX_CHARS and lines:
            break
        lines.append(line)
        used += len(line)
    if not lines:
        return ""
    body = "\n".join(lines)
    return (
        "【世界记忆（长期事实，来自过往剧情与问询；须与之一致）】\n"
        f"{body}\n"
        "【/世界记忆】\n\n"
    )


def _injection(state: dict, action: str | None) -> str:
    """行动/开场前注入的约束串：当前主角状态 + 当前物品档案（热+召回）+ 世界记忆。

    冷热按 turn - last_turn < HOT_TURNS 划分。有玩家输入时对冷物品做语义召回，
    命中者升回热并并入注入。把本次实际注入的物品名记入 state['_injected']，
    供下回合 _reconcile 判定"注入过却从面板消失=失去"。
    """
    inv = state["inventory"]
    turn = state["turns"]
    hot, cold = _hot_cold(inv, turn)
    recalled = _recall_cold(state, cold, action or "")
    active = hot + recalled
    state["_injected"] = [it["name"] for it in active]
    query = "\n\n".join(p for p in (action or "", _recent_scene(state["transcript"])) if p)
    memories = _recall_world_memory(state, query)
    return (
        _character_state_dossier(state.get("character_state") or {})
        + _inventory_dossier(active)
        + _world_memory_dossier(memories)
        + _director_injection(state.get("director_state") or {})
    )


def messages_for_action(session_id: str, action: str) -> list[dict]:
    """构造用于响应玩家行动的消息。

    在玩家行动前注入"当前主角状态"、"当前物品档案"（热物品 + 按本次输入语义召回的冷物品）
    与"世界记忆"，让状态、物件属性与已问明的背景即使在上下文被 MAX_TURNS 截断后
    也不丢失。冷物品不进 prompt，故 LLM 不会再把无关旧物抄进面板，物品栏膨胀自止。
    注入串只随本次请求发送，不写入历史（落库仍是玩家原始行动）。
    """
    state = _get(session_id)
    inject = _injection(state, action)
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
    """构造用于"世界记忆"问询的独立消息数组（不复用 GM 历史）。

    只给问询引擎：当前情境（最近情节正文）+ 相关世界记忆 + 玩家的问题，
    让它基于"主角此刻理应知道的见识"作答，且与已发生剧情、长期记忆一致。
    """
    state = _get(session_id)
    scene = _recent_scene(state["transcript"])
    query = "\n\n".join(p for p in (question, scene) if p)
    memory = _world_memory_dossier(_recall_world_memory(state, query))
    parts = []
    if scene:
        parts.append(f"【当前情境（主角所处的最近情节）】\n{scene}")
    if memory:
        parts.append(memory.rstrip())
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
    # 喂给 LLM 的历史剥掉灵光提示（不让备选动作被误当既成剧情回引）；
    # transcript 与下方 _reconcile 仍用完整原文（前端要显示提示、解析要读面板）。
    messages.append({"role": "assistant", "content": _strip_hint(assistant_content)})
    transcript.append({"role": "narration", "text": assistant_content})

    state["turns"] += 1
    _trim(state)
    status_match = _STATUS_RE.search(assistant_content)
    character_state = _parse_character_state(
        status_match.group(1) if status_match else None,
        state["turns"],
    )
    if character_state:
        state["character_state"] = character_state
    # 解析本回合面板回影子库（新物入库、失去物移除、正文命中刷 last_turn）
    _reconcile_inventory(state, assistant_content)
    store.save_state(session_id, state["messages"], state["transcript"], state["turns"])
    if character_state:
        store.save_character_state(session_id, character_state)
    store.save_inventory(session_id, state["inventory"])
    _schedule_memory_extraction(session_id, user_content, assistant_content, state["turns"])
    _schedule_director(session_id, user_content, assistant_content, state["turns"])


def _schedule_memory_extraction(
    session_id: str,
    user_content: str | None,
    assistant_content: str,
    turn: int,
) -> None:
    """后台提取长期记忆；没有运行中的事件循环时跳过，不影响主流程。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(_extract_and_store_memory(session_id, user_content, assistant_content, turn))


async def _extract_and_store_memory(
    session_id: str,
    user_content: str | None,
    assistant_content: str,
    turn: int,
) -> None:
    try:
        raw = await complete_chat(
            [
                {"role": "system", "content": MEMORY_EXTRACT_SYSTEM_PROMPT},
                {"role": "user", "content": _memory_extract_user_prompt(user_content, assistant_content, turn)},
            ],
            temperature=0.2,
            max_tokens=800,
        )
        items = _parse_extracted_memories(raw, turn)
        if not items:
            return
        current = store.append_world_memory(session_id, items)
        if current is not None and session_id in _CACHE:
            _CACHE[session_id]["world_memory"] = current
    except Exception:  # noqa: BLE001
        _LOG.exception("world memory extraction failed for session %s turn %s", session_id, turn)


def _memory_extract_user_prompt(user_content: str | None, assistant_content: str, turn: int) -> str:
    status = _STATUS_RE.search(assistant_content)
    objects = _OBJECTS_RE.search(assistant_content)
    body = _narration_body(assistant_content)
    action = user_content if user_content is not None else "（开始这一世）"
    parts = [
        f"【回合】\n{turn}",
        f"【玩家行动】\n{action}",
        f"【本轮叙事正文】\n{body}",
    ]
    if status:
        parts.append(f"【状态面板】\n{status.group(1).strip()}")
    if objects:
        parts.append(f"【关键物件面板】\n{objects.group(1).strip()}")
    return "\n\n".join(parts)


def _extract_json_array(raw: str) -> list | None:
    text = (raw or "").strip()
    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("["), text.rfind("]")
        if start == -1 or end == -1 or end <= start:
            return None
        try:
            data = json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, list) else None


def _extract_json_object(raw: str) -> dict | None:
    """从模型输出里抠出一个 JSON 对象；抠不出返回 None。仿 _extract_json_array。"""
    text = (raw or "").strip()
    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        try:
            data = json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, dict) else None


def _parse_extracted_memories(raw: str, turn: int) -> list[dict]:
    data = _extract_json_array(raw)
    if not data:
        return []
    allowed = {"plot", "character", "item", "location"}
    items: list[dict] = []
    now = time.time()
    for entry in data[:MEMORY_EXTRACT_MAX_ITEMS]:
        if not isinstance(entry, dict):
            continue
        kind = entry.get("type")
        text = (entry.get("text") or "").strip()
        if kind not in allowed or not text:
            continue
        entities = entry.get("entities") or []
        if not isinstance(entities, list):
            entities = []
        try:
            importance = float(entry.get("importance", 0.5))
        except (TypeError, ValueError):
            importance = 0.5
        importance = max(0.0, min(1.0, importance))
        items.append({
            "id": uuid.uuid4().hex,
            "type": kind,
            "text": text,
            "entities": [str(e).strip() for e in entities if str(e).strip()][:12],
            "turn": turn,
            "importance": importance,
            "source": "extractor",
            "ts": now,
        })
    return items


# ---- 导演模块：注入渲染 / 异步维护 / Python 兜底 ----

def _director_injection(state: dict) -> str:
    """把导演状态渲染成给 GM 的注入块。留白期或无爽点返回 ""（不注入）。

    守住"上膛不开枪"：块内只给背景压力与机会（剧情指导），并声明玩家行动仍决定走向；
    armed 时附上触发条件与兑现方向，供 GM 在玩家跨线的同一轮当场兑现。
    """
    if not state or state.get("phase") == "cooldown":
        return ""
    payoff = state.get("payoff")
    if not isinstance(payoff, dict):
        return ""
    guidance = (payoff.get("guidance") or "").strip()
    if not guidance:
        return ""
    lines = [
        "【导演·剧情走向（仅背景压力与机会，非必演剧本；玩家行动仍决定走向）】",
        f"- 本轮推进方向：{guidance}",
    ]
    if payoff.get("armed"):
        trigger = (payoff.get("trigger") or "").strip()
        desc = (payoff.get("desc") or "").strip()
        if trigger:
            lines.append(f"- 若玩家本轮做到「{trigger}」，即当场顺势兑现：{desc}")
        lines.append("- 玩家若未触及上述条件，则勿强行兑现，只按其真实行动合理推进。")
    body = "\n".join(lines)
    if len(body) > DIRECTOR_INJECT_MAX_CHARS:
        body = body[:DIRECTOR_INJECT_MAX_CHARS].rstrip()
    return f"{body}\n【/导演】\n\n"


def _schedule_director(
    session_id: str,
    user_content: str | None,
    assistant_content: str,
    turn: int,
) -> None:
    """后台跑导演思维链；无运行中的事件循环时跳过，不影响主流程（降级）。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(_run_director(session_id, user_content, assistant_content, turn))


async def _run_director(
    session_id: str,
    user_content: str | None,
    assistant_content: str,
    turn: int,
) -> None:
    """调导演 LLM 更新爽点状态；解析失败/异常则保留旧状态（降级不注入错值）。"""
    state = _CACHE.get(session_id)
    if state is None:
        return
    prev = state.get("director_state") or {}
    try:
        raw = await complete_chat(
            [
                {"role": "system", "content": DIRECTOR_SYSTEM_PROMPT},
                {"role": "user", "content": _director_user_prompt(user_content, assistant_content, turn, prev)},
            ],
            temperature=0.4,
            max_tokens=700,
        )
        result = _extract_json_object(raw)
        if result is None:
            return
        new_state, buried = _apply_director_result(prev, result, turn)
        state["director_state"] = new_state
        store.save_director_state(session_id, new_state)
        if buried:
            current = store.append_world_memory(session_id, [buried])
            if current is not None:
                state["world_memory"] = current
    except Exception:  # noqa: BLE001
        _LOG.exception("director failed for session %s turn %s", session_id, turn)


def _director_user_prompt(user_content: str | None, assistant_content: str, turn: int, prev: dict) -> str:
    body = _narration_body(assistant_content)
    status = _STATUS_RE.search(assistant_content)
    action = user_content if user_content is not None else "（开始这一世）"
    phase = prev.get("phase") or "active"
    parts = [
        f"【回合】\n{turn}",
        f"【当前阶段】\n{phase}（cooldown=留白期，不上膛只顺势观察；active=正常养爽点）",
        f"【玩家行动】\n{action}",
        f"【本轮叙事正文】\n{body}",
    ]
    if status:
        parts.append(f"【状态面板】\n{status.group(1).strip()}")
    parts.append(f"【上一轮导演状态】\n{json.dumps(prev, ensure_ascii=False)}")
    return "\n\n".join(parts)


def _apply_director_result(prev: dict, result: dict, turn: int) -> tuple[dict, dict | None]:
    """把导演 LLM 的判断并进状态，并做 Python 侧兜底夹逼（drift/cooldown/字段校验）。

    返回 (新状态, 可选的暗线世界记忆项)。留白/偏离/停滞主要由 LLM 在提示词约束下更新，
    这里只兜底：连续偏离达 K 强制留白、留白到点转回 active、abandon/fired 一律进留白。
    """
    prev = prev if isinstance(prev, dict) else {}
    phase = prev.get("phase") or "active"
    drift_turns = int(prev.get("drift_turns") or 0)
    buried: dict | None = None

    # 留白期：到点转回 active，其余保持无爽点
    if phase == "cooldown":
        cooldown_until = int(prev.get("cooldown_until") or 0)
        new_state = {
            "payoff": None,
            "phase": "cooldown" if turn < cooldown_until else "active",
            "cooldown_until": cooldown_until,
            "drift_turns": 0,
            "last_fired": prev.get("last_fired"),
            "note": (result.get("note") or "").strip(),
        }
        return new_state, None

    payoff_in = result.get("payoff") if isinstance(result.get("payoff"), dict) else None
    fired = bool(result.get("fired"))
    abandon = bool(result.get("abandon"))
    drift = bool(result.get("drift"))

    # 连续偏离累计；达阈值强制废弃
    drift_turns = drift_turns + 1 if drift else 0
    if drift_turns >= DIRECTOR_DRIFT_K:
        abandon = True

    # 退场（兑现或废弃）→ 埋暗线（仅废弃时）+ 进入留白
    if fired or abandon:
        outcome = "fired" if fired else "abandoned"
        src = payoff_in if isinstance(payoff_in, dict) else prev.get("payoff")
        desc = (src or {}).get("desc", "") if isinstance(src, dict) else ""
        if abandon and not fired:
            thread = (result.get("buried_thread") or "").strip()
            if thread:
                buried = {
                    "id": uuid.uuid4().hex,
                    "type": "plot",
                    "text": thread,
                    "entities": [],
                    "turn": turn,
                    "importance": 0.4,
                    "source": "director",
                    "ts": time.time(),
                }
        new_state = {
            "payoff": None,
            "phase": "cooldown",
            "cooldown_until": turn + DIRECTOR_COOLDOWN_TURNS,
            "drift_turns": 0,
            "last_fired": {"desc": desc, "outcome": outcome, "turn": turn},
            "note": (result.get("note") or "").strip(),
        }
        return new_state, buried

    # 正常维护当前爽点
    payoff_out = _sanitize_payoff(payoff_in, prev.get("payoff"), turn)
    new_state = {
        "payoff": payoff_out,
        "phase": "active",
        "cooldown_until": int(prev.get("cooldown_until") or 0),
        "drift_turns": drift_turns,
        "last_fired": prev.get("last_fired"),
        "note": (result.get("note") or "").strip(),
    }
    return new_state, None


def _sanitize_payoff(payoff_in: dict | None, prev_payoff, turn: int) -> dict | None:
    """校验/夹逼爽点字段；desc 缺失视为无爽点。"""
    if not isinstance(payoff_in, dict):
        return None
    desc = (payoff_in.get("desc") or "").strip()
    if not desc:
        return None

    def _clip(v):
        try:
            return max(0.0, min(1.0, float(v)))
        except (TypeError, ValueError):
            return 0.0

    start_turn = turn
    if isinstance(prev_payoff, dict) and prev_payoff.get("desc") == desc:
        start_turn = int(prev_payoff.get("start_turn") or turn)
    return {
        "desc": desc,
        "trigger": (payoff_in.get("trigger") or "").strip(),
        "guidance": (payoff_in.get("guidance") or "").strip(),
        "armed": bool(payoff_in.get("armed")),
        "maturity": _clip(payoff_in.get("maturity")),
        "proximity": _clip(payoff_in.get("proximity")),
        "start_turn": start_turn,
    }


def _trim(state: dict) -> None:
    messages = state["messages"]
    system, rest = messages[0], messages[1:]
    if len(rest) > MAX_TURNS * 2:
        rest = rest[-MAX_TURNS * 2:]
    state["messages"] = [system] + rest


# ---- 物品影子库（供前端读档/流结束后渲染冷热分组）----

def get_character_state(session_id: str) -> dict | None:
    """返回主角当前状态快照；存档不存在返回 None。"""
    state = _get(session_id)
    return None if state is None else state.get("character_state", {})


def get_inventory(session_id: str) -> list[dict] | None:
    """返回物品库视图，每件带 hot 标记（供前端分组：热=关注区，冷=折叠区）。

    hot = turn - last_turn < HOT_TURNS。存档不存在返回 None。
    """
    state = _get(session_id)
    if state is None:
        return None
    turn = state["turns"]
    view = []
    for it in state["inventory"]:
        view.append({
            "name": it.get("name", ""),
            "attrs": it.get("attrs", ""),
            "kind": it.get("kind", ""),
            "whereabouts": it.get("whereabouts", ""),
            "hot": turn - int(it.get("last_turn", 0)) < HOT_TURNS,
        })
    return view


# ---- 世界记忆（含问询旁路）----

def get_world_memory(session_id: str) -> list[dict] | None:
    state = _get(session_id)
    return None if state is None else state["world_memory"]


def get_director_state(session_id: str) -> dict | None:
    """返回导演状态（供前端调试/展示）；存档不存在返回 None。"""
    state = _get(session_id)
    return None if state is None else (state.get("director_state") or {})


def commit_inquiry_memory(session_id: str, question: str, answer: str) -> None:
    """把一次问答追加进世界记忆并落盘（不碰 messages/transcript）。"""
    state = _get(session_id)
    if state is None:
        return
    item = {
        "id": uuid.uuid4().hex,
        "type": "qa",
        "text": f"问：{question}　答：{answer}",
        "entities": [],
        "turn": state["turns"],
        "importance": 0.7,
        "source": "inquiry",
        "q": question,
        "a": answer,
        "ts": time.time(),
    }
    current = store.append_world_memory(session_id, [item])
    state["world_memory"] = current if current is not None else state["world_memory"] + [item]


def delete_world_memory(session_id: str, index: int) -> list[dict] | None:
    """按下标删除一条世界记忆，落盘并返回新列表；下标越界或存档不存在返回 None。"""
    state = _get(session_id)
    if state is None:
        return None
    world_memory = state["world_memory"]
    if not (0 <= index < len(world_memory)):
        return None
    world_memory.pop(index)
    store.save_world_memory(session_id, world_memory)
    return world_memory


def get_lore(session_id: str) -> list[dict] | None:
    """兼容旧接口命名：返回世界记忆。"""
    return get_world_memory(session_id)


def commit_lore(session_id: str, question: str, answer: str) -> None:
    """兼容旧接口命名：把问询写成 qa 类型世界记忆。"""
    commit_inquiry_memory(session_id, question, answer)


def delete_lore(session_id: str, index: int) -> list[dict] | None:
    """兼容旧接口命名：删除世界记忆。"""
    return delete_world_memory(session_id, index)


# ---- 存档管理（转发到 store）----

def list_saves() -> list[dict]:
    return store.list_saves()


def rename(session_id: str, name: str) -> bool:
    return store.rename(session_id, name)


def delete(session_id: str) -> bool:
    _CACHE.pop(session_id, None)
    return store.delete(session_id)
