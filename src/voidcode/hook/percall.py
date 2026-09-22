from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Literal, Protocol

PerCallRewriteAction = Literal["unchanged", "rewrite"]

_MAX_HANDLERS = 16

# ponytail: bounded chain length (16), sync-only handlers; go async/bigger only with measured need.


@dataclass(frozen=True, slots=True)
class PerCallMessage:
    """One bound message; per_call=True marks per-call-only (never persisted, never in cache prefix)."""

    role: str
    content: str
    per_call: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.role, str) or not self.role.strip():
            raise ValueError("per-call message role must be non-empty")
        if not isinstance(self.content, str):
            raise ValueError("per-call message content must be a string")
        if not isinstance(self.per_call, bool):
            raise ValueError("per-call marker must be a boolean")


@dataclass(frozen=True, slots=True)
class PerCallRewriteDecision:
    """Small result vocabulary for one chained handler."""

    action: PerCallRewriteAction
    messages: tuple[PerCallMessage, ...] | None = None

    def __post_init__(self) -> None:
        if self.action not in {"unchanged", "rewrite"}:
            raise ValueError(f"unsupported per-call handler action: {self.action}")
        if self.action == "unchanged":
            if self.messages is not None:
                raise ValueError("unchanged per-call decision cannot provide messages")
            return
        if not isinstance(self.messages, tuple) or not self.messages:
            raise ValueError("per-call rewrite must provide a non-empty message tuple")
        if not all(isinstance(m, PerCallMessage) for m in self.messages):
            raise ValueError("per-call rewrite messages must be PerCallMessage items")
        object.__setattr__(self, "messages", tuple(self.messages))


class PerCallHandler(Protocol):
    """Sync-only chained handler: sees prior handler output, returns rewrite or unchanged."""

    def __call__(self, messages: tuple[PerCallMessage, ...], /) -> PerCallRewriteDecision: ...


@dataclass(frozen=True, slots=True)
class PerCallHandlerBinding:
    name: str
    handler: PerCallHandler
    priority: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("per-call handler name must be non-empty")
        if not isinstance(self.priority, int) or isinstance(self.priority, bool):
            raise ValueError("per-call handler priority must be an integer")
        if not callable(self.handler):
            raise ValueError("per-call handler must be callable")


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
        if not isinstance(self.bindings, tuple):
            raise ValueError("per-call chain bindings must be a tuple")
        if len(self.bindings) > _MAX_HANDLERS:
            raise ValueError(f"per-call chain exceeds {_MAX_HANDLERS} handlers")
        if not all(isinstance(b, PerCallHandlerBinding) for b in self.bindings):
            raise ValueError("per-call chain bindings must be PerCallHandlerBinding items")
        names = [b.name for b in self.bindings]
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate per-call handler name: {names}")
        object.__setattr__(self, "bindings", tuple(sorted(self.bindings, key=lambda b: b.priority)))

    def apply(self, *, messages: Sequence[PerCallMessage]) -> PerCallRewriteOutcome:
        """Chain handlers over a deep clone; input history is never mutated."""
        if not isinstance(messages, (tuple, list)) or not all(isinstance(m, PerCallMessage) for m in messages):
            raise ValueError("per-call apply requires PerCallMessage items")
        current = deepcopy(tuple(messages))
        names: list[str] = []
        changed = False
        for binding in self.bindings:
            names.append(binding.name)
            decision = binding.handler(deepcopy(current))
            if not isinstance(decision, PerCallRewriteDecision):
                raise ValueError(f"per-call handler '{binding.name}' returned an invalid decision")
            if decision.action == "unchanged":
                continue
            assert decision.messages is not None
            nxt = deepcopy(tuple(decision.messages))
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
    "PerCallRewriteAction",
    "PerCallRewriteDecision",
    "PerCallRewriteOutcome",
    "percall_cache_prefix",
    "percall_messages_sha256",
    "percall_persistent_messages",
]
