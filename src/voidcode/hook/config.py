from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from ..formatter.config import (
    RuntimeFormatterPresetConfig,
    default_formatter_presets,
    resolve_formatter_preset,
)

type RuntimeHookSurface = Literal[
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
    "background_task_notification_enqueued",
    "background_task_result_read",
    "delegated_result_available",
    "context_pressure",
]


@dataclass(frozen=True, slots=True)
class RuntimeHooksConfig:
    enabled: bool | None = None
    timeout_seconds: float | None = 30.0
    pre_tool: tuple[tuple[str, ...], ...] = ()
    post_tool: tuple[tuple[str, ...], ...] = ()
    on_session_start: tuple[tuple[str, ...], ...] = ()
    on_session_end: tuple[tuple[str, ...], ...] = ()
    on_session_idle: tuple[tuple[str, ...], ...] = ()
    on_background_task_registered: tuple[tuple[str, ...], ...] = ()
    on_background_task_started: tuple[tuple[str, ...], ...] = ()
    on_background_task_progress: tuple[tuple[str, ...], ...] = ()
    on_background_task_completed: tuple[tuple[str, ...], ...] = ()
    on_background_task_failed: tuple[tuple[str, ...], ...] = ()
    on_background_task_cancelled: tuple[tuple[str, ...], ...] = ()
    on_background_task_notification_enqueued: tuple[tuple[str, ...], ...] = ()
    on_background_task_result_read: tuple[tuple[str, ...], ...] = ()
    on_delegated_result_available: tuple[tuple[str, ...], ...] = ()
    on_context_pressure: tuple[tuple[str, ...], ...] = ()
    formatter_presets: Mapping[str, RuntimeFormatterPresetConfig] = field(
        default_factory=default_formatter_presets
    )

    def commands_for_surface(self, surface: RuntimeHookSurface) -> tuple[tuple[str, ...], ...]:
        return {
            "pre_tool": self.pre_tool,
            "post_tool": self.post_tool,
            "session_start": self.on_session_start,
            "session_end": self.on_session_end,
            "session_idle": self.on_session_idle,
            "background_task_registered": self.on_background_task_registered,
            "background_task_started": self.on_background_task_started,
            "background_task_progress": self.on_background_task_progress,
            "background_task_completed": self.on_background_task_completed,
            "background_task_failed": self.on_background_task_failed,
            "background_task_cancelled": self.on_background_task_cancelled,
            "background_task_notification_enqueued": self.on_background_task_notification_enqueued,
            "background_task_result_read": self.on_background_task_result_read,
            "delegated_result_available": self.on_delegated_result_available,
            "context_pressure": self.on_context_pressure,
        }[surface]

    def resolve_formatter(self, file_path: Path) -> tuple[str, RuntimeFormatterPresetConfig] | None:
        return resolve_formatter_preset(self.formatter_presets, file_path)
