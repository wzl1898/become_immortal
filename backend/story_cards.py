"""Validated local world packages. Each save owns a complete immutable snapshot."""

import copy
import json
import re
from pathlib import Path

CARD_DIR = Path(__file__).resolve().parent / "story_cards"
DEFAULT_CARD_ID = "xiuxian"
_ID = re.compile(r"^[a-z][a-z0-9_]*$")
WORLD_COLLECTIONS = (
    "regions",
    "locations",
    "routes",
    "factions",
    "rewards",
    "opportunities",
    "special_locations",
    "power_levels",
    "characters",
    "items",
)
PROMPT_SECTIONS = ("premise", "style", "rules", "rewards", "opening", "example")


def validate(card: dict) -> None:
    """Reject malformed cards and dangling world references before creating saves."""

    def require(ok, message):
        if not ok:
            raise ValueError(f"故事卡 {card.get('id', '?')}：{message}")

    require(
        isinstance(card.get("id"), str) and _ID.fullmatch(card["id"]), "id 格式无效"
    )
    require(
        type(card.get("version")) is int and card["version"] > 0, "version 必须是正整数"
    )
    for key in ("name", "genre", "description", "default_save_name"):
        require(isinstance(card.get(key), str) and card[key].strip(), f"缺少 {key}")
    require(isinstance(card.get("prompts"), dict), "缺少 prompts")
    for key in PROMPT_SECTIONS:
        require(
            isinstance(card["prompts"].get(key), str) and card["prompts"][key].strip(),
            f"缺少提示词 {key}",
        )
    fields = card.get("status_fields")
    require(isinstance(fields, list) and bool(fields), "状态字段不能为空")
    keys, labels = set(), set()
    for field in fields:
        require(isinstance(field, dict), "状态字段必须为对象")
        key, label = field.get("key"), field.get("label")
        require(
            isinstance(key, str)
            and _ID.fullmatch(key)
            and key not in {"turn", "updated_at"}
            and key not in keys,
            "状态字段 key 重复或无效",
        )
        require(
            isinstance(label, str)
            and label.strip()
            and not any(c in label for c in ":：\n")
            and label not in labels,
            "状态字段 label 重复或无效",
        )
        require(
            field.get("kind") in {"text", "resources", "equipment"},
            "状态字段 kind 无效",
        )
        require(isinstance(field.get("initial"), str), "状态字段必须有初始文本")
        keys.add(key)
        labels.add(label)
    world = card.get("world")
    require(isinstance(world, dict) and bool(world.get("name")), "缺少世界名称")
    ids = {}
    required_text = {
        "regions": ("role", "summary"),
        "locations": ("kind", "summary"),
        "routes": ("difficulty", "risk", "summary"),
        "factions": ("kind", "summary"),
        "rewards": ("summary",),
        "opportunities": ("kind", "clue", "danger", "default_state"),
        "special_locations": ("kind", "opening_rule", "entry_limit"),
        "power_levels": ("rarity", "prevalence", "npc_rule"),
        "characters": ("summary", "visibility"),
        "items": ("summary", "attrs", "kind", "visibility"),
    }
    for collection in WORLD_COLLECTIONS:
        rows = world.get(collection)
        require(isinstance(rows, list), f"{collection} 必须为数组")
        ids[collection] = set()
        for row in rows:
            require(isinstance(row, dict), f"{collection} 条目必须为对象")
            key = row.get("id")
            require(
                isinstance(key, str)
                and _ID.fullmatch(key)
                and key not in ids[collection],
                f"{collection} id 重复或无效",
            )
            require(
                isinstance(row.get("name"), str) and row["name"].strip(),
                f"{collection} 缺少名称",
            )
            for field in required_text[collection]:
                require(
                    isinstance(row.get(field), str),
                    f"{collection}.{key} 缺少文本字段 {field}",
                )
            if collection == "power_levels":
                require(
                    type(row.get("sort_order")) is int and row["sort_order"] >= 0,
                    "能力层级顺序无效",
                )
            ids[collection].add(key)
    require(ids["regions"] and ids["locations"], "至少需要一个区域和地点")
    for row in world["locations"]:
        require(row.get("region_id") in ids["regions"], "地点引用了不存在的区域")
        require(
            not row.get("parent_id") or row["parent_id"] in ids["locations"],
            "父地点不存在",
        )
    for row in world["routes"]:
        require(
            row.get("from_location_id") in ids["locations"]
            and row.get("to_location_id") in ids["locations"],
            "路线端点不存在",
        )
    for row in world["factions"]:
        require(row.get("region_id") in ids["regions"], "势力所在区域不存在")
    for collection in ("opportunities", "characters", "items"):
        for row in world[collection]:
            require(
                row.get("location_id") in ids["locations"],
                f"{collection} 所在地点不存在",
            )
    for row in world["special_locations"]:
        require(
            row.get("entrance_location_id") in ids["locations"], "特殊场所入口不存在"
        )
    for row in world["rewards"]:
        require(
            row.get("source_location_id") in ids["locations"] | ids["factions"],
            "奖励来源不存在",
        )
        require(
            isinstance(row.get("reward_kind"), str)
            and _ID.fullmatch(row["reward_kind"]),
            "奖励类型无效",
        )
    for key in ("location_aliases", "site_aliases", "faction_homes"):
        require(isinstance(world.get(key, {}), dict), f"{key} 必须为对象")
    for location, aliases in world.get("location_aliases", {}).items():
        require(
            location in ids["locations"]
            and isinstance(aliases, list)
            and all(isinstance(a, str) and a for a in aliases),
            "地点别名无效",
        )
    for location, sites in world.get("site_aliases", {}).items():
        require(
            location in ids["locations"] and isinstance(sites, dict), "局部场景无效"
        )
        for site, aliases in sites.items():
            require(
                bool(site)
                and isinstance(aliases, list)
                and all(isinstance(a, str) and a for a in aliases),
                "局部场景别名无效",
            )
    for faction, location in world.get("faction_homes", {}).items():
        require(
            faction in ids["factions"] and location in ids["locations"], "势力驻地无效"
        )
    initial = card.get("initial")
    require(isinstance(initial, dict), "缺少初始状态")
    location = initial.get("location", {})
    require(location.get("location_id") in ids["locations"], "开局地点不存在")
    actual = next(
        row for row in world["locations"] if row["id"] == location["location_id"]
    )
    require(location.get("region_id") == actual["region_id"], "开局地点与区域不一致")
    clock = initial.get("time", {})
    require(type(clock.get("day")) is int and clock["day"] >= 1, "初始日期无效")
    require(
        type(clock.get("minute_of_day")) is int and 0 <= clock["minute_of_day"] < 1440,
        "初始时间无效",
    )
    for key in ("season", "calendar_label"):
        require(isinstance(clock.get(key), str) and clock[key], f"初始时间缺少 {key}")
    mapping = {
        "location": "locations",
        "route": "routes",
        "faction": "factions",
        "art": "rewards",
        "opportunity": "opportunities",
        "realm": "special_locations",
        "character": "characters",
        "item": "items",
    }
    require(isinstance(initial.get("knowledge"), list), "初始知识必须为数组")
    for row in initial["knowledge"]:
        kind = row.get("knowledge_type")
        require(
            kind in mapping and row.get("target_id") in ids[mapping[kind]],
            "初始知识引用不存在的实体",
        )
        require(
            row.get("status") in {"confirmed", "known", "rumored"}, "初始知识状态无效"
        )
    require(
        isinstance(initial.get("inventory"), list)
        and all(key in ids["items"] for key in initial["inventory"]),
        "初始物品不存在",
    )


def _load_cards() -> dict:
    cards = {}
    for path in sorted(CARD_DIR.glob("*/card.json")):
        card = json.loads(path.read_text(encoding="utf-8"))
        for key in PROMPT_SECTIONS:
            filename = card.get("prompts", {}).get(key)
            if not isinstance(filename, str):
                raise ValueError(f"{path}: 缺少提示词文件 {key}")
            prompt_path = (path.parent / filename).resolve()
            if (
                not prompt_path.is_relative_to(path.parent.resolve())
                or prompt_path.suffix != ".md"
            ):
                raise ValueError(
                    f"{path}: 提示词路径必须指向故事卡目录内的 Markdown 文件"
                )
            card["prompts"][key] = prompt_path.read_text(encoding="utf-8").strip()
        validate(card)
        if card["id"] in cards or card["id"] != path.parent.name:
            raise ValueError(f"{path}: 故事卡 id 重复或与目录名不符")
        cards[card["id"]] = card
    if DEFAULT_CARD_ID not in cards:
        raise ValueError("缺少兼容旧存档的默认故事卡")
    return cards


_CARDS = _load_cards()


def get(card_id: str = DEFAULT_CARD_ID) -> dict:
    if card_id not in _CARDS:
        raise ValueError("故事卡不存在，请重新选择")
    return copy.deepcopy(_CARDS[card_id])


def public(card: dict) -> dict:
    """Only player-facing metadata; never reveal the full world or hidden facts."""
    return {
        key: copy.deepcopy(card[key])
        for key in (
            "id",
            "version",
            "name",
            "genre",
            "description",
            "default_save_name",
            "status_fields",
        )
    }


def list_cards() -> list[dict]:
    return [public(card) for card in _CARDS.values()]


def field_labels(card: dict | None = None) -> tuple:
    return tuple(
        (field["key"], field["label"])
        for field in (card or _CARDS[DEFAULT_CARD_ID])["status_fields"]
    )


def initial_inventory(card: dict) -> list[dict]:
    return [
        {
            "id": row["id"],
            "name": row["name"],
            "attrs": row.get("attrs", ""),
            "kind": row.get("kind", "物件"),
            "whereabouts": "随身",
            "last_turn": 0,
        }
        for row in card["world"]["items"]
        if row["id"] in card["initial"]["inventory"]
    ]
