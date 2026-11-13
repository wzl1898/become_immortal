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
    return store.list_prompt_definitions()


def get_prompt(name: str) -> dict | None:
    return store.get_prompt_definition(name)


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
    return result


def create_version(name: str, content: str, created_by: str) -> dict | None:
    name = _validate_name(name)
    content, variables = _validated_content(content, name)
    return store.create_prompt_version(name, content, sorted(variables), created_by)


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
    return result


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
