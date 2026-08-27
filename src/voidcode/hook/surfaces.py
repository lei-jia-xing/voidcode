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
    "stuck_detected",
]
HookSurfacePhase = Literal["foreground", "background"]


@dataclass(frozen=True, slots=True)
class HookSurfaceDescriptor:
    """Canonical runtime metadata shared by hook configuration and execution."""

    surface: RuntimeHookSurface
    config_attribute: str
    phase: HookSurfacePhase
    event_type: str


HOOK_SURFACE_DESCRIPTORS: Final[tuple[HookSurfaceDescriptor, ...]] = (
    HookSurfaceDescriptor("pre_tool", "pre_tool", "foreground", "runtime.tool_hook_pre"),
    HookSurfaceDescriptor("post_tool", "post_tool", "foreground", "runtime.tool_hook_post"),
    HookSurfaceDescriptor("session_start", "on_session_start", "foreground", "runtime.session_started"),
    HookSurfaceDescriptor("session_end", "on_session_end", "foreground", "runtime.session_ended"),
    HookSurfaceDescriptor("session_idle", "on_session_idle", "foreground", "runtime.session_idle"),
    HookSurfaceDescriptor(
        "background_task_registered",
        "on_background_task_registered",
        "background",
        "runtime.background_task_registered",
    ),
    HookSurfaceDescriptor("background_task_started", "on_background_task_started", "background", "runtime.background_task_started"),
    HookSurfaceDescriptor("background_task_progress", "on_background_task_progress", "background", "runtime.background_task_progress"),
    HookSurfaceDescriptor("background_task_completed", "on_background_task_completed", "background", "runtime.background_task_completed"),
    HookSurfaceDescriptor("background_task_failed", "on_background_task_failed", "background", "runtime.background_task_failed"),
    HookSurfaceDescriptor("background_task_cancelled", "on_background_task_cancelled", "background", "runtime.background_task_cancelled"),
    HookSurfaceDescriptor("background_task_interrupted", "on_background_task_interrupted", "background", "runtime.background_task_interrupted"),
    HookSurfaceDescriptor(
        "background_task_notification_enqueued",
        "on_background_task_notification_enqueued",
        "background",
        "runtime.background_task_notification_enqueued",
    ),
    HookSurfaceDescriptor("background_task_result_read", "on_background_task_result_read", "background", "runtime.background_task_result_read"),
    HookSurfaceDescriptor("delegated_result_available", "on_delegated_result_available", "background", "runtime.delegated_result_available"),
    HookSurfaceDescriptor("turn_progress", "on_turn_progress", "foreground", "runtime.turn_progress"),
    HookSurfaceDescriptor("stuck_detected", "on_stuck_detected", "foreground", "runtime.stuck_detected"),
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
