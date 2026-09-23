from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Literal, Protocol

_MAX_HANDLERS = 16

# ponytail: bounded chain length (16), sync-only handlers; go async/bigger only with measured need.


@dataclass(frozen=True, slots=True)
class PerCallMessage:
    """One bound message; per_call=True marks per-call-only (never persisted, never in cache prefix)."""

    role: str
    content: str
    per_call: bool = False

    def __post_init__(self) -> None:
        if not self.role.strip():
            raise ValueError("per-call message role must be non-empty")


@dataclass(frozen=True, slots=True)
class UnchangedPerCall:
    """Handler leaves the message list untouched."""

    action: Literal["unchanged"] = "unchanged"


@dataclass(frozen=True, slots=True)
class RewritePerCall:
    """Handler replaces the message list; the payload is part of the type."""

    messages: tuple[PerCallMessage, ...]
    action: Literal["rewrite"] = "rewrite"

    def __post_init__(self) -> None:
        if not self.messages:
            raise ValueError("per-call rewrite must provide a non-empty message tuple")
        if not all(isinstance(m, PerCallMessage) for m in self.messages):
            raise ValueError("per-call rewrite messages must be PerCallMessage items")
        object.__setattr__(self, "messages", tuple(self.messages))


# Small result vocabulary for one chained handler.
type PerCallRewriteDecision = UnchangedPerCall | RewritePerCall


class PerCallHandler(Protocol):
    """Sync-only chained handler: sees prior handler output, returns rewrite or unchanged."""

    def __call__(self, messages: tuple[PerCallMessage, ...], /) -> PerCallRewriteDecision: ...


@dataclass(frozen=True, slots=True)
class PerCallHandlerBinding:
    name: str
    handler: PerCallHandler
    priority: int = 0

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("per-call handler name must be non-empty")
        if isinstance(self.priority, bool):
            raise ValueError("per-call handler priority must be an integer")


@dataclass(frozen=True, slots=True)
class PerCallRewriteOutcome:
    messages: tuple[PerCallMessage, ...]
    handler_names: tuple[str, ...] = ()
    changed: bool = False


@dataclass(frozen=True, slots=True)
class PerCallChain:
    """Frozen ordered handler chain; pure contract only, no executor or runtime wiring."""

    bindings: tuple[PerCallHandlerBinding, ...] = ()

    def __post_init__(self) -> None:
        if len(self.bindings) > _MAX_HANDLERS:
            raise ValueError(f"per-call chain exceeds {_MAX_HANDLERS} handlers")
        names = [b.name for b in self.bindings]
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate per-call handler name: {names}")
        object.__setattr__(self, "bindings", tuple(sorted(self.bindings, key=lambda b: b.priority)))

    def apply(self, *, messages: Sequence[PerCallMessage]) -> PerCallRewriteOutcome:
        """Chain handlers over a deep clone; input history is never mutated."""
        current = deepcopy(tuple(messages))
        names: list[str] = []
        changed = False
        for binding in self.bindings:
            names.append(binding.name)
            decision = binding.handler(deepcopy(current))
            if decision.action == "unchanged":
                continue
            nxt = deepcopy(decision.messages)
            changed = changed or nxt != current
            current = nxt
        return PerCallRewriteOutcome(messages=current, handler_names=tuple(names), changed=changed)


def percall_persistent_messages(messages: Sequence[PerCallMessage]) -> tuple[PerCallMessage, ...]:
    """Return only persistable messages; per-call-only markers are dropped."""
    return tuple(m for m in messages if not m.per_call)


def percall_messages_sha256(messages: Sequence[PerCallMessage]) -> str:
    """Hash persistent messages only (role/content keys); per-call markers never reach the hash."""
    payload = [{"role": m.role, "content": m.content} for m in percall_persistent_messages(messages)]
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def percall_cache_prefix(messages: Sequence[PerCallMessage]) -> str:
    """Cache prefix derived from the persistent-message hash only."""
    return percall_messages_sha256(messages)[:16]


__all__ = [
    "PerCallChain",
    "PerCallHandler",
    "PerCallHandlerBinding",
    "PerCallMessage",
    "PerCallRewriteDecision",
    "PerCallRewriteOutcome",
    "RewritePerCall",
    "UnchangedPerCall",
    "percall_cache_prefix",
    "percall_messages_sha256",
    "percall_persistent_messages",
]
