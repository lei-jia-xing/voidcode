from __future__ import annotations

from typing import cast

from .config import RuntimeAgentConfig
from .policy import PRODUCT_DELEGATION_DENIAL_REASON
from .tool_provider import BUILTIN_TOOL_NAMES
from .tool_registry import ToolRegistry

AGENT_CAPABILITY_SNAPSHOT_VERSION = 2


class AgentCapabilitySnapshotVersionError(ValueError):
    """Raised when persisted agent capability materialization is not current."""


def validate_agent_capability_snapshot(
    snapshot: dict[str, object],
) -> dict[str, object]:
    version = snapshot.get("snapshot_version")
    if version != AGENT_CAPABILITY_SNAPSHOT_VERSION:
        raise AgentCapabilitySnapshotVersionError(
            "unsupported agent_capability_snapshot snapshot_version: "
            f"{version!r}; expected {AGENT_CAPABILITY_SNAPSHOT_VERSION!r}"
        )
    tools = snapshot.get("tools")
    if not isinstance(tools, dict):
        raise AgentCapabilitySnapshotVersionError(
            "agent_capability_snapshot v2 requires a tools object"
        )
    generation = cast(dict[str, object], tools).get("generation")
    if not isinstance(generation, str) or not generation:
        raise AgentCapabilitySnapshotVersionError(
            "agent_capability_snapshot v2 requires tools.generation"
        )
    return snapshot


def agent_capability_agent_snapshot(
    agent: RuntimeAgentConfig | None,
    manifest: object | None,
) -> dict[str, object]:
    if agent is None:
        return {"preset": None}
    manifest_id = getattr(manifest, "id", None)
    return {
        "preset": agent.preset,
        "manifest_id": manifest_id if isinstance(manifest_id, str) else None,
        "mode": getattr(manifest, "mode", None),
        "source": "manifest" if manifest is not None else "runtime_config",
        "source_scope": agent.manifest_source_scope,
        "source_path": agent.manifest_source_path,
    }


def agent_capability_prompt_snapshot(
    agent: RuntimeAgentConfig | None,
    manifest: object | None,
    runtime_config_payload: dict[str, object],
) -> dict[str, object]:
    prompt: dict[str, object] = {
        "profile": agent.prompt_profile if agent is not None else None,
        "ref": agent.prompt_ref if agent is not None else None,
        "source": agent.prompt_source if agent is not None else None,
    }
    raw_agent = runtime_config_payload.get("agent")
    if isinstance(raw_agent, dict):
        raw_materialization = cast(dict[str, object], raw_agent).get("prompt_materialization")
        if isinstance(raw_materialization, dict):
            prompt["materialization"] = cast(dict[str, object], raw_materialization)
    if "materialization" not in prompt and manifest is not None:
        materialization = getattr(manifest, "prompt_materialization", None)
        if materialization is not None:
            prompt["materialization"] = materialization.to_payload(
                profile=agent.prompt_profile if agent is not None else None
            )
    if (
        "materialization" not in prompt
        and agent is not None
        and agent.prompt_materialization is not None
    ):
        prompt["materialization"] = dict(agent.prompt_materialization)
    return {key: value for key, value in prompt.items() if value is not None}


def agent_capability_tool_snapshot(
    registry: ToolRegistry,
    agent: RuntimeAgentConfig | None,
    generation: str,
) -> dict[str, object]:
    manifest_allowlist = agent.manifest_tool_allowlist if agent is not None else ()
    return {
        "manifest_allowlist": list(manifest_allowlist),
        "request_allowlist": list(agent.tools.allowlist)
        if agent is not None and agent.tools is not None and agent.tools.allowlist is not None
        else None,
        "request_default": list(agent.tools.default)
        if agent is not None and agent.tools is not None and agent.tools.default is not None
        else None,
        "builtin_tools_enabled": not (
            agent is not None
            and agent.tools is not None
            and agent.tools.builtin is not None
            and agent.tools.builtin.enabled is False
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
    delegation = cast(dict[str, object], raw_delegation) if isinstance(raw_delegation, dict) else {}
    selected_preset = delegation.get("selected_preset")
    parent_delegation = (
        cast(dict[str, object], parent_capability_snapshot.get("delegation"))
        if parent_capability_snapshot is not None
        and isinstance(parent_capability_snapshot.get("delegation"), dict)
        else {}
    )
    parent_allowed = parent_delegation.get("allowed_child_presets")
    allowed_parent_presets = (
        tuple(item for item in cast(list[object], parent_allowed) if isinstance(item, str))
        if isinstance(parent_allowed, list)
        else ("advisor", "explore", "researcher", "worker")
    )
    allowed_child_presets = [
        preset
        for preset in ("advisor", "explore", "researcher", "worker")
        if preset in allowed_parent_presets
    ]
    return {
        "selected_preset": selected_preset if isinstance(selected_preset, str) else None,
        "allowed_child_presets": allowed_child_presets,
        "denied": [{"target": "product", "reason": PRODUCT_DELEGATION_DENIAL_REASON}],
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
