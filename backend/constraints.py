"""世界约束层：在叙事生成前裁剪事实与剧情边界。

它不是负责写正文的 GM，而是前置裁判：
- 固定世界事实来自 SQLite 的 world_* 表。
- 主角视野来自 save_player_knowledge 与 save_player_location。
- 输出一段本回合可用/不可用的剧情指导，供 GM 严格遵守。
"""

from __future__ import annotations

import re
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

# Only aliases that are unambiguous within the fixed world. They help the
# reconciliation pass understand natural prose without creating locations.
LOCATION_ALIASES = {
    "baishi_village": ("村里", "村中", "村内"),
    "baishi_back_mountain": ("后山",),
    "baishi_ruined_temple": ("破庙",),
    "xuanxiao_outer_gate": ("玄霄宗山门", "外山门"),
}

# Local scenes are deliberately allow-listed.  They refine ``site_name`` but
# never create a new world location or move the player across the world map.
SITE_ALIASES = {
    "baishi_village": {
        "村西老槐树": ("村西老槐树", "老槐树下", "老槐树旁"),
        "村西小径": ("村西小径",),
        "白石村村口": ("白石村村口", "村口"),
        "白石村村中": ("白石村村中", "村中", "村里", "村内"),
        "张婶家": ("张婶家",),
    },
    "baishi_back_mountain": {
        "后山入口": ("后山入口", "后山山脚"),
        "后山林中": ("后山林中", "后山密林", "林间空地"),
    },
    "baishi_ruined_temple": {
        "破庙门前": ("破庙门前", "破庙外"),
        "破庙内": ("破庙内", "破庙里", "庙内"),
    },
}

_ARRIVAL_BEFORE_RE = re.compile(
    r"(?:抵达|到达|来到|赶到|走到|行至|回到|返回到?|进入|走进|踏入|"
    r"身处|置身于?|住进|落脚在?|(?:你|主角)(?:已经|已|正)?在|到了|进了)"
    r"[了在于]?\s*[「『\u201c\"]?$"
)
_ARRIVAL_AFTER_RE = re.compile(
    r"^[\s，。；：、——-]*(?:已经|已)?"
    r"(?:抵达|到达|到了|进入|走进|踏入|赶到|来到|行至|回到|返回到?|进了)"
)
_UNREALIZED_BEFORE_RE = re.compile(
    r"(?:尚未|还未|还没|未能|没能|并未|无法|不能|不曾|打算|计划|准备|想要|希望|欲|"
    r"若|如果|一旦|需要|须|必须|可以|试图|尝试|开始).{0,8}"
    r"(?:抵达|到达|来到|赶到|走到|行至|回到|返回到?|进入|走进|踏入|身处|置身|住进|落脚)"
)
_UNREALIZED_AFTER_RE = re.compile(r"^.{0,6}(?:计划|打算|念头|想法|路线|尚未|还未|还没)")


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
            _cultivation_demographics_line(snap),
            _location_line(snap),
            _time_line(snap),
            _knowledge_line(snap, "location", "confirmed", "已确认地点", limit=8),
            _knowledge_line(snap, "location", "rumored", "听闻地点", limit=8),
        ],
    )


def action_constraints(session_id: str, action: str) -> str:
    """根据用户输入与世界知识生成本回合叙事边界，并记录移动意图。"""
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
            _cultivation_demographics_line(snap),
            "显示规则：不要列出“可前往地点”菜单；可在叙事中自然提到道路、传闻、人物反应或线索。",
            _location_line(snap),
            _time_line(snap),
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
            "时间必须从当前世界时间向前发展，不得倒退；若行动明显耗时，应在正文自然体现经过的时间。",
        ],
    )


def inquiry_constraints(session_id: str) -> str:
    """给世界记忆问询旁路的主角知识边界。"""
    snap = store.world_snapshot(session_id)
    if not snap:
        return ""
    return _render_constraint_block(
        title="世界约束 Agent（问询知识边界）",
        lines=[
            "这是主角当前知识视野，回答问询时必须以它为准；若它与旧世界记忆冲突，以本边界为准。",
            "status=confirmed 表示主角确认知道；status=rumored 表示主角只听闻过名字或模糊传闻，不等于掌握细节。",
            _cultivation_demographics_line(snap),
            _location_line(snap),
            _time_line(snap),
            _knowledge_detail_line(snap, "location", "地点知识", limit=14),
            _knowledge_detail_line(snap, "route", "路线知识", limit=12),
            _knowledge_detail_line(snap, "faction", "势力知识", limit=10),
            _knowledge_detail_line(snap, "art", "功法知识", limit=10),
            _knowledge_detail_line(snap, "opportunity", "机缘线索", limit=10),
            "回答分寸：若主角仅 rumored 某功法，只能说听过名字/大概用途/来源传闻，不能说已经会修，也不能说完全不知道。",
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
        "time": _public_time(snap["time"]),
        "knowledge": {
            "confirmed_locations": _names_for_knowledge(snap, knowledge, "location", "confirmed"),
            "rumored_locations": _names_for_knowledge(snap, knowledge, "location", "rumored"),
            "confirmed_routes": _names_for_knowledge(snap, knowledge, "route", "confirmed"),
            "known_factions": _names_for_knowledge(snap, knowledge, "faction", None),
            "known_arts": _names_for_knowledge(snap, knowledge, "art", None),
            "known_opportunities": _names_for_knowledge(snap, knowledge, "opportunity", None),
        },
        "cultivation_demographics": snap["cultivation_demographics"],
    }


def reconcile_location(
    session_id: str, assistant_content: str, action: str | None = None
) -> dict | None:
    """Reconcile confirmed location/site changes and advance story time."""
    snap = store.world_snapshot(session_id)
    narrative = (assistant_content or "").strip()
    if not snap or not narrative:
        return None

    arrivals: list[tuple[int, dict, str]] = []
    for location in snap["locations"]:
        aliases = (location["name"],) + LOCATION_ALIASES.get(location["id"], ())
        for alias in aliases:
            start = 0
            while True:
                index = narrative.find(alias, start)
                if index < 0:
                    break
                before = narrative[max(0, index - 28):index]
                after = narrative[index + len(alias):index + len(alias) + 16]
                if _is_confirmed_arrival(before, after):
                    arrivals.append((index, location, alias))
                start = index + len(alias)

    target = None
    target_index = -1
    target_alias = ""
    if arrivals:
        # The final confirmed arrival in the prose is the end-of-turn location.
        target_index, target, target_alias = max(arrivals, key=lambda row: (row[0], len(row[2])))

    location_id = target["id"] if target else snap["location"]["location_id"]
    site_arrivals = _confirmed_site_arrivals(narrative, location_id)
    site_name = snap["location"]["site_name"]
    if target:
        # A macro move must never retain a stale site from the previous place.
        site_name = target_alias if target_alias != target["name"] else target["name"]
    if site_arrivals:
        site_index, confirmed_site = max(site_arrivals, key=lambda row: row[0])
        if not target or site_index >= target_index:
            site_name = confirmed_site

    if target or site_name != snap["location"]["site_name"]:
        current_location = target or next(
            row for row in snap["locations"] if row["id"] == location_id
        )
        route = _route_between(snap, snap["location"]["location_id"], location_id)
        lost_risk = "低" if route and route.get("risk") in ("medium", "high") else "无"
        store.update_player_location(
            session_id,
            region_id=current_location["region_id"],
            location_id=location_id,
            site_name=site_name,
            location_state=_state_for_location(current_location),
            intended_destination_id=None if target else snap["location"]["intended_destination_id"],
            lost_risk=lost_risk if target else snap["location"]["lost_risk"],
        )

    if target:
        store.upsert_knowledge(
            session_id, "location", target["id"], "confirmed",
            reliability="high", source="正文确认抵达",
        )

    if action is not None:
        store.advance_world_time(session_id, _elapsed_minutes(action, narrative, snap["time"]))
    return target


def _confirmed_site_arrivals(narrative: str, location_id: str) -> list[tuple[int, str]]:
    arrivals = []
    for canonical, aliases in SITE_ALIASES.get(location_id, {}).items():
        for alias in aliases:
            start = 0
            while True:
                index = narrative.find(alias, start)
                if index < 0:
                    break
                before = narrative[max(0, index - 28):index]
                after = narrative[index + len(alias):index + len(alias) + 16]
                if _is_confirmed_arrival(before, after):
                    arrivals.append((index, canonical))
                start = index + len(alias)
    return arrivals


def _is_confirmed_arrival(before: str, after: str) -> bool:
    if _UNREALIZED_BEFORE_RE.search(before) or _UNREALIZED_AFTER_RE.search(after):
        return False
    return bool(
        _ARRIVAL_BEFORE_RE.search(before)
        or _ARRIVAL_AFTER_RE.search(after)
    )


def director_context(session_id: str, action: str) -> dict:
    """Return the objective world slice the director may use this turn.

    The director may inspect hidden local opportunities so it can plan real
    payoffs, but the narrative agent only receives references selected by the
    validated plan. This keeps hidden world truth separate from protagonist
    cognition.
    """
    snap = store.world_snapshot(session_id)
    if not snap:
        return {}
    action = (action or "").strip()
    matches = _match_entities(snap, action)
    local = _local_context(snap, matches)
    knowledge = _knowledge_map(snap)
    current_id = snap["location"]["location_id"]
    relevant_location_ids = {current_id}
    relevant_location_ids.update(row["id"] for row in matches.get("location", []))

    arts = []
    for row in snap["arts"]:
        known = knowledge.get(("art", row["id"]))
        if (
            row.get("source_location_id") in relevant_location_ids
            or row in matches.get("art", [])
            or known is not None
        ):
            arts.append({
                "id": row["id"],
                "name": row["name"],
                "rank": row["rank"],
                "category": row["category"],
                "summary": row["summary"],
                "visibility": row["visibility"],
                "source_location_id": row.get("source_location_id"),
                "knowledge_status": known.get("status") if known else "unknown",
            })

    reward_candidates = [{
        "id": row["id"],
        "name": row["name"],
        "reward_kind": "art",
        "rank": row["rank"],
        "category": row["category"],
        "summary": row["summary"],
        "source_location_id": row.get("source_location_id"),
        "source_label": row["source_label"],
    } for row in snap["arts"]]
    opportunity_names = {row["id"]: row["name"] for row in snap["opportunities"]}
    reward_names = {row["id"]: row["name"] for row in reward_candidates}
    existing_reward_bindings = [{
        **row,
        "opportunity_name": opportunity_names.get(row["opportunity_id"], ""),
        "reward_name": reward_names.get(row["reward_id"], ""),
    } for row in store.list_opportunity_reward_bindings(session_id)]

    opportunities = []
    seen_opportunities = set()
    for row in local["opportunities"] + matches.get("opportunity", []):
        if row["id"] in seen_opportunities:
            continue
        seen_opportunities.add(row["id"])
        known = knowledge.get(("opportunity", row["id"]))
        opportunities.append({
            "id": row["id"],
            "name": row["name"],
            "kind": row["kind"],
            "clue": row["clue"],
            "danger": row["danger"],
            "state": row.get("save_state") or row["default_state"],
            "knowledge_status": known.get("status") if known else "unknown",
        })

    facts = [{
        "id": f"location:{snap['location']['location_id']}",
        "kind": "location",
        "text": snap["location"]["location_summary"],
    }]
    facts.extend({
        "id": f"route:{row['id']}",
        "kind": "route",
        "text": row["summary"],
    } for row in local["routes"])
    facts.extend({
        "id": f"faction:{row['id']}",
        "kind": "faction",
        "text": row["summary"],
    } for row in local["factions"])
    facts.extend({
        "id": f"opportunity:{row['id']}",
        "kind": "opportunity",
        "text": f"{row['name']}：{row['clue']}（危险：{row['danger']}）",
    } for row in opportunities)
    facts.extend({
        "id": f"art:{row['id']}",
        "kind": "art",
        "text": f"{row['name']}：{row['summary']}，来源为{row['source_label']}",
    } for row in snap["arts"] if row["id"] in {art["id"] for art in arts})

    allowed_reference_ids = {row["id"] for row in facts}
    allowed_reference_ids.update(row["id"] for row in arts)
    allowed_reference_ids.update(row["id"] for row in opportunities)
    allowed_reference_ids.update(row["id"] for row in reward_candidates)
    return {
        "location": {
            "region_id": snap["location"]["region_id"],
            "region_name": snap["location"]["region_name"],
            "location_id": current_id,
            "location_name": snap["location"]["location_name"],
            "site_name": snap["location"]["site_name"],
            "location_state": snap["location"]["location_state"],
            "lost_risk": snap["location"]["lost_risk"],
        },
        "time": _public_time(snap["time"]),
        "player_cognition": {
            "locations": _names_for_knowledge(snap, knowledge, "location", None),
            "routes": _names_for_knowledge(snap, knowledge, "route", None),
            "factions": _names_for_knowledge(snap, knowledge, "faction", None),
            "arts": _names_for_knowledge(snap, knowledge, "art", None),
            "opportunities": _names_for_knowledge(snap, knowledge, "opportunity", None),
        },
        "facts": facts,
        "arts": arts,
        "opportunities": opportunities,
        "reward_candidates": reward_candidates,
        "existing_reward_bindings": existing_reward_bindings,
        "cultivation_demographics": snap["cultivation_demographics"],
        "allowed_reference_ids": sorted(allowed_reference_ids),
        "forbidden_reveals": [
            "切片之外的地点、路线、势力、功法、机缘和秘境",
            "主角未知且未被本轮骨架选中的客观世界事实",
            "没有固定来源 ID 的功法、传承、法宝或重大资源",
        ],
    }


def selected_director_facts(context: dict, reference_ids: list[str]) -> list[dict]:
    """Resolve validated director references to the minimal GM-visible facts."""
    wanted = {str(ref) for ref in reference_ids}
    selected = [row for row in context.get("facts", []) if row.get("id") in wanted]
    reward_rows = {
        row["id"]: row
        for row in (context.get("arts", []) + context.get("reward_candidates", []))
        if row.get("id")
    }
    selected.extend(
        {"id": row["id"], "kind": "art", "text": f"{row['name']}：{row['summary']}"}
        for row in reward_rows.values()
        if row["id"] in wanted
    )
    selected.extend(
        {"id": row["id"], "kind": "opportunity", "text": f"{row['name']}：{row['clue']}"}
        for row in context.get("opportunities", [])
        if row.get("id") in wanted
    )
    return selected


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
            aliases = LOCATION_ALIASES.get(row["id"], ()) if kind == "location" else ()
            if (name and name in action) or any(alias in action for alias in aliases):
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
    if status == "confirmed" and (
        target["id"] == snap["location"]["location_id"]
        or (route and route_status == "confirmed")
    ):
        store.set_intended_destination(session_id, target["id"])
        route_reason = f"且已确认路线「{route['name']}」" if route else "且当前就在该地点范围内"
        return {
            "verdict": "allowed",
            "reason": f"主角知道{target['name']}，{route_reason}。",
            "core_result": f"本回合可以抵达或进入{target['name']}；路上可有小阻滞，但不要阻止已确认路线的基本移动。",
            "allowed_reveals": allowed + [target["summary"]] + ([route["summary"]] if route else []),
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


def _knowledge_detail_line(snap: dict, kind: str, label: str, *, limit: int) -> str:
    names = _names_by_table(snap)
    rows = [row for row in snap["knowledge"] if row["knowledge_type"] == kind]
    chunks = []
    for row in rows[:limit]:
        name = names.get(row["target_id"], row["target_id"])
        detail = row.get("notes") or row.get("source") or ""
        suffix = f"，{detail}" if detail else ""
        chunks.append(f"{name}（{row['status']}，可靠度 {row['reliability']}{suffix}）")
    return f"{label}：{_join_or_none(chunks)}"


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


def _public_time(world_time: dict) -> dict:
    minute = int(world_time["minute_of_day"])
    hour, minute = divmod(minute, 60)
    return {
        "day": int(world_time["day"]),
        "minute_of_day": int(world_time["minute_of_day"]),
        "clock": f"{hour:02d}:{minute:02d}",
        "period": _period_name(hour),
        "season": world_time["season"],
        "calendar_label": world_time["calendar_label"],
    }


def _time_line(snap: dict) -> str:
    value = _public_time(snap["time"])
    return (
        f"当前世界时间：{value['calendar_label']}第{value['day']}日 "
        f"{value['clock']}（{value['period']}），季节：{value['season']}"
    )


def _period_name(hour: int) -> str:
    if hour < 5:
        return "深夜"
    if hour < 8:
        return "清晨"
    if hour < 12:
        return "上午"
    if hour < 14:
        return "正午"
    if hour < 17:
        return "下午"
    if hour < 19:
        return "傍晚"
    if hour < 22:
        return "入夜"
    return "深夜"


_CN_NUMBERS = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6}
_DURATION_RE = re.compile(
    r"(?P<prefix>过了|经过|耗时|用了|花了|足足|持续|修炼了|赶了|走了|等了)?"
    r"(?P<num>[一二两三四五六\d]+|半)(?:个)?(?P<unit>时辰|小时|刻钟|分钟)"
)
_PLAN_DURATION_RE = re.compile(r"(?:打算|计划|准备|想要|预计|约定).{0,10}$")


def _elapsed_minutes(action: str, narrative: str, world_time: dict) -> int:
    """Estimate elapsed in-world time from completed prose, then action type."""
    explicit = []
    for match in _DURATION_RE.finditer(narrative):
        before = narrative[max(0, match.start() - 16):match.start()]
        if _PLAN_DURATION_RE.search(before):
            continue
        raw = match.group("num")
        amount = 0.5 if raw == "半" else float(_CN_NUMBERS.get(raw, int(raw) if raw.isdigit() else 1))
        unit = match.group("unit")
        factor = {"时辰": 120, "小时": 60, "刻钟": 15, "分钟": 1}[unit]
        explicit.append(max(1, round(amount * factor)))
    if explicit:
        return max(explicit)

    action_type = _infer_action_type(action or "")
    return {
        "移动": 30,
        "探索": 15,
        "社交/交易": 15,
        "修炼": 60,
        "物品": 5,
    }.get(action_type, 10)


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


def _cultivation_demographics_line(snap: dict) -> str:
    tiers = snap.get("cultivation_demographics") or []
    rendered = "；".join(
        f"{row['name']}（{row['rarity']}）：{row['prevalence']} NPC限制：{row['npc_rule']}"
        for row in tiers
    )
    return "固定修为人口分布（生成 NPC 时必须服从，优先采用身份可解释的最低合理修为）：" + rendered


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
