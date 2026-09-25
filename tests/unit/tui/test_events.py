"""``SessionView``: the runtime-event projection the inline app loop paints.

Every fixture is a synthetic ``EventEnvelope`` -- no runtime, no I/O, no
terminal.  Assertions are on the observable view: rendered transcript rows and
``view_state()``.
"""

from __future__ import annotations

from voidcode.runtime.events import EventEnvelope
from voidcode.tui.events import ApprovalRequest, QuestionRequest, SessionView, format_runtime_error

from .conftest import plain, theme

WIDTH = 80
SESSION = "session-4f2a1b2c9d"
OTHER_SESSION = "session-99aa88bb77"

READ_DISPLAY = {"kind": "read", "title": "Read", "summary": "src/app.py", "copyable": {"path": "src/app.py"}, "args": ["src/app.py"]}
EDIT_DISPLAY = {"kind": "edit", "title": "Edit", "summary": "src/app.py (1 change)", "copyable": {"path": "src/app.py"}, "args": ["src/app.py"]}


def envelope(
    event_type: str,
    payload: dict[str, object] | None = None,
    *,
    sequence: int = 1,
    source: str = "runtime",
    session_id: str = SESSION,
) -> EventEnvelope:
    return EventEnvelope(session_id=session_id, sequence=sequence, event_type=event_type, source=source, payload=payload or {})  # type: ignore[arg-type]


def view(width: int = WIDTH) -> SessionView:
    return SessionView(theme=theme(), width=width)


def rows(state: SessionView) -> list[str]:
    return plain(state.transcript().rows())


def text(state: SessionView) -> str:
    return "\n".join(rows(state))


# ---------------------------------------------------------------------------
# Dedupe and the live-only bypass
# ---------------------------------------------------------------------------


def test_persisted_sequences_are_deduped_per_session() -> None:
    state = view()
    completed = {"task_id": "task-abcdef1234567", "summary_output": "done"}
    assert state.apply_event(envelope("runtime.background_task_completed", completed, sequence=7)) is True
    assert state.apply_event(envelope("runtime.background_task_completed", completed, sequence=7)) is False
    assert state.apply_event(envelope("runtime.background_task_completed", completed, sequence=6)) is False
    assert text(state).count("Background completed") == 1

    # A replayed child session keeps its own cursor.
    assert state.apply_event(envelope("runtime.background_task_completed", completed, sequence=7, session_id=OTHER_SESSION)) is True


def test_live_only_stream_events_bypass_the_dedupe() -> None:
    state = view()
    delta = {"channel": "text", "kind": "delta", "text": "Hello "}
    assert state.apply_event(envelope("graph.provider_stream", delta, sequence=3, source="graph")) is True
    assert (
        state.apply_event(envelope("graph.provider_stream", {"channel": "text", "kind": "delta", "text": "world"}, sequence=3, source="graph"))
        is True
    )
    assert "Hello world" in text(state)

    # The live deltas never advanced the persisted cursor, so the first
    # persisted event at that same sequence still lands -- and is deduped after.
    assert state.apply_event(
        envelope("runtime.tool_started", {"tool": "read", "tool_call_id": "call-1", "display": READ_DISPLAY}, sequence=3, source="graph")
    )
    assert (
        state.apply_event(
            envelope("runtime.tool_started", {"tool": "read", "tool_call_id": "call-1", "display": READ_DISPLAY}, sequence=3, source="graph")
        )
        is False
    )


def test_events_the_old_app_ignored_stay_ignored() -> None:
    state = view()
    for event_type in ("runtime.skills_loaded", "graph.loop_step", "runtime.background_task_registered", "graph.tool_call_delta"):
        assert state.apply_event(envelope(event_type, {"anything": 1}, sequence=1)) is False
    assert rows(state) == []


# ---------------------------------------------------------------------------
# Streaming and retraction
# ---------------------------------------------------------------------------


def test_provider_stream_renders_thinking_then_prose() -> None:
    state = view()
    state.apply_event(
        envelope("graph.provider_stream", {"channel": "reasoning", "kind": "delta", "text": "weigh the options"}, sequence=2, source="graph")
    )
    assert "weigh the options" in text(state)

    state.apply_event(envelope("graph.provider_stream", {"channel": "text", "kind": "delta", "text": "answer"}, sequence=2, source="graph"))
    # Prose collapses the reasoning block (the old app's collapse_block).
    assert "weigh the options" not in text(state)
    assert "answer" in text(state)


def test_retraction_clears_the_discarded_attempts_text() -> None:
    state = view()
    state.apply_event(
        envelope("graph.provider_stream", {"channel": "reasoning", "kind": "delta", "text": "stale thinking"}, sequence=4, source="graph")
    )
    state.apply_event(envelope("graph.provider_stream", {"channel": "text", "kind": "delta", "text": "stale answer"}, sequence=4, source="graph"))
    assert "stale answer" in text(state)

    retry = {"discarded_streamed_output": True, "attempt": 2}
    assert state.apply_event(envelope("runtime.provider_transient_retry", retry, sequence=4)) is True
    assert "stale answer" not in text(state)
    assert "stale thinking" not in text(state)

    # The surviving attempt's text is the only assistant output for the turn.
    state.apply_event(envelope("graph.provider_stream", {"channel": "text", "kind": "delta", "text": "fresh answer"}, sequence=5, source="graph"))
    assert text(state).strip().endswith("fresh answer")


def test_provider_fallback_without_the_flag_does_not_retract() -> None:
    state = view()
    state.apply_event(envelope("graph.provider_stream", {"channel": "text", "kind": "delta", "text": "kept"}, sequence=1, source="graph"))
    assert state.apply_event(envelope("runtime.provider_fallback", {"from": "a", "to": "b"}, sequence=2)) is False
    assert "kept" in text(state)


def test_streamed_provider_text_tracks_deltas_and_retraction() -> None:
    state = view()
    assert state.streamed_provider_text is False
    state.apply_event(envelope("graph.provider_stream", {"channel": "reasoning", "kind": "delta", "text": "hmm"}, sequence=1, source="graph"))
    assert state.streamed_provider_text is False

    state.apply_event(envelope("graph.provider_stream", {"channel": "text", "kind": "delta", "text": "answer"}, sequence=1, source="graph"))
    assert state.streamed_provider_text is True

    state.apply_event(envelope("runtime.provider_transient_retry", {"discarded_streamed_output": True}, sequence=1))
    assert state.streamed_provider_text is False

    state.apply_event(envelope("graph.provider_stream", {"channel": "text", "kind": "delta", "text": "answer"}, sequence=2, source="graph"))
    state.finish_stream("completed")
    assert state.streamed_provider_text is False


# ---------------------------------------------------------------------------
# Tool block lifecycle
# ---------------------------------------------------------------------------


def test_tool_block_lifecycle_drives_the_display_payload() -> None:
    state = view()
    assert state.apply_event(
        envelope("graph.tool_request_created", {"tool": "read", "tool_call_id": "call-1", "display": READ_DISPLAY}, sequence=1, source="graph")
    )
    assert "Read · src/app.py" in text(state)

    state.apply_event(envelope("runtime.tool_started", {"tool": "read", "tool_call_id": "call-1", "display": READ_DISPLAY}, sequence=2))
    assert "Read · src/app.py" in text(state)
    assert "… (streaming)" in text(state)

    state.apply_event(
        envelope("runtime.tool_progress", {"tool_call_id": "call-1", "stream": "stdout", "chunk": "one\ntwo\n"}, sequence=3, source="tool")
    )
    assert "└ stdout" in text(state)
    state.apply_event(envelope("runtime.tool_progress", {"tool_call_id": "call-1", "stream": "stderr", "chunk": "warn"}, sequence=4, source="tool"))
    # The collapsed card shows only the preview budget (ctrl+o expands it).
    state.transcript().set_expanded(True)
    assert "└ stdout" in text(state) and "└ stderr" in text(state) and "warn" in text(state)
    state.transcript().set_expanded(False)

    state.apply_event(
        envelope(
            "runtime.tool_completed",
            {"tool": "read", "tool_call_id": "call-1", "status": "ok", "content": "def main():\n    return 1\n", "display": READ_DISPLAY},
            sequence=5,
            source="tool",
        )
    )
    assert "✔ Read · src/app.py" in text(state)
    assert "… (streaming)" not in text(state)
    state.transcript().set_expanded(True)
    assert "def main():" in text(state)
    assert "└ stdout" in text(state)
    assert state.tool_content("call-1") == "def main():\n    return 1\n"


def test_edit_completion_renders_the_diff_gutter() -> None:
    state = view()
    state.apply_event(
        envelope(
            "runtime.tool_completed",
            {"tool": "edit", "tool_call_id": "call-2", "status": "ok", "content": "@@ -1,1 +1,1 @@\n-old\n+new\n", "display": EDIT_DISPLAY},
            sequence=1,
            source="tool",
        )
    )
    body = [row.strip() for row in rows(state)]
    assert "✔ Edit · src/app.py (1 change)" in body
    assert any(row.endswith("│old") for row in body)
    assert any(row.endswith("│new") for row in body)


def test_write_completion_does_not_echo_the_file_body() -> None:
    state = view()
    display = {"kind": "write", "title": "Write", "summary": "src/new.py", "copyable": {"path": "src/new.py"}}
    state.apply_event(
        envelope(
            "runtime.tool_completed",
            {"tool": "write", "tool_call_id": "call-3", "status": "ok", "content": "print('secret')\n", "display": display},
            sequence=1,
            source="tool",
        )
    )
    assert "✔ Write · src/new.py" in text(state)
    assert "secret" not in text(state)
    assert state.tool_content("call-3") == "print('secret')\n"


def test_search_completion_highlights_by_path_and_others_stay_plain() -> None:
    def render(kind: str, copyable: dict[str, object]) -> list[str]:
        state = view()
        state.apply_event(
            envelope(
                "runtime.tool_completed",
                {
                    "tool": "grep",
                    "tool_call_id": "call-5",
                    "status": "ok",
                    "content": "12:TODO fix\n",
                    "display": {"kind": kind, "title": "Search", "summary": "TODO", "copyable": copyable},
                },
                sequence=1,
                source="tool",
            )
        )
        return [row for row in state.transcript().rows() if "TODO fix" in plain([row])[0]]

    highlighted = render("search", {"path": "src/app.py"})
    plain_text = render("context", {})
    assert highlighted and plain_text
    assert "\x1b[38" in highlighted[0]
    assert "\x1b[38" not in plain_text[0]


def test_error_completion_marks_the_block_as_failed() -> None:
    state = view()
    display = {"kind": "shell", "title": "Shell", "summary": "false", "copyable": {"command": "false"}}
    state.apply_event(
        envelope(
            "runtime.tool_completed",
            {"tool": "shell_exec", "tool_call_id": "call-4", "status": "error", "content": "boom", "error": "boom", "display": display},
            sequence=1,
            source="tool",
        )
    )
    assert any(row.startswith("✘ Shell") for row in rows(state))
    assert "boom" in text(state)


# ---------------------------------------------------------------------------
# Approvals and questions
# ---------------------------------------------------------------------------


def test_approval_request_surfaces_and_resolution_clears_pending() -> None:
    state = view()
    payload = {
        "request_id": "approval-1",
        "tool": "shell_exec",
        "decision": "ask",
        "target_summary": "rm -rf build",
        "reason": "destructive command",
        "arguments": {"command": "rm -rf build"},
    }
    state.apply_event(envelope("runtime.approval_requested", payload, sequence=1))

    current = state.view_state()
    assert current.state == "Waiting approval"
    assert isinstance(current.pending, ApprovalRequest)
    assert current.pending.request_id == "approval-1"
    assert current.pending.tool == "shell_exec"
    assert current.pending.target == "rm -rf build"
    assert current.pending.reason == "destructive command"
    assert '"command": "rm -rf build"' in current.pending.arguments
    assert "⚠ Approval requested for tool: shell_exec" in text(state)
    assert current.status.state == "Waiting approval"
    assert current.status.session_name == "4f2a1b2c"

    # The overlay is handed to the app once, but stays pending until resolved.
    assert state.take_pending_overlay() is current.pending
    assert state.take_pending_overlay() is None
    assert state.view_state().pending is not None

    # An unrelated resolution never clears it.
    state.apply_event(envelope("runtime.approval_resolved", {"request_id": "approval-9", "decision": "deny"}, sequence=2))
    assert state.view_state().pending is not None

    state.apply_event(envelope("runtime.approval_resolved", {"request_id": "approval-1", "decision": "allow"}, sequence=3))
    resolved = state.view_state()
    assert resolved.pending is None
    assert resolved.state == "Running"
    assert "ℹ Approval allow for tool: shell_exec" in text(state)


def test_question_request_and_answer_round_trip() -> None:
    state = view()
    questions = [{"header": "Scope", "question": "Which files?", "multiple": False, "options": [{"label": "src", "description": "sources only"}]}]
    state.apply_event(
        envelope(
            "runtime.question_requested", {"request_id": "question-1", "tool": "question", "question_count": 1, "questions": questions}, sequence=1
        )
    )

    current = state.view_state()
    assert current.state == "Waiting input"
    assert isinstance(current.pending, QuestionRequest)
    assert current.pending.questions == tuple(questions)
    assert "? Agent requested input (1)" in text(state)
    assert state.take_pending_overlay() is current.pending

    state.apply_event(
        envelope("runtime.question_answered", {"request_id": "question-1", "responses": [{"header": "Scope", "answers": ["src"]}]}, sequence=2)
    )
    answered = state.view_state()
    assert answered.pending is None
    assert answered.state == "Running"
    body = [row.strip() for row in rows(state)]
    assert "✔ Answered" in body
    assert "Scope" in body and "src" in body


def test_resolve_overlay_clears_the_matching_request_only() -> None:
    state = view()
    state.apply_event(
        envelope("runtime.question_requested", {"request_id": "question-1", "tool": "question", "question_count": 0, "questions": []}, sequence=1)
    )
    state.resolve_overlay("question-9")
    assert state.view_state().pending is not None
    state.resolve_overlay("question-1")
    assert state.view_state().pending is None
    assert state.view_state().state == "Running"
    assert state.take_pending_overlay() is None


# ---------------------------------------------------------------------------
# Failures and background notices
# ---------------------------------------------------------------------------


def test_failed_event_prefers_the_diagnostics_summary() -> None:
    state = view()
    state.apply_event(
        envelope("runtime.failed", {"error": "Runtime failed: provider exploded", "diagnostics": {"summary": "Error: boom"}}, sequence=1)
    )
    assert rows(state) == [" ✘ Failed: boom"]
    assert state.view_state().state == "Failed"


def test_failed_event_falls_back_to_the_error_field() -> None:
    state = view()
    state.apply_event(envelope("runtime.failed", {"error": "Runtime failed: nope"}, sequence=1))
    assert rows(state) == [" ✘ Failed: nope"]


def test_format_runtime_error_strips_the_transport_noise() -> None:
    assert format_runtime_error("Error: boom") == "boom"
    assert format_runtime_error("Runtime failed: nope") == "nope"
    assert format_runtime_error("runtime failed: nope") == "nope"
    assert format_runtime_error("Error: ") == "Error: "
    assert format_runtime_error(None) == "Unknown error"
    assert format_runtime_error(ValueError("x")) == "Unknown error"


def test_background_task_notice_matches_the_old_lines() -> None:
    state = view()
    state.apply_event(
        envelope(
            "runtime.background_task_completed",
            {"task_id": "task-abcdef1234567", "summary_output": "all tests green", "child_session_id": "session-child"},
            sequence=1,
        )
    )
    rendered = text(state)
    assert "✓ Background completed · abcdef123456" in rendered
    assert "all tests green" in rendered
    assert "child: session-child" in rendered
    assert "task: task-abcdef1234567" in rendered


def test_delegated_result_and_group_notices() -> None:
    state = view()
    state.apply_event(envelope("runtime.background_task_group_completed", {"parallel_group_id": "group-7", "terminal_task_count": 3}, sequence=1))
    state.apply_event(envelope("runtime.delegated_result_available", {"task_id": "task-zzz", "summary_output": "ready"}, sequence=2))
    rendered = text(state)
    assert "✓ Background group completed · group-7" in rendered
    assert "Terminal tasks: 3" in rendered
    assert "↳ Delegated result available · zzz" in rendered
    assert "ready" in rendered


def test_background_failure_and_reminder_notices() -> None:
    state = view()
    state.apply_event(envelope("runtime.background_task_failed", {"task_id": "task-1", "error": "exit 2"}, sequence=1))
    state.apply_event(envelope("runtime.background_task_idle_reminder", {"task_id": "task-1", "reminder": "waiting on approval"}, sequence=2))
    rendered = text(state)
    assert "✖ Background failed · 1" in rendered
    assert "exit 2" in rendered
    assert "◌ Background waiting · 1" in rendered
    assert "waiting on approval" in rendered


# ---------------------------------------------------------------------------
# State machine and stream end
# ---------------------------------------------------------------------------


def test_state_transitions_follow_the_old_vocabulary() -> None:
    state = view()
    assert state.view_state().state == "Idle"

    state.apply_event(envelope("runtime.request_received", {"prompt": "hi"}, sequence=1))
    assert state.view_state().state == "Running"

    state.apply_event(envelope("runtime.approval_requested", {"request_id": "approval-1", "tool": "write"}, sequence=2))
    assert state.view_state().state == "Waiting approval"

    state.apply_event(envelope("runtime.approval_resolved", {"request_id": "approval-1", "decision": "deny"}, sequence=3))
    assert state.view_state().state == "Running"

    state.apply_event(envelope("graph.response_ready", {"output_preview": "done"}, sequence=4, source="graph"))
    assert state.view_state().state == "Completed"

    state.finish_stream("completed")
    assert state.view_state().state == "Idle"

    state.apply_event(envelope("runtime.failed", {"error": "boom"}, sequence=5))
    assert state.view_state().state == "Failed"
    state.finish_stream("failed")
    assert state.view_state().state == "Failed"


def test_finish_stream_while_waiting_keeps_the_overlay_and_live_rows() -> None:
    state = view()
    state.apply_event(envelope("runtime.approval_requested", {"request_id": "approval-1", "tool": "write"}, sequence=1))
    state.apply_event(envelope("graph.provider_stream", {"channel": "text", "kind": "delta", "text": "waiting"}, sequence=1, source="graph"))
    state.finish_stream("waiting")
    assert state.view_state().state == "Waiting approval"
    assert state.view_state().pending is not None
    # The old app returned early here, leaving the live tail unsettled.
    assert state.transcript().frontier() < len(state.transcript().blocks)


def test_finish_stream_settles_and_reports_a_transport_failure() -> None:
    state = view()
    state.apply_event(envelope("graph.provider_stream", {"channel": "text", "kind": "delta", "text": "half"}, sequence=1, source="graph"))
    assert state.transcript().frontier() < len(state.transcript().blocks)

    state.apply_event(envelope("runtime.approval_requested", {"request_id": "approval-1", "tool": "write"}, sequence=2))
    state.finish_stream("failed", error="Error: connection reset")
    assert "✘ connection reset" in text(state)
    assert state.view_state().pending is None
    assert state.view_state().state == "Failed"
    assert state.transcript().frontier() == len(state.transcript().blocks)
    assert "… (streaming)" not in text(state)


# ---------------------------------------------------------------------------
# Expand, resize, reset
# ---------------------------------------------------------------------------


def test_expand_replaces_the_body_with_the_fetched_artifact() -> None:
    state = view()
    state.apply_event(
        envelope(
            "runtime.tool_completed",
            {"tool": "read", "tool_call_id": "call-1", "status": "ok", "content": "one\n", "artifact_id": "artifact-7", "display": READ_DISPLAY},
            sequence=1,
            source="tool",
        )
    )
    assert state.pending_tool_artifact("call-1") == "artifact-7"
    assert state.tool_content("call-1") == "one\n"

    state.expand_tool("call-1", "one\ntwo\nthree\n")
    assert state.tool_content("call-1") == "one\ntwo\nthree\n"
    rendered = [row.strip() for row in rows(state)]
    assert any("three" in row for row in rendered)
    assert any(row.startswith("╭───") and "Read · src/app.py" in row for row in rendered)


def test_expand_for_an_unknown_tool_call_writes_its_own_block() -> None:
    state = view()
    state.expand_tool("call-missing", "recovered output\n")
    rendered = text(state)
    assert "/expand call-missing" in rendered
    assert "recovered output" in rendered


def test_expand_without_an_id_reports_usage() -> None:
    state = view()
    state.expand_tool("", "")
    assert rows(state) == [" Usage: /expand <tool_call_id>"]


def test_set_width_reflows_the_tape() -> None:
    state = view(width=80)
    state.apply_event(envelope("runtime.background_task_completed", {"task_id": "task-1", "summary_output": "x" * 120}, sequence=1))
    wide = rows(state)
    state.set_width(40)
    narrow = rows(state)
    assert len(narrow) > len(wide)
    assert state.transcript().width == 40


def test_reset_for_new_session_drops_every_traced_thing() -> None:
    state = view()
    state.apply_event(
        envelope("graph.tool_request_created", {"tool": "read", "tool_call_id": "call-1", "display": READ_DISPLAY}, sequence=1, source="graph")
    )
    state.apply_event(envelope("runtime.approval_requested", {"request_id": "approval-1", "tool": "read"}, sequence=2))
    state.apply_event(envelope("graph.provider_stream", {"channel": "text", "kind": "delta", "text": "hi"}, sequence=2, source="graph"))

    state.reset_for_new_session()
    assert rows(state) == []
    assert state.view_state().state == "Idle"
    assert state.view_state().pending is None
    assert state.view_state().status.session_name == ""
    assert state.tool_content("call-1") is None
    assert state.take_pending_overlay() is None

    # The sequence cursor starts over: the old app reset it per session.
    assert (
        state.apply_event(envelope("runtime.tool_started", {"tool": "read", "tool_call_id": "call-2", "display": READ_DISPLAY}, sequence=1)) is True
    )


def test_status_line_carries_the_state_word_and_session_label() -> None:
    state = view()
    state.apply_event(envelope("runtime.request_received", {"prompt": "hi"}, sequence=1))
    status = state.view_state().status
    assert status.state == "Running"
    assert status.session_name == "4f2a1b2c"


def test_approval_without_a_request_id_still_notices_and_waits() -> None:
    state = view()
    state.apply_event(envelope("runtime.approval_requested", {"tool": "write"}, sequence=1))
    assert state.view_state().state == "Waiting approval"
    assert state.view_state().pending is None
    assert state.take_pending_overlay() is None
    assert "⚠ Approval requested for tool: write" in text(state)


def test_question_without_a_request_id_still_notices_and_waits() -> None:
    state = view()
    state.apply_event(envelope("runtime.question_requested", {"tool": "question"}, sequence=1))
    assert state.view_state().state == "Waiting input"
    assert state.view_state().pending is None
    assert "? Agent requested input (1)" in text(state)


def test_tool_events_without_a_call_id_write_bare_lines() -> None:
    state = view()
    state.apply_event(envelope("graph.tool_request_created", {"tool": "read"}, sequence=1, source="graph"))
    state.apply_event(envelope("runtime.tool_started", {"tool": "read", "display": READ_DISPLAY}, sequence=2))
    state.apply_event(envelope("runtime.tool_completed", {"tool": "read", "status": "ok", "content": "body"}, sequence=3, source="tool"))
    rendered = text(state)
    assert "▶ Started tool: read" in rendered
    assert "Read: src/app.py" in rendered
    # Without an id there is nothing to key the block map on, so the completion
    # falls back to the bare tool name -- exactly as the old app did.
    assert "✔ read" in rendered
    assert "body" in rendered
