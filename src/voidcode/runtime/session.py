from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

type SessionStatus = Literal["idle", "running", "waiting", "completed", "failed"]
type SessionKind = Literal["top_level", "child"]


@dataclass(frozen=True, slots=True)
class SessionRef:
    id: str
    parent_id: str | None = None

    @property
    def kind(self) -> SessionKind:
        return "child" if self.parent_id is not None else "top_level"

    @property
    def is_child(self) -> bool:
        return self.parent_id is not None

    @property
    def is_top_level(self) -> bool:
        return self.parent_id is None


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
