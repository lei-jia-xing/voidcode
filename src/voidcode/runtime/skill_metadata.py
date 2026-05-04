from __future__ import annotations

from collections.abc import Iterable
from typing import cast

from ..skills import SkillRegistry
from .config import RuntimeAgentConfig
from .skills import (
    SkillExecutionSnapshot,
    SkillRuntimeContext,
    build_runtime_context,
    snapshot_from_payload,
    snapshot_payload,
)


def loaded_skill_names(skill_registry: SkillRegistry) -> list[str]:
    # Builtin skills are catalog resources: they stay resolvable through the
    # skill tool and selected workflow refs, but they are not workspace skills
    # that were actively loaded for an ordinary run.
    return sorted(
        skill_name
        for skill_name, skill in skill_registry.skills.items()
        if skill.origin != "builtin"
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
    parsed_names: list[str] = []
    for index, raw_name in enumerate(cast(list[object], raw_skills)):
        if not isinstance(raw_name, str) or not raw_name:
            raise ValueError(f"request metadata '{key}[{index}]' must be a non-empty string")
        parsed_names.append(raw_name)
    return tuple(parsed_names)


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
        if not persisted_selected_explicit and not manifest_skill_refs:
            manifest_skill_refs = agent.manifest_skill_refs

    if request_skill_names is None:
        if persisted_selected_explicit:
            return manifest_skill_refs
        return manifest_skill_refs if manifest_skill_refs else None

    selected_names: list[str] = []
    for skill_name in (*manifest_skill_refs, *request_skill_names):
        if skill_name not in selected_names:
            selected_names.append(skill_name)
    return tuple(selected_names)


def fresh_request_metadata(metadata: dict[str, object]) -> dict[str, object]:
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
    for index, raw_name in enumerate(cast(list[object], raw_skill_names)):
        if not isinstance(raw_name, str):
            raise ValueError(f"persisted selected skill names[{index}] must be a string")
        selected_skill_names.append(raw_name)
    return tuple(selected_skill_names)


def snapshot_to_session_metadata(snapshot: SkillExecutionSnapshot) -> dict[str, object]:
    return {
        "selected_skill_names": list(snapshot.selected_skill_names),
        "applied_skills": [payload["name"] for payload in snapshot.applied_skill_payloads],
        "skill_snapshot": snapshot_payload(snapshot),
    }


def force_loaded_skill_payloads(
    snapshot: SkillExecutionSnapshot,
) -> tuple[dict[str, object], ...]:
    payloads: list[dict[str, object]] = []
    for payload in snapshot.applied_skill_payloads:
        payloads.append(
            {
                "name": payload.get("name"),
                "source": "force_load",
                "source_path": payload.get("source_path"),
            }
        )
    return tuple(payloads)


def skill_snapshot_from_metadata(
    metadata: dict[str, object],
) -> SkillExecutionSnapshot | None:
    raw_snapshot = metadata.get("skill_snapshot")
    if isinstance(raw_snapshot, dict):
        return snapshot_from_payload(cast(dict[str, object], raw_snapshot))
    return None


def skill_binding_snapshot_from_agent_capability_snapshot(
    capability_snapshot: dict[str, object],
) -> dict[str, object]:
    snapshot: dict[str, object] = {}
    execution = capability_snapshot.get("execution")
    if isinstance(execution, dict):
        execution_payload = cast(dict[str, object], execution)
        execution_key_map = {
            "execution_engine": "execution_engine",
            "model": "model",
            "fallback_models": "fallback_models",
            "resolved_provider": "resolved_provider",
            "reasoning_effort": "reasoning_effort",
        }
        for source_key, target_key in execution_key_map.items():
            if source_key in execution_payload:
                snapshot[target_key] = execution_payload[source_key]
    agent = capability_snapshot.get("agent")
    if isinstance(agent, dict):
        snapshot["agent"] = cast(dict[str, object], agent)
    runtime = capability_snapshot.get("runtime")
    if isinstance(runtime, dict):
        runtime_payload = cast(dict[str, object], runtime)
        for key in (
            "approval_mode",
            "max_steps",
            "tool_timeout_seconds",
            "permission",
        ):
            if key in runtime_payload:
                snapshot[key] = runtime_payload[key]
    mcp = capability_snapshot.get("mcp")
    if isinstance(mcp, dict):
        snapshot["mcp"] = cast(dict[str, object], mcp)
    return snapshot


def available_runtime_contexts(
    skill_registry: SkillRegistry,
    skill_names: Iterable[str],
) -> tuple[SkillRuntimeContext, ...]:
    contexts: list[SkillRuntimeContext] = []
    for skill_name in skill_names:
        skill = skill_registry.skills.get(skill_name)
        if skill is None:
            continue
        contexts.append(build_runtime_context(skill))
    return tuple(contexts)


def catalog_skill_context(
    skill_registry: SkillRegistry,
    *,
    available_skill_names: tuple[str, ...],
    selected_skill_names: tuple[str, ...],
) -> str:
    names = selected_skill_names or available_skill_names
    if not names:
        return ""
    lines = [
        "Runtime skills catalog (recommended/visible).",
        "Load full instructions with tool: skill(name=...).",
        "",
        "<available_skills>",
    ]
    for skill_name in names:
        skill = skill_registry.skills.get(skill_name)
        if skill is None:
            continue
        lines.extend(
            (
                "  <skill>",
                f"    <name>{skill.name}</name>",
                f"    <description>{skill.description}</description>",
                f"    <location>{skill.entry_path.as_uri()}</location>",
                "  </skill>",
            )
        )
    lines.append("</available_skills>")
    return "\n".join(lines)


__all__ = [
    "available_runtime_contexts",
    "catalog_skill_context",
    "effective_selected_skill_names",
    "force_loaded_skill_payloads",
    "fresh_request_metadata",
    "loaded_skill_names",
    "persisted_selected_skill_names",
    "request_skill_names_from_metadata",
    "selected_skill_names_for_agent",
    "skill_binding_snapshot_from_agent_capability_snapshot",
    "skill_snapshot_from_metadata",
    "snapshot_to_session_metadata",
]
