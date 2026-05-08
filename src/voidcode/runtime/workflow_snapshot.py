from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from .tool_registry import ToolRegistry

_SNAPSHOT_VERSION = 2
_EFFECTIVE_FIELDS = (
    "mode",
    "legacy_preset",
    "source",
    "category",
    "default_agent",
    "effective_agent",
    "read_only_default",
    "prompt_append",
    "hook_preset_refs",
    "skill_refs",
    "force_load_skills",
    "mcp_binding_intents",
    "verification_guidance",
)


def workflow_snapshot_from_metadata(
    metadata: dict[str, object] | None,
) -> dict[str, object] | None:
    if metadata is None:
        return None
    raw_workflow = metadata.get("workflow")
    if isinstance(raw_workflow, dict):
        return _normalized_workflow_snapshot(cast(dict[str, object], raw_workflow), metadata)
    raw_runtime_config = metadata.get("runtime_config")
    if isinstance(raw_runtime_config, dict):
        runtime_config = cast(dict[str, object], raw_runtime_config)
        runtime_workflow = runtime_config.get("workflow")
        if isinstance(runtime_workflow, dict):
            return _normalized_workflow_snapshot(
                cast(dict[str, object], runtime_workflow),
                metadata,
            )
    raw_capability_snapshot = metadata.get("agent_capability_snapshot")
    if isinstance(raw_capability_snapshot, dict):
        capability_snapshot = cast(dict[str, object], raw_capability_snapshot)
        capability_workflow = capability_snapshot.get("workflow")
        if isinstance(capability_workflow, dict):
            return _normalized_workflow_snapshot(
                cast(dict[str, object], capability_workflow),
                metadata,
            )
    if _has_workflow_selector(metadata):
        return _normalized_workflow_snapshot({}, metadata)
    return None


def workflow_snapshot_selected_preset(snapshot: dict[str, object]) -> str | None:
    raw_effective = snapshot.get("effective")
    if isinstance(raw_effective, dict):
        effective = cast(dict[str, object], raw_effective)
        legacy_preset = effective.get("legacy_preset")
        if isinstance(legacy_preset, str) and legacy_preset:
            return legacy_preset
    selected_preset = snapshot.get("selected_preset")
    return selected_preset if isinstance(selected_preset, str) and selected_preset else None


def read_only_workflow_tool_names(registry: ToolRegistry) -> tuple[str, ...]:
    return tuple(name for name, tool in registry.tools.items() if tool.definition.read_only)


def _normalized_workflow_snapshot(
    raw_snapshot: Mapping[str, object],
    metadata: Mapping[str, object],
) -> dict[str, object]:
    snapshot = dict(raw_snapshot)
    raw_requested = snapshot.get("requested")
    requested = (
        dict(cast(dict[str, object], raw_requested)) if isinstance(raw_requested, dict) else {}
    )
    if "workflow_mode" not in requested:
        requested["workflow_mode"] = _string_or_none(metadata.get("workflow_mode"))
    if "workflow_preset" not in requested:
        requested["workflow_preset"] = _first_string(
            snapshot.get("selected_preset"),
            metadata.get("workflow_preset"),
        )

    raw_effective = snapshot.get("effective")
    effective = (
        dict(cast(dict[str, object], raw_effective))
        if isinstance(raw_effective, dict)
        else _effective_from_legacy_snapshot(snapshot)
    )
    if not _string_or_none(effective.get("mode")):
        effective["mode"] = _string_or_none(requested.get("workflow_mode"))
    if not _string_or_none(effective.get("legacy_preset")):
        effective["legacy_preset"] = _first_string(
            snapshot.get("selected_preset"),
            requested.get("workflow_preset"),
            metadata.get("workflow_preset"),
        )
    if "source" not in effective:
        effective["source"] = _first_string(snapshot.get("source"), snapshot.get("preset_source"))

    normalized = dict(snapshot)
    normalized["snapshot_version"] = _snapshot_version(snapshot.get("snapshot_version"))
    normalized["requested"] = requested
    normalized["effective"] = effective
    _promote_effective_fields(normalized, effective)
    selected_preset = workflow_snapshot_selected_preset(normalized)
    if selected_preset is not None:
        normalized["selected_preset"] = selected_preset
    return normalized


def _effective_from_legacy_snapshot(snapshot: Mapping[str, object]) -> dict[str, object]:
    effective: dict[str, object] = {}
    for field in _EFFECTIVE_FIELDS:
        if field in snapshot:
            effective[field] = snapshot[field]
    if "legacy_preset" not in effective:
        effective["legacy_preset"] = _string_or_none(snapshot.get("selected_preset"))
    if "source" not in effective:
        effective["source"] = _first_string(snapshot.get("source"), snapshot.get("preset_source"))
    return effective


def _promote_effective_fields(
    normalized: dict[str, object],
    effective: Mapping[str, object],
) -> None:
    for field in _EFFECTIVE_FIELDS:
        if field in effective:
            normalized[field] = effective[field]


def _has_workflow_selector(metadata: Mapping[str, object]) -> bool:
    return _first_string(metadata.get("workflow_mode"), metadata.get("workflow_preset")) is not None


def _snapshot_version(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return _SNAPSHOT_VERSION


def _first_string(*values: object) -> str | None:
    for value in values:
        if isinstance(value, str) and value:
            return value
    return None


def _string_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
