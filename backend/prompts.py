"""Local prompt templates and strict, text-only variable substitution.

Templates are loaded once at process startup. Restart the service after editing
Markdown files; a running process keeps a consistent snapshot of all prompts.
"""

from pathlib import Path
from string import Template

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


def render_prompt(name: str, **variables: object) -> str:
    """Render ${name} slots once, preserving JSON, whitespace and input text.

    Missing files/variables and extra variables are errors. Values are plain
    text: template-looking content in player input is never evaluated again.
    """
    try:
        template = _TEMPLATES[name]
    except KeyError as exc:
        raise ValueError(f"Unknown prompt template: {name}.md") from exc
    expected = _VARIABLES[name]
    missing = expected - variables.keys()
    extra = variables.keys() - expected
    if missing or extra:
        raise ValueError(
            f"Prompt variables for {name}.md: "
            f"missing={sorted(missing)}, unexpected={sorted(extra)}"
        )
    return template.substitute(variables)


def _story_card_context(card: dict, consumer: str) -> str:
    parts = [card["prompts"]["premise"], card["prompts"]["rules"]]
    if consumer in {
        "engine/payoff/system",
        "engine/event/system",
        "guidance/conflict/system",
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
    expected = _VARIABLES.get(name, set())
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
CHARACTER_SETTING_SYSTEM_PROMPT = render_system_prompt("observation/character/system")
