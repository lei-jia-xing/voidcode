from __future__ import annotations

from ..hook.presets import validate_hook_preset_refs
from .models import AgentManifest, AgentPromptMaterialization
from .prompts import render_builtin_prompt_profile

_WORKSPACE_DISCOVERY_TOOLS = (
    "read",
    "glob",
    "grep",
    "ast_grep",
    "lsp",
)

_CHILD_HANDOFF_TOOLS = ("yield",)


_DEFAULT_TOOL_ALLOWLIST = (
    *_WORKSPACE_DISCOVERY_TOOLS,
    "write",
    "edit",
    "background_process",
    "shell_exec",
    "task",
    "task_batch",
    "todo",
    "question",
    "skill",
    "web_search",
    "web_fetch",
    "invoke_tool",
    "mcp/*",
)

_BUILTIN_PROMPT_MATERIALIZATION_VERSION = 3

_DEFAULT_PRESET_HOOK_REFS = ("delegation_guard",)

_DELEGATED_PRESET_HOOK_REFS = ("delegation_guard",)

DEFAULT_AGENT_MANIFEST = AgentManifest(
    id="default",
    name="Default",
    mode="primary",
    description="General user-facing assistant; runtime owns policy and tool scope.",
    prompt_profile="default",
    execution_engine="provider",
    tool_allowlist=_DEFAULT_TOOL_ALLOWLIST,
    preset_hook_refs=_DEFAULT_PRESET_HOOK_REFS,
    top_level_selectable=True,
    prompt_materialization=AgentPromptMaterialization(
        profile="default",
        version=_BUILTIN_PROMPT_MATERIALIZATION_VERSION,
        source="builtin",
        format="text",
    ),
)

EXECUTE_AGENT_MANIFEST = AgentManifest(
    id="execute",
    name="Execute",
    mode="subagent",
    description="Delegated execution of an assigned task; runtime enforces the active tool scope.",
    prompt_profile="execute",
    execution_engine="provider",
    tool_allowlist=(
        *_WORKSPACE_DISCOVERY_TOOLS,
        "write",
        "edit",
        "background_process",
        "shell_exec",
        "todo",
        "mcp/*",
        *_CHILD_HANDOFF_TOOLS,
    ),
    preset_hook_refs=_DELEGATED_PRESET_HOOK_REFS,
    top_level_selectable=False,
    prompt_materialization=AgentPromptMaterialization(
        profile="execute",
        version=_BUILTIN_PROMPT_MATERIALIZATION_VERSION,
        source="builtin",
        format="text",
    ),
)

REVIEW_AGENT_MANIFEST = AgentManifest(
    id="review",
    name="Review",
    mode="subagent",
    description="Delegated review of evidence, risks, and options.",
    prompt_profile="review",
    execution_engine="provider",
    tool_allowlist=(*_WORKSPACE_DISCOVERY_TOOLS, *_CHILD_HANDOFF_TOOLS),
    preset_hook_refs=_DELEGATED_PRESET_HOOK_REFS,
    top_level_selectable=False,
    prompt_materialization=AgentPromptMaterialization(
        profile="review",
        version=_BUILTIN_PROMPT_MATERIALIZATION_VERSION,
        source="builtin",
        format="text",
    ),
)

EXPLORE_AGENT_MANIFEST = AgentManifest(
    id="explore",
    name="Explore",
    mode="subagent",
    description="Delegated read-only discovery of relevant workspace evidence.",
    prompt_profile="explore",
    execution_engine="provider",
    tool_allowlist=(*_WORKSPACE_DISCOVERY_TOOLS, *_CHILD_HANDOFF_TOOLS),
    preset_hook_refs=_DELEGATED_PRESET_HOOK_REFS,
    top_level_selectable=False,
    prompt_materialization=AgentPromptMaterialization(
        profile="explore",
        version=_BUILTIN_PROMPT_MATERIALIZATION_VERSION,
        source="builtin",
        format="text",
    ),
)

RESEARCH_AGENT_MANIFEST = AgentManifest(
    id="research",
    name="Research",
    mode="subagent",
    description="Delegated research using public sources and external evidence.",
    prompt_profile="research",
    execution_engine="provider",
    tool_allowlist=("web_search", "web_fetch", *_CHILD_HANDOFF_TOOLS),
    preset_hook_refs=_DELEGATED_PRESET_HOOK_REFS,
    top_level_selectable=False,
    prompt_materialization=AgentPromptMaterialization(
        profile="research",
        version=_BUILTIN_PROMPT_MATERIALIZATION_VERSION,
        source="builtin",
        format="text",
    ),
)

_PLAN_TOOL_ALLOWLIST = (
    *_WORKSPACE_DISCOVERY_TOOLS,
    *_CHILD_HANDOFF_TOOLS,
    "skill",
    "web_search",
    "web_fetch",
    "invoke_tool",
)

PLAN_AGENT_MANIFEST = AgentManifest(
    id="plan",
    name="Planning",
    mode="subagent",
    description="Delegated read-only planning grounded in the request and relevant evidence.",
    prompt_profile="plan",
    execution_engine="provider",
    tool_allowlist=_PLAN_TOOL_ALLOWLIST,
    preset_hook_refs=_DELEGATED_PRESET_HOOK_REFS,
    top_level_selectable=False,
    prompt_materialization=AgentPromptMaterialization(
        profile="plan",
        version=_BUILTIN_PROMPT_MATERIALIZATION_VERSION,
        source="builtin",
        format="text",
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
        if manifest.execution_engine is None:
            raise ValueError(f"builtin agent manifest '{manifest.id}' must declare an execution_engine")
        if len(manifest.tool_allowlist) != len(set(manifest.tool_allowlist)):
            raise ValueError(f"builtin agent manifest '{manifest.id}' must not contain duplicate tool patterns")
        if len(manifest.preset_hook_refs) != len(set(manifest.preset_hook_refs)):
            raise ValueError(f"builtin agent manifest '{manifest.id}' must not contain duplicate preset hook refs")
        for hook_ref in manifest.preset_hook_refs:
            if not hook_ref.strip():
                raise ValueError(f"builtin agent manifest '{manifest.id}' preset hook refs must be non-empty")
        validate_hook_preset_refs(
            manifest.preset_hook_refs,
            field_path=f"builtin agent manifest '{manifest.id}' preset_hook_refs",
        )
        if manifest.mcp_binding is not None:
            binding_profile = manifest.mcp_binding.profile
            if binding_profile is not None and not binding_profile.strip():
                raise ValueError(f"builtin agent manifest '{manifest.id}' mcp_binding.profile must be a non-empty string")
            if len(manifest.mcp_binding.servers) != len(set(manifest.mcp_binding.servers)):
                raise ValueError(f"builtin agent manifest '{manifest.id}' mcp_binding.servers must not contain duplicate server refs")
            for server_ref in manifest.mcp_binding.servers:
                if not server_ref.strip():
                    raise ValueError(f"builtin agent manifest '{manifest.id}' mcp_binding.servers refs must be non-empty")
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
