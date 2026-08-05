from __future__ import annotations

import pytest

from voidcode.runtime.workflow import (
    WorkflowMode,
    WorkflowModeResolution,
    get_builtin_workflow_mode,
    list_builtin_workflow_modes,
    resolve_workflow_mode,
)
from voidcode.runtime.workflow_snapshot import (
    WorkflowSnapshotVersionError,
    workflow_snapshot_from_metadata,
)


def test_builtin_workflow_modes_define_public_catalog() -> None:
    modes = list_builtin_workflow_modes()

    assert tuple(mode.id for mode in modes) == (
        "default",
        "deep_work",
        "review",
        "product",
        "sustain",
    )
    assert all(isinstance(mode, WorkflowMode) for mode in modes)
    assert get_builtin_workflow_mode("default") == modes[0]
    assert get_builtin_workflow_mode("unknown") is None


def test_resolve_workflow_mode_uses_current_precedence() -> None:
    default = resolve_workflow_mode()
    request = resolve_workflow_mode(metadata_workflow_mode="deep_work")
    command = resolve_workflow_mode(
        command_workflow_mode="product",
        metadata_workflow_mode="deep_work",
    )

    assert isinstance(default, WorkflowModeResolution)
    assert (default.workflow_mode, default.source) == ("default", "default")
    assert (request.workflow_mode, request.source) == ("deep_work", "workflow_mode")
    assert (command.workflow_mode, command.source) == ("product", "command")


def test_resolve_workflow_mode_rejects_unknown_mode() -> None:
    with pytest.raises(ValueError, match="unknown workflow_mode.*banana"):
        resolve_workflow_mode(metadata_workflow_mode="banana")


def test_workflow_mode_rejects_unknown_hook_preset_refs() -> None:
    with pytest.raises(ValueError, match="missing_hook"):
        WorkflowMode(
            id="deep_work",
            description="Invalid deep work mode.",
            hook_preset_refs=("missing_hook",),
        )


def test_workflow_snapshot_reads_current_contract_from_supported_locations() -> None:
    snapshot = {
        "snapshot_version": 2,
        "requested": {"workflow_mode": "review"},
        "effective": {"mode": "review", "source": "workflow_mode"},
        "mode": "review",
        "source": "workflow_mode",
    }

    for metadata in (
        {"workflow": snapshot},
        {"runtime_config": {"workflow": snapshot}},
        {"agent_capability_snapshot": {"workflow": snapshot}},
    ):
        assert workflow_snapshot_from_metadata(metadata) == snapshot


@pytest.mark.parametrize(
    "snapshot",
    [
        {},
        {"snapshot_version": 1},
        {"snapshot_version": 2, "requested": {}, "effective": {}},
        {
            "snapshot_version": 2,
            "requested": {"workflow_mode": "review"},
            "effective": {"mode": "deep_work"},
            "mode": "deep_work",
        },
    ],
)
def test_workflow_snapshot_rejects_non_current_contract(
    snapshot: dict[str, object],
) -> None:
    with pytest.raises(WorkflowSnapshotVersionError):
        workflow_snapshot_from_metadata({"workflow": snapshot})
