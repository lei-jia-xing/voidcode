"""Shared cron runtime knobs and the wall-clock seam used by every module.

The facade forwards monkeypatches of ``cron_plugin._clock_ms`` and
``cron_plugin.MAX_SCHEDULES_PER_SESSION`` here; consumers must resolve these
names through this module at call time rather than importing the values.
"""

from __future__ import annotations

import time

#: Wakeup payload kind this plugin owns; a fired wakeup of any other kind is not ours.
WAKEUP_KIND = "cron"
#: Claude parity: maximum schedules one session may hold (legacy per-workspace cap).
MAX_SCHEDULES_PER_SESSION = 50
#: Claude parity: 7d default expiration for recurring schedules.
DEFAULT_RECURRING_EXPIRY_SECONDS = 7 * 86400
#: The documented /loop granularity: schedule iterations at most once a minute.
MIN_EVERY_INTERVAL_SECONDS = 60
#: Bounded, user-facing note on the one deliberate delete/claim race.
DELETE_IN_FLIGHT_NOTE = "A run already in flight is not interrupted."
#: Source events per facts page; catch-up keeps paging until the stream is exhausted.
FACT_PAGE_SIZE = 200


def _clock_ms() -> int:
    """The wall clock the schedule math runs on, in the core's due-time unit.

    The legacy scheduler took this as an injectable ``clock``; it stays a module-level
    seam so a test can pin the instant a fire is computed for. The facade forwards
    monkeypatches here, and the observer keeps its own local wrapper so the existing
    ``observer.__globals__['_clock_ms']`` patch path keeps working.
    """
    return int(time.time() * 1000)
