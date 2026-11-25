"""Shared constants and generic service errors."""

from __future__ import annotations

from typing import Final

#: Rendered-text budget for one history page. Defined here because the SDK
#: service default and core's renderer must agree, and the SDK cannot import core.
PAGE_BYTES: Final[int] = 40 * 1024


class StateConflict(RuntimeError):
    """One owner-state optimistic-concurrency check lost; the transaction committed nothing.

    Raised both when a callback stages with a stale version and when the commit's row
    version no longer matches. A caller that can re-read and re-apply a pure state
    mutation may retry; a caller must never treat it as a completed write.
    """


class QueryUnavailable(ValueError):
    """No admitted plugin in this runtime generation answers the named query."""


class QueryContractError(ValueError):
    """Raised when a query answer violates its expected contract schema."""
