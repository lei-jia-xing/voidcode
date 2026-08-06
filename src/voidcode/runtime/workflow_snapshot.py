from __future__ import annotations

from collections.abc import Mapping
from typing import cast

WORKFLOW_SNAPSHOT_VERSION = 2


class WorkflowSnapshotVersionError(ValueError):
    """Raised when persisted workflow metadata is not the current contract."""


def workflow_snapshot_from_metadata(
    metadata: dict[str, object] | None,
) -> dict[str, object] | None:
    if metadata is None:
        return None
    if "workflow" in metadata:
        raw_workflow = metadata["workflow"]
        if not isinstance(raw_workflow, dict):
            raise WorkflowSnapshotVersionError("persisted workflow snapshot must be an object")
        return validate_workflow_snapshot(cast(dict[str, object], raw_workflow))
    raw_runtime_config = metadata.get("runtime_config")
    if isinstance(raw_runtime_config, dict):
        runtime_config = cast(dict[str, object], raw_runtime_config)
        if "workflow" in runtime_config:
            runtime_workflow = runtime_config["workflow"]
            if not isinstance(runtime_workflow, dict):
                raise WorkflowSnapshotVersionError("persisted runtime_config.workflow snapshot must be an object")
            return validate_workflow_snapshot(cast(dict[str, object], runtime_workflow))
    raw_capability_snapshot = metadata.get("agent_capability_snapshot")
    if isinstance(raw_capability_snapshot, dict):
        capability_snapshot = cast(dict[str, object], raw_capability_snapshot)
        if "workflow" in capability_snapshot:
            capability_workflow = capability_snapshot["workflow"]
            if not isinstance(capability_workflow, dict):
                raise WorkflowSnapshotVersionError("persisted agent_capability_snapshot.workflow must be an object")
            return validate_workflow_snapshot(cast(dict[str, object], capability_workflow))
    return None


def validate_workflow_snapshot(snapshot: dict[str, object]) -> dict[str, object]:
    version = snapshot.get("snapshot_version")
    if version != WORKFLOW_SNAPSHOT_VERSION:
        raise WorkflowSnapshotVersionError(f"unsupported workflow snapshot_version: {version!r}; expected {WORKFLOW_SNAPSHOT_VERSION!r}")
    requested = _required_mapping(snapshot.get("requested"), field="requested")
    effective = _required_mapping(snapshot.get("effective"), field="effective")
    requested_mode = requested.get("workflow_mode")
    effective_mode = effective.get("mode")
    if not isinstance(requested_mode, str) or not requested_mode:
        raise WorkflowSnapshotVersionError("workflow snapshot v2 requires requested.workflow_mode")
    if effective_mode != requested_mode:
        raise WorkflowSnapshotVersionError("workflow snapshot v2 effective.mode must match requested.workflow_mode")
    if snapshot.get("mode") != effective_mode:
        raise WorkflowSnapshotVersionError("workflow snapshot v2 mode must match effective.mode")
    return snapshot


def _required_mapping(value: object, *, field: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise WorkflowSnapshotVersionError(f"workflow snapshot v2 requires {field} object")
    return cast(dict[str, object], value)


__all__ = [
    "WORKFLOW_SNAPSHOT_VERSION",
    "WorkflowSnapshotVersionError",
    "validate_workflow_snapshot",
    "workflow_snapshot_from_metadata",
]
