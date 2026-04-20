from __future__ import annotations

from .models import AgentManifest, AgentManifestId

LEADER_AGENT_MANIFEST = AgentManifest(
    id="leader",
    name="Leader",
    mode="primary",
    description="Primary user-facing agent preset mapped to the current single-agent path.",
    prompt_profile="leader",
    execution_engine="single_agent",
)

_BUILTIN_AGENT_MANIFESTS: dict[AgentManifestId, AgentManifest] = {
    LEADER_AGENT_MANIFEST.id: LEADER_AGENT_MANIFEST
}


def get_builtin_agent_manifest(agent_id: str) -> AgentManifest | None:
    if agent_id not in _BUILTIN_AGENT_MANIFESTS:
        return None
    return _BUILTIN_AGENT_MANIFESTS[agent_id]


def list_builtin_agent_manifests() -> tuple[AgentManifest, ...]:
    return tuple(_BUILTIN_AGENT_MANIFESTS.values())
