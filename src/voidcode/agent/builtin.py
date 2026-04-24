from __future__ import annotations

from .models import AgentManifest, AgentManifestId

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
    tool_allowlist=("web_search", "web_fetch", "code_search"),
)

PRODUCT_AGENT_MANIFEST = AgentManifest(
    id="product",
    name="Product",
    mode="subagent",
    description="Requirements-alignment preset for scope, acceptance, and product intent review.",
    prompt_profile="product",
    tool_allowlist=("read_file", "list", "glob", "grep"),
)

_BUILTIN_AGENT_MANIFESTS: dict[AgentManifestId, AgentManifest] = {
    LEADER_AGENT_MANIFEST.id: LEADER_AGENT_MANIFEST,
    WORKER_AGENT_MANIFEST.id: WORKER_AGENT_MANIFEST,
    ADVISOR_AGENT_MANIFEST.id: ADVISOR_AGENT_MANIFEST,
    EXPLORE_AGENT_MANIFEST.id: EXPLORE_AGENT_MANIFEST,
    RESEARCHER_AGENT_MANIFEST.id: RESEARCHER_AGENT_MANIFEST,
    PRODUCT_AGENT_MANIFEST.id: PRODUCT_AGENT_MANIFEST,
}


def get_builtin_agent_manifest(agent_id: str) -> AgentManifest | None:
    if agent_id not in _BUILTIN_AGENT_MANIFESTS:
        return None
    return _BUILTIN_AGENT_MANIFESTS[agent_id]


def list_builtin_agent_manifests() -> tuple[AgentManifest, ...]:
    return tuple(_BUILTIN_AGENT_MANIFESTS.values())
