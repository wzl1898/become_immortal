"""Local prompt templates and strict, text-only variable substitution.

Templates are loaded once at process startup. Restart the service after editing
Markdown files; a running process keeps a consistent snapshot of all prompts.
"""

from pathlib import Path
import re
import sqlite3
from string import Template

import store
import story_cards


TEMPLATE_DIR = Path(__file__).resolve().parent / "prompt_templates"
_TEMPLATES = {
    path.relative_to(TEMPLATE_DIR)
    .with_suffix("")
    .as_posix(): Template(path.read_text(encoding="utf-8"))
    for path in sorted(TEMPLATE_DIR.rglob("*.md"))
}
_VARIABLES = {}
for _name, _template in _TEMPLATES.items():
    _identifiers = set()
    for _match in _template.pattern.finditer(_template.template):
        if _match.group("invalid") is not None:
            raise ValueError(f"Invalid prompt template syntax: {_name}.md")
        _identifier = _match.group("named") or _match.group("braced")
        if _identifier:
            _identifiers.add(_identifier)
    _VARIABLES[_name] = _identifiers

# Only database versions that differ from the current file baseline live here.
# This keeps the existing file-template test and CLI inspection contracts intact.
_ACTIVE_TEMPLATES: dict[str, Template] = {}
_ACTIVE_VARIABLES: dict[str, set[str]] = {}
_PROMPT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,159}$")

# The catalog is intentionally Agent-first. A single Agent can own several
# system, user, retry, fallback and composition templates.
_AGENT_GROUPS = {
    "engine/narrative": "叙事 Agent",
    "engine/event": "事件 Agent",
    "engine/causal": "因果 Agent",
    "engine/viewpoint": "视角 Agent",
    "engine/hook": "伏笔 Agent",
    "engine/payoff": "回收 Agent",
    "engine/pacing": "节奏 Agent",
    "engine/progression": "成长 Agent",
    "engine/skeleton": "剧情骨架 Agent",
    "engine/audit": "审计 Agent",
    "guidance/conflict": "冲突引导 Agent",
    "guidance/stage": "阶段引导 Agent",
    "memory/extract": "记忆提取 Agent",
    "memory/inquiry": "记忆询问 Agent",
    "observation/character": "角色观察 Agent",
    "observation/conflict": "冲突观察 Agent",
    "observation/location": "地点观察 Agent",
    "observation/state": "状态观察 Agent",
}
_SUPPORT_GROUPS = {
    "constraints": "约束片段",
    "shared": "共享片段",
    "legacy": "旧版导演",
}
_TEMPLATE_SEGMENT_LABELS = {
    "system": "系统提示词",
    "user": "用户提示词",
    "action": "行动",
    "audit_feedback": "审计反馈",
    "base": "基础",
    "beats": "节拍",
    "binding": "绑定规则",
    "block": "阻断",
    "causal_context": "因果上下文",
    "character_state": "角色状态",
    "cultivation": "修炼",
    "entities": "实体",
    "event_ended": "事件结束",
    "facts": "事实",
    "fallback": "回退提示词",
    "fallback_direction": "回退方向",
    "fallback_reason": "回退原因",
    "forced": "强制规划",
    "forced_resolution": "强制收束",
    "guidance": "引导",
    "header": "头部",
    "hook": "伏笔",
    "hook_guard": "伏笔守卫",
    "inquiry": "询问",
    "intent": "意图",
    "intent_resolved": "意图完成",
    "inventory": "物品栏",
    "known": "已知记忆",
    "location": "地点",
    "narration": "叙事",
    "next_user": "下一轮用户输入",
    "objects": "对象",
    "opening": "开场",
    "payoff": "回收",
    "plan": "规划",
    "player_action": "玩家行动",
    "previous_state": "前序状态",
    "prohibited": "禁止项",
    "question": "问题",
    "recent_memories": "近期记忆",
    "retry": "重试提示词",
    "retry_failure": "重试失败",
    "retry_requirements": "重试要求",
    "scene": "场景",
    "scene_only": "仅场景",
    "scene_push": "场景推进",
    "stable_world": "稳定世界",
    "stage": "阶段",
    "stage_summary": "阶段摘要",
    "status_panel": "状态面板",
    "story_card": "故事卡",
    "story_card_status": "故事卡状态",
    "story_seed": "故事种子",
    "time": "时间",
    "tool_result": "工具结果",
    "trigger": "触发",
    "truncated": "截断续写",
    "untriggered": "未触发",
    "viewpoint_context": "视角上下文",
    "world": "世界状态",
    "world_memory": "世界记忆",
    "wrapper": "包装模板",
}


def _catalog_metadata(name: str) -> dict:
    parts = name.split("/")
    agent_key = "/".join(parts[:2])
    if agent_key in _AGENT_GROUPS:
        group_key = agent_key
        group_label = _AGENT_GROUPS[group_key]
        catalog_scope = "agent"
        group_order = list(_AGENT_GROUPS).index(group_key)
        template_parts = parts[2:]
    elif parts[0] in _SUPPORT_GROUPS:
        group_key = parts[0]
        group_label = _SUPPORT_GROUPS[group_key]
        catalog_scope = "support"
        group_order = 100 + list(_SUPPORT_GROUPS).index(group_key)
        template_parts = parts[1:]
    else:
        group_key = "custom"
        group_label = "自建提示词"
        catalog_scope = "custom"
        group_order = 200
        template_parts = parts[1:] if parts[0] == "custom" else parts

    template_path = "/".join(template_parts) or name
    template_label = " / ".join(
        _TEMPLATE_SEGMENT_LABELS.get(part, part.replace("_", " "))
        for part in template_parts
    ) or name
    leaf = template_parts[-1] if template_parts else name
    template_order = {"system": 0, "user": 1}.get(leaf, 10)
    return {
        "catalog_scope": catalog_scope,
        "group_key": group_key,
        "group_label": group_label,
        "group_order": group_order,
        "template_path": template_path,
        "template_label": template_label,
        "template_order": template_order,
    }


def _with_catalog_metadata(item: dict) -> dict:
    return {**item, **_catalog_metadata(item["name"])}


def _compile_template(content: str, *, name: str = "prompt") -> tuple[Template, set[str]]:
    template = Template(content)
    identifiers = set()
    for match in template.pattern.finditer(template.template):
        if match.group("invalid") is not None:
            raise ValueError(f"提示词模板语法无效：{name}")
        identifier = match.group("named") or match.group("braced")
        if identifier:
            identifiers.add(identifier)
    return template, identifiers


def _reset_active_templates() -> None:
    _ACTIVE_TEMPLATES.clear()
    _ACTIVE_VARIABLES.clear()


def init_registry() -> None:
    """Seed the prompt registry and load every production version into memory."""
    store.seed_prompt_templates(
        [
            {
                "name": name,
                "content": template.template,
                "variables": sorted(_VARIABLES[name]),
            }
            for name, template in _TEMPLATES.items()
        ]
    )
    _reset_active_templates()
    for item in store.list_production_prompt_versions():
        template, variables = _compile_template(item["content"], name=item["name"])
        if variables != set(item["variables"]):
            raise ValueError(f"提示词版本变量记录不一致：{item['name']}")
        baseline = _TEMPLATES.get(item["name"])
        if baseline is None or baseline.template != item["content"]:
            _ACTIVE_TEMPLATES[item["name"]] = template
            _ACTIVE_VARIABLES[item["name"]] = variables


def _validate_name(name: str) -> str:
    name = name.strip()
    if (
        not _PROMPT_NAME_RE.fullmatch(name)
        or ".." in name
        or "//" in name
        or name.endswith("/")
    ):
        raise ValueError(
            "提示词名称需为 1–160 位字母、数字及 . _ / -，且不能包含 .. 或连续斜杠"
        )
    return name


def _validated_content(content: str, name: str) -> tuple[str, set[str]]:
    if not content.strip():
        raise ValueError("提示词内容不能为空")
    if len(content) > 500_000:
        raise ValueError("提示词内容不能超过 500000 个字符")
    _, variables = _compile_template(content, name=name)
    return content, variables


def list_prompts() -> list[dict]:
    return [_with_catalog_metadata(item) for item in store.list_prompt_definitions()]


def get_prompt(name: str) -> dict | None:
    item = store.get_prompt_definition(name)
    return _with_catalog_metadata(item) if item is not None else None


def create_prompt(
    name: str,
    description: str,
    content: str,
    created_by: str,
    *,
    make_production: bool = False,
) -> dict:
    name = _validate_name(name)
    description = description.strip()
    if len(description) > 500:
        raise ValueError("提示词说明不能超过 500 个字符")
    content, variables = _validated_content(content, name)
    try:
        result = store.create_prompt_definition(
            name,
            description,
            content,
            sorted(variables),
            created_by,
            make_production=make_production,
        )
    except sqlite3.IntegrityError as exc:
        raise ValueError("提示词名称已存在") from exc
    if make_production:
        _ACTIVE_TEMPLATES[name], _ACTIVE_VARIABLES[name] = _compile_template(
            content, name=name
        )
    return _with_catalog_metadata(result)


def create_version(name: str, content: str, created_by: str) -> dict | None:
    name = _validate_name(name)
    content, variables = _validated_content(content, name)
    result = store.create_prompt_version(
        name, content, sorted(variables), created_by
    )
    return _with_catalog_metadata(result) if result is not None else None


def promote_version(name: str, version: int) -> dict | None:
    name = _validate_name(name)
    definition = store.get_prompt_definition(name)
    if definition is None:
        return None
    selected = next(
        (item for item in definition["versions"] if item["version"] == version),
        None,
    )
    if selected is None:
        return None
    if set(selected["variables"]) != set(definition["expected_variables"]):
        raise ValueError(
            "该版本的变量与提示词契约不一致，不能发布到 production"
        )
    result = store.set_prompt_production_version(name, version)
    _ACTIVE_TEMPLATES[name], _ACTIVE_VARIABLES[name] = _compile_template(
        selected["content"], name=name
    )
    return _with_catalog_metadata(result)


def render_prompt(template_name: str, **variables: object) -> str:
    """Render ${name} slots once, preserving JSON, whitespace and input text.

    Missing files/variables and extra variables are errors. Values are plain
    text: template-looking content in player input is never evaluated again.
    """
    try:
        template = _ACTIVE_TEMPLATES.get(template_name) or _TEMPLATES[template_name]
    except KeyError as exc:
        raise ValueError(f"Unknown prompt template: {template_name}.md") from exc
    expected = _ACTIVE_VARIABLES.get(template_name, _VARIABLES.get(template_name, set()))
    missing = expected - variables.keys()
    extra = variables.keys() - expected
    if missing or extra:
        raise ValueError(
            f"Prompt variables for {template_name}.md: "
            f"missing={sorted(missing)}, unexpected={sorted(extra)}"
        )
    return template.substitute(variables)


def _story_card_context(card: dict, consumer: str) -> str:
    parts = [card["prompts"]["premise"], card["prompts"]["rules"]]
    if consumer in {
        "engine/payoff/system",
        "engine/event/system",
        "guidance/conflict/system",
        "guidance/stage/system",
    }:
        parts.append(card["prompts"]["rewards"])
    if consumer == "engine/narrative/system":
        parts.extend(
            [
                card["prompts"]["style"],
                render_prompt(
                    "shared/story_card_status",
                    fields="、".join(field["label"] for field in card["status_fields"]),
                    inventory_fields="、".join(
                        field["label"]
                        for field in card["status_fields"]
                        if field["kind"] in {"resources", "equipment"}
                    ),
                ),
                card["prompts"]["example"],
            ]
        )
    return render_prompt(
        "shared/story_card", card_name=card["name"], content="\n\n".join(parts)
    )


def render_system_prompt(
    name: str, *, context: str = "", card: dict | None = None
) -> str:
    """Compose neutral Agent rules with the selected save's story-card rules."""
    card = card if card is not None else story_cards.get()
    variables = {}
    expected = _ACTIVE_VARIABLES.get(name, _VARIABLES.get(name, set()))
    if "context" in expected:
        variables["context"] = context
    elif context:
        raise ValueError(f"System prompt has no context slot: {name}.md")
    if "story_rules" in expected:
        variables["story_rules"] = _story_card_context(card, name)
    return render_prompt(name, **variables)


# Compatibility names for existing callers and prompt-contract tests.
CULTIVATION_SYSTEM_APPENDIX = story_cards.get()["prompts"]["rules"]
SYSTEM_PROMPT = render_system_prompt("engine/narrative/system")
OPENING_PROMPT = render_prompt("engine/narrative/opening")
INQUIRY_SYSTEM_PROMPT = render_system_prompt("memory/inquiry/system")
MEMORY_EXTRACT_SYSTEM_PROMPT = render_system_prompt("memory/extract/system")
DIRECTOR_SYSTEM_PROMPT = render_system_prompt("legacy/director/system")
DIRECTOR_EVENT_SYSTEM_PROMPT = render_system_prompt("engine/event/system")
DIRECTOR_PROGRESSION_SYSTEM_PROMPT = render_system_prompt("engine/progression/system")
DIRECTOR_CAUSAL_SYSTEM_PROMPT = render_system_prompt("engine/causal/system")
DIRECTOR_VIEWPOINT_SYSTEM_PROMPT = render_system_prompt("engine/viewpoint/system")
DIRECTOR_HOOK_SYSTEM_PROMPT = render_system_prompt("engine/hook/system")
DIRECTOR_PAYOFF_SYSTEM_PROMPT = render_system_prompt("engine/payoff/system")
DIRECTOR_PACING_SYSTEM_PROMPT = render_system_prompt("engine/pacing/system")
DIRECTOR_SKELETON_SYSTEM_PROMPT = render_system_prompt("engine/skeleton/system")
DIRECTOR_AUDIT_SYSTEM_PROMPT = render_system_prompt("engine/audit/system")
NARRATIVE_OBSERVER_SYSTEM_PROMPT = render_system_prompt("observation/conflict/system")
GUIDANCE_CONFLICT_SYSTEM_PROMPT = render_system_prompt("guidance/conflict/system")
GUIDANCE_STAGE_SYSTEM_PROMPT = render_system_prompt("guidance/stage/system")
CHARACTER_SETTING_SYSTEM_PROMPT = render_system_prompt("observation/character/system")
