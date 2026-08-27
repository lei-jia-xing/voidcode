from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, cast

from ..formatter.config import (
    RuntimeFormatterPresetConfig,
    default_formatter_presets,
    resolve_formatter_preset,
)
from .surfaces import RuntimeHookSurface, hook_surface_descriptor

type RuntimeHookFailureMode = Literal["warn", "fail"]


@dataclass(frozen=True, slots=True)
class RuntimeHooksConfig:
    enabled: bool | None = True
    #: Opt-in auto-format after edit/write. Off by default; enabled via the
    #: top-level ``formatter`` config section (``enabled`` / ``format_on_write``).
    format_on_write: bool = False
    timeout_seconds: float | None = 30.0
    failure_mode: RuntimeHookFailureMode = "warn"
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
    on_background_task_interrupted: tuple[tuple[str, ...], ...] = ()
    on_background_task_notification_enqueued: tuple[tuple[str, ...], ...] = ()
    on_background_task_result_read: tuple[tuple[str, ...], ...] = ()
    on_delegated_result_available: tuple[tuple[str, ...], ...] = ()
    on_turn_progress: tuple[tuple[str, ...], ...] = ()
    on_stuck_detected: tuple[tuple[str, ...], ...] = ()
    formatter_presets: Mapping[str, RuntimeFormatterPresetConfig] = field(default_factory=default_formatter_presets)

    def commands_for_surface(self, surface: RuntimeHookSurface) -> tuple[tuple[str, ...], ...]:
        descriptor = hook_surface_descriptor(surface)
        return cast(tuple[tuple[str, ...], ...], getattr(self, descriptor.config_attribute))

    def resolve_formatter(self, file_path: Path) -> tuple[str, RuntimeFormatterPresetConfig] | None:
        return resolve_formatter_preset(self.formatter_presets, file_path)
