from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass


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
class PerCallRewriteOutcome:
    messages: tuple[PerCallMessage, ...]
    handler_names: tuple[str, ...] = ()
    changed: bool = False


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
    "PerCallMessage",
    "PerCallRewriteOutcome",
    "percall_cache_prefix",
    "percall_messages_sha256",
    "percall_persistent_messages",
]
