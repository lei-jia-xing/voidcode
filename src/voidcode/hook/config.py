"""Hook configuration plus the tool-name glob integration point.

``hook_tool_matches`` is the single seam the tool-hook loops use to decide
whether a ``pre_tool``/``post_tool`` binding fires for a tool name.
Empty patterns match every tool (backcompat); otherwise stdlib
``fnmatch.fnmatchcase`` applies, so ``write*`` matches ``write`` and
``write_file`` but not ``read``, and matching is case-sensitive.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Mapping, Sequence
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
    #: Optional tool-name glob filters for the tool-hook surfaces. Empty means
    #: "match every tool" (backcompat); non-empty filters with stdlib fnmatch.
    pre_tool_match: tuple[str, ...] = ()
    post_tool: tuple[tuple[str, ...], ...] = ()
    post_tool_match: tuple[str, ...] = ()
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
    on_approval_requested: tuple[tuple[str, ...], ...] = ()
    on_question_asked: tuple[tuple[str, ...], ...] = ()
    on_before_compact: tuple[tuple[str, ...], ...] = ()
    formatter_presets: Mapping[str, RuntimeFormatterPresetConfig] = field(default_factory=default_formatter_presets)

    def commands_for_surface(self, surface: RuntimeHookSurface) -> tuple[tuple[str, ...], ...]:
        descriptor = hook_surface_descriptor(surface)
        return cast(tuple[tuple[str, ...], ...], getattr(self, descriptor.config_attribute))

    def resolve_formatter(self, file_path: Path) -> tuple[str, RuntimeFormatterPresetConfig] | None:
        return resolve_formatter_preset(self.formatter_presets, file_path)


def hook_tool_matches(patterns: Sequence[str], tool_name: str) -> bool:
    """Return True when ``tool_name`` passes the surface glob filter.

    Wired into ``run_tool_hooks`` for the ``pre_tool``/``post_tool`` surfaces;
    that loop calls this with the surface's ``*_match`` tuple before running
    each binding. Empty patterns match all (backcompat), otherwise stdlib
    ``fnmatch.fnmatchcase`` applies and is case-sensitive.
    """
    if not patterns:
        return True
    return any(fnmatch.fnmatchcase(tool_name, pattern) for pattern in patterns)
