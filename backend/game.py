"""游戏会话状态管理（持久化版）。

内存里缓存活跃存档，SQLite 落盘。每手结束 write-through 写库，
重启后可从库里读档继续。

每个存档维护两份数据：
- messages   : 喂给 LLM 的消息（system + user/assistant，按 MAX_TURNS 截断）
- transcript : 展示用剧情块列表 [{role: narration|player, text}]，只增不删
"""

import re
import time
import uuid

import embed
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

# ---- 物品冷热分离 ----
# 热窗口：物品最后一次在剧情正文里被提及后，往后多少回合内算"热"（每回合注入）。
# 连续 HOT_TURNS 回合正文都没提到 → 变冷、进折叠区、退出注入。
HOT_TURNS = 5
# 冷物品语义召回：命中数上限与相似度阈值（阈值实测微调）。
RECALL_TOP_K = 3
RECALL_THRESHOLD = 0.35

DEFAULT_NAME = "无名修士"


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
        "lore": [],
        "inventory": [],
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
    _CACHE[session_id] = {
        "messages": data["messages"],
        "transcript": data["transcript"],
        "turns": data["turns"],
        "lore": data.get("lore", []),
        "inventory": data.get("inventory", []),
        "_injected": [],
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

    返回 [{name, attrs, kind}]：
    - 资源行里带括号的条目 → kind="资源"
    - 法宝行非空 → kind="法宝"
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
                        parsed.append({"name": _norm_name(part), "attrs": _attrs_of(part), "kind": "资源"})
            else:  # 法宝：整行留存
                parsed.append({"name": _norm_name(val), "attrs": _attrs_of(val), "kind": "法宝"})

    if objects:
        for line in objects.splitlines():
            line = line.strip()
            if line:
                parsed.append({"name": _norm_name(line), "attrs": _attrs_of(line), "kind": "物件"})

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
    """召回用的候选文本：名 + 属性。"""
    attrs = it.get("attrs") or ""
    return f"{it.get('name', '')}　{attrs}".strip()


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
    """把一件物品拼成档案行：名称（属性）〔类别〕。"""
    name = it.get("name", "")
    attrs = it.get("attrs") or ""
    kind = it.get("kind") or ""
    head = f"{name}（{attrs}）" if attrs else name
    return f"- {head}　类别：{kind}" if kind else f"- {head}"


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


def _injection(state: dict, action: str | None) -> str:
    """行动/开场前注入的约束串：当前物品档案（热+召回）+ 既定见闻。

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
    return _inventory_dossier(active) + _lore_dossier(state["lore"])


def messages_for_action(session_id: str, action: str) -> list[dict]:
    """构造用于响应玩家行动的消息。

    在玩家行动前注入"当前物品档案"（热物品 + 按本次输入语义召回的冷物品）与
    "既定见闻"，让相关物件属性与已问明的背景即使在上下文被 MAX_TURNS 截断后也
    不丢失。冷物品不进 prompt，故 LLM 不会再把无关旧物抄进面板，物品栏膨胀自止。
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
    # 解析本回合面板回影子库（新物入库、失去物移除、正文命中刷 last_turn）
    _reconcile_inventory(state, assistant_content)
    store.save_state(session_id, state["messages"], state["transcript"], state["turns"])
    store.save_inventory(session_id, state["inventory"])


def _trim(state: dict) -> None:
    messages = state["messages"]
    system, rest = messages[0], messages[1:]
    if len(rest) > MAX_TURNS * 2:
        rest = rest[-MAX_TURNS * 2:]
    state["messages"] = [system] + rest


# ---- 物品影子库（供前端读档/流结束后渲染冷热分组）----

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
            "hot": turn - int(it.get("last_turn", 0)) < HOT_TURNS,
        })
    return view


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
