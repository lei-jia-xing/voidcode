"""Stable facade for the todo plugin modules.

The implementation lives in cohesive sibling modules. This module re-exports the
public surface used by plugin registration and tests, and keeps the historical
``todo_plugin.MAX_TODO_*`` monkeypatch path working by forwarding bound writes to
the shared :mod:`voidcode.plugins.todo.limits` module.
"""

from __future__ import annotations

import sys
import types

from . import limits as _limits
from .commands import TodoAddCommand, TodoClearCommand, TodoEditCommand, TodoStatusCommand, TodoUpdateCommand
from .context import prepare_todo_deactivation, todo_context
from .identity import TASK_ID_PATTERN
from .limits import (
    MAX_TODO_BLOCKER_BYTES,
    MAX_TODO_COMMAND_OUTPUT_BYTES,
    MAX_TODO_CONTEXT_BYTES,
    MAX_TODO_PHASE_NAME_BYTES,
    MAX_TODO_PHASES,
    MAX_TODO_REMINDER_BYTES,
    MAX_TODO_STATE_CONFLICT_ATTEMPTS,
    MAX_TODO_TASK_CONTENT_BYTES,
    MAX_TODO_TASKS,
    MAX_TODO_TASKS_PER_PHASE,
)
from .models import (
    ACTIVE_PROVIDER_STATUSES,
    INCOMPLETE_STATUSES,
    TERMINAL_STATUSES,
    TODO_MID_RUN_KIND,
    TODO_REMINDER_KIND,
    TODO_STATUSES,
    ReminderDecision,
    ReminderKind,
    ReminderPolicy,
    TodoPhase,
    TodoStatus,
    TodoSummary,
    TodoTask,
    is_todo_status,
    todo_summary,
)
from .mutations import TodoChange, apply_and_store
from .observer import OWN_TOOL_NAMES, OWNER, todo_observer
from .operations import (
    RESETTING_OPS,
    TARGET_OPS,
    TASK_UPDATE_OPS,
    TodoOp,
    apply,
    parse_args,
)
from .operations import (
    _target_tasks as _target_tasks,
)
from .operations import (
    _TodoArgs as _TodoArgs,
)
from .presentation import paged_phase_payload, task_from_id, task_operations, todo_presentation
from .reminders import (
    DEFAULT_TODO_REMINDER_MAX_PER_CYCLE,
    TODO_MID_RUN_MAX_PER_CYCLE,
    TODO_MID_RUN_MUTATION_THRESHOLD,
    TODO_REMINDER_ROLE,
    TodoRemindersConfig,
    decide_reminder,
    incomplete_todo_phases,
    parse_todo_config,
    reminder_text,
)
from .rendering import render_phases
from .state import (
    SCHEMA_VERSION,
    TodoState,
    parse_phases,
    parse_reminders,
    phases_payload,
    todo_event_payload,
)
from .state import (
    _store as _store,
)
from .tool import TODO_DESCRIPTION, TodoResultBody, TodoTool

__all__ = [
    "ACTIVE_PROVIDER_STATUSES",
    "DEFAULT_TODO_REMINDER_MAX_PER_CYCLE",
    "INCOMPLETE_STATUSES",
    "MAX_TODO_BLOCKER_BYTES",
    "MAX_TODO_COMMAND_OUTPUT_BYTES",
    "MAX_TODO_CONTEXT_BYTES",
    "MAX_TODO_PHASE_NAME_BYTES",
    "MAX_TODO_PHASES",
    "MAX_TODO_REMINDER_BYTES",
    "MAX_TODO_STATE_CONFLICT_ATTEMPTS",
    "MAX_TODO_TASK_CONTENT_BYTES",
    "MAX_TODO_TASKS",
    "MAX_TODO_TASKS_PER_PHASE",
    "OWNER",
    "OWN_TOOL_NAMES",
    "RESETTING_OPS",
    "ReminderDecision",
    "ReminderKind",
    "ReminderPolicy",
    "SCHEMA_VERSION",
    "TARGET_OPS",
    "TASK_UPDATE_OPS",
    "TERMINAL_STATUSES",
    "TODO_DESCRIPTION",
    "TODO_MID_RUN_KIND",
    "TODO_MID_RUN_MAX_PER_CYCLE",
    "TODO_MID_RUN_MUTATION_THRESHOLD",
    "TODO_REMINDER_KIND",
    "TODO_REMINDER_ROLE",
    "TODO_STATUSES",
    "TodoAddCommand",
    "TodoChange",
    "TodoClearCommand",
    "TodoEditCommand",
    "TodoOp",
    "TodoPhase",
    "TodoRemindersConfig",
    "TodoResultBody",
    "TodoState",
    "TodoStatus",
    "TodoStatusCommand",
    "TodoSummary",
    "TodoTask",
    "TodoTool",
    "TodoUpdateCommand",
    "apply",
    "apply_and_store",
    "decide_reminder",
    "incomplete_todo_phases",
    "is_todo_status",
    "paged_phase_payload",
    "parse_args",
    "parse_phases",
    "parse_reminders",
    "parse_todo_config",
    "phases_payload",
    "prepare_todo_deactivation",
    "reminder_text",
    "render_phases",
    "task_from_id",
    "task_operations",
    "todo_context",
    "todo_event_payload",
    "todo_observer",
    "todo_presentation",
    "todo_summary",
]

#: Historical private aliases used by focused tests and diagnostics.
_apply = apply
_apply_and_store = apply_and_store
_parse_args = parse_args
_todo_presentation = todo_presentation
_task_from_id = task_from_id
_paged_phase_payload = paged_phase_payload
_task_operations = task_operations
_TodoChange = TodoChange
_TASK_ID_PATTERN = TASK_ID_PATTERN
_TERMINAL_STATUSES = TERMINAL_STATUSES
_INCOMPLETE_STATUSES = INCOMPLETE_STATUSES
_ACTIVE_PROVIDER_STATUSES = ACTIVE_PROVIDER_STATUSES

_LIMIT_NAMES = frozenset(
    {
        "MAX_TODO_BLOCKER_BYTES",
        "MAX_TODO_COMMAND_OUTPUT_BYTES",
        "MAX_TODO_CONTEXT_BYTES",
        "MAX_TODO_PHASES",
        "MAX_TODO_PHASE_NAME_BYTES",
        "MAX_TODO_REMINDER_BYTES",
        "MAX_TODO_STATE_CONFLICT_ATTEMPTS",
        "MAX_TODO_TASK_CONTENT_BYTES",
        "MAX_TODO_TASKS",
        "MAX_TODO_TASKS_PER_PHASE",
    }
)


class _TodoFacade(types.ModuleType):
    """Keep the documented facade monkeypatch path in sync with shared limits."""

    def __setattr__(self, name: str, value: object) -> None:
        if name in _LIMIT_NAMES:
            setattr(_limits, name, value)
        super().__setattr__(name, value)


sys.modules[__name__].__class__ = _TodoFacade
