"""Deterministic polling for tests that wait on committed state."""

from __future__ import annotations

import time
from collections.abc import Callable

DEFAULT_POLL_SECONDS = 0.05


def wait_for(
    predicate: Callable[[], bool],
    *,
    what: str,
    timeout: float = 20.0,
    interval: float = DEFAULT_POLL_SECONDS,
) -> None:
    """Block until ``predicate`` is true, or fail with the wait description.

    A real timeout is never hidden: this raises ``AssertionError`` (including the
    caller's description and budget) instead of returning on expiry.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {what} (after {timeout:g}s)")
