from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

from .events import EventEnvelope
from .session import SessionRef, SessionState
from .task import BackgroundTaskState, StoredBackgroundTaskSummary


class RuntimeRequestError(ValueError):
    """Raised when a client-supplied runtime request is invalid."""


class UnknownSessionError(ValueError):
    """Raised when a referenced session does not exist in storage."""


@dataclass(frozen=True, slots=True)
class RuntimeRequest:
    prompt: str
    session_id: str | None = None
    parent_session_id: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)
    allocate_session_id: bool = False


def validate_session_reference_id(value: str, *, field_name: str = "session_id") -> str:
    if not value:
        raise RuntimeRequestError(f"{field_name} must be a non-empty string when provided")
    if "/" in value:
        raise RuntimeRequestError(f"{field_name} must not contain '/'")
    return value


def validate_session_id(session_id: str) -> str:
    return validate_session_reference_id(session_id, field_name="session_id")


@dataclass(frozen=True, slots=True)
class RuntimeResponse:
    session: SessionState
    events: tuple[EventEnvelope, ...] = ()
    output: str | None = None


type RuntimeNotificationKind = Literal[
    "completion",
    "failure",
    "cancellation",
    "approval_blocked",
]
type RuntimeNotificationStatus = Literal["unread", "acknowledged"]


@dataclass(frozen=True, slots=True)
class RuntimeSessionResult:
    session: SessionState
    prompt: str
    status: str
    summary: str
    output: str | None = None
    error: str | None = None
    transcript: tuple[EventEnvelope, ...] = ()
    last_event_sequence: int = 0


@dataclass(frozen=True, slots=True)
class RuntimeNotification:
    id: str
    session: SessionRef
    kind: RuntimeNotificationKind
    status: RuntimeNotificationStatus
    summary: str
    event_sequence: int
    created_at: int
    acknowledged_at: int | None = None
    payload: dict[str, object] = field(default_factory=dict)


type RuntimeStreamChunkKind = Literal["event", "output"]


@dataclass(frozen=True, slots=True)
class RuntimeStreamChunk:
    kind: RuntimeStreamChunkKind
    session: SessionState
    event: EventEnvelope | None = None
    output: str | None = None

    def __post_init__(self) -> None:
        if self.kind == "event" and self.event is None:
            raise ValueError("event chunks require an event")
        if self.kind == "output" and self.output is None:
            raise ValueError("output chunks require output content")


@runtime_checkable
class RuntimeEntrypoint(Protocol):
    def run(self, request: RuntimeRequest) -> RuntimeResponse: ...


@runtime_checkable
class StreamingRuntimeEntrypoint(Protocol):
    def run_stream(self, request: RuntimeRequest) -> Iterator[RuntimeStreamChunk]: ...


@runtime_checkable
class BackgroundTaskRuntimeEntrypoint(Protocol):
    def start_background_task(self, request: RuntimeRequest) -> BackgroundTaskState: ...

    def load_background_task(self, task_id: str) -> BackgroundTaskState: ...

    def list_background_tasks(self) -> tuple[StoredBackgroundTaskSummary, ...]: ...

    def cancel_background_task(self, task_id: str) -> BackgroundTaskState: ...
