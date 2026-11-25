"""Cron deactivation: park schedules disabled with no armed wakeup."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

from voidcode.sdk.plugin import StateScope

from .state import CronState


def prepare_cron_deactivation(
    state: Mapping[str, object],
    scope: StateScope,
    scope_key: str,
    tip_sequence: int | None,
) -> Mapping[str, object]:
    """Pure deactivation transform: park every schedule disabled with no armed wakeup.

    The activation transaction cancels this owner's pending wakeups, so a kept wakeup id
    would name a dead row and a kept enabled flag would promise fires that cannot happen.
    Schedules stay inspectable after re-enable but never silently re-arm (there is no
    enable path; recreate the schedule to fire again). A claimed wakeup already has its
    run, which the core's running-session blockers govern, not this state copy.
    """
    _ = (scope, scope_key, tip_sequence)
    parsed = CronState.from_payload(state)
    parked = {
        schedule_id: replace(entry, enabled=False, wakeup_id=None, next_fire_at=None, pending=None) for schedule_id, entry in parsed.schedules.items()
    }
    return replace(parsed, schedules=parked).to_payload()
