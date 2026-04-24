from __future__ import annotations

from .models import AgentManifest, AgentManifestId
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
    "todo_write",
    "task",
    "background_cancel",
    "ast_grep_preview",
    "ast_grep_replace",
    "web_search",
    "web_fetch",
    "code_search",
    "mcp/*",
)

LEADER_AGENT_MANIFEST = AgentManifest(
    id="leader",
    name="Leader",
    mode="primary",
    description="Primary user-facing agent preset mapped to the runtime provider path.",
    prompt_profile="leader",
    execution_engine="provider",
    tool_allowlist=_LEADER_TOOL_ALLOWLIST,
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
)

ADVISOR_AGENT_MANIFEST = AgentManifest(
    id="advisor",
    name="Advisor",
    mode="subagent",
    description="Read-only advisory preset for architecture, risk, and review guidance.",
    prompt_profile="advisor",
    execution_engine="provider",
    tool_allowlist=_READ_ONLY_WORKSPACE_TOOLS,
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
)

PRODUCT_AGENT_MANIFEST = AgentManifest(
    id="product",
    name="Product",
    mode="subagent",
    description="Requirements-alignment preset for scope, acceptance, and product intent review.",
    prompt_profile="product",
    execution_engine="provider",
    tool_allowlist=("read_file", "list", "glob", "grep"),
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
