"""Behavioral regressions for live tool progress in the TUI Live region."""

from __future__ import annotations

import re
from typing import Any

from voidcode.sdk.client import StreamEvent
from voidcode.sdk.codec import encode_tool_call
from voidcode.sdk.commands import CommandCatalog
from voidcode.sdk.tools import TextOutput, ToolCall, ToolSuccess
from voidcode.sdk.transcript import TranscriptEntry, TranscriptTool
from voidcode.tui import rendering
from voidcode.tui.terminal_app import CoreTuiApp


class _StubTerminal:
    color_system = None
    eof = False

    def __init__(self, width: int = 80, height: int = 24) -> None:
        self.width = width
        self.height = height
        self.frames: list[list[str]] = []
        self.committed: list[str] = []

    def query_default_background_rgb(self) -> None:
        return None

    def commit_rows(self, rows: list[str]) -> None:
        self.committed.extend(rows)

    def paint_frame(self, frame: Any) -> None:
        self.frames.append(list(frame.chrome_rows))


class _StubClient:
    def __init__(self) -> None:
        self._catalog = CommandCatalog("rev-1", ())

    def command_catalog(self) -> CommandCatalog:
        return self._catalog

    def health(self) -> Any:
        return None

    def cancel(self, session_id: str) -> Any:
        class _CancelRes:
            cancellation_requested = True

        return _CancelRes()


def _event(
    sequence: int,
    event_type: str,
    session_id: str = "s1",
    run_id: str = "run-1",
    revision_id: str = "rev-1",
    content: Any = None,
    **payload: object,
) -> StreamEvent:
    entry: TranscriptEntry | None = None
    if content is not None:
        entry = TranscriptEntry(sequence, run_id, revision_id, "core", content)
    return StreamEvent(
        session_id=session_id,
        sequence=sequence,
        type=event_type,
        run_id=run_id,
        revision_id=revision_id,
        source="core",
        actor="user",
        payload=dict(payload),
        transcript=entry,
    )


def _plain_frame(terminal: _StubTerminal) -> str:
    if not terminal.frames:
        return ""
    return "\n".join(re.sub(r"\x1b\[[0-9;]*m", "", r) for r in terminal.frames[-1])


def test_tool_intent_rendered_in_live_frame_without_committing_scrollback() -> None:
    terminal = _StubTerminal()
    app = CoreTuiApp(_StubClient(), session_id="s1", terminal=terminal)
    rows: list[str] = []

    call = ToolCall(tool_name="shell_exec", arguments={"command": "sleep 10"})
    app._handle_stream_event(
        _event(
            1,
            "core.tool_intent",
            invocation_id="inv-1",
            call=encode_tool_call(call),
            icon={"glyph": "⌁"},
        ),
        rows,
    )

    # Scrollback ledger must NOT receive any rows before completion
    assert rows == []
    assert terminal.committed == []

    # Render frame: live region paints the active tool call
    app._render_frame()
    frame_text = _plain_frame(terminal)
    assert "shell_exec" in frame_text
    assert terminal.committed == []


def test_tool_progress_chunking_joins_split_lines_and_unicode_visibly() -> None:
    terminal = _StubTerminal(width=80, height=20)
    app = CoreTuiApp(_StubClient(), session_id="s1", terminal=terminal)
    rows: list[str] = []

    call = ToolCall(tool_name="shell_exec", arguments={"command": "compile"})
    app._handle_stream_event(
        _event(1, "core.tool_intent", invocation_id="inv-1", call=encode_tool_call(call)),
        rows,
    )

    # Chunk 1: fragment without newline
    app._handle_stream_event(
        _event(2, "core.tool_progress", invocation_id="inv-1", progress={"stream": "stdout", "text": "Compil"}),
        rows,
    )
    app._render_frame()
    assert "Compil" in _plain_frame(terminal)

    # Chunk 2: completes the line with newline -> joins into single line "Compiling foo.c"
    app._handle_stream_event(
        _event(3, "core.tool_progress", invocation_id="inv-1", progress={"stream": "stdout", "text": "ing foo.c\n"}),
        rows,
    )
    app._render_frame()
    frame_2 = _plain_frame(terminal)
    assert "Compiling foo.c" in frame_2

    # Chunk 3: stream switch from stdout to stderr flushes pending fragment instead of merging
    app._handle_stream_event(
        _event(4, "core.tool_progress", invocation_id="inv-1", progress={"stream": "stdout", "text": "warning prefix "}),
        rows,
    )
    app._handle_stream_event(
        _event(5, "core.tool_progress", invocation_id="inv-1", progress={"stream": "stderr", "text": "stderr error message\n"}),
        rows,
    )
    app._render_frame()
    frame_switch = _plain_frame(terminal)
    assert "warning prefix" in frame_switch
    assert "stderr error message" in frame_switch

    # Chunk 4: Unicode characters preserved
    app._handle_stream_event(
        _event(6, "core.tool_progress", invocation_id="inv-1", progress={"stream": "stdout", "text": "Done 🚀 [100%]\n"}),
        rows,
    )
    app._render_frame()
    assert "Done 🚀 [100%]" in _plain_frame(terminal)


def test_narrow_viewport_preserves_tool_identity_and_composer_usability() -> None:
    # 10-row viewport: 1 header row + tail rows must not push composer out of screen
    terminal = _StubTerminal(width=60, height=10)
    app = CoreTuiApp(_StubClient(), session_id="s1", terminal=terminal)
    rows: list[str] = []

    call = ToolCall(tool_name="long_running_task", arguments={"command": "run"})
    app._handle_stream_event(
        _event(1, "core.tool_intent", invocation_id="inv-1", call=encode_tool_call(call)),
        rows,
    )

    # Stream 50 lines of output into progress
    bulk = "".join(f"output row {i}\n" for i in range(50))
    app._handle_stream_event(
        _event(2, "core.tool_progress", invocation_id="inv-1", progress={"stream": "stdout", "text": bulk}),
        rows,
    )

    app.composer.set_text("cat myfile.txt")
    app._render_frame()

    frame = terminal.frames[-1]
    assert len(frame) <= 10, f"Frame height exceeded viewport budget: {len(frame)} > 10"

    frame_text = _plain_frame(terminal)
    # Tool identity/name is ALWAYS preserved, never sliced off
    assert "long_running_task" in frame_text
    # Composer text remains visible and usable
    assert "cat myfile.txt" in frame_text


def test_one_row_live_space_with_multiple_tools_never_overflows() -> None:
    # Set height so exactly 1 row of live space remains above composer and footer
    # Footer has 2 rows (context + status), composer has 3 rows (border+input) -> available for live is 1 row
    # If height=6: footer=2, composer=3 -> live_space = 1 row
    terminal = _StubTerminal(width=80, height=6)
    app = CoreTuiApp(_StubClient(), session_id="s1", terminal=terminal)
    rows: list[str] = []

    # 3 active tools running concurrently
    for i in range(1, 4):
        call = ToolCall(tool_name=f"worker_tool_{i}", arguments={})
        app._handle_stream_event(
            _event(i, "core.tool_intent", invocation_id=f"inv-{i}", call=encode_tool_call(call)),
            rows,
        )

    app._render_frame()
    frame = terminal.frames[-1]
    # Height must strictly be <= 6 rows without any overflow
    assert len(frame) <= 6, f"Frame height overflowed: {len(frame)} > 6"
    frame_text = _plain_frame(terminal)
    # Multiple active tools with 1 row available must render a compact summary row
    assert "running 3 tools" in frame_text


def test_multi_invocation_headers_survive_concurrently() -> None:
    terminal = _StubTerminal(width=80, height=20)
    app = CoreTuiApp(_StubClient(), session_id="s1", terminal=terminal)
    rows: list[str] = []

    # Two tools running in parallel
    call_1 = ToolCall(tool_name="build_engine", arguments={})
    call_2 = ToolCall(tool_name="test_runner", arguments={})
    app._handle_stream_event(
        _event(1, "core.tool_intent", invocation_id="inv-1", call=encode_tool_call(call_1)),
        rows,
    )
    app._handle_stream_event(
        _event(2, "core.tool_intent", invocation_id="inv-2", call=encode_tool_call(call_2)),
        rows,
    )

    app._render_frame()
    frame_text = _plain_frame(terminal)
    # Both concurrent tool headers survive in the frame
    assert "build_engine" in frame_text
    assert "test_runner" in frame_text


def test_stale_run_and_wrong_session_progress_rejected_and_subsequent_valid_event_applied() -> None:
    terminal = _StubTerminal()
    app = CoreTuiApp(_StubClient(), session_id="s1", terminal=terminal)
    rows: list[str] = []

    call = ToolCall(tool_name="current_tool", arguments={})
    app._handle_stream_event(
        _event(1, "core.tool_intent", session_id="s1", run_id="run-active", revision_id="rev-1", invocation_id="inv-1", call=encode_tool_call(call)),
        rows,
    )

    # 1. Event from a different session (with a higher sequence) must be rejected immediately
    # AND must NOT advance app._session_cursors["s1"]
    app._handle_stream_event(
        _event(
            999,
            "core.tool_progress",
            session_id="s2",
            run_id="run-active",
            revision_id="rev-1",
            invocation_id="inv-1",
            progress={"stream": "stdout", "text": "WRONG_SESSION\n"},
        ),
        rows,
    )
    assert app._session_cursors.get("s1") == 1  # Session s1 cursor was NOT advanced to 999
    app._render_frame()
    assert "WRONG_SESSION" not in _plain_frame(terminal)

    # 2. Event with stale run_id from an older run must be rejected
    app._handle_stream_event(
        _event(
            2,
            "core.tool_progress",
            session_id="s1",
            run_id="run-old",
            revision_id="rev-1",
            invocation_id="inv-1",
            progress={"stream": "stdout", "text": "OLD_RUN\n"},
        ),
        rows,
    )
    app._render_frame()
    assert "OLD_RUN" not in _plain_frame(terminal)

    # 3. Event with mismatched revision_id must be rejected
    app._handle_stream_event(
        _event(
            3,
            "core.tool_progress",
            session_id="s1",
            run_id="run-active",
            revision_id="rev-wrong",
            invocation_id="inv-1",
            progress={"stream": "stdout", "text": "WRONG_REVISION\n"},
        ),
        rows,
    )
    app._render_frame()
    assert "WRONG_REVISION" not in _plain_frame(terminal)

    # 4. Valid event with matching session, run, revision, and generation is accepted and applied
    app._handle_stream_event(
        _event(
            4,
            "core.tool_progress",
            session_id="s1",
            run_id="run-active",
            revision_id="rev-1",
            invocation_id="inv-1",
            progress={"stream": "stdout", "text": "VALID_PROGRESS\n"},
        ),
        rows,
    )
    assert app._session_cursors.get("s1") == 4
    app._render_frame()
    assert "VALID_PROGRESS" in _plain_frame(terminal)


def test_tool_completion_settles_header_and_result_exactly_once() -> None:
    terminal = _StubTerminal()
    app = CoreTuiApp(_StubClient(), session_id="s1", terminal=terminal)
    rows: list[str] = []

    call = ToolCall(tool_name="shell_exec", arguments={"command": "echo done"})
    app._handle_stream_event(
        _event(1, "core.tool_intent", invocation_id="inv-1", call=encode_tool_call(call)),
        rows,
    )
    assert len(app._active_tools) == 1

    # Settle tool completion with TranscriptTool
    transcript_tool = TranscriptTool(
        call=call,
        result=ToolSuccess(tool_name="shell_exec", output=TextOutput(text="done output\n")),
        interrupted=False,
        batch_sequence=1,
        deferred=False,
    )
    app._handle_stream_event(
        _event(2, "core.tool_completed", invocation_id="inv-1", content=transcript_tool),
        rows,
    )

    # Active tool popped from Live region
    assert "inv-1" not in app._active_tools

    # Settled block in rows contains header AND result together exactly once
    plain = [re.sub(r"\x1b\[[0-9;]*m", "", r) for r in rows]
    text = "\n".join(plain)
    assert "shell_exec" in text
    assert "echo done" in text
    assert "done output" in text
    assert text.count("shell_exec") == 1

    # Live frame no longer shows the active card
    app._render_frame()
    assert "shell_exec" not in _plain_frame(terminal)


def test_run_termination_clears_matching_run_tools_and_spinner_stays_active_for_surviving_tools() -> None:
    terminal = _StubTerminal()
    app = CoreTuiApp(_StubClient(), session_id="s1", terminal=terminal)
    rows: list[str] = []

    call_1 = ToolCall(tool_name="tool_run_1", arguments={})
    call_2 = ToolCall(tool_name="tool_run_2", arguments={})
    app._handle_stream_event(
        _event(1, "core.tool_intent", session_id="s1", run_id="run-1", invocation_id="inv-1", call=encode_tool_call(call_1)),
        rows,
    )
    app._handle_stream_event(
        _event(2, "core.tool_intent", session_id="s1", run_id="run-2", invocation_id="inv-2", call=encode_tool_call(call_2)),
        rows,
    )
    assert len(app._active_tools) == 2

    # Interrupt run-1 only
    app._handle_stream_event(
        _event(3, "core.run_interrupted", session_id="s1", run_id="run-1", reason="user cancel"),
        rows,
    )
    # inv-1 is cleared; inv-2 in run-2 survives
    assert "inv-1" not in app._active_tools
    assert "inv-2" in app._active_tools

    # Spinner and border status remain ACTIVE because inv-2 is still running
    status = rendering.border_status(app)
    assert "tool_run_2" in status
