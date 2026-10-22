"""Builtin agent manifests, ported from ``voidcode.agent.builtin``.

The tool allowlists name the real admitted tools of the new world: the legacy handoff tool
(``yield``), the ask-user tool (``question``), the nested-invocation wrapper (``invoke_tool``) and
the ``mcp/*`` glob are gone, replaced by ``mcp``. Descriptions and prompt
materializations keep their legacy values; only the module paths of the validators moved.
"""

from __future__ import annotations

from .models import AgentManifest, AgentPromptMaterialization
from .prompts import render_builtin_prompt_profile

_WORKSPACE_DISCOVERY_TOOLS = (
    "read",
    "glob",
    "grep",
    "ast_grep",
    "lsp",
)

_DEFAULT_TOOL_ALLOWLIST = (
    *_WORKSPACE_DISCOVERY_TOOLS,
    "write",
    "edit",
    "background_process",
    "shell_exec",
    "task",
    "task_batch",
    "todo",
    "get_goal",
    "create_goal",
    "update_goal",
    "skill",
    "web_search",
    "mcp",
)


DEFAULT_AGENT_MANIFEST = AgentManifest(
    id="default",
    name="Default",
    mode="primary",
    description="General user-facing assistant; runtime owns policy and tool scope.",
    prompt_profile="default",
    tool_allowlist=_DEFAULT_TOOL_ALLOWLIST,
    top_level_selectable=True,
    prompt_materialization=AgentPromptMaterialization(
        profile="default",
        source="builtin",
    ),
)

EXECUTE_AGENT_MANIFEST = AgentManifest(
    id="execute",
    name="Execute",
    mode="subagent",
    description="Delegated execution of an assigned task; runtime enforces the active tool scope.",
    prompt_profile="execute",
    tool_allowlist=(
        *_WORKSPACE_DISCOVERY_TOOLS,
        "write",
        "edit",
        "background_process",
        "shell_exec",
        "todo",
        "mcp",
    ),
    top_level_selectable=False,
    prompt_materialization=AgentPromptMaterialization(
        profile="execute",
        source="builtin",
    ),
)

REVIEW_AGENT_MANIFEST = AgentManifest(
    id="review",
    readonly=True,
    name="Review",
    mode="subagent",
    description="Delegated review of evidence, risks, and options.",
    prompt_profile="review",
    tool_allowlist=_WORKSPACE_DISCOVERY_TOOLS,
    top_level_selectable=False,
    prompt_materialization=AgentPromptMaterialization(
        profile="review",
        source="builtin",
    ),
)

EXPLORE_AGENT_MANIFEST = AgentManifest(
    id="explore",
    readonly=True,
    name="Explore",
    mode="subagent",
    description="Delegated read-only discovery of relevant workspace evidence.",
    prompt_profile="explore",
    tool_allowlist=_WORKSPACE_DISCOVERY_TOOLS,
    top_level_selectable=False,
    prompt_materialization=AgentPromptMaterialization(
        profile="explore",
        source="builtin",
    ),
)

RESEARCH_AGENT_MANIFEST = AgentManifest(
    id="research",
    readonly=True,
    name="Research",
    mode="subagent",
    description="Delegated research using public sources and external evidence.",
    prompt_profile="research",
    tool_allowlist=("web_search",),
    top_level_selectable=False,
    prompt_materialization=AgentPromptMaterialization(
        profile="research",
        source="builtin",
    ),
)

_PLAN_TOOL_ALLOWLIST = (
    *_WORKSPACE_DISCOVERY_TOOLS,
    "skill",
    "web_search",
)

PLAN_AGENT_MANIFEST = AgentManifest(
    id="plan",
    readonly=True,
    name="Planning",
    mode="subagent",
    description="Delegated read-only planning grounded in the request and relevant evidence.",
    prompt_profile="plan",
    tool_allowlist=_PLAN_TOOL_ALLOWLIST,
    top_level_selectable=False,
    prompt_materialization=AgentPromptMaterialization(
        profile="plan",
        source="builtin",
    ),
)


def validate_builtin_agent_manifests(
    manifests: tuple[AgentManifest, ...],
) -> tuple[AgentManifest, ...]:
    manifest_ids: set[str] = set()
    for manifest in manifests:
        if manifest.id in manifest_ids:
            raise ValueError(f"duplicate builtin agent manifest id: {manifest.id}")
        manifest_ids.add(manifest.id)
        if not manifest.name.strip():
            raise ValueError(f"builtin agent manifest '{manifest.id}' must declare a non-empty name")
        if not manifest.description.strip():
            raise ValueError(f"builtin agent manifest '{manifest.id}' must declare a non-empty description")
        if manifest.prompt_profile is None or not manifest.prompt_profile.strip():
            raise ValueError(f"builtin agent manifest '{manifest.id}' must declare a builtin prompt_profile")
        if render_builtin_prompt_profile(manifest.prompt_profile) is None:
            raise ValueError(f"builtin agent manifest '{manifest.id}' references unknown prompt profile '{manifest.prompt_profile}'")
        if len(manifest.tool_allowlist) != len(set(manifest.tool_allowlist)):
            raise ValueError(f"builtin agent manifest '{manifest.id}' must not contain duplicate tool patterns")
        if manifest.mode == "primary" and not manifest.top_level_selectable:
            raise ValueError(f"builtin agent manifest '{manifest.id}' has mode='primary' but is not marked top_level_selectable")
        if manifest.mode == "subagent" and manifest.top_level_selectable:
            raise ValueError(
                f"builtin agent manifest '{manifest.id}' has mode='subagent' but is marked "
                "top_level_selectable; subagent presets must not be top-level selectable"
            )
        if manifest.prompt_materialization is None:
            raise ValueError(f"builtin agent manifest '{manifest.id}' must declare prompt_materialization")
        materialization = manifest.prompt_materialization
        if render_builtin_prompt_profile(materialization.profile) is None:
            raise ValueError(
                f"builtin agent manifest '{manifest.id}' prompt_materialization.profile references unknown prompt profile '{materialization.profile}'"
            )
        if materialization.profile != manifest.prompt_profile:
            raise ValueError(
                f"builtin agent manifest '{manifest.id}' prompt_materialization.profile "
                f"'{materialization.profile}' must match prompt_profile "
                f"'{manifest.prompt_profile}'"
            )
    return manifests


_VALIDATED_BUILTIN_AGENT_MANIFESTS = validate_builtin_agent_manifests(
    (
        DEFAULT_AGENT_MANIFEST,
        EXECUTE_AGENT_MANIFEST,
        REVIEW_AGENT_MANIFEST,
        EXPLORE_AGENT_MANIFEST,
        RESEARCH_AGENT_MANIFEST,
        PLAN_AGENT_MANIFEST,
    )
)

_BUILTIN_AGENT_MANIFESTS: dict[str, AgentManifest] = {manifest.id: manifest for manifest in _VALIDATED_BUILTIN_AGENT_MANIFESTS}


def get_builtin_agent_manifest(agent_id: str) -> AgentManifest | None:
    return _BUILTIN_AGENT_MANIFESTS.get(agent_id)


def list_builtin_agent_manifests() -> tuple[AgentManifest, ...]:
    return tuple(_BUILTIN_AGENT_MANIFESTS.values())
