from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from ..hook.presets import validate_hook_preset_refs

type WorkflowModeId = Literal["default", "deep_work", "review", "product", "sustain"]
type WorkflowModeKey = WorkflowModeId | str

_WORKFLOW_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*$")


@dataclass(frozen=True, slots=True)
class WorkflowMode:
    id: WorkflowModeKey
    description: str
    hook_preset_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not _WORKFLOW_ID_PATTERN.fullmatch(self.id):
            raise ValueError(
                f"WorkflowMode.id value {self.id!r} must match {_WORKFLOW_ID_PATTERN.pattern!r}"
            )
        if not self.description.strip():
            raise ValueError(f"workflow mode '{self.id}' must declare a description")
        if len(self.hook_preset_refs) != len(set(self.hook_preset_refs)):
            raise ValueError(
                f"workflow mode '{self.id}' hook_preset_refs must not contain duplicates"
            )
        if any(not ref.strip() for ref in self.hook_preset_refs):
            raise ValueError(
                f"workflow mode '{self.id}' hook_preset_refs entries must be non-empty strings"
            )
        validate_hook_preset_refs(
            self.hook_preset_refs,
            field_path=f"workflow mode '{self.id}' hook_preset_refs",
        )

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {"id": self.id, "description": self.description}
        if self.hook_preset_refs:
            payload["hook_preset_refs"] = list(self.hook_preset_refs)
        return payload


@dataclass(frozen=True, slots=True)
class WorkflowModeResolution:
    mode: WorkflowMode
    source: Literal["command", "workflow_mode", "default"]
    workflow_mode: str


def list_builtin_workflow_modes() -> tuple[WorkflowMode, ...]:
    return tuple(_BUILTIN_WORKFLOW_MODES.values())


def get_builtin_workflow_mode(mode_id: str) -> WorkflowMode | None:
    return _BUILTIN_WORKFLOW_MODES.get(mode_id)


def resolve_workflow_mode(
    *,
    command_workflow_mode: str | None = None,
    metadata_workflow_mode: str | None = None,
) -> WorkflowModeResolution:
    command_mode = _normalize_optional_selector(
        command_workflow_mode,
        field_name="command workflow_mode",
    )
    metadata_mode = _normalize_optional_selector(
        metadata_workflow_mode,
        field_name="workflow_mode",
    )
    selected_mode_id = command_mode or metadata_mode or "default"
    selected_mode = get_builtin_workflow_mode(selected_mode_id)
    if selected_mode is None:
        raise ValueError(f"unknown workflow_mode: {selected_mode_id}")
    source: Literal["command", "workflow_mode", "default"] = (
        "command" if command_mode is not None else "workflow_mode" if metadata_mode else "default"
    )
    return WorkflowModeResolution(
        mode=selected_mode,
        source=source,
        workflow_mode=selected_mode.id,
    )


def _normalize_optional_selector(value: str | None, *, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string when provided")
    return value.strip()


_BUILTIN_WORKFLOW_MODES: dict[str, WorkflowMode] = {
    mode.id: mode
    for mode in (
        WorkflowMode(id="default", description="Balanced mode for ordinary runtime requests."),
        WorkflowMode(
            id="deep_work",
            description="Depth-first mode for research-heavy or complex implementation work.",
            hook_preset_refs=(
                "role_reminder",
                "delegated_task_timing_guidance",
                "background_output_quality_guidance",
            ),
        ),
        WorkflowMode(
            id="review",
            description="Read-oriented mode for reviews and verification-focused analysis.",
            hook_preset_refs=("role_reminder",),
        ),
        WorkflowMode(
            id="product",
            description="Product-facing mode for user-visible application work.",
            hook_preset_refs=("role_reminder",),
        ),
        WorkflowMode(
            id="sustain",
            description="Maintenance mode for implementation, repository, and upkeep tasks.",
            hook_preset_refs=(
                "role_reminder",
                "todo_continuation_guidance",
                "delegated_task_timing_guidance",
                "delegated_retry_guidance",
            ),
        ),
    )
}


__all__ = [
    "WorkflowMode",
    "WorkflowModeId",
    "WorkflowModeKey",
    "WorkflowModeResolution",
    "get_builtin_workflow_mode",
    "list_builtin_workflow_modes",
    "resolve_workflow_mode",
]
