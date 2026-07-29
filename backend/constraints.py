"""世界约束层：在叙事生成前裁剪事实与剧情边界。

它不是负责写正文的 GM，而是前置裁判：
- 固定世界事实来自 SQLite 的 world_* 表。
- 主角视野来自 save_player_knowledge 与 save_player_location。
- 输出一段本回合可用/不可用的剧情指导，供 GM 严格遵守。
"""

from __future__ import annotations

from collections import defaultdict

import store


MOVE_WORDS = ("去", "前往", "赶往", "回", "返回", "离开", "沿", "往", "进", "进入", "赶路", "找路")
EXPLORE_WORDS = ("搜", "搜索", "探索", "查探", "查看", "观察", "寻找", "翻找", "调查", "摸索")
SOCIAL_WORDS = ("问", "打听", "拜访", "交谈", "交易", "买", "卖", "求见", "拜师")
CULTIVATE_WORDS = ("修炼", "闭关", "突破", "吐纳", "疗伤", "研读", "参悟")
ITEM_WORDS = ("使用", "服用", "拿", "收起", "丢", "买", "卖", "炼制")

FACTION_HOME_LOCATIONS = {
    "xuanxiao_sect": "xuanxiao_outer_gate",
    "wanbao_tower": "wanbao_tower",
    "tianheng_sect": "tianheng_sect",
    "taixuan_academy": "taixuan_academy",
}


def opening_constraints(session_id: str) -> str:
    """开场约束：把新一世锚在固定开局，而不是让 GM 随机起点。"""
    snap = store.world_snapshot(session_id)
    if not snap:
        return ""
    return _render_constraint_block(
        title="世界约束 Agent（开场）",
        lines=[
            "本局世界事实固定在 SQLite，地点、势力、功法、机缘、秘境不得临时生成。",
            "开场必须锚定在玄苍大陆 / 青梧郡 / 白石村，主角是凡人村镇开局。",
            "主角开局只知道白石村、白石村后山、村外破庙、青溪镇；只听闻黑风山、青木集、玄霄宗。",
            "不要展示可前往地点列表；只能在叙事中自然露出道路、传闻和环境线索。",
            "不要给固定主线；只给当前处境和可被玩家自由回应的契机。",
            _location_line(snap),
            _knowledge_line(snap, "location", "confirmed", "已确认地点", limit=8),
            _knowledge_line(snap, "location", "rumored", "听闻地点", limit=8),
        ],
    )


def action_constraints(session_id: str, action: str) -> str:
    """根据用户输入与世界知识生成本回合叙事边界，并做轻量状态更新。"""
    snap = store.world_snapshot(session_id)
    if not snap:
        return ""
    action = (action or "").strip()
    matches = _match_entities(snap, action)
    action_type = _infer_action_type(action)
    verdict = _adjudicate(session_id, snap, action, action_type, matches)
    local = _local_context(snap, matches)
    return _render_constraint_block(
        title="世界约束 Agent（本回合剧情规格书）",
        lines=[
            "你是剧情生成 Agent。必须服从本规格书；它高于自由发挥。",
            "固定规则：地点、功法、机缘、秘境、势力只能来自 SQLite 固定库；玩家未知内容不得直接暴露。",
            "显示规则：不要列出“可前往地点”菜单；可在叙事中自然提到道路、传闻、人物反应或线索。",
            _location_line(snap),
            _intent_line(action_type, verdict),
            _entity_line(matches),
            _knowledge_line(snap, "location", "confirmed", "已确认地点", limit=10),
            _knowledge_line(snap, "location", "rumored", "听闻地点", limit=10),
            _knowledge_line(snap, "route", "confirmed", "已确认路线", limit=10),
            _knowledge_line(snap, "art", "rumored", "已听闻功法", limit=8),
            _knowledge_line(snap, "opportunity", "rumored", "已知机缘线索", limit=8),
            _local_line(local),
            f"动作裁判：{verdict['verdict']}。{verdict['reason']}",
            f"本回合核心结果边界：{verdict['core_result']}",
            f"允许揭示：{_join_or_none(verdict['allowed_reveals'])}",
            f"禁止揭示：{_join_or_none(verdict['forbidden_reveals'])}",
            "若玩家说出库中不存在的核心地点/功法/机缘/势力，不得补造；只能写成主角无从确认、无人听过或需要另行打听。",
        ],
    )


def get_world_state(session_id: str) -> dict | None:
    """供 API/调试读取的玩家世界视野。"""
    snap = store.world_snapshot(session_id)
    if not snap:
        return None
    knowledge = _knowledge_map(snap)
    names = _names_by_table(snap)
    intended_id = snap["location"]["intended_destination_id"]
    return {
        "location": {
            "region_id": snap["location"]["region_id"],
            "region_name": snap["location"]["region_name"],
            "location_id": snap["location"]["location_id"],
            "location_name": snap["location"]["location_name"],
            "site_name": snap["location"]["site_name"],
            "location_state": snap["location"]["location_state"],
            "intended_destination_id": intended_id,
            "intended_destination_name": names.get(intended_id, "") if intended_id else "",
            "lost_risk": snap["location"]["lost_risk"],
        },
        "knowledge": {
            "confirmed_locations": _names_for_knowledge(snap, knowledge, "location", "confirmed"),
            "rumored_locations": _names_for_knowledge(snap, knowledge, "location", "rumored"),
            "confirmed_routes": _names_for_knowledge(snap, knowledge, "route", "confirmed"),
            "known_factions": _names_for_knowledge(snap, knowledge, "faction", None),
            "known_arts": _names_for_knowledge(snap, knowledge, "art", None),
            "known_opportunities": _names_for_knowledge(snap, knowledge, "opportunity", None),
        },
    }


def _render_constraint_block(title: str, lines: list[str]) -> str:
    body = "\n".join(line for line in lines if line)
    return f"【{title}】\n{body}\n【/世界约束 Agent】\n\n"


def _infer_action_type(action: str) -> str:
    if any(w in action for w in CULTIVATE_WORDS):
        return "修炼"
    if any(w in action for w in MOVE_WORDS):
        return "移动"
    if any(w in action for w in EXPLORE_WORDS):
        return "探索"
    if any(w in action for w in SOCIAL_WORDS):
        return "社交/交易"
    if any(w in action for w in ITEM_WORDS):
        return "物品"
    return "一般行动"


def _match_entities(snap: dict, action: str) -> dict[str, list[dict]]:
    buckets: dict[str, list[dict]] = defaultdict(list)
    specs = [
        ("locations", "location"),
        ("factions", "faction"),
        ("arts", "art"),
        ("opportunities", "opportunity"),
        ("realms", "realm"),
    ]
    for source, kind in specs:
        for row in snap[source]:
            name = row.get("name") or ""
            if name and name in action:
                buckets[kind].append(row)
    return dict(buckets)


def _adjudicate(
    session_id: str,
    snap: dict,
    action: str,
    action_type: str,
    matches: dict[str, list[dict]],
) -> dict:
    forbidden = [
        "未进入主角知识系统的固定机缘细节",
        "未确认路线的远方地点具体路径",
        "SQLite 固定库不存在的功法、地点、秘境、势力",
    ]
    allowed = ["当前位置可见环境", "主角已知/听闻内容", "与玩家行动直接相关的低层线索"]
    if action_type == "移动":
        return _adjudicate_movement(session_id, snap, matches, allowed, forbidden)
    if action_type == "探索":
        return {
            "verdict": "allowed",
            "reason": "探索发生在当前地点或当前叙事范围内。",
            "core_result": "可检查当前位置的固定线索；若无固定机缘，不得临时生成重大奖励。",
            "allowed_reveals": allowed + _local_reveals(snap),
            "forbidden_reveals": forbidden,
        }
    if action_type == "社交/交易":
        return {
            "verdict": "allowed",
            "reason": "可与当前地点合理存在的人群互动，但核心实体仍须来自固定库。",
            "core_result": "可通过问询、交易或传闻让主角获得模糊知识；不要直接给远方秘境精确入口。",
            "allowed_reveals": allowed + ["可把固定库中常见功法/公开势力作为坊间传闻露出"],
            "forbidden_reveals": forbidden,
        }
    if action_type == "修炼":
        return {
            "verdict": "allowed",
            "reason": "修炼按简单体系处理：境界、修为、主修功法、灵根软绑定、暗伤和突破资源。",
            "core_result": "小境界可随修为推进；大境界突破需要关键资源或机缘，不能无因跃迁。",
            "allowed_reveals": allowed,
            "forbidden_reveals": forbidden + ["不在功法库中的新功法"],
        }
    if _mentions_unknown_core(action, matches):
        return {
            "verdict": "invalid",
            "reason": "玩家提到的核心名词没有命中固定库或主角知识。",
            "core_result": "写成主角无从确认或需要打听；不得把该名词创建成真实设定。",
            "allowed_reveals": allowed,
            "forbidden_reveals": forbidden,
        }
    return {
        "verdict": "allowed",
        "reason": "一般行动可按当前场景推进。",
        "core_result": "贴合玩家动作回应，保持世界事实与知识边界。",
        "allowed_reveals": allowed,
        "forbidden_reveals": forbidden,
    }


def _adjudicate_movement(
    session_id: str,
    snap: dict,
    matches: dict[str, list[dict]],
    allowed: list[str],
    forbidden: list[str],
) -> dict:
    loc_targets = matches.get("location") or []
    realm_targets = matches.get("realm") or []
    if not loc_targets and realm_targets:
        entrances = {r["entrance_location_id"] for r in realm_targets}
        loc_targets = [l for l in snap["locations"] if l["id"] in entrances]
    if not loc_targets and matches.get("faction"):
        home_ids = {
            FACTION_HOME_LOCATIONS[f["id"]]
            for f in matches["faction"]
            if f["id"] in FACTION_HOME_LOCATIONS
        }
        loc_targets = [l for l in snap["locations"] if l["id"] in home_ids]
    if not loc_targets:
        return {
            "verdict": "partial",
            "reason": "玩家表达了移动意图，但没有命中固定地点名。",
            "core_result": "可沿当前叙事中的道路/环境尝试移动，但不得创造新的目的地。",
            "allowed_reveals": allowed + _local_reveals(snap),
            "forbidden_reveals": forbidden,
        }
    target = loc_targets[0]
    knowledge = _knowledge_map(snap)
    status = knowledge.get(("location", target["id"]), {}).get("status")
    route = _route_between(snap, snap["location"]["location_id"], target["id"])
    route_status = knowledge.get(("route", route["id"]), {}).get("status") if route else None
    if status == "confirmed" and route and route_status == "confirmed":
        store.update_player_location(
            session_id,
            region_id=target["region_id"],
            location_id=target["id"],
            site_name="",
            location_state=_state_for_location(target),
            intended_destination_id=None,
            lost_risk="低" if route["risk"] in ("medium", "high") else "无",
        )
        store.upsert_knowledge(session_id, "location", target["id"], "confirmed", reliability="high", source="亲自抵达")
        return {
            "verdict": "allowed",
            "reason": f"主角知道{target['name']}，且已确认路线「{route['name']}」。",
            "core_result": f"本回合可以抵达或进入{target['name']}；路上可有小阻滞，但不要阻止已确认路线的基本移动。",
            "allowed_reveals": allowed + [target["summary"], route["summary"]],
            "forbidden_reveals": forbidden,
        }
    if status in ("confirmed", "known", "rumored"):
        store.set_intended_destination(session_id, target["id"])
        return {
            "verdict": "partial",
            "reason": f"主角{_status_text(status)}{target['name']}，但没有从当前位置通往该处的已确认路线。",
            "core_result": "不能直接抵达；可开始找路、问人、购买地图、跟随商队或先去已知中转地。",
            "allowed_reveals": allowed + [f"{target['name']}的公开/模糊传闻"],
            "forbidden_reveals": forbidden + [f"{target['name']}的精确路线"],
        }
    return {
        "verdict": "blocked",
        "reason": f"{target['name']}虽在固定世界中存在，但主角当前并不知道它。",
        "core_result": "不能直接把主角送到那里；只能写成无从下手、名字陌生或需要先获得线索。",
        "allowed_reveals": allowed,
        "forbidden_reveals": forbidden + [target["summary"]],
    }


def _mentions_unknown_core(action: str, matches: dict[str, list[dict]]) -> bool:
    bookish = ("诀", "经", "功", "法", "术", "秘境", "洞府", "宗", "派", "楼", "谷", "山", "镇", "城")
    has_entity = any(matches.values())
    return not has_entity and any(ch in action for ch in bookish)


def _knowledge_map(snap: dict) -> dict[tuple[str, str], dict]:
    return {(row["knowledge_type"], row["target_id"]): row for row in snap["knowledge"]}


def _names_by_table(snap: dict) -> dict[str, str]:
    names = {}
    for key in ("regions", "locations", "routes", "factions", "arts", "opportunities", "realms"):
        for row in snap[key]:
            names[row["id"]] = row["name"]
    return names


def _names_for_knowledge(snap: dict, knowledge: dict, kind: str, status: str | None) -> list[str]:
    names = _names_by_table(snap)
    items = []
    for (ktype, target_id), row in knowledge.items():
        if ktype != kind:
            continue
        if status is not None and row["status"] != status:
            continue
        name = names.get(target_id, target_id)
        items.append(f"{name}（{row['status']}）" if status is None else name)
    return items


def _knowledge_line(snap: dict, kind: str, status: str, label: str, *, limit: int) -> str:
    names = _names_for_knowledge(snap, _knowledge_map(snap), kind, status)
    return f"{label}：{_join_or_none(names[:limit])}"


def _location_line(snap: dict) -> str:
    loc = snap["location"]
    site = f" / {loc['site_name']}" if loc.get("site_name") else ""
    intended = loc.get("intended_destination_id")
    name_by_id = _names_by_table(snap)
    dest = f"；行动意图：{name_by_id.get(intended, intended)}" if intended else ""
    return (
        f"当前真实位置：玄苍大陆 / {loc['region_name']} / {loc['location_name']}{site}"
        f"；状态：{loc['location_state']}；迷路风险：{loc['lost_risk']}{dest}"
    )


def _intent_line(action_type: str, verdict: dict) -> str:
    return f"玩家行动类型：{action_type}；裁判结论：{verdict['verdict']}"


def _entity_line(matches: dict[str, list[dict]]) -> str:
    parts = []
    labels = {
        "location": "地点",
        "faction": "势力",
        "art": "功法",
        "opportunity": "机缘",
        "realm": "秘境/洞府",
    }
    for kind, rows in matches.items():
        names = "、".join(row["name"] for row in rows[:6])
        parts.append(f"{labels.get(kind, kind)}={names}")
    return "命中的固定实体：" + ("；".join(parts) if parts else "无")


def _local_context(snap: dict, matches: dict[str, list[dict]]) -> dict:
    current_id = snap["location"]["location_id"]
    current_region = snap["location"]["region_id"]
    related_routes = [
        r for r in snap["routes"]
        if r["from_location_id"] == current_id or r["to_location_id"] == current_id
    ]
    local_opps = [o for o in snap["opportunities"] if o["location_id"] == current_id]
    named_locs = matches.get("location") or []
    if named_locs:
        ids = {row["id"] for row in named_locs}
        local_opps.extend([o for o in snap["opportunities"] if o["location_id"] in ids])
    factions = [f for f in snap["factions"] if f["region_id"] == current_region]
    return {"routes": related_routes[:5], "opportunities": local_opps[:5], "factions": factions[:5]}


def _local_line(local: dict) -> str:
    chunks = []
    if local["routes"]:
        chunks.append("隐性本地路线：" + "、".join(r["name"] for r in local["routes"]))
    if local["opportunities"]:
        chunks.append("本地固定机缘线索：" + "、".join(o["clue"] for o in local["opportunities"]))
    if local["factions"]:
        chunks.append("本区域固定势力：" + "、".join(f["name"] for f in local["factions"]))
    return "；".join(chunks)


def _local_reveals(snap: dict) -> list[str]:
    local = _local_context(snap, {})
    reveals = []
    reveals.extend(r["summary"] for r in local["routes"][:3])
    reveals.extend(o["clue"] for o in local["opportunities"][:2])
    reveals.extend(f"{f['name']}的公开名声" for f in local["factions"][:2])
    return reveals


def _route_between(snap: dict, from_id: str, to_id: str) -> dict | None:
    for row in snap["routes"]:
        if row["from_location_id"] == from_id and row["to_location_id"] == to_id:
            return row
        if row["from_location_id"] == to_id and row["to_location_id"] == from_id:
            return row
    return None


def _state_for_location(location: dict) -> str:
    kind = location.get("kind")
    if kind in {"wild", "ruin", "secret_entrance", "lake", "peak", "rift"}:
        return "野外"
    if kind in {"market", "town", "city"}:
        return "坊市" if kind == "market" else "安全"
    if kind in {"sect", "academy"}:
        return "宗门"
    return "安全"


def _status_text(status: str | None) -> str:
    return {
        "confirmed": "确认过",
        "known": "知道",
        "rumored": "听闻过",
    }.get(status or "", "不了解")


def _join_or_none(items: list[str]) -> str:
    return "；".join(str(i) for i in items if i) if items else "无"
