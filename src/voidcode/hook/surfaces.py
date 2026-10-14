from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

RuntimeHookSurface = Literal[
    "pre_tool",
    "post_tool",
    "session_start",
    "session_end",
    "session_idle",
    "background_task_registered",
    "background_task_started",
    "background_task_progress",
    "background_task_completed",
    "background_task_failed",
    "background_task_cancelled",
    "background_task_interrupted",
    "background_task_notification_enqueued",
    "background_task_result_read",
    "delegated_result_available",
    "turn_progress",
    "tool_batch_repeated",
    "approval_requested",
    "question_asked",
    "before_compact",
]
HookSurfacePhase = Literal["foreground", "background"]


@dataclass(frozen=True, slots=True)
class HookSurfaceDescriptor:
    """Canonical runtime metadata shared by hook configuration and execution."""

    surface: RuntimeHookSurface
    config_attribute: str
    phase: HookSurfacePhase


HOOK_SURFACE_DESCRIPTORS: Final[tuple[HookSurfaceDescriptor, ...]] = (
    HookSurfaceDescriptor("pre_tool", "pre_tool", "foreground"),
    HookSurfaceDescriptor("post_tool", "post_tool", "foreground"),
    HookSurfaceDescriptor("session_start", "on_session_start", "foreground"),
    HookSurfaceDescriptor("session_end", "on_session_end", "foreground"),
    HookSurfaceDescriptor("session_idle", "on_session_idle", "foreground"),
    HookSurfaceDescriptor("background_task_registered", "on_background_task_registered", "background"),
    HookSurfaceDescriptor("background_task_started", "on_background_task_started", "background"),
    HookSurfaceDescriptor("background_task_progress", "on_background_task_progress", "background"),
    HookSurfaceDescriptor("background_task_completed", "on_background_task_completed", "background"),
    HookSurfaceDescriptor("background_task_failed", "on_background_task_failed", "background"),
    HookSurfaceDescriptor("background_task_cancelled", "on_background_task_cancelled", "background"),
    HookSurfaceDescriptor("background_task_interrupted", "on_background_task_interrupted", "background"),
    HookSurfaceDescriptor("background_task_notification_enqueued", "on_background_task_notification_enqueued", "background"),
    HookSurfaceDescriptor("background_task_result_read", "on_background_task_result_read", "background"),
    HookSurfaceDescriptor("delegated_result_available", "on_delegated_result_available", "background"),
    HookSurfaceDescriptor("turn_progress", "on_turn_progress", "foreground"),
    HookSurfaceDescriptor("tool_batch_repeated", "on_tool_batch_repeated", "foreground"),
    HookSurfaceDescriptor("approval_requested", "on_approval_requested", "foreground"),
    HookSurfaceDescriptor("question_asked", "on_question_asked", "foreground"),
    HookSurfaceDescriptor("before_compact", "on_before_compact", "foreground"),
)

_SURFACE_DESCRIPTORS: Final[dict[str, HookSurfaceDescriptor]] = {descriptor.surface: descriptor for descriptor in HOOK_SURFACE_DESCRIPTORS}


def hook_surface_descriptor(surface: str) -> HookSurfaceDescriptor:
    """Return the canonical descriptor, preserving unknown-surface KeyError semantics."""
    return _SURFACE_DESCRIPTORS[surface]


__all__ = [
    "HOOK_SURFACE_DESCRIPTORS",
    "HookSurfaceDescriptor",
    "HookSurfacePhase",
    "RuntimeHookSurface",
    "hook_surface_descriptor",
]
