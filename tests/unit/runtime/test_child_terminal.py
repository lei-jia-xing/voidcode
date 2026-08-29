from __future__ import annotations

from voidcode.runtime.child_terminal import child_completion_evidence, child_terminal_outcome
from voidcode.runtime.contracts import RuntimeResponse
from voidcode.runtime.events import EventEnvelope
from voidcode.runtime.session import SessionRef, SessionState


def _event(event_type: str, payload: dict[str, object] | None = None) -> EventEnvelope:
    return EventEnvelope(
        session_id="child-session",
        sequence=1,
        event_type=event_type,
        source="runtime" if event_type == "runtime.tool_completed" else "graph",
        payload=payload or {},
    )


def _response(status: str, events: tuple[EventEnvelope, ...]) -> RuntimeResponse:
    return RuntimeResponse(
        session=SessionState(session=SessionRef(id="child-session", parent_id="parent"), status=status),
        events=events,
    )


def test_submit_result_handoff_followed_by_response_ready_proves_completion() -> None:
    events = (
        _event(
            "runtime.tool_completed",
            {"tool": "submit_result", "status": "ok", "handoff": {"summary": "done"}},
        ),
        _event("graph.response_ready", {"source": "submit_result"}),
    )

    evidence = child_completion_evidence(events)
    assert evidence.completed is True
    assert evidence.handoff == {"summary": "done"}
    assert evidence.response_ready is True
    assert child_terminal_outcome(_response("interrupted", events)) == "completed"


def test_handoff_without_response_ready_is_not_completion() -> None:
    events = (
        _event(
            "runtime.tool_completed",
            {"tool": "submit_result", "status": "ok", "handoff": {"summary": "done"}},
        ),
    )

    evidence = child_completion_evidence(events)
    assert evidence.completed is False
    assert evidence.response_ready is False
    assert child_terminal_outcome(_response("interrupted", events)) is None


def test_interrupted_child_without_handoff_has_no_terminal_outcome() -> None:
    events = (_event("graph.response_ready", {"output_preview": "partial"}),)

    evidence = child_completion_evidence(events)
    assert evidence.completed is False
    assert evidence.handoff is None
    assert child_terminal_outcome(_response("interrupted", events)) is None


def test_terminal_session_rows_map_directly_to_terminal_outcome() -> None:
    assert child_terminal_outcome(_response("completed", ())) == "completed"
    assert child_terminal_outcome(_response("failed", ())) == "failed"
