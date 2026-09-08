from __future__ import annotations

from typing import cast

from .background.routing import CALLABLE_SUBAGENT_PRESETS
from .config import RuntimeAgentConfig
from .session_metadata_helpers import parse_delegation_metadata
from .tool_provider import BUILTIN_TOOL_NAMES
from .tool_registry import ToolRegistry

AGENT_CAPABILITY_SNAPSHOT_VERSION = 3


class AgentCapabilitySnapshotVersionError(ValueError):
    """Raised when persisted agent capability materialization is not current."""


def validate_agent_capability_snapshot(
    snapshot: dict[str, object],
) -> dict[str, object]:
    version = snapshot.get("snapshot_version")
    if version != AGENT_CAPABILITY_SNAPSHOT_VERSION:
        raise AgentCapabilitySnapshotVersionError(
            f"unsupported agent_capability_snapshot snapshot_version: {version!r}; expected {AGENT_CAPABILITY_SNAPSHOT_VERSION!r}"
        )
    object_fields = (
        "precedence",
        "agent",
        "prompt",
        "tools",
        "skills",
        "hooks",
        "mcp",
        "delegation",
        "runtime",
        "execution",
    )
    for field in object_fields:
        if not isinstance(snapshot.get(field), dict):
            raise AgentCapabilitySnapshotVersionError(f"agent_capability_snapshot v2 requires a {field} object")
    tools = cast(dict[str, object], snapshot["tools"])
    required_tool_fields = {
        "manifest_allowlist",
        "request_allowlist",
        "request_default",
        "builtin_tools_enabled",
        "builtin_tool_names",
        "effective_names",
        "generation",
    }
    missing_tool_fields = sorted(required_tool_fields - tools.keys())
    if missing_tool_fields:
        raise AgentCapabilitySnapshotVersionError(f"agent_capability_snapshot v2 tools is missing required fields: {missing_tool_fields!r}")
    generation = tools["generation"]
    if not isinstance(generation, str) or not generation:
        raise AgentCapabilitySnapshotVersionError("agent_capability_snapshot v2 requires tools.generation")
    return snapshot


def agent_capability_agent_snapshot(
    agent: RuntimeAgentConfig | None,
    manifest: object | None,
) -> dict[str, object]:
    if agent is None:
        return {"preset": None}
    internal = agent.runtime_internal
    manifest_id = getattr(manifest, "id", None)
    return {
        "preset": agent.preset,
        "manifest_id": manifest_id if isinstance(manifest_id, str) else None,
        "mode": getattr(manifest, "mode", None),
        "source": "manifest" if manifest is not None else "runtime_config",
        "source_scope": internal.manifest_source_scope if internal is not None else None,
        "source_path": internal.manifest_source_path if internal is not None else None,
    }


def agent_capability_prompt_snapshot(
    agent: RuntimeAgentConfig | None,
    manifest: object | None,
    runtime_config_payload: dict[str, object],
) -> dict[str, object]:
    internal = agent.runtime_internal if agent is not None else None
    prompt: dict[str, object] = {
        "profile": agent.prompt_profile if agent is not None else None,
        "ref": internal.prompt_ref if internal is not None else None,
        "source": internal.prompt_source if internal is not None else None,
    }
    raw_agent = runtime_config_payload.get("agent")
    if isinstance(raw_agent, dict):
        raw_internal = cast(dict[str, object], raw_agent).get("runtime_internal")
        if isinstance(raw_internal, dict):
            raw_materialization = cast(dict[str, object], raw_internal).get("prompt_materialization")
            if isinstance(raw_materialization, dict):
                prompt["materialization"] = cast(dict[str, object], raw_materialization)
    if "materialization" not in prompt and manifest is not None:
        materialization = getattr(manifest, "prompt_materialization", None)
        if materialization is not None:
            materialization_profile = agent.prompt_profile if agent is not None else None
            prompt["materialization"] = materialization.to_payload(profile=materialization_profile)
    if "materialization" not in prompt and internal is not None and internal.prompt_materialization is not None:
        prompt["materialization"] = dict(internal.prompt_materialization)
    return {key: value for key, value in prompt.items() if value is not None}


def agent_capability_tool_snapshot(
    registry: ToolRegistry,
    agent: RuntimeAgentConfig | None,
    generation: str,
) -> dict[str, object]:
    internal = agent.runtime_internal if agent is not None else None
    manifest_allowlist = internal.manifest_tool_allowlist if internal is not None else ()
    return {
        "manifest_allowlist": list(manifest_allowlist),
        "request_allowlist": list(agent.tools.allowlist)
        if agent is not None and agent.tools is not None and agent.tools.allowlist is not None
        else None,
        "request_default": list(agent.tools.default) if agent is not None and agent.tools is not None and agent.tools.default is not None else None,
        "builtin_tools_enabled": not (
            agent is not None and agent.tools is not None and agent.tools.builtin is not None and agent.tools.builtin.enabled is False
        ),
        "builtin_tool_names": sorted(BUILTIN_TOOL_NAMES),
        "effective_names": sorted(registry.tools),
        "generation": generation,
    }


def agent_capability_delegation_snapshot(
    *,
    metadata: dict[str, object],
    parent_capability_snapshot: dict[str, object] | None,
) -> dict[str, object]:
    raw_delegation = metadata.get("delegation")
    delegation = parse_delegation_metadata(raw_delegation) if raw_delegation is not None else {}
    selected_preset = delegation.get("selected_preset")
    parent_delegation = (
        cast(dict[str, object], parent_capability_snapshot.get("delegation"))
        if parent_capability_snapshot is not None and isinstance(parent_capability_snapshot.get("delegation"), dict)
        else {}
    )
    parent_allowed = parent_delegation.get("allowed_child_presets")
    allowed_parent_presets = (
        tuple(item for item in cast(list[object], parent_allowed) if isinstance(item, str))
        if isinstance(parent_allowed, list)
        else CALLABLE_SUBAGENT_PRESETS
    )
    allowed_child_presets = [preset for preset in CALLABLE_SUBAGENT_PRESETS if preset in allowed_parent_presets]
    return {
        "selected_preset": selected_preset if isinstance(selected_preset, str) else None,
        "allowed_child_presets": allowed_child_presets,
        "denied": [],
        "parent_bounded": parent_capability_snapshot is not None,
        "can_expand_parent_policy": False,
    }


def agent_mcp_binding_payload(
    agent: RuntimeAgentConfig | None,
    manifest: object | None,
) -> dict[str, object]:
    binding = agent.mcp_binding if agent is not None else None
    if binding is None and manifest is not None:
        binding = getattr(manifest, "mcp_binding", None)
    return binding.to_payload() if binding is not None else {}
