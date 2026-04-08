from __future__ import annotations

from dataclasses import dataclass
from typing import cast
from unittest.mock import MagicMock

from voidcode.runtime.contracts import RuntimeRequest, RuntimeResponse, RuntimeStreamChunk
from voidcode.runtime.events import EventEnvelope, EventSource
from voidcode.runtime.session import SessionRef, SessionState, SessionStatus, StoredSessionSummary
from voidcode.tui import TuiRuntimeClient


@dataclass(slots=True)
class _StubRuntime:
    list_sessions: MagicMock
    resume: MagicMock
    run_stream: MagicMock
    resume_stream: MagicMock


def _session_state(
    session_id: str,
    *,
    status: SessionStatus = "completed",
    turn: int = 1,
    metadata: dict[str, object] | None = None,
) -> SessionState:
    return SessionState(
        session=SessionRef(id=session_id),
        status=status,
        turn=turn,
        metadata={} if metadata is None else dict(metadata),
    )


def _event(
    sequence: int,
    event_type: str,
    *,
    session_id: str = "session-1",
    source: str = "runtime",
    **payload: object,
) -> EventEnvelope:
    return EventEnvelope(
        session_id=session_id,
        sequence=sequence,
        event_type=event_type,
        source=cast(EventSource, source),
        payload=dict(payload),
    )


def _runtime() -> _StubRuntime:
    return _StubRuntime(
        list_sessions=MagicMock(),
        resume=MagicMock(),
        run_stream=MagicMock(),
        resume_stream=MagicMock(),
    )


def test_list_sessions_uses_runtime_list_only() -> None:
    runtime = _runtime()
    runtime.list_sessions.return_value = (
        StoredSessionSummary(
            session=SessionRef(id="session-2"),
            status="waiting",
            turn=3,
            prompt="write notes.txt",
            updated_at=1700000000,
        ),
    )
    client = TuiRuntimeClient(runtime=runtime)

    result = client.list_sessions()

    assert len(result) == 1
    assert result[0].session_id == "session-2"
    assert result[0].status == "waiting"
    assert result[0].turn == 3
    assert result[0].prompt == "write notes.txt"
    assert result[0].updated_at == 1700000000
    runtime.list_sessions.assert_called_once_with()
    runtime.resume.assert_not_called()
    runtime.run_stream.assert_not_called()
    runtime.resume_stream.assert_not_called()


def test_open_session_normalizes_replayed_response_without_cli_text_output() -> None:
    runtime = _runtime()
    runtime.resume.return_value = RuntimeResponse(
        session=_session_state("session-1", status="waiting", metadata={"workspace": "/tmp/demo"}),
        events=(
            _event(1, "runtime.request_received", prompt="write notes.txt"),
            _event(
                2,
                "runtime.approval_requested",
                request_id="approval-1",
                tool="write_file",
                decision="ask",
                arguments={"path": "notes.txt", "content": "hi"},
                target_summary="write notes.txt",
                reason="write-capable tool invocation",
                policy={"mode": "ask"},
            ),
        ),
        output=None,
    )
    client = TuiRuntimeClient(runtime=runtime)

    snapshot = client.open_session("session-1")

    assert snapshot.session.session_id == "session-1"
    assert snapshot.session.status == "waiting"
    assert [event.sequence for event in snapshot.timeline] == [1, 2]
    assert [event.event_type for event in snapshot.timeline] == [
        "runtime.request_received",
        "runtime.approval_requested",
    ]
    assert snapshot.pending_approval is not None
    assert snapshot.pending_approval.request_id == "approval-1"
    assert snapshot.pending_approval.tool == "write_file"
    assert snapshot.pending_approval.arguments == {"path": "notes.txt", "content": "hi"}
    runtime.resume.assert_called_once_with("session-1")
    runtime.list_sessions.assert_not_called()
    runtime.run_stream.assert_not_called()
    runtime.resume_stream.assert_not_called()


def test_replay_session_preserves_runtime_order_without_sorting() -> None:
    runtime = _runtime()
    runtime.resume_stream.return_value = iter(
        (
            RuntimeStreamChunk(
                kind="event",
                session=_session_state("session-1", status="running"),
                event=_event(5, "graph.loop_step", phase="later"),
            ),
            RuntimeStreamChunk(
                kind="event",
                session=_session_state("session-1", status="running"),
                event=_event(3, "runtime.skills_loaded", skills=[]),
            ),
            RuntimeStreamChunk(
                kind="output",
                session=_session_state("session-1", status="completed"),
                output="done\n",
            ),
        )
    )
    client = TuiRuntimeClient(runtime=runtime)

    replayed = tuple(client.replay_session("session-1"))

    assert [chunk.kind for chunk in replayed] == ["event", "event", "output"]
    assert [chunk.event.sequence for chunk in replayed[:2] if chunk.event is not None] == [5, 3]
    assert replayed[2].output == "done\n"
    runtime.resume_stream.assert_called_once_with("session-1")
    runtime.list_sessions.assert_not_called()
    runtime.resume.assert_not_called()
    runtime.run_stream.assert_not_called()


def test_stream_run_builds_runtime_request_and_preserves_unknown_event_types() -> None:
    runtime = _runtime()
    runtime.run_stream.return_value = iter(
        (
            RuntimeStreamChunk(
                kind="event",
                session=_session_state(
                    "session-9", status="running", metadata={"workspace": "/tmp/demo"}
                ),
                event=_event(
                    7,
                    "runtime.future_added",
                    source="runtime",
                    detail="kept as generic timeline event",
                ),
            ),
            RuntimeStreamChunk(
                kind="output",
                session=_session_state("session-9", status="completed"),
                output="answer",
            ),
        )
    )
    client = TuiRuntimeClient(runtime=runtime)

    metadata: dict[str, object] = {"client": "tui"}
    chunks = tuple(
        client.stream_run(
            "read README.md",
            session_id="session-9",
            metadata=metadata,
            allocate_session_id=True,
        )
    )

    assert len(chunks) == 2
    assert chunks[0].event is not None
    assert chunks[0].event.event_type == "runtime.future_added"
    assert chunks[0].event.payload == {"detail": "kept as generic timeline event"}
    assert chunks[0].approval_request is None
    assert chunks[1].output == "answer"
    runtime.run_stream.assert_called_once()
    request = runtime.run_stream.call_args.args[0]
    assert isinstance(request, RuntimeRequest)
    assert request.prompt == "read README.md"
    assert request.session_id == "session-9"
    assert request.metadata == {"client": "tui"}
    assert request.allocate_session_id is True
    assert metadata == {"client": "tui"}
    runtime.list_sessions.assert_not_called()
    runtime.resume.assert_not_called()
    runtime.resume_stream.assert_not_called()


def test_resolve_approval_uses_runtime_resume_stream_with_request_id() -> None:
    runtime = _runtime()
    runtime.resume_stream.return_value = iter(
        (
            RuntimeStreamChunk(
                kind="event",
                session=_session_state("session-approval", status="running"),
                event=_event(
                    8,
                    "runtime.approval_resolved",
                    request_id="approval-9",
                    decision="allow",
                ),
            ),
        )
    )
    client = TuiRuntimeClient(runtime=runtime)

    chunks = tuple(
        client.resolve_approval(
            session_id="session-approval",
            request_id="approval-9",
            decision="allow",
        )
    )

    assert len(chunks) == 1
    assert chunks[0].event is not None
    assert chunks[0].event.event_type == "runtime.approval_resolved"
    assert chunks[0].event.payload == {"request_id": "approval-9", "decision": "allow"}
    runtime.resume_stream.assert_called_once_with(
        "session-approval",
        approval_request_id="approval-9",
        approval_decision="allow",
    )
    runtime.list_sessions.assert_not_called()
    runtime.resume.assert_not_called()
    runtime.run_stream.assert_not_called()
