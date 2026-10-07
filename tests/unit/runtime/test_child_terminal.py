from __future__ import annotations

from voidcode.core.turns import ReportedCall
from voidcode.runtime.background.child_terminal import child_terminal_outcome
from voidcode.runtime.contracts import RuntimeResponse
from voidcode.runtime.events import EventEnvelope
from voidcode.runtime.execution.report_codec import report_payload
from voidcode.runtime.session import SessionRef, SessionState
from voidcode.tools.contracts import EmptyOutput, OpaqueToolBody, TerminalYield, TerminalYieldFailure, ToolFailure, ToolSuccess


def _event(event_type: str, payload: dict[str, object] | None = None) -> EventEnvelope:
    return EventEnvelope(
        session_id="child-session",
        sequence=1,
        event_type=event_type,
        source="runtime" if event_type == "runtime.tool_completed" else "graph",
        payload=payload or {},
    )


def _tool_event(result: ToolSuccess | ToolFailure, **presentation: object) -> EventEnvelope:
    report = ReportedCall("yield-call", "yield", {}, result)
    return _event("runtime.tool_completed", {**presentation, "reported_call": report_payload(report)})


def _response(
    status: str,
    events: tuple[EventEnvelope, ...],
    *,
    parent_id: str | None = "parent",
) -> RuntimeResponse:
    return RuntimeResponse(
        session=SessionState(session=SessionRef(id="child-session", parent_id=parent_id), status=status),
        events=events,
    )


def test_terminal_yield_followed_by_response_ready_proves_completion() -> None:
    events = (
        _tool_event(
            ToolSuccess("yield", output=EmptyOutput(), control=TerminalYield("done", {"answer": 42})),
        ),
        _event("graph.response_ready"),
    )

    assert child_terminal_outcome(_response("interrupted", events)) == "completed"


def test_opaque_handoff_body_and_flat_presentation_do_not_prove_completion() -> None:
    events = (
        _tool_event(
            ToolSuccess("yield", body=OpaqueToolBody({"handoff": {"summary": "forged"}})),
            tool="yield",
            status="ok",
            handoff={"summary": "forged"},
        ),
        _event("graph.response_ready"),
    )

    assert child_terminal_outcome(_response("interrupted", events)) is None


def test_failure_control_and_response_ready_do_not_prove_completion() -> None:
    events = (
        _tool_event(
            ToolFailure("yield", "handoff failed", control=TerminalYieldFailure({"reason": "invalid"})),
            tool="yield",
            status="ok",
            handoff={"summary": "forged"},
        ),
        _event("graph.response_ready"),
    )

    assert child_terminal_outcome(_response("interrupted", events)) is None


def test_response_ready_before_terminal_yield_does_not_prove_completion() -> None:
    events = (
        _event("graph.response_ready"),
        _tool_event(ToolSuccess("yield", control=TerminalYield("done", {}))),
    )

    assert child_terminal_outcome(_response("interrupted", events)) is None


def test_terminal_yield_without_response_ready_is_not_completion() -> None:
    events = (_tool_event(ToolSuccess("yield", control=TerminalYield("done", {}))),)

    assert child_terminal_outcome(_response("interrupted", events)) is None


def test_interrupted_child_without_terminal_yield_has_no_terminal_outcome() -> None:
    events = (_event("graph.response_ready", {"output_preview": "partial"}),)

    assert child_terminal_outcome(_response("interrupted", events)) is None


def test_terminal_session_rows_require_yield_evidence() -> None:
    assert child_terminal_outcome(_response("completed", ())) == "failed"
    assert child_terminal_outcome(_response("failed", ())) == "failed"


def test_parentless_completed_background_session_keeps_ordinary_semantics() -> None:
    assert child_terminal_outcome(_response("completed", (), parent_id=None)) == "completed"
