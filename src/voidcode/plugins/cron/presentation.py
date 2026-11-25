"""Shared cron list formatting used by both tools and commands."""

from __future__ import annotations

import datetime

from .state import ScheduleEntry


def _human_time(ms: int) -> str:
    """One local timestamp for list output; never used for schedule arithmetic."""
    value = datetime.datetime.fromtimestamp(ms / 1000).astimezone()
    return value.strftime("%Y-%m-%dT%H:%M:%S")


def _schedule_status(entry: ScheduleEntry) -> str:
    if entry.pending is not None:
        return "running"
    return "enabled" if entry.enabled else "disabled"
