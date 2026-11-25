"""Process timing constants shared by capture, supervision, and cleanup."""

from __future__ import annotations

from typing import Final

#: Retry interval while a background process's stdin pipe is full.
_WRITE_POLL_SECONDS: Final[float] = 0.01
#: Default bound on one stdin write when the caller supplies no deadline.
_WRITE_DEADLINE_SECONDS: Final[float] = 5.0
#: SIGTERM window before a group is escalated to SIGKILL.
_STOP_GRACE_SECONDS: Final[float] = 0.25
#: A stopped child's pipes are drained inside this budget, so its partial output is not lost.
#: A reader still alive when it expires is marked as a partial capture, never a complete result.
_READER_GRACE_SECONDS: Final[float] = 2.0
#: A cancelled invocation answers inside the core's cooperative-cancel window, partial output and all.
_CANCEL_RESPONSE_SECONDS: Final[float] = 0.75
#: Poll interval for process exit and abandoned-descendant checks.
_POLL_SECONDS: Final[float] = 0.05
