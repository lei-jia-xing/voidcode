from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from ...hook.percall import PerCallMessage, PerCallRewriteOutcome, percall_cache_prefix, percall_persistent_messages

if TYPE_CHECKING:
    from .window import RuntimeContextSegment


def segments_to_percall_messages(
    segments: Sequence[RuntimeContextSegment],
) -> tuple[PerCallMessage, ...]:
    """Bind provider segments to per-call messages; caller segments are only read."""
    from .window import RuntimeContextSegment as _Segment

    bound: list[PerCallMessage] = []
    for segment in segments:
        if not isinstance(segment, _Segment):
            raise ValueError("per-call binding requires RuntimeContextSegment items")
        bound.append(PerCallMessage(role=segment.role, content=segment.content or ""))
    return tuple(bound)


def percall_wire_cache_prefix(outcome: PerCallRewriteOutcome) -> str:
    """Cache prefix from persistent messages only; per-call markers never reach the hash."""
    return percall_cache_prefix(percall_persistent_messages(outcome.messages))


__all__ = [
    "percall_wire_cache_prefix",
    "segments_to_percall_messages",
]
