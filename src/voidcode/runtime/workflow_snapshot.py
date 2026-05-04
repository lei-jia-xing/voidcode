from __future__ import annotations

from typing import cast

from .tool_registry import ToolRegistry


def workflow_snapshot_from_metadata(
    metadata: dict[str, object] | None,
) -> dict[str, object] | None:
    if metadata is None:
        return None
    raw_workflow = metadata.get("workflow")
    if isinstance(raw_workflow, dict):
        return dict(cast(dict[str, object], raw_workflow))
    raw_runtime_config = metadata.get("runtime_config")
    if isinstance(raw_runtime_config, dict):
        runtime_config = cast(dict[str, object], raw_runtime_config)
        runtime_workflow = runtime_config.get("workflow")
        if isinstance(runtime_workflow, dict):
            return dict(cast(dict[str, object], runtime_workflow))
    raw_capability_snapshot = metadata.get("agent_capability_snapshot")
    if isinstance(raw_capability_snapshot, dict):
        capability_snapshot = cast(dict[str, object], raw_capability_snapshot)
        capability_workflow = capability_snapshot.get("workflow")
        if isinstance(capability_workflow, dict):
            return dict(cast(dict[str, object], capability_workflow))
    return None


def workflow_snapshot_selected_preset(snapshot: dict[str, object]) -> str | None:
    selected_preset = snapshot.get("selected_preset")
    return selected_preset if isinstance(selected_preset, str) and selected_preset else None


def read_only_workflow_tool_names(registry: ToolRegistry) -> tuple[str, ...]:
    return tuple(name for name, tool in registry.tools.items() if tool.definition.read_only)
