from __future__ import annotations

from voidcode.cli.runtime_gateway import incomplete_runtime_stream_message, session_result_exit_code
from voidcode.cli.support import EXIT_APPROVAL_DENIED, EXIT_APPROVAL_REQUIRED, EXIT_RUNTIME_ERROR, EXIT_SUCCESS, StreamResult
from voidcode.runtime.events import EventEnvelope
from voidcode.runtime.session import SessionRef, SessionState


def _result(status: str, *events: EventEnvelope) -> StreamResult:
    return StreamResult(None, SessionState(SessionRef("gateway-test"), status=status), tuple(events))  # type: ignore[arg-type]


def test_incomplete_stream_requires_a_terminal_status_not_prior_denial() -> None:
    denial = EventEnvelope("gateway-test", 1, "runtime.tool_completed", "runtime", {"permission_denied": True})

    assert incomplete_runtime_stream_message(_result("completed", denial)) is None
    assert incomplete_runtime_stream_message(_result("failed", denial)) is None
    assert incomplete_runtime_stream_message(_result("interrupted", denial)) == (
        "runtime reported an interrupted outcome; the request did not complete"
    )
    assert "running" in (incomplete_runtime_stream_message(_result("running", denial)) or "")
    assert incomplete_runtime_stream_message(_result("waiting")) is not None


def test_session_exit_code_requires_completed_status() -> None:
    assert session_result_exit_code(_result("completed")) == EXIT_SUCCESS
    assert session_result_exit_code(_result("failed")) == EXIT_RUNTIME_ERROR
    assert session_result_exit_code(_result("interrupted")) == EXIT_RUNTIME_ERROR
    assert session_result_exit_code(_result("idle")) == EXIT_RUNTIME_ERROR

    denied = EventEnvelope("gateway-test", 1, "runtime.approval_resolved", "runtime", {"decision": "deny"})
    assert session_result_exit_code(_result("completed", denied)) == EXIT_APPROVAL_DENIED


def test_waiting_status_is_only_expected_with_a_pending_blocked_event() -> None:
    approval = EventEnvelope("gateway-test", 1, "runtime.approval_requested", "runtime", {"request_id": "approval-1"})
    waiting = _result("waiting", approval)
    assert incomplete_runtime_stream_message(waiting) is None
    assert session_result_exit_code(waiting) == EXIT_APPROVAL_REQUIRED
