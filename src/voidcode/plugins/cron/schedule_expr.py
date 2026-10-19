"""Schedule expression parsing/formatting ported from ``voidcode.runtime.background.schedule_expr``.

Pure algorithm copy: grammar constants, cron dialect validation, next-fire search and
human formatting are preserved verbatim apart from import paths.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass
from typing import Literal

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


def _parse_cron_field(field: str, min_val: int, max_val: int, field_name: str) -> None:
    """Validate a single cron field according to Claude dialect."""
    if not field:
        raise ValueError(f"Empty {field_name} field")
    for part in field.split(","):
        part = part.strip()
        if not part:
            raise ValueError(f"Empty element in {field_name}")
        if part == "*":
            continue
        if "/" in part:
            sub = part.split("/")
            if len(sub) != 2:
                raise ValueError(f"Invalid step syntax in {field_name}: {part!r}")
            base, step_s = sub
            if not step_s.isdigit() or int(step_s) <= 0:
                raise ValueError(f"Step in {field_name} must be a positive integer: {step_s!r}")
            step = int(step_s)
            if base == "*":
                start, end = min_val, max_val
            elif "-" in base:
                r = base.split("-")
                if len(r) != 2 or not r[0].isdigit() or not r[1].isdigit():
                    raise ValueError(f"Invalid range in step base for {field_name}: {base!r}")
                start, end = int(r[0]), int(r[1])
            elif base.isdigit():
                start, end = int(base), max_val
            else:
                raise ValueError(f"Invalid step base in {field_name}: {base!r}")
            if not (min_val <= start <= max_val and min_val <= end <= max_val and start <= end):
                raise ValueError(f"Value out of bounds ({min_val}-{max_val}) in {field_name}: {base!r}")
            _ = step
        elif "-" in part:
            r = part.split("-")
            if len(r) != 2 or not r[0].isdigit() or not r[1].isdigit():
                raise ValueError(f"Invalid range syntax in {field_name}: {part!r}")
            start, end = int(r[0]), int(r[1])
            if not (min_val <= start <= max_val and min_val <= end <= max_val and start <= end):
                raise ValueError(f"Range out of bounds ({min_val}-{max_val}) in {field_name}: {part!r}")
        elif part.isdigit():
            val = int(part)
            if not (min_val <= val <= max_val):
                raise ValueError(f"Value out of bounds ({min_val}-{max_val}) in {field_name}: {val}")
        else:
            raise ValueError(f"Unsupported character or alias in {field_name}: {part!r}")


def _matches_cron_field(val: int, field: str, min_val: int, max_val: int) -> bool:
    for part in field.split(","):
        part = part.strip()
        if part == "*":
            return True
        if "/" in part:
            sub = part.split("/")
            if len(sub) != 2:
                continue
            base, step_s = sub
            if not step_s.isdigit():
                continue
            step = int(step_s)
            if step <= 0:
                continue
            if base == "*":
                start, end = min_val, max_val
            elif "-" in base:
                r = base.split("-")
                if len(r) != 2 or not r[0].isdigit() or not r[1].isdigit():
                    continue
                start, end = int(r[0]), int(r[1])
            elif base.isdigit():
                start, end = int(base), max_val
            else:
                continue
            if start <= val <= end and (val - start) % step == 0:
                return True
        elif "-" in part:
            r = part.split("-")
            if len(r) != 2 or not r[0].isdigit() or not r[1].isdigit():
                continue
            start, end = int(r[0]), int(r[1])
            if start <= val <= end:
                return True
        elif part.isdigit():
            if int(part) == val:
                return True
    return False


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
        # Disallow unsupported aliases / modifiers: L, W, ?, #, @
        for char in ("L", "W", "?", "#", "@"):
            if char in cron_text.upper():
                raise ValueError(f"Cron dialect does not support {char!r} in {clean!r}. {_GRAMMAR_HELP}")

        try:
            _parse_cron_field(parts[0], 0, 59, "minute")
            _parse_cron_field(parts[1], 0, 23, "hour")
            _parse_cron_field(parts[2], 1, 31, "day-of-month")
            _parse_cron_field(parts[3], 1, 12, "month")
            _parse_cron_field(parts[4], 0, 7, "day-of-week")
        except ValueError as exc:
            raise ValueError(f"Invalid cron expression {clean!r}: {exc}. {_GRAMMAR_HELP}") from exc

        return ScheduleExpr(kind="cron", cron_fields=" ".join(parts))

    raise ValueError(f"Invalid schedule {clean!r}. {_GRAMMAR_HELP}")


def next_fire_after(expr: ScheduleExpr, after_epoch: int) -> int:
    """Compute the next epoch timestamp to fire strictly after ``after_epoch``."""
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
        fields = expr.cron_fields.split()
        dt = datetime.datetime.fromtimestamp(after_epoch).astimezone()
        # Advance at least 1 second and align to start of minute
        dt = (dt + datetime.timedelta(seconds=1)).replace(second=0, microsecond=0)
        if dt.timestamp() <= after_epoch:
            dt += datetime.timedelta(minutes=1)

        max_steps = 5 * 365 * 24 * 60  # bound search to 5 years
        steps = 0
        while steps < max_steps:
            # Check month
            if not _matches_cron_field(dt.month, fields[3], 1, 12):
                year = dt.year + (1 if dt.month == 12 else 0)
                month = 1 if dt.month == 12 else dt.month + 1
                dt = dt.replace(year=year, month=month, day=1, hour=0, minute=0, second=0)
                steps += 60
                continue

            # Check day
            dom_matches = _matches_cron_field(dt.day, fields[2], 1, 31)
            cron_dow = (dt.weekday() + 1) % 7
            dow_matches = _matches_cron_field(cron_dow, fields[4], 0, 6) or (cron_dow == 0 and _matches_cron_field(7, fields[4], 0, 7))
            dom_restricted = fields[2] != "*"
            dow_restricted = fields[4] != "*"
            day_ok = (
                (dom_matches or dow_matches)
                if (dom_restricted and dow_restricted)
                else (dom_matches if dom_restricted else (dow_matches if dow_restricted else True))
            )

            if not day_ok:
                dt = (dt + datetime.timedelta(days=1)).replace(hour=0, minute=0, second=0)
                steps += 60
                continue

            # Check hour
            if not _matches_cron_field(dt.hour, fields[1], 0, 23):
                dt = (dt + datetime.timedelta(hours=1)).replace(minute=0, second=0)
                steps += 60
                continue

            # Check minute
            if not _matches_cron_field(dt.minute, fields[0], 0, 59):
                dt += datetime.timedelta(minutes=1)
                steps += 1
                continue

            return int(dt.timestamp())

        raise ValueError(f"No matching fire time found within 5 years for cron {expr.cron_fields!r}")

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
