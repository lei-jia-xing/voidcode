from __future__ import annotations

import pytest

from voidcode.runtime.events import EventEnvelope, EventSource
from voidcode.tui.events import SessionView
from voidcode.tui.theme import resolve_theme
from voidcode.tui.transcript import AssistantBlock, ErrorBlock, NoticeBlock, ToolBlock


def _view() -> SessionView:
    return SessionView(theme=resolve_theme(mode="dark", color_system=None), width=80)


def _event(
    event_type: str,
    sequence: int,
    payload: dict[str, object],
    *,
    source: EventSource = "runtime",
) -> EventEnvelope:
    return EventEnvelope(
        session_id="stream-outcome-test",
        sequence=sequence,
        event_type=event_type,
        source=source,
        payload=payload,
    )


def test_reused_native_call_id_keeps_tool_activities_and_artifacts_distinct() -> None:
    view = _view()
    events = [
        _event("runtime.request_received", 1, {"prompt": "Read two versions"}),
        _event(
            "core.model_turn",
            2,
            {"tool_batch_calls": [{"tool": "read", "tool_call_id": "read_1", "arguments": {"path": "note.txt"}}]},
            source="core",
        ),
        _event(
            "core.tool_request_created",
            3,
            {
                "tool": "read",
                "tool_call_id": "read_1",
                "display": {"kind": "context", "title": "First read", "summary": "first version"},
            },
            source="core",
        ),
        _event("runtime.tool_started", 4, {"tool": "read", "tool_call_id": "read_1"}),
        _event(
            "runtime.tool_progress",
            5,
            {"tool": "read", "tool_call_id": "read_1", "stream": "stdout", "chunk": "first progress"},
        ),
        _event(
            "runtime.tool_completed",
            6,
            {
                "tool": "read",
                "tool_call_id": "read_1",
                "status": "ok",
                "content": "first file result",
                "artifact_id": "artifact-first",
            },
            source="tool",
        ),
        _event(
            "core.model_turn",
            7,
            {"tool_batch_calls": [{"tool": "read", "tool_call_id": "read_1", "arguments": {"path": "note.txt"}}]},
            source="core",
        ),
        _event(
            "core.tool_request_created",
            8,
            {
                "tool": "read",
                "tool_call_id": "read_1",
                "display": {"kind": "context", "title": "Second read", "summary": "second version"},
            },
            source="core",
        ),
        _event("runtime.tool_started", 9, {"tool": "read", "tool_call_id": "read_1"}),
        _event(
            "runtime.tool_progress",
            10,
            {"tool": "read", "tool_call_id": "read_1", "stream": "stdout", "chunk": "second progress"},
        ),
        _event(
            "runtime.tool_completed",
            11,
            {
                "tool": "read",
                "tool_call_id": "read_1",
                "status": "ok",
                "content": "second file result",
                "artifact_id": "artifact-second",
            },
            source="tool",
        ),
    ]

    for event in events:
        view.apply_event(event)

    blocks = [block for block in view.transcript().blocks if isinstance(block, ToolBlock)]
    assert len(blocks) == 2
    first, second = blocks
    assert first.tool_call_id == second.tool_call_id == "read_1"
    assert first.body == ["└ stdout", "first progress", "first file result"]
    assert second.body == ["└ stdout", "second progress", "second file result"]
    assert first.artifact_id == "artifact-first"
    assert second.artifact_id == "artifact-second"


@pytest.mark.parametrize("status", ["running", "idle"])
def test_nonterminal_stream_close_preserves_live_rows_and_stops_spinners(status: str) -> None:
    view = _view()
    committed = view.transcript().add(NoticeBlock(text="committed history", settled=True))
    committed_rows = view.transcript().take_settled()
    assistant = AssistantBlock(text="partial answer", settled=False, streaming=True, pulse_frame=0)
    tool = ToolBlock(tool="read", title="Read", settled=False, streaming=True)
    view.transcript().add(assistant)
    view.transcript().add(tool)

    view.finish_stream(status)

    assert view.view_state().state == "Outcome unknown"
    assert not assistant.settled and not tool.settled
    assert not assistant.streaming and assistant.pulse_frame is None and not tool.streaming
    assert any("outcome is unknown" in block.text for block in view.transcript().blocks if isinstance(block, NoticeBlock))
    assert "partial answer" not in "\n".join(view.transcript().take_settled())
    assert committed in view.transcript().blocks and committed_rows


def test_transport_loss_is_not_a_failed_or_settled_run() -> None:
    view = _view()
    assistant = AssistantBlock(text="partial answer", settled=False, streaming=True, pulse_frame=0)
    view.transcript().add(assistant)

    view.finish_stream("running", error=ConnectionError("pipe lost"))

    assert view.view_state().state == "Stream disconnected"
    assert not assistant.settled and not assistant.streaming and assistant.pulse_frame is None
    assert any(isinstance(block, ErrorBlock) for block in view.transcript().blocks)
    assert any(isinstance(block, NoticeBlock) and "outcome is unknown" in block.text for block in view.transcript().blocks)


@pytest.mark.parametrize(
    ("status", "expected_state"),
    [("completed", "Idle"), ("failed", "Failed"), ("interrupted", "Interrupted")],
)
def test_terminal_status_remains_authoritative_after_iterator_error(status: str, expected_state: str) -> None:
    view = _view()
    assistant = AssistantBlock(text="terminal answer", settled=False)
    view.transcript().add(assistant)

    view.finish_stream(status, error=ConnectionError("iterator closed unexpectedly"))

    assert view.view_state().state == expected_state
    assert assistant.settled


@pytest.mark.parametrize(
    ("request_type", "resolution_type", "request_payload", "resolution_payload"),
    [
        (
            "runtime.approval_requested",
            "runtime.approval_resolved",
            {"request_id": "approval-1", "tool": "shell"},
            {"request_id": "approval-1", "decision": "allow"},
        ),
        (
            "runtime.question_requested",
            "runtime.question_answered",
            {"request_id": "question-1", "questions": []},
            {"request_id": "question-1", "responses": []},
        ),
    ],
)
def test_pending_overlay_waits_for_its_matching_resolution_event(
    request_type: str,
    resolution_type: str,
    request_payload: dict[str, object],
    resolution_payload: dict[str, object],
) -> None:
    view = _view()
    request_id = str(request_payload["request_id"])
    view.apply_event(_event(request_type, 1, request_payload))
    pending = view.take_pending_overlay()
    assert pending is not None
    view.resolve_overlay(request_id)

    view.finish_stream("running")
    view.apply_event(_event(resolution_type, 2, {**resolution_payload, "request_id": "other-request"}))
    assert view.view_state().pending == pending
    view.apply_event(_event(resolution_type, 3, resolution_payload))
    assert view.view_state().pending is None


def test_interrupted_runtime_failure_is_not_failed() -> None:
    view = _view()
    view.apply_event(_event("runtime.failed", 1, {"kind": "interrupted", "cancelled": True, "error": "runtime interruption"}))

    assert view.view_state().state == "Interrupted"
    assert not any(isinstance(block, ErrorBlock) for block in view.transcript().blocks)

    view.finish_stream("interrupted")

    assert view.view_state().state == "Interrupted"
    assert all(block.settled for block in view.transcript().blocks)
    assert any(isinstance(block, NoticeBlock) and block.text == "Run interrupted." for block in view.transcript().blocks)


@pytest.mark.parametrize(
    ("request_type", "request_payload"),
    [
        ("runtime.approval_requested", {"request_id": "approval-1", "tool": "shell"}),
        ("runtime.question_requested", {"request_id": "question-1", "questions": []}),
    ],
)
def test_failed_stream_does_not_reopen_an_unresolved_answer(request_type: str, request_payload: dict[str, object]) -> None:
    view = _view()
    request_id = str(request_payload["request_id"])
    view.apply_event(_event(request_type, 1, request_payload))
    pending = view.take_pending_overlay()
    assert pending is not None
    view.resolve_overlay(request_id)

    view.apply_event(_event("runtime.failed", 2, {"error": "runtime failure"}))
    view.finish_stream("failed")

    assert view.view_state().pending == pending
    assert view.take_pending_overlay() is None
