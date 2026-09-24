from __future__ import annotations

from collections.abc import Mapping

from .config import RuntimeAgentConfig
from .contracts import _parse_string_list
from .session_metadata_helpers import parse_skill_snapshot_metadata
from .skills import (
    SkillExecutionSnapshot,
    snapshot_from_payload,
    snapshot_payload,
)


def request_skill_names_from_metadata(
    metadata: dict[str, object] | None,
    *,
    key: str,
) -> tuple[str, ...] | None:
    if metadata is None or key not in metadata:
        return None
    raw_skills = metadata[key]
    if not isinstance(raw_skills, list):
        raise ValueError(f"request metadata '{key}' must be a list of skill names")
    return tuple(_parse_string_list(raw_skills, field=f"request metadata '{key}'"))


def effective_selected_skill_names(
    selected_skill_names: tuple[str, ...] | None,
    force_load_skill_names: tuple[str, ...] | None,
) -> tuple[str, ...] | None:
    if force_load_skill_names is None:
        return selected_skill_names

    merged_names: list[str] = []
    for skill_name in (*(selected_skill_names or ()), *force_load_skill_names):
        if skill_name not in merged_names:
            merged_names.append(skill_name)
    return tuple(merged_names)


def selected_skill_names_for_agent(
    agent: RuntimeAgentConfig | None,
    *,
    request_skill_names: tuple[str, ...] | None,
    persisted_selected_skill_names: tuple[str, ...] | None = None,
) -> tuple[str, ...] | None:
    manifest_skill_refs: tuple[str, ...] = ()
    persisted_selected_explicit = persisted_selected_skill_names is not None
    if persisted_selected_skill_names is not None:
        manifest_skill_refs = persisted_selected_skill_names
    if agent is not None:
        internal = agent.runtime_internal
        if not persisted_selected_explicit and not manifest_skill_refs and internal is not None:
            manifest_skill_refs = internal.manifest_skill_refs

    if request_skill_names is None:
        if persisted_selected_explicit:
            return manifest_skill_refs
        return manifest_skill_refs if manifest_skill_refs else None

    selected_names: list[str] = []
    for skill_name in (*manifest_skill_refs, *request_skill_names):
        if skill_name not in selected_names:
            selected_names.append(skill_name)
    return tuple(selected_names)


def fresh_request_metadata(metadata: Mapping[str, object]) -> dict[str, object]:
    sanitized = dict(metadata)
    sanitized.pop("applied_skills", None)
    sanitized.pop("applied_skill_payloads", None)
    sanitized.pop("selected_skill_names", None)
    sanitized.pop("skill_snapshot", None)
    return sanitized


def persisted_selected_skill_names(metadata: dict[str, object]) -> tuple[str, ...] | None:
    if "selected_skill_names" not in metadata:
        return None
    raw_skill_names = metadata["selected_skill_names"]
    if not isinstance(raw_skill_names, list):
        raise ValueError("persisted selected skill names must be a list")

    selected_skill_names: list[str] = []
    for index, raw_name in enumerate(raw_skill_names):
        if not isinstance(raw_name, str):
            raise ValueError(f"persisted selected skill names[{index}] must be a string")
        selected_skill_names.append(raw_name)
    return tuple(selected_skill_names)


def snapshot_to_session_metadata(snapshot: SkillExecutionSnapshot) -> dict[str, object]:
    return {
        "selected_skill_names": list(snapshot.selected_skill_names),
        "applied_skills": [payload["name"] for payload in snapshot.applied_skill_payloads],
        "skill_snapshot": parse_skill_snapshot_metadata(snapshot_payload(snapshot)),
    }


def skill_snapshot_from_metadata(
    metadata: dict[str, object],
) -> SkillExecutionSnapshot | None:
    if "skill_snapshot" not in metadata:
        return None
    raw_snapshot = metadata["skill_snapshot"]
    if not isinstance(raw_snapshot, dict):
        raise ValueError("persisted skill_snapshot must be an object")
    return snapshot_from_payload(raw_snapshot)


def skill_binding_snapshot_from_agent_capability_snapshot(
    capability_snapshot: dict[str, object],
) -> dict[str, object]:
    snapshot: dict[str, object] = {}
    execution = capability_snapshot.get("execution")
    if isinstance(execution, dict):
        execution_key_map = {
            "execution_engine": "execution_engine",
            "model": "model",
            "fallback_models": "fallback_models",
            "resolved_provider": "resolved_provider",
            "reasoning_effort": "reasoning_effort",
        }
        for source_key, target_key in execution_key_map.items():
            if source_key in execution:
                snapshot[target_key] = execution[source_key]
    agent = capability_snapshot.get("agent")
    if isinstance(agent, dict):
        snapshot["agent"] = agent
    runtime = capability_snapshot.get("runtime")
    if isinstance(runtime, dict):
        for key in (
            "approval_mode",
            "tool_timeout_seconds",
            "permission",
        ):
            if key in runtime:
                snapshot[key] = runtime[key]
    mcp = capability_snapshot.get("mcp")
    if isinstance(mcp, dict):
        snapshot["mcp"] = mcp
    return snapshot


__all__ = [
    "effective_selected_skill_names",
    "fresh_request_metadata",
    "persisted_selected_skill_names",
    "request_skill_names_from_metadata",
    "selected_skill_names_for_agent",
    "skill_binding_snapshot_from_agent_capability_snapshot",
    "skill_snapshot_from_metadata",
    "snapshot_to_session_metadata",
]
