"""Schedule syntax and formatting, with cron iteration delegated to croniter."""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass
from typing import Literal

from croniter import CroniterBadDateError, croniter

_GRAMMAR_HELP = "Accepted grammar: 'every <N><s|m|h|d>', 'at <ISO-8601>', 'cron <min> <hour> <day> <month> <dow>', or 'once'"

_EVERY_RE = re.compile(r"^every\s+(\d+)([smhd])$", re.IGNORECASE)
_AT_PREFIX = "at "
_CRON_PREFIX = "cron "

_INTERVAL_MULTIPLIERS: dict[str, int] = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
}


@dataclass(frozen=True, slots=True)
class ScheduleExpr:
    kind: Literal["every", "at", "cron", "once"]
    interval_seconds: int | None = None
    at_epoch: int | None = None
    cron_fields: str | None = None


def parse_schedule(text: str) -> ScheduleExpr:
    """Parse a schedule string into a ScheduleExpr.

    Accepted grammar:
    - `every <N><s|m|h|d>` (interval in seconds, minutes, hours, days)
    - `at <ISO-8601>` (one-shot timestamp)
    - `cron <min> <hour> <day> <month> <dow>` (5-field cron in local timezone)
    - `once` (fire immediately on next tick)
    """
    clean = text.strip()
    if not clean:
        raise ValueError(f"Schedule expression cannot be empty. {_GRAMMAR_HELP}")

    if clean.lower() == "once":
        return ScheduleExpr(kind="once")

    match = _EVERY_RE.match(clean)
    if match:
        amount_s, unit = match.group(1), match.group(2).lower()
        amount = int(amount_s)
        if amount <= 0:
            raise ValueError(f"Interval amount must be greater than 0: {clean!r}. {_GRAMMAR_HELP}")
        seconds = amount * _INTERVAL_MULTIPLIERS[unit]
        return ScheduleExpr(kind="every", interval_seconds=seconds)

    if clean.lower().startswith(_AT_PREFIX):
        raw_iso = clean[len(_AT_PREFIX) :].strip()
        try:
            dt = datetime.datetime.fromisoformat(raw_iso)
        except ValueError as exc:
            raise ValueError(f"Invalid ISO-8601 timestamp in schedule {clean!r}: {exc}. {_GRAMMAR_HELP}") from exc
        if dt.tzinfo is None:
            dt = dt.astimezone()
        epoch = int(dt.timestamp())
        return ScheduleExpr(kind="at", at_epoch=epoch)

    if clean.lower().startswith(_CRON_PREFIX):
        cron_text = clean[len(_CRON_PREFIX) :].strip()
        parts = cron_text.split()
        if len(parts) != 5:
            raise ValueError(f"Cron expression must have exactly 5 fields, got {len(parts)} in {clean!r}. {_GRAMMAR_HELP}")
        if not croniter.is_valid(cron_text):
            raise ValueError(f"Invalid cron expression {clean!r}. {_GRAMMAR_HELP}")

        return ScheduleExpr(kind="cron", cron_fields=" ".join(parts))

    raise ValueError(f"Invalid schedule {clean!r}. {_GRAMMAR_HELP}")


def next_fire_after(expr: ScheduleExpr, after_epoch: int) -> int:
    """Compute the fire time; recurring cron schedules advance strictly past the supplied epoch."""
    if expr.kind == "once":
        return after_epoch

    if expr.kind == "every":
        if expr.interval_seconds is None or expr.interval_seconds <= 0:
            raise ValueError("Invalid interval_seconds for 'every' schedule")
        return after_epoch + expr.interval_seconds

    if expr.kind == "at":
        if expr.at_epoch is None:
            raise ValueError("Missing at_epoch for 'at' schedule")
        return expr.at_epoch

    if expr.kind == "cron":
        if expr.cron_fields is None:
            raise ValueError("Missing cron_fields for 'cron' schedule")
        # Naive datetimes preserve the host-local wall-clock schedule, including
        # conversion back through the host timezone at the resulting date.
        base = datetime.datetime.fromtimestamp(after_epoch)
        try:
            next_time = croniter(expr.cron_fields, base, day_or=True, max_years_between_matches=5).get_next(datetime.datetime)
        except CroniterBadDateError as exc:
            raise ValueError(f"No matching fire time found within 5 years for cron {expr.cron_fields!r}") from exc
        return int(next_time.timestamp())

    raise ValueError(f"Unknown schedule kind: {expr.kind!r}")


def human_schedule(expr: ScheduleExpr) -> str:
    """Format a ScheduleExpr into a human-readable string."""
    if expr.kind == "once":
        return "once"

    if expr.kind == "every":
        s = expr.interval_seconds or 0
        if s > 0 and s % 86400 == 0:
            return f"every {s // 86400}d"
        if s > 0 and s % 3600 == 0:
            return f"every {s // 3600}h"
        if s > 0 and s % 60 == 0:
            return f"every {s // 60}m"
        return f"every {s}s"

    if expr.kind == "at":
        if expr.at_epoch is None:
            return "at <unknown>"
        dt = datetime.datetime.fromtimestamp(expr.at_epoch).astimezone()
        if dt.second == 0:
            return f"at {dt.strftime('%Y-%m-%dT%H:%M')}"
        return f"at {dt.strftime('%Y-%m-%dT%H:%M:%S')}"

    if expr.kind == "cron":
        return f"cron {expr.cron_fields or ''}"

    return str(expr.kind)
