from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

type SessionStatus = Literal["idle", "running", "waiting", "completed", "failed"]


@dataclass(frozen=True, slots=True)
class SessionRef:
    id: str


@dataclass(frozen=True, slots=True)
class SessionState:
    session: SessionRef
    status: SessionStatus = "idle"
    turn: int = 0
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class StoredSessionSummary:
    session: SessionRef
    status: SessionStatus
    turn: int
    prompt: str
    updated_at: int
