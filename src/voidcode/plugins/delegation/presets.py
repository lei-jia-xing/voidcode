"""Builtin child presets for the delegation ``task`` tools.

Each preset pairs one delegated profile with the scope its child runs under: the prompt text the
child starts from, the tools it may use, and whether it is read-only. The prompt bodies are verbatim
copies of the agents plugin's ``profiles/<id>.txt`` text, embedded here as module constants because
plugins cannot import each other, so this plugin reads no other plugin's resources and duplicates
no loader.

``model_preference`` exists per preset but is never applied: the kernel has no per-child model seam
(a child session carries no model of its own and runs the workspace model), so a non-null
preference is surfaced as a result note instead of being silently faked.
"""

from __future__ import annotations

from dataclasses import dataclass

# --- Verbatim ``plugins/agents/profiles/<id>.txt`` bodies (plugins cannot import each other) ---

EXECUTE_PROMPT_BODY = (
    "Carry out the delegated task with the available tools. Stay within scope, preserve unrelated "
    "user changes, and report the outcome and what was verified. Do not commit, push, or take "
    "external side effects without explicit user approval."
)

REVIEW_PROMPT_BODY = (
    "Assess the delegated question or material. Report actionable findings and supporting evidence, "
    "distinguishing facts from uncertainty. Do not change workspace files or take external side "
    "effects."
)

EXPLORE_PROMPT_BODY = (
    "Find and summarize workspace evidence relevant to the delegated question. Cite sources and "
    "keep findings focused. Do not change files or take external side effects."
)

RESEARCH_PROMPT_BODY = (
    "Investigate the delegated question using available external sources. Distinguish sourced facts "
    "from uncertainty and cite evidence. Do not change workspace files or take external side "
    "effects."
)

PLAN_PROMPT_BODY = (
    "Prepare a concise, actionable plan for the delegated goal, using the request and relevant "
    "evidence. State assumptions and unresolved questions. Do not change files or take external "
    "side effects."
)

#: The read-only discovery scope shared by every preset that reads the workspace.
_DISCOVERY_TOOLS: tuple[str, ...] = ("read", "glob", "grep", "ast_grep", "lsp")


@dataclass(frozen=True, slots=True)
class ChildPreset:
    """One builtin child preset: the profile it materializes and the scope its child runs under."""

    id: str
    description: str
    prompt_body: str
    tool_allowlist: tuple[str, ...]
    readonly: bool
    model_preference: str | None = None


PRESETS: tuple[ChildPreset, ...] = (
    ChildPreset(
        id="execute",
        description="Delegated execution of an assigned task with the workspace tools.",
        prompt_body=EXECUTE_PROMPT_BODY,
        tool_allowlist=(*_DISCOVERY_TOOLS, "write", "edit", "background_process", "shell_exec", "todo", "mcp"),
        readonly=False,
    ),
    ChildPreset(
        id="review",
        description="Delegated review of existing material with findings and supporting evidence.",
        prompt_body=REVIEW_PROMPT_BODY,
        tool_allowlist=_DISCOVERY_TOOLS,
        readonly=True,
    ),
    ChildPreset(
        id="explore",
        description="Delegated read-only discovery of relevant workspace evidence.",
        prompt_body=EXPLORE_PROMPT_BODY,
        tool_allowlist=_DISCOVERY_TOOLS,
        readonly=True,
    ),
    ChildPreset(
        id="research",
        description="Delegated research over external sources and evidence.",
        prompt_body=RESEARCH_PROMPT_BODY,
        tool_allowlist=("web_search", "web_fetch"),
        readonly=True,
    ),
    ChildPreset(
        id="plan",
        description="Delegated read-only planning grounded in the request and relevant evidence.",
        prompt_body=PLAN_PROMPT_BODY,
        tool_allowlist=(*_DISCOVERY_TOOLS, "skill", "web_search", "web_fetch"),
        readonly=True,
    ),
)

#: The preset ids in declaration order; this is the enum a caller may select from.
PRESET_IDS: tuple[str, ...] = tuple(preset.id for preset in PRESETS)

_PRESETS_BY_ID: dict[str, ChildPreset] = {preset.id: preset for preset in PRESETS}


def get_preset(preset_id: str) -> ChildPreset:
    """The preset behind an id a caller already validated against ``PRESET_IDS``."""
    return _PRESETS_BY_ID[preset_id]
