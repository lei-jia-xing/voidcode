from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..runtime.contracts import RuntimeRequest, RuntimeResponse, RuntimeStreamChunk
from ..runtime.events import EventEnvelope
from ..runtime.permission import PermissionResolution
from ..runtime.service import VoidCodeRuntime
from ..runtime.session import SessionState, StoredSessionSummary
from .models import (
    TuiSessionSnapshot,
    TuiSessionState,
    TuiSessionSummary,
    TuiTimelineEvent,
)
from .models import (
    TuiStreamChunk as TuiStreamEventChunk,
)


@runtime_checkable
class TuiRuntimeBackend(Protocol):
    def list_sessions(self) -> tuple[StoredSessionSummary, ...]: ...

    def resume(
        self,
        session_id: str,
        *,
        approval_request_id: str | None = None,
        approval_decision: PermissionResolution | None = None,
    ) -> RuntimeResponse: ...

    def run_stream(self, request: RuntimeRequest) -> Iterator[RuntimeStreamChunk]: ...

    def resume_stream(
        self,
        session_id: str,
        *,
        approval_request_id: str | None = None,
        approval_decision: PermissionResolution | None = None,
    ) -> Iterator[RuntimeStreamChunk]: ...


@dataclass(slots=True)
class TuiRuntimeClient:
    runtime: TuiRuntimeBackend

    @classmethod
    def for_workspace(cls, *, workspace: Path) -> TuiRuntimeClient:
        return cls(runtime=VoidCodeRuntime(workspace=workspace))

    def list_sessions(self) -> tuple[TuiSessionSummary, ...]:
        return tuple(
            _normalize_session_summary(session) for session in self.runtime.list_sessions()
        )

    def open_session(self, session_id: str) -> TuiSessionSnapshot:
        return _normalize_response(self.runtime.resume(session_id))

    def replay_session(self, session_id: str) -> Iterator[TuiStreamEventChunk]:
        yield from _normalize_stream(self.runtime.resume_stream(session_id))

    def stream_run(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        metadata: dict[str, object] | None = None,
        allocate_session_id: bool = False,
    ) -> Iterator[TuiStreamEventChunk]:
        request = RuntimeRequest(
            prompt=prompt,
            session_id=session_id,
            metadata={} if metadata is None else dict(metadata),
            allocate_session_id=allocate_session_id,
        )
        yield from _normalize_stream(self.runtime.run_stream(request))

    def resolve_approval(
        self,
        *,
        session_id: str,
        request_id: str,
        decision: PermissionResolution,
    ) -> Iterator[TuiStreamEventChunk]:
        yield from _normalize_stream(
            self.runtime.resume_stream(
                session_id,
                approval_request_id=request_id,
                approval_decision=decision,
            )
        )


def _normalize_session_summary(session: StoredSessionSummary) -> TuiSessionSummary:
    return TuiSessionSummary(
        session_id=session.session.id,
        status=session.status,
        turn=session.turn,
        prompt=session.prompt,
        updated_at=session.updated_at,
    )


def _normalize_response(response: RuntimeResponse) -> TuiSessionSnapshot:
    return TuiSessionSnapshot(
        session=_normalize_session_state(response.session),
        timeline=tuple(_normalize_event(event) for event in response.events),
        output=response.output,
    )


def _normalize_stream(chunks: Iterator[RuntimeStreamChunk]) -> Iterator[TuiStreamEventChunk]:
    for chunk in chunks:
        yield TuiStreamEventChunk(
            kind=chunk.kind,
            session=_normalize_session_state(chunk.session),
            event=_normalize_event(chunk.event) if chunk.event is not None else None,
            output=chunk.output,
        )


def _normalize_session_state(session: SessionState) -> TuiSessionState:
    return TuiSessionState(
        session_id=session.session.id,
        status=session.status,
        turn=session.turn,
        metadata=dict(session.metadata),
    )


def _normalize_event(event: EventEnvelope) -> TuiTimelineEvent:
    return TuiTimelineEvent(
        session_id=event.session_id,
        sequence=event.sequence,
        event_type=event.event_type,
        source=event.source,
        payload=dict(event.payload),
    )
