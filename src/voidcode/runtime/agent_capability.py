from __future__ import annotations

from collections.abc import Mapping

from ..agent.models import AgentManifest
from .background.routing import CALLABLE_SUBAGENT_PRESETS
from .composition import CompositionRef
from .config import RuntimeAgentConfig
from .session_metadata_helpers import parse_delegation_metadata
from .tool_provider import BUILTIN_TOOL_NAMES
from .tool_registry import ToolRegistry

AGENT_CAPABILITY_SNAPSHOT_VERSION = 4


class AgentCapabilitySnapshotVersionError(ValueError):
    """Raised when persisted agent capability materialization is not current."""


def _require_exact_fields(
    value: Mapping[str, object],
    *,
    name: str,
    expected: frozenset[str],
) -> None:
    if frozenset(value) != expected:
        raise AgentCapabilitySnapshotVersionError(f"agent_capability_snapshot {name} fields are unsupported")


def validate_agent_capability_snapshot(
    snapshot: dict[str, object],
) -> dict[str, object]:
    version = snapshot.get("snapshot_version")
    if type(version) is not int or version != AGENT_CAPABILITY_SNAPSHOT_VERSION:
        raise AgentCapabilitySnapshotVersionError(
            f"unsupported agent_capability_snapshot snapshot_version: {version!r}; expected {AGENT_CAPABILITY_SNAPSHOT_VERSION!r}"
        )
    _require_exact_fields(
        snapshot,
        name="root",
        expected=frozenset(
            {
                "composition_ref",
                "snapshot_version",
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
            }
        ),
    )
    raw_composition_ref = snapshot["composition_ref"]
    if not isinstance(raw_composition_ref, dict):
        raise AgentCapabilitySnapshotVersionError("agent_capability_snapshot v4 requires a canonical composition_ref object")
    try:
        CompositionRef.model_validate(raw_composition_ref)
    except Exception as error:
        raise AgentCapabilitySnapshotVersionError("agent_capability_snapshot composition_ref is invalid") from error

    expected_sections = {
        "precedence": frozenset({"order", "notes"}),
        "skills": frozenset({"manifest_refs", "selected_names", "force_loaded_names", "scope"}),
        "hooks": frozenset({"manifest_refs", "resolved_refs", "snapshot", "materialization", "authority"}),
        "mcp": frozenset({"binding_intent", "configured_enabled", "mode", "configured_servers", "governance"}),
        "delegation": frozenset({"selected_preset", "allowed_child_presets", "denied", "parent_bounded", "can_expand_parent_policy"}),
        "runtime": frozenset({"approval_mode", "tool_timeout_seconds", "permission"}),
        "execution": frozenset({"execution_engine", "model", "fallback_models", "resolved_provider", "reasoning_effort"}),
        "tools": frozenset(
            {
                "manifest_allowlist",
                "request_allowlist",
                "request_default",
                "builtin_tools_enabled",
                "builtin_tool_names",
                "effective_names",
                "generation",
            }
        ),
    }
    sections: dict[str, Mapping[str, object]] = {}
    for name, expected in expected_sections.items():
        value = snapshot[name]
        if not isinstance(value, dict):
            raise AgentCapabilitySnapshotVersionError(f"agent_capability_snapshot v4 requires a {name} object")
        _require_exact_fields(value, name=name, expected=expected)
        sections[name] = value

    precedence = sections["precedence"]
    notes = precedence["notes"]
    if not isinstance(notes, dict):
        raise AgentCapabilitySnapshotVersionError("agent_capability_snapshot precedence.notes must be an object")
    _require_exact_fields(notes, name="precedence.notes", expected=frozenset({"skills", "hooks", "mcp"}))

    agent = snapshot["agent"]
    if not isinstance(agent, dict) or frozenset(agent) not in (
        frozenset({"preset"}),
        frozenset({"preset", "manifest_id", "mode", "source", "source_scope", "source_path"}),
    ):
        raise AgentCapabilitySnapshotVersionError("agent_capability_snapshot agent fields are unsupported")
    prompt = snapshot["prompt"]
    if not isinstance(prompt, dict) or not frozenset(prompt).issubset({"profile", "ref", "source", "materialization"}):
        raise AgentCapabilitySnapshotVersionError("agent_capability_snapshot prompt fields are unsupported")

    generation = sections["tools"]["generation"]
    if not isinstance(generation, str) or not generation:
        raise AgentCapabilitySnapshotVersionError("agent_capability_snapshot v4 requires tools.generation")
    return snapshot


def composition_ref_from_session_metadata(metadata: Mapping[str, object]) -> CompositionRef:
    raw_ref = metadata.get("composition_ref")
    raw_snapshot = metadata.get("agent_capability_snapshot")
    if not isinstance(raw_snapshot, dict):
        raise AgentCapabilitySnapshotVersionError("persisted session requires agent_capability_snapshot")
    validate_agent_capability_snapshot(raw_snapshot)
    ref = CompositionRef.model_validate(raw_ref)
    if CompositionRef.model_validate(raw_snapshot["composition_ref"]) != ref:
        raise AgentCapabilitySnapshotVersionError("session and capability composition references disagree")
    return ref


def agent_capability_agent_snapshot(
    agent: RuntimeAgentConfig | None,
    manifest: AgentManifest | None,
) -> dict[str, object]:
    if agent is None:
        return {"preset": None}
    internal = agent.runtime_internal
    return {
        "preset": agent.preset,
        "manifest_id": manifest.id if manifest is not None else None,
        "mode": manifest.mode if manifest is not None else None,
        "source": "manifest" if manifest is not None else "runtime_config",
        "source_scope": internal.manifest_source_scope if internal is not None else None,
        "source_path": internal.manifest_source_path if internal is not None else None,
    }


def agent_capability_prompt_snapshot(
    agent: RuntimeAgentConfig | None,
    manifest: AgentManifest | None,
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
        raw_internal = raw_agent.get("runtime_internal")
        if isinstance(raw_internal, dict):
            raw_materialization = raw_internal.get("prompt_materialization")
            if isinstance(raw_materialization, dict):
                prompt["materialization"] = raw_materialization
    if "materialization" not in prompt:
        materialization = internal.prompt_materialization if internal is not None else None
        if materialization is not None:
            materialization_profile = agent.prompt_profile if agent is not None else None
            prompt["materialization"] = materialization.to_payload(profile=materialization_profile)
        elif manifest is not None and manifest.prompt_materialization is not None:
            materialization_profile = agent.prompt_profile if agent is not None else None
            prompt["materialization"] = manifest.prompt_materialization.to_payload(profile=materialization_profile)
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
        "effective_names": sorted(registry.declarations),
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
    raw_parent_delegation = parent_capability_snapshot.get("delegation") if parent_capability_snapshot is not None else None
    parent_delegation: Mapping[str, object] = raw_parent_delegation if isinstance(raw_parent_delegation, dict) else {}
    parent_allowed = parent_delegation.get("allowed_child_presets")
    allowed_parent_presets = (
        tuple(item for item in parent_allowed if isinstance(item, str)) if isinstance(parent_allowed, list) else CALLABLE_SUBAGENT_PRESETS
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
    manifest: AgentManifest | None,
) -> dict[str, object]:
    binding = agent.mcp_binding if agent is not None else None
    if binding is None and manifest is not None:
        binding = manifest.mcp_binding
    return binding.to_payload() if binding is not None else {}
