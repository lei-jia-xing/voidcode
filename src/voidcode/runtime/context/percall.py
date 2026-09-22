from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from typing import TYPE_CHECKING, Literal, cast

from ...hook.percall import (
    PerCallChain,
    PerCallMessage,
    PerCallRewriteOutcome,
    percall_cache_prefix,
    percall_persistent_messages,
)

if TYPE_CHECKING:
    from .window import RuntimeAssembledContext, RuntimeContextSegment

# ponytail: empty registry + 2000-char wire cap; add members/raise cap only with measured need.
EMPTY_PERCALL_CHAIN = PerCallChain()

MAX_PERCALL_WIRE_CHARS = 2000

_PERCALL_WIRE_SOURCE = "percall_wire"

_VALID_WIRE_ROLES = frozenset({"system", "user", "assistant", "tool"})


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


def apply_percall_chain(
    messages: Sequence[PerCallMessage],
    *,
    chain: PerCallChain | None = None,
) -> PerCallRewriteOutcome:
    """Run the chain over a deep clone; input history is never mutated."""
    active = EMPTY_PERCALL_CHAIN if chain is None else chain
    if not isinstance(active, PerCallChain):
        raise ValueError("per-call chain must be a PerCallChain")
    return active.apply(messages=deepcopy(tuple(messages)))


def percall_wire_segments(
    outcome: PerCallRewriteOutcome,
) -> tuple[RuntimeContextSegment, ...]:
    """Project per-call-only markers to wire segments; provenance keys only, never raw history."""
    from .window import RuntimeContextSegment as _Segment

    wire: list[RuntimeContextSegment] = []
    for message in percall_ephemeral_messages(outcome):
        role = message.role if message.role in _VALID_WIRE_ROLES else "system"
        content = message.content
        truncated = len(content) > MAX_PERCALL_WIRE_CHARS
        if truncated:
            content = content[:MAX_PERCALL_WIRE_CHARS]
        wire.append(
            _Segment(
                role=cast(Literal["system", "user", "assistant", "tool"], role),
                content=content,
                metadata={
                    "source": _PERCALL_WIRE_SOURCE,
                    "per_call": True,
                    "truncated": truncated,
                    "content_chars": len(message.content),
                },
            )
        )
    return tuple(wire)


def percall_ephemeral_messages(
    outcome: PerCallRewriteOutcome,
) -> tuple[PerCallMessage, ...]:
    """Return only per-call-only markers; persistent messages stay out of the wire view."""
    return tuple(m for m in outcome.messages if m.per_call)


def percall_wire_cache_prefix(outcome: PerCallRewriteOutcome) -> str:
    """Cache prefix from persistent messages only; per-call markers never reach the hash."""
    return percall_cache_prefix(percall_persistent_messages(outcome.messages))


def provider_wire_segments(
    assembled: RuntimeAssembledContext,
) -> tuple[RuntimeContextSegment, ...]:
    """Provider-wire view: persisted segments plus per-call-only wire segments for one call."""
    return (*assembled.segments, *assembled.percall_wire_segments)


__all__ = [
    "EMPTY_PERCALL_CHAIN",
    "MAX_PERCALL_WIRE_CHARS",
    "apply_percall_chain",
    "percall_ephemeral_messages",
    "percall_wire_cache_prefix",
    "percall_wire_segments",
    "provider_wire_segments",
    "segments_to_percall_messages",
]
