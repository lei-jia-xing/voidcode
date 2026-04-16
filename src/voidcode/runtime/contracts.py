from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

from .events import EventEnvelope
from .session import SessionState
from .task import BackgroundTaskState, StoredBackgroundTaskSummary


@dataclass(frozen=True, slots=True)
class RuntimeRequest:
    prompt: str
    session_id: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)
    allocate_session_id: bool = False


def validate_session_id(session_id: str) -> str:
    if not session_id:
        raise ValueError("session_id must be a non-empty string when provided")
    if "/" in session_id:
        raise ValueError("session_id must not contain '/'")
    return session_id


@dataclass(frozen=True, slots=True)
class RuntimeResponse:
    session: SessionState
    events: tuple[EventEnvelope, ...] = ()
    output: str | None = None


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
