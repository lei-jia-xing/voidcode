"""Stable facade for the cron plugin.

The implementation is split into cohesive modules: ``runtime`` (the clock and
shared policy knobs), ``state`` (schema, typed models, strict parsing, and the CAS
store), ``scheduling`` (create/delete and staged wakeup arming), ``presentation``
(shared list formatting), ``tools``, ``commands``, ``observer`` (pending
settlement and durable facts catch-up), and ``lifecycle`` (deactivation).

Modules depend only downward, and this module is the single import surface for
the plugin factory, tests, and historical callers. No behavior lives here; the
pre-split private names remain importable because the existing suites exercise
them directly and monkeypatch ``_clock_ms`` / ``MAX_SCHEDULES_PER_SESSION``.
"""

from __future__ import annotations

import sys
import types

# ruff: noqa: F401 - compatibility re-exports are the point of this facade.
from . import runtime as _runtime
from .commands import (
    CronDeleteCommand,
    CronListCommand,
    CronLoopCommand,
    _LoopSummary,
)
from .lifecycle import prepare_cron_deactivation
from .observer import (
    _apply_finished,
    _apply_fired,
    _catch_up,
    _pending_from_fact,
    _settle,
    _stage_state_error,
    cron_observer,
)
from .presentation import _human_time, _schedule_status
from .runtime import (
    DEFAULT_RECURRING_EXPIRY_SECONDS,
    DELETE_IN_FLIGHT_NOTE,
    FACT_PAGE_SIZE,
    MAX_SCHEDULES_PER_SESSION,
    MIN_EVERY_INTERVAL_SECONDS,
    WAKEUP_KIND,
    _clock_ms,
)
from .scheduling import (
    _arm,
    _create,
    _created_event,
    _delete_event,
    _delete_schedule,
    _disarm,
)
from .state import (
    _ONE_SHOT_KINDS,
    SCHEMA_VERSION,
    CronState,
    PendingFire,
    PendingRole,
    ScheduleEntry,
    ScheduleKind,
    _expiry_for,
    _is_schedule_kind,
    _store,
    _validate_recurring_policy,
)
from .tools import (
    CRON_CREATE_DESCRIPTION,
    CRON_DELETE_DESCRIPTION,
    CRON_LIST_DESCRIPTION,
    CronCreateTool,
    CronDeleteTool,
    CronListTool,
    _failure,
)

__all__ = [
    "CRON_CREATE_DESCRIPTION",
    "CRON_DELETE_DESCRIPTION",
    "CRON_LIST_DESCRIPTION",
    "DEFAULT_RECURRING_EXPIRY_SECONDS",
    "DELETE_IN_FLIGHT_NOTE",
    "FACT_PAGE_SIZE",
    "MAX_SCHEDULES_PER_SESSION",
    "MIN_EVERY_INTERVAL_SECONDS",
    "SCHEMA_VERSION",
    "WAKEUP_KIND",
    "CronCreateTool",
    "CronDeleteCommand",
    "CronDeleteTool",
    "CronListCommand",
    "CronListTool",
    "CronLoopCommand",
    "CronState",
    "PendingFire",
    "PendingRole",
    "ScheduleEntry",
    "ScheduleKind",
    "cron_observer",
    "prepare_cron_deactivation",
]

#: Historical private aliases used by focused tests and diagnostics.
__all__.extend(
    [
        "_LoopSummary",
        "_ONE_SHOT_KINDS",
        "_apply_fired",
        "_apply_finished",
        "_arm",
        "_catch_up",
        "_clock_ms",
        "_create",
        "_created_event",
        "_delete_event",
        "_delete_schedule",
        "_disarm",
        "_expiry_for",
        "_failure",
        "_human_time",
        "_is_schedule_kind",
        "_pending_from_fact",
        "_schedule_status",
        "_settle",
        "_stage_state_error",
        "_store",
        "_validate_recurring_policy",
    ]
)

#: Names whose historical facade patch path must stay authoritative for consumers.
_PATCH_NAMES = frozenset({"_clock_ms", "MAX_SCHEDULES_PER_SESSION"})


class _CronFacade(types.ModuleType):
    """Forward clock/limit monkeypatches to the shared runtime module."""

    def __setattr__(self, name: str, value: object) -> None:
        if name in _PATCH_NAMES:
            setattr(_runtime, name, value)
        super().__setattr__(name, value)


sys.modules[__name__].__class__ = _CronFacade
