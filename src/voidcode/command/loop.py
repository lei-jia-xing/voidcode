"""Native /loop command parsing and formatting (Claude parity)."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from ..runtime.background.models import ScheduleSummary

DEFAULT_INTERVAL: Final[str] = "10m"

USAGE_MESSAGE: Final[str] = """Usage: /loop [interval] <prompt>

Run a prompt or slash command on a recurring interval.

Intervals: Ns, Nm, Nh, Nd (e.g. 5m, 30m, 2h, 1d). Minimum granularity is 1 minute.
If no interval is specified, defaults to 10m.

Examples:
  /loop 5m /babysit-prs
  /loop 30m check the deploy
  /loop 1h /standup 1
  /loop check the deploy          (defaults to 10m)
  /loop check the deploy every 20m"""

_LEADING_INTERVAL_RE = re.compile(r"^(\d+[smhd])(?:\s+(.*))?$", re.IGNORECASE)
_TRAILING_EVERY_RE = re.compile(r"(?:^|\s+)every\s+(\d+)\s*([a-zA-Z]+)$", re.IGNORECASE)

_TIME_UNIT_MAP: Final[dict[str, str]] = {
    "s": "s",
    "sec": "s",
    "second": "s",
    "seconds": "s",
    "m": "m",
    "min": "m",
    "minute": "m",
    "minutes": "m",
    "h": "h",
    "hr": "h",
    "hour": "h",
    "hours": "h",
    "d": "d",
    "day": "d",
    "days": "d",
}


def parse_loop_command(args: str) -> tuple[str | None, str | None]:
    """Parse a /loop command argument string into (interval, prompt).

    Returns:
        (interval, prompt) if valid, or (None, None) if the resulting prompt is empty
        (indicating the caller should show the usage message).
    """
    cleaned = args.strip()
    if not cleaned:
        return None, None

    # Priority 1: Leading token matches ^\d+[smhd]$
    leading_match = _LEADING_INTERVAL_RE.match(cleaned)
    if leading_match:
        interval = leading_match.group(1).lower()
        prompt = (leading_match.group(2) or "").strip()
        if not prompt:
            return None, None
        return interval, prompt

    # Priority 2: Trailing "every" clause with a valid time expression
    trailing_match = _TRAILING_EVERY_RE.search(cleaned)
    if trailing_match:
        amount_s = trailing_match.group(1)
        raw_unit = trailing_match.group(2).lower()
        if raw_unit in _TIME_UNIT_MAP:
            normalized_unit = _TIME_UNIT_MAP[raw_unit]
            interval = f"{amount_s}{normalized_unit}"
            prompt = cleaned[: trailing_match.start()].strip()
            if not prompt:
                return None, None
            return interval, prompt

    # Priority 3: Default interval (10m), entire input is the prompt
    return DEFAULT_INTERVAL, cleaned


def loop_confirmation_message(summary: ScheduleSummary) -> str:
    """Format the confirmation message returned after scheduling a /loop task."""
    return (
        f"Scheduled '{summary.prompt}' on cadence '{summary.human_schedule}' "
        f"(job ID: {summary.id}). "
        f"Recurring tasks auto-expire after 7 days. "
        f"To cancel sooner, use `cron_delete` or run `voidcode cron remove {summary.id}`."
    )
