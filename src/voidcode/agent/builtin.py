from __future__ import annotations

from types import MappingProxyType

from .models import AgentManifest, AgentManifestId, AgentPromptMaterialization
from .prompts import render_builtin_prompt_profile

_READ_ONLY_WORKSPACE_TOOLS = (
    "read_file",
    "list",
    "glob",
    "grep",
    "ast_grep_search",
    "lsp",
    "question",
    "skill",
    "background_output",
)

_LEADER_TOOL_ALLOWLIST = (
    *_READ_ONLY_WORKSPACE_TOOLS,
    "write_file",
    "edit",
    "multi_edit",
    "apply_patch",
    "shell_exec",
    "format_file",
    "task",
    "todo_write",
    "background_cancel",
    "ast_grep_preview",
    "ast_grep_replace",
    "web_search",
    "web_fetch",
    "code_search",
    "mcp/*",
)

_BUILTIN_PROMPT_MATERIALIZATION_VERSION = 1

LEADER_AGENT_MANIFEST = AgentManifest(
    id="leader",
    name="Leader",
    mode="primary",
    description="Primary user-facing agent preset mapped to the runtime provider path.",
    prompt_profile="leader",
    execution_engine="provider",
    tool_allowlist=_LEADER_TOOL_ALLOWLIST,
    top_level_selectable=True,
    prompt_materialization=AgentPromptMaterialization(
        profile="leader",
        version=_BUILTIN_PROMPT_MATERIALIZATION_VERSION,
        source="builtin",
        format="text",
        model_family_overrides=MappingProxyType({}),
    ),
)

WORKER_AGENT_MANIFEST = AgentManifest(
    id="worker",
    name="Worker",
    mode="subagent",
    description="Focused future executor preset for narrow implementation tasks.",
    prompt_profile="worker",
    execution_engine="provider",
    tool_allowlist=(
        *_READ_ONLY_WORKSPACE_TOOLS,
        "write_file",
        "edit",
        "multi_edit",
        "apply_patch",
        "shell_exec",
        "format_file",
        "task",
    ),
    top_level_selectable=False,
    prompt_materialization=AgentPromptMaterialization(
        profile="worker",
        version=_BUILTIN_PROMPT_MATERIALIZATION_VERSION,
        source="builtin",
        format="text",
        model_family_overrides=MappingProxyType({}),
    ),
)

ADVISOR_AGENT_MANIFEST = AgentManifest(
    id="advisor",
    name="Advisor",
    mode="subagent",
    description="Read-only advisory preset for architecture, risk, and review guidance.",
    prompt_profile="advisor",
    execution_engine="provider",
    tool_allowlist=_READ_ONLY_WORKSPACE_TOOLS,
    top_level_selectable=False,
    prompt_materialization=AgentPromptMaterialization(
        profile="advisor",
        version=_BUILTIN_PROMPT_MATERIALIZATION_VERSION,
        source="builtin",
        format="text",
        model_family_overrides=MappingProxyType({}),
    ),
)

EXPLORE_AGENT_MANIFEST = AgentManifest(
    id="explore",
    name="Explore",
    mode="subagent",
    description=(
        "Workspace-bound exploration preset for local code structure and pattern discovery."
    ),
    prompt_profile="explore",
    execution_engine="provider",
    tool_allowlist=_READ_ONLY_WORKSPACE_TOOLS,
    top_level_selectable=False,
    prompt_materialization=AgentPromptMaterialization(
        profile="explore",
        version=_BUILTIN_PROMPT_MATERIALIZATION_VERSION,
        source="builtin",
        format="text",
        model_family_overrides=MappingProxyType({}),
    ),
)

RESEARCHER_AGENT_MANIFEST = AgentManifest(
    id="researcher",
    name="Researcher",
    mode="subagent",
    description=(
        "External research preset for public docs, repositories, and implementation examples."
    ),
    prompt_profile="researcher",
    execution_engine="provider",
    tool_allowlist=("web_search", "web_fetch", "code_search"),
    top_level_selectable=False,
    prompt_materialization=AgentPromptMaterialization(
        profile="researcher",
        version=_BUILTIN_PROMPT_MATERIALIZATION_VERSION,
        source="builtin",
        format="text",
        model_family_overrides=MappingProxyType({}),
    ),
)

_PRODUCT_TOOL_ALLOWLIST = (
    *_READ_ONLY_WORKSPACE_TOOLS,
    "todo_write",
    "web_search",
    "web_fetch",
    "code_search",
)

PRODUCT_AGENT_MANIFEST = AgentManifest(
    id="product",
    name="Product",
    mode="primary",
    description=(
        "Planning agent preset for requirement discussion, scope shaping, "
        "acceptance criteria, and issue drafting."
    ),
    prompt_profile="product",
    execution_engine="provider",
    tool_allowlist=_PRODUCT_TOOL_ALLOWLIST,
    top_level_selectable=True,
    prompt_materialization=AgentPromptMaterialization(
        profile="product",
        version=2,
        source="builtin",
        format="text",
        model_family_overrides=MappingProxyType({}),
    ),
)


def validate_builtin_agent_manifests(
    manifests: tuple[AgentManifest, ...],
) -> tuple[AgentManifest, ...]:
    manifest_ids: set[AgentManifestId] = set()
    for manifest in manifests:
        if manifest.id in manifest_ids:
            raise ValueError(f"duplicate builtin agent manifest id: {manifest.id}")
        manifest_ids.add(manifest.id)
        if not manifest.name.strip():
            raise ValueError(
                f"builtin agent manifest '{manifest.id}' must declare a non-empty name"
            )
        if not manifest.description.strip():
            raise ValueError(
                f"builtin agent manifest '{manifest.id}' must declare a non-empty description"
            )
        if manifest.prompt_profile is None or not manifest.prompt_profile.strip():
            raise ValueError(
                f"builtin agent manifest '{manifest.id}' must declare a builtin prompt_profile"
            )
        if render_builtin_prompt_profile(manifest.prompt_profile) is None:
            raise ValueError(
                f"builtin agent manifest '{manifest.id}' references unknown prompt profile "
                f"'{manifest.prompt_profile}'"
            )
        if manifest.execution_engine is None:
            raise ValueError(
                f"builtin agent manifest '{manifest.id}' must declare an execution_engine"
            )
        if len(manifest.tool_allowlist) != len(set(manifest.tool_allowlist)):
            raise ValueError(
                f"builtin agent manifest '{manifest.id}' must not contain duplicate tool patterns"
            )
        if manifest.mode == "primary" and not manifest.top_level_selectable:
            raise ValueError(
                f"builtin agent manifest '{manifest.id}' has mode='primary' but is not "
                "marked top_level_selectable"
            )
        if manifest.mode == "subagent" and manifest.top_level_selectable:
            raise ValueError(
                f"builtin agent manifest '{manifest.id}' has mode='subagent' but is marked "
                "top_level_selectable; subagent presets must not be top-level selectable"
            )
        if manifest.prompt_materialization is None:
            raise ValueError(
                f"builtin agent manifest '{manifest.id}' must declare prompt_materialization"
            )
        materialization = manifest.prompt_materialization
        if render_builtin_prompt_profile(materialization.profile) is None:
            raise ValueError(
                f"builtin agent manifest '{manifest.id}' prompt_materialization.profile "
                f"references unknown prompt profile '{materialization.profile}'"
            )
        if materialization.profile != manifest.prompt_profile:
            raise ValueError(
                f"builtin agent manifest '{manifest.id}' prompt_materialization.profile "
                f"'{materialization.profile}' must match prompt_profile "
                f"'{manifest.prompt_profile}'"
            )
        for family, override_profile in materialization.model_family_overrides.items():
            if render_builtin_prompt_profile(override_profile) is None:
                raise ValueError(
                    f"builtin agent manifest '{manifest.id}' prompt_materialization "
                    f"model_family_overrides[{family!r}] references unknown prompt profile "
                    f"'{override_profile}'"
                )
    return manifests


_VALIDATED_BUILTIN_AGENT_MANIFESTS = validate_builtin_agent_manifests(
    (
        LEADER_AGENT_MANIFEST,
        WORKER_AGENT_MANIFEST,
        ADVISOR_AGENT_MANIFEST,
        EXPLORE_AGENT_MANIFEST,
        RESEARCHER_AGENT_MANIFEST,
        PRODUCT_AGENT_MANIFEST,
    )
)

_BUILTIN_AGENT_MANIFESTS: dict[AgentManifestId, AgentManifest] = {
    manifest.id: manifest for manifest in _VALIDATED_BUILTIN_AGENT_MANIFESTS
}


def get_builtin_agent_manifest(agent_id: str) -> AgentManifest | None:
    if agent_id not in _BUILTIN_AGENT_MANIFESTS:
        return None
    return _BUILTIN_AGENT_MANIFESTS[agent_id]


def list_builtin_agent_manifests() -> tuple[AgentManifest, ...]:
    return tuple(_BUILTIN_AGENT_MANIFESTS.values())


def is_agent_top_level_selectable(agent_id: str) -> bool:
    manifest = get_builtin_agent_manifest(agent_id)
    if manifest is None:
        return False
    return manifest.top_level_selectable


def list_top_level_selectable_agent_manifests() -> tuple[AgentManifest, ...]:
    return tuple(
        manifest for manifest in _BUILTIN_AGENT_MANIFESTS.values() if manifest.top_level_selectable
    )
