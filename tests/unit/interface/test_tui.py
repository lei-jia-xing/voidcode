from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from textual.widgets import Collapsible, Input, OptionList

from voidcode.runtime.config import (
    RuntimeTuiConfig,
    RuntimeTuiPreferences,
    RuntimeTuiReadingPreferences,
    RuntimeTuiThemePreferences,
)
from voidcode.tui.timeline import TimelineView


@dataclass(frozen=True)
class _StubEvent:
    sequence: int
    event_type: str
    source: str
    payload: dict[str, object]


@dataclass(frozen=True)
class _StubSessionRef:
    id: str
    parent_id: str | None = None


@dataclass(frozen=True)
class _StubSession:
    session: _StubSessionRef
    status: str
    turn: int = 1
    metadata: dict[str, object] | None = None


@dataclass(frozen=True)
class _StubChunk:
    kind: str
    session: _StubSession
    event: _StubEvent | None = None
    output: str | None = None


def _runtime_event(
    event_type: str,
    *,
    sequence: int = 0,
    source: str = "runtime",
    **payload: object,
) -> _StubEvent:
    return _StubEvent(
        sequence=sequence,
        event_type=event_type,
        source=source,
        payload=dict(payload),
    )


def _make_chunk(
    *,
    session_id: str = "demo-session",
    status: str,
    event: _StubEvent | None = None,
    output: str | None = None,
) -> _StubChunk:
    return _StubChunk(
        kind="output" if output is not None else "event",
        session=_StubSession(session=_StubSessionRef(id=session_id), status=status),
        event=event,
        output=output,
    )


@pytest.fixture
def app_class() -> Any:
    from voidcode.tui import StreamChunkReceived, StreamCompleted, VoidCodeTUI

    return VoidCodeTUI, StreamChunkReceived, StreamCompleted


def _mock_runtime() -> MagicMock:
    """Build a runtime double satisfying RuntimeProtocol for constructor injection."""
    from voidcode.tui.app import RuntimeProtocol

    return MagicMock(spec=RuntimeProtocol)


def _mock_runtime_config(
    *,
    leader_key: str = "alt+x",
    preferences: RuntimeTuiPreferences | None = None,
    keymap: dict[str, str] | None = None,
) -> MagicMock:
    config = MagicMock()
    config.tui = RuntimeTuiConfig(
        leader_key=leader_key,
        keymap=keymap,
        preferences=preferences,
    )
    return config


def _make_keybind_tui(base_class: Any) -> type[Any]:
    """Subclass exposing palette commands as actions so keybindings can dispatch them."""

    class _TestVoidCodeTUI(base_class):  # type: ignore[misc,valid-type]
        def action_theme_switch(self) -> None:
            self._handle_command("theme.switch")

        def action_view_wrap(self) -> None:
            self._handle_command("view.wrap")

    return _TestVoidCodeTUI


@pytest.mark.anyio
async def test_tui_waiting_stream_keeps_waiting_state(app_class: Any) -> None:
    VoidCodeTUI, _, _ = app_class
    waiting_stream = iter(
        (
            _make_chunk(
                status="waiting",
                event=_runtime_event(
                    "runtime.approval_requested",
                    request_id="req-1",
                    tool="write_file",
                    target_summary="sample.txt",
                ),
            ),
        )
    )

    mock_runtime = _mock_runtime()
    mock_runtime.run_stream.return_value = waiting_stream
    app = VoidCodeTUI(workspace=Path("."), runtime=mock_runtime)

    async with app.run_test() as pilot:
        await pilot.press("a", "b", "c", "enter")
        await pilot.pause()
        await pilot.pause()

        assert app.current_state == "Waiting approval"
        assert app.pending_request_id == "req-1"
        main_screen = app.screen_stack[-2]
        assert main_screen.query_one("#status-panel").renderable == "Waiting approval"
        assert main_screen.query_one("#composer-input").disabled is False


@pytest.mark.anyio
async def test_tui_queues_submissions_fifo_while_stream_active(app_class: Any) -> None:
    VoidCodeTUI, _, StreamCompleted = app_class

    mock_runtime = _mock_runtime()
    app = VoidCodeTUI(workspace=Path("."), runtime=mock_runtime)

    with patch.object(app, "_start_stream") as start_stream:
        async with app.run_test() as pilot:
            composer = app.query_one("#composer-input", Input)
            app._stream_active = True
            app.on_input_submitted(Input.Submitted(composer, "first"))
            app.on_input_submitted(Input.Submitted(composer, "second"))
            assert list(app._prompt_queue) == ["first", "second"]

            app._stream_active = False
            assert app._start_next_queued_prompt() is True
            app.on_stream_completed(StreamCompleted("completed"))
            await pilot.pause()

    prompts = [call.args[0].prompt for call in start_stream.call_args_list]
    assert prompts == ["first", "second"]


@pytest.mark.anyio
async def test_tui_renders_output_as_markdown(app_class: Any) -> None:
    VoidCodeTUI, StreamChunkReceived, StreamCompleted = app_class

    mock_runtime = _mock_runtime()
    app = VoidCodeTUI(workspace=Path("."), runtime=mock_runtime)

    async with app.run_test() as pilot:
        app.on_stream_chunk_received(StreamChunkReceived(_make_chunk(status="completed", output="**bold**")))
        app.on_stream_completed(StreamCompleted("completed"))
        await pilot.pause()

        log = app.query_one("#transcript-log")
        last_line = log.lines[-1]
        plain_text = "".join(segment.text for segment in last_line)
        assert "bold" in plain_text
        assert "**" not in plain_text
        assert app.current_state == "Idle"


@pytest.mark.anyio
async def test_tui_visually_separates_user_and_agent_messages(app_class: Any) -> None:
    VoidCodeTUI, StreamChunkReceived, StreamCompleted = app_class

    app = VoidCodeTUI(workspace=Path("."), runtime=_mock_runtime())
    async with app.run_test() as pilot:
        app._write_user_prompt("Please inspect this")
        app.on_stream_chunk_received(StreamChunkReceived(_make_chunk(status="completed", output="Inspection done")))
        app.on_stream_completed(StreamCompleted("completed"))
        await pilot.pause()

        user = app.query_one("#transcript-log .user-message")
        agent = app.query_one("#transcript-log .assistant-stream")
        assert user.has_class("user-message")
        assert agent.has_class("assistant-stream")
        plain = "\n".join("".join(segment.text for segment in line) for line in app.query_one("#transcript-log").lines)
        assert "Please inspect this" in plain
        assert "Inspection done" in plain
        assert "You" not in plain
        assert "Agent" not in plain


@pytest.mark.anyio
async def test_tui_stream_keeps_thinking_block_and_updates_one_response_entry(app_class: Any) -> None:
    VoidCodeTUI, StreamChunkReceived, StreamCompleted = app_class

    app = VoidCodeTUI(workspace=Path("."), runtime=_mock_runtime())
    async with app.run_test() as pilot:
        for text in ("Check ", "the code"):
            app.on_stream_chunk_received(
                StreamChunkReceived(
                    _make_chunk(
                        status="running",
                        event=_runtime_event(
                            "graph.provider_stream",
                            source="graph",
                            channel="reasoning",
                            kind="delta",
                            text=text,
                        ),
                    )
                )
            )
        for text in ("Hello ", "**world**"):
            app.on_stream_chunk_received(
                StreamChunkReceived(
                    _make_chunk(
                        status="running",
                        event=_runtime_event(
                            "graph.provider_stream",
                            source="graph",
                            channel="text",
                            kind="delta",
                            text=text,
                        ),
                    )
                )
            )
        app._flush_stream_preview()
        app.on_stream_completed(StreamCompleted("completed"))
        await pilot.pause()

        thinking = app.query_one("#transcript-log .thinking-block", Collapsible)
        assert thinking.title == "Thinking"
        assert thinking.collapsed is True
        plain = "\n".join("".join(segment.text for segment in line) for line in app.query_one("#transcript-log").lines)
        assert "Check the code" in plain
        assert "Hello world" in plain
        assert "**world**" not in plain
        assert len(list(app.query("#transcript-log .assistant-stream"))) == 1


@pytest.mark.anyio
async def test_tui_failed_stream_stays_failed(app_class: Any) -> None:
    VoidCodeTUI, StreamChunkReceived, StreamCompleted = app_class

    mock_runtime = _mock_runtime()
    app = VoidCodeTUI(workspace=Path("."), runtime=mock_runtime)

    async with app.run_test() as pilot:
        app.on_stream_chunk_received(
            StreamChunkReceived(
                _make_chunk(
                    status="failed",
                    event=_runtime_event("runtime.failed", error="boom"),
                )
            )
        )
        app.on_stream_completed(StreamCompleted("failed"))
        await pilot.pause()

        assert app.current_state == "Failed"
        assert app.query_one("#status-panel").renderable == "Failed"


@pytest.mark.anyio
async def test_tui_strips_runtime_failed_prefix_from_error_lines(app_class: Any) -> None:
    VoidCodeTUI, StreamChunkReceived, StreamCompleted = app_class

    mock_runtime = _mock_runtime()
    app = VoidCodeTUI(workspace=Path("."), runtime=mock_runtime)

    async with app.run_test() as pilot:
        app.on_stream_chunk_received(
            StreamChunkReceived(
                _make_chunk(
                    status="failed",
                    event=_runtime_event(
                        "runtime.failed",
                        error="Runtime failed: provider fallback exhausted",
                    ),
                )
            )
        )
        app.on_stream_completed(StreamCompleted("failed"))
        await pilot.pause()

        log = app.query_one("#transcript-log")
        plain_lines = ["".join(segment.text for segment in line) for line in log.lines]
        assert any("✖ Failed: provider fallback exhausted" in line for line in plain_lines)


@pytest.mark.anyio
async def test_tui_prefers_runtime_error_summary_when_present(app_class: Any) -> None:
    VoidCodeTUI, StreamChunkReceived, StreamCompleted = app_class

    mock_runtime = _mock_runtime()
    app = VoidCodeTUI(workspace=Path("."), runtime=mock_runtime)

    async with app.run_test() as pilot:
        app.on_stream_chunk_received(
            StreamChunkReceived(
                _make_chunk(
                    status="failed",
                    event=_runtime_event(
                        "runtime.failed",
                        error="Runtime failed: provider fallback exhausted",
                        error_summary="provider fallback exhausted",
                    ),
                )
            )
        )
        app.on_stream_completed(StreamCompleted("failed"))
        await pilot.pause()

        log = app.query_one("#transcript-log")
        plain_lines = ["".join(segment.text for segment in line) for line in log.lines]
        assert any("✖ Failed: provider fallback exhausted" in line for line in plain_lines)


@pytest.mark.anyio
async def test_tui_sidebar_updates_on_mount(app_class: Any) -> None:
    VoidCodeTUI, _, _ = app_class

    mock_runtime = _mock_runtime()

    # mock lsp
    mock_lsp = MagicMock()
    mock_lsp.mode = "managed"
    mock_server = MagicMock()
    mock_server.status = "running"
    mock_lsp.servers = {"pylsp": mock_server}
    mock_runtime.current_lsp_state.return_value = mock_lsp

    app = VoidCodeTUI(workspace=Path("/fake/workspace"), runtime=mock_runtime)

    async with app.run_test() as pilot:
        await pilot.pause()

        assert app.query_one("#workspace-panel").renderable == "workspace"
        assert app.query_one("#lsp-panel").renderable == "Active: 1"


@pytest.mark.anyio
async def test_tui_closes_runtime_on_unmount(app_class: Any) -> None:
    VoidCodeTUI, _, _ = app_class

    mock_runtime = _mock_runtime()
    app = VoidCodeTUI(workspace=Path("."), runtime=mock_runtime)

    async with app.run_test() as pilot:
        await pilot.pause()

    mock_runtime.__exit__.assert_called_once_with(None, None, None)


@pytest.mark.anyio
async def test_session_new_via_keybinding(app_class: Any) -> None:
    VoidCodeTUI, _, _ = app_class

    mock_runtime = _mock_runtime()
    app = VoidCodeTUI(workspace=Path("."), runtime=mock_runtime)
    app._configure_keybindings(_mock_runtime_config(keymap={"ctrl+n": "session_new"}))
    app.session_id = "old-session"

    async with app.run_test() as pilot:
        await pilot.press("ctrl+n")
        await pilot.pause()

        assert app.session_id is None
        assert app.query_one("#session-panel").renderable == "None"


@pytest.mark.anyio
async def test_session_resume_via_keybinding(app_class: Any) -> None:
    VoidCodeTUI, _, _ = app_class

    mock_runtime = _mock_runtime()

    from voidcode.runtime.session import SessionRef, StoredSessionSummary

    mock_session = StoredSessionSummary(
        session=SessionRef(id="session-test-id"),
        status="completed",
        turn=2,
        prompt="test prompt",
        updated_at=0,
    )
    mock_runtime.list_sessions.return_value = (mock_session,)

    app = VoidCodeTUI(workspace=Path("."), runtime=mock_runtime)
    app._configure_keybindings(_mock_runtime_config(keymap={"ctrl+r": "session_resume"}))

    async with app.run_test() as pilot:
        await pilot.press("ctrl+r")
        await pilot.pause()

        from voidcode.tui.screens import SessionListModal

        assert isinstance(app.screen, SessionListModal)


@pytest.mark.anyio
async def test_theme_switch_via_keybinding(app_class: Any) -> None:
    VoidCodeTUI, _, _ = app_class
    TestTUI = _make_keybind_tui(VoidCodeTUI)

    mock_runtime = _mock_runtime()
    app = TestTUI(
        workspace=Path("."),
        runtime=mock_runtime,
        tui_preferences=RuntimeTuiPreferences(),
    )
    app._configure_keybindings(_mock_runtime_config(keymap={"ctrl+t": "theme_switch"}))

    async with app.run_test() as pilot:
        await pilot.press("ctrl+t")
        await pilot.pause()

        from voidcode.tui.screens import ThemePickerModal

        assert isinstance(app.screen, ThemePickerModal)


@pytest.mark.anyio
async def test_tui_command_palette_view_wrap(app_class: Any) -> None:
    VoidCodeTUI, _, _ = app_class
    TestTUI = _make_keybind_tui(VoidCodeTUI)

    mock_runtime = _mock_runtime()
    app = TestTUI(
        workspace=Path("."),
        runtime=mock_runtime,
        tui_preferences=RuntimeTuiPreferences(
            theme=RuntimeTuiThemePreferences(name="textual-dark", mode="auto"),
            reading=RuntimeTuiReadingPreferences(wrap=True, sidebar_collapsed=True),
        ),
    )
    app._configure_keybindings(
        _mock_runtime_config(
            keymap={"f2": "view_wrap"},
            preferences=RuntimeTuiPreferences(
                theme=RuntimeTuiThemePreferences(name="textual-dark", mode="auto"),
                reading=RuntimeTuiReadingPreferences(sidebar_collapsed=True),
            ),
        )
    )

    with patch(
        "voidcode.tui.app.load_workspace_tui_preferences",
        autospec=True,
        return_value=None,
    ):
        with patch("voidcode.tui.app.save_global_tui_preferences") as mock_save:
            async with app.run_test() as pilot:
                await pilot.press("f2")
                await pilot.pause()

                mock_save.assert_called_once()
                assert app._tui_preferences.reading is not None
                assert app._tui_preferences.reading.wrap is False
                saved_preferences = mock_save.call_args.args[0]
                assert saved_preferences.reading == RuntimeTuiReadingPreferences(wrap=False, sidebar_collapsed=True)
                assert saved_preferences.theme == RuntimeTuiThemePreferences(name="textual-dark", mode="auto")


@pytest.mark.anyio
async def test_tui_wrap_toggle_does_not_snapshot_inherited_global_theme(app_class: Any) -> None:
    VoidCodeTUI, _, _ = app_class
    TestTUI = _make_keybind_tui(VoidCodeTUI)

    mock_runtime = _mock_runtime()
    app = TestTUI(
        workspace=Path("."),
        runtime=mock_runtime,
        tui_preferences=RuntimeTuiPreferences(
            theme=RuntimeTuiThemePreferences(name="tokyo-night", mode="dark"),
            reading=RuntimeTuiReadingPreferences(wrap=True, sidebar_collapsed=True),
        ),
    )
    app._configure_keybindings(
        _mock_runtime_config(
            keymap={"f2": "view_wrap"},
            preferences=RuntimeTuiPreferences(
                theme=RuntimeTuiThemePreferences(name="tokyo-night", mode="dark"),
                reading=RuntimeTuiReadingPreferences(wrap=True, sidebar_collapsed=True),
            ),
        )
    )

    with patch(
        "voidcode.tui.app.load_workspace_tui_preferences",
        autospec=True,
        return_value=None,
    ):
        with patch("voidcode.tui.app.save_global_tui_preferences") as mock_save:
            async with app.run_test() as pilot:
                await pilot.press("f2")
                await pilot.pause()

                saved_preferences = mock_save.call_args.args[0]
                assert saved_preferences == RuntimeTuiPreferences(
                    theme=RuntimeTuiThemePreferences(name="tokyo-night", mode="dark"),
                    reading=RuntimeTuiReadingPreferences(wrap=False, sidebar_collapsed=True),
                )


@pytest.mark.anyio
async def test_tui_default_preference_changes_write_global_not_workspace(app_class: Any) -> None:
    VoidCodeTUI, _, _ = app_class
    TestTUI = _make_keybind_tui(VoidCodeTUI)

    mock_runtime = _mock_runtime()
    app = TestTUI(
        workspace=Path("."),
        runtime=mock_runtime,
        tui_preferences=RuntimeTuiPreferences(
            theme=RuntimeTuiThemePreferences(name="textual-dark", mode="auto"),
            reading=RuntimeTuiReadingPreferences(wrap=True, sidebar_collapsed=False),
        ),
    )
    app._configure_keybindings(
        _mock_runtime_config(
            keymap={"f2": "view_wrap"},
            preferences=RuntimeTuiPreferences(
                theme=RuntimeTuiThemePreferences(name="textual-dark", mode="auto"),
                reading=RuntimeTuiReadingPreferences(wrap=True, sidebar_collapsed=False),
            ),
        )
    )

    with patch(
        "voidcode.tui.app.load_workspace_tui_preferences",
        autospec=True,
        return_value=RuntimeTuiPreferences(reading=RuntimeTuiReadingPreferences(sidebar_collapsed=True)),
    ):
        with patch("voidcode.tui.app.save_global_tui_preferences") as mock_global_save:
            async with app.run_test() as pilot:
                await pilot.press("f2")
                await pilot.pause()

                mock_global_save.assert_called_once()


@pytest.mark.anyio
async def test_tui_global_save_does_not_snapshot_workspace_only_override(app_class: Any) -> None:
    VoidCodeTUI, _, _ = app_class
    TestTUI = _make_keybind_tui(VoidCodeTUI)

    mock_runtime = _mock_runtime()
    app = TestTUI(
        workspace=Path("."),
        runtime=mock_runtime,
        tui_preferences=RuntimeTuiPreferences(
            theme=RuntimeTuiThemePreferences(name="textual-dark", mode="auto"),
            reading=RuntimeTuiReadingPreferences(wrap=True, sidebar_collapsed=False),
        ),
    )
    app._configure_keybindings(
        _mock_runtime_config(
            keymap={"f2": "view_wrap"},
            preferences=RuntimeTuiPreferences(
                theme=RuntimeTuiThemePreferences(name="textual-dark", mode="auto"),
                reading=RuntimeTuiReadingPreferences(wrap=True, sidebar_collapsed=True),
            ),
        )
    )

    with patch(
        "voidcode.tui.app.load_workspace_tui_preferences",
        autospec=True,
        return_value=RuntimeTuiPreferences(reading=RuntimeTuiReadingPreferences(sidebar_collapsed=True)),
    ):
        with patch("voidcode.tui.app.save_global_tui_preferences") as mock_save:
            async with app.run_test() as pilot:
                await pilot.press("f2")
                await pilot.pause()

                saved_preferences = mock_save.call_args.args[0]
                assert saved_preferences == RuntimeTuiPreferences(
                    theme=RuntimeTuiThemePreferences(name="textual-dark", mode="auto"),
                    reading=RuntimeTuiReadingPreferences(wrap=False, sidebar_collapsed=False),
                )


@pytest.mark.anyio
async def test_tui_transcript_log_wraps(app_class: Any) -> None:
    VoidCodeTUI, _, _ = app_class

    mock_runtime = _mock_runtime()
    app = VoidCodeTUI(workspace=Path("."), runtime=mock_runtime)
    async with app.run_test() as pilot:
        await pilot.pause()
        log = app.query_one("#transcript-log")
        assert getattr(log, "wrap", False) is True


@pytest.mark.anyio
async def test_tui_filters_transcript_events(app_class: Any) -> None:
    VoidCodeTUI, StreamChunkReceived, StreamCompleted = app_class

    mock_runtime = _mock_runtime()
    app = VoidCodeTUI(workspace=Path("."), runtime=mock_runtime)

    async with app.run_test() as pilot:
        app.on_stream_chunk_received(
            StreamChunkReceived(
                _make_chunk(
                    status="running",
                    event=_runtime_event("graph.tool_request_created", tool="read"),
                )
            )
        )

        app.on_stream_chunk_received(
            StreamChunkReceived(
                _make_chunk(
                    status="running",
                    event=_runtime_event("runtime.internal_spam", some_data="ignored"),
                )
            )
        )

        app.on_stream_completed(StreamCompleted("completed"))
        await pilot.pause()

        log = app.query_one("#transcript-log")
        plain_text = "\\n".join("".join(segment.text for segment in line) for line in log.lines)

        assert "read" in plain_text
        assert "runtime.internal_spam" not in plain_text


@pytest.mark.anyio
async def test_tui_tool_display_summary_rendered(app_class: Any) -> None:
    VoidCodeTUI, StreamChunkReceived, StreamCompleted = app_class

    mock_runtime = _mock_runtime()
    app = VoidCodeTUI(workspace=Path("."), runtime=mock_runtime)

    async with app.run_test() as pilot:
        app.on_stream_chunk_received(
            StreamChunkReceived(
                _make_chunk(
                    status="running",
                    event=_runtime_event(
                        "graph.tool_request_created",
                        tool="read_file",
                        display={
                            "kind": "read",
                            "title": "Read",
                            "summary": "src/app.py",
                            "copyable": {"path": "src/app.py"},
                        },
                    ),
                )
            )
        )
        app.on_stream_completed(StreamCompleted("completed"))
        await pilot.pause()

        log = app.query_one("#transcript-log")
        plain = "\n".join("".join(seg.text for seg in line) for line in log.lines)
        assert "▶ Read: src/app.py" in plain


@pytest.mark.anyio
async def test_tui_approval_resolution_reuses_requested_tool_context(app_class: Any) -> None:
    VoidCodeTUI, StreamChunkReceived, _ = app_class

    app = VoidCodeTUI(workspace=Path("."), runtime=_mock_runtime())
    async with app.run_test() as pilot:
        app.on_stream_chunk_received(
            StreamChunkReceived(
                _make_chunk(
                    status="running",
                    event=_runtime_event(
                        "runtime.approval_requested",
                        request_id="approval-1",
                        tool="write_file",
                        target_summary="README.md",
                    ),
                )
            )
        )
        app.on_stream_chunk_received(
            StreamChunkReceived(
                _make_chunk(
                    status="running",
                    event=_runtime_event(
                        "runtime.approval_resolved",
                        request_id="approval-1",
                        decision="allow",
                    ),
                )
            )
        )
        await pilot.pause()

        log = app.query_one("#transcript-log")
        plain = "\n".join("".join(seg.text for seg in line) for line in log.lines)
        assert "Approval allow for tool: write_file" in plain
        assert "unknown_tool" not in plain


@pytest.mark.anyio
async def test_tui_tool_result_content_inline_for_short_results(app_class: Any) -> None:
    VoidCodeTUI, StreamChunkReceived, StreamCompleted = app_class

    mock_runtime = _mock_runtime()
    app = VoidCodeTUI(workspace=Path("."), runtime=mock_runtime)

    async with app.run_test() as pilot:
        app.on_stream_chunk_received(
            StreamChunkReceived(
                _make_chunk(
                    status="running",
                    event=_runtime_event(
                        "runtime.tool_completed",
                        tool="shell_exec",
                        tool_call_id="call-short",
                        status="ok",
                        content="line one\nline two\nline three",
                        display={
                            "kind": "shell",
                            "title": "Shell",
                            "summary": "echo hello",
                        },
                    ),
                )
            )
        )
        app.on_stream_completed(StreamCompleted("completed"))
        await pilot.pause()

        log = app.query_one("#transcript-log")
        plain = "\n".join("".join(seg.text for seg in line) for line in log.lines)
        assert "✔ Shell: echo hello" in plain
        assert "line one" in plain
        assert "line three" in plain
        assert "more lines" not in plain


@pytest.mark.anyio
async def test_tui_tool_lifecycle_updates_one_collapsible_block(app_class: Any) -> None:
    VoidCodeTUI, StreamChunkReceived, _ = app_class

    app = VoidCodeTUI(workspace=Path("."), runtime=_mock_runtime())
    display = {"kind": "shell", "title": "Shell", "summary": "echo hello"}
    async with app.run_test() as pilot:
        for event in (
            _runtime_event("graph.tool_request_created", tool="shell_exec", tool_call_id="call-1", display=display),
            _runtime_event("runtime.tool_started", tool="shell_exec", tool_call_id="call-1", display=display),
            _runtime_event(
                "runtime.tool_progress",
                tool="shell_exec",
                tool_call_id="call-1",
                stream="stdout",
                chunk="hello",
            ),
            _runtime_event(
                "runtime.tool_completed",
                tool="shell_exec",
                tool_call_id="call-1",
                status="ok",
                content="hello",
                display=display,
            ),
        ):
            app.on_stream_chunk_received(StreamChunkReceived(_make_chunk(status="running", event=event)))
        await pilot.pause()

        blocks = list(app.query("#transcript-log Collapsible"))
        assert len(blocks) == 1
        assert isinstance(blocks[0], Collapsible)
        assert blocks[0].title == "✔ Shell: echo hello"


@pytest.mark.anyio
async def test_tui_ctrl_o_toggles_tool_blocks(app_class: Any) -> None:
    VoidCodeTUI, StreamChunkReceived, _ = app_class

    app = VoidCodeTUI(workspace=Path("."), runtime=_mock_runtime())
    async with app.run_test() as pilot:
        app.on_stream_chunk_received(
            StreamChunkReceived(
                _make_chunk(
                    status="running",
                    event=_runtime_event(
                        "runtime.tool_started",
                        tool="read_file",
                        tool_call_id="call-expand",
                    ),
                )
            )
        )
        await pilot.pause()
        block = app.query_one("#transcript-log Collapsible", Collapsible)
        assert block.collapsed is True
        await pilot.press("ctrl+o")
        await pilot.pause()
        assert block.collapsed is False


@pytest.mark.anyio
async def test_tui_tool_result_truncated_shows_expand_hint(app_class: Any) -> None:
    VoidCodeTUI, StreamChunkReceived, StreamCompleted = app_class

    long_content = "\n".join(f"line {i}" for i in range(1, 26))

    mock_runtime = _mock_runtime()
    app = VoidCodeTUI(workspace=Path("."), runtime=mock_runtime)

    async with app.run_test() as pilot:
        app.on_stream_chunk_received(
            StreamChunkReceived(
                _make_chunk(
                    status="running",
                    event=_runtime_event(
                        "runtime.tool_completed",
                        tool="shell_exec",
                        tool_call_id="call-long",
                        status="ok",
                        content=long_content,
                        display={
                            "kind": "shell",
                            "title": "Shell",
                            "summary": "seq 25",
                        },
                    ),
                )
            )
        )
        app.on_stream_completed(StreamCompleted("completed"))
        await pilot.pause()

        log = app.query_one("#transcript-log")
        plain = "\n".join("".join(seg.text for seg in line) for line in log.lines)
        assert "line 1" in plain
        assert "line 5" in plain
        assert "line 25" not in plain
        assert "/expand call-long" in plain
        assert "20 more lines" in plain


@pytest.mark.anyio
async def test_tui_tool_result_read_uses_syntax_renderable(app_class: Any) -> None:
    VoidCodeTUI, StreamChunkReceived, StreamCompleted = app_class

    mock_runtime = _mock_runtime()
    app = VoidCodeTUI(workspace=Path("."), runtime=mock_runtime)

    async with app.run_test() as pilot:
        app.on_stream_chunk_received(
            StreamChunkReceived(
                _make_chunk(
                    status="running",
                    event=_runtime_event(
                        "runtime.tool_completed",
                        tool="read_file",
                        tool_call_id="call-read",
                        status="ok",
                        content="def foo():\n    return 42\n",
                        display={
                            "kind": "read",
                            "title": "Read",
                            "summary": "src/foo.py",
                            "copyable": {"path": "src/foo.py"},
                        },
                    ),
                )
            )
        )
        app.on_stream_completed(StreamCompleted("completed"))
        await pilot.pause()

        log = app.query_one("#transcript-log")
        plain = "\n".join("".join(seg.text for seg in line) for line in log.lines)
        assert "def foo():" in plain
        assert "return 42" in plain


@pytest.mark.anyio
async def test_tui_tool_result_edit_diff_coloring(app_class: Any) -> None:
    VoidCodeTUI, StreamChunkReceived, StreamCompleted = app_class

    diff_content = "--- a/src/foo.py\n+++ b/src/foo.py\n@@ -1,2 +1,2 @@\n-old_line\n+new_line\n context\n"

    mock_runtime = _mock_runtime()
    app = VoidCodeTUI(workspace=Path("."), runtime=mock_runtime)

    async with app.run_test() as pilot:
        app.on_stream_chunk_received(
            StreamChunkReceived(
                _make_chunk(
                    status="running",
                    event=_runtime_event(
                        "runtime.tool_completed",
                        tool="edit",
                        tool_call_id="call-edit",
                        status="ok",
                        content=diff_content,
                        display={
                            "kind": "edit",
                            "title": "Edit",
                            "summary": "src/foo.py (1 change)",
                            "copyable": {"path": "src/foo.py"},
                        },
                    ),
                )
            )
        )
        app.on_stream_completed(StreamCompleted("completed"))
        await pilot.pause()

        log = app.query_one("#transcript-log")
        found_red = False
        found_green = False
        found_blue = False
        for line in log.lines:
            for seg in line:
                style = str(seg.style or "")
                if "+new_line" in seg.text and "green" in style:
                    found_green = True
                if "-old_line" in seg.text and "red" in style:
                    found_red = True
                if seg.text.startswith("@@") and "blue" in style:
                    found_blue = True
        assert found_green, "expected + line styled green"
        assert found_red, "expected - line styled red"
        assert found_blue, "expected @@ header styled blue"


@pytest.mark.anyio
async def test_tui_expand_slash_command_expands_tool_block_with_stored_content(app_class: Any) -> None:
    VoidCodeTUI, StreamChunkReceived, StreamCompleted = app_class

    long_content = "\n".join(f"row {i}" for i in range(1, 26))

    mock_runtime = _mock_runtime()
    app = VoidCodeTUI(workspace=Path("."), runtime=mock_runtime)

    async with app.run_test() as pilot:
        app.on_stream_chunk_received(
            StreamChunkReceived(
                _make_chunk(
                    status="running",
                    event=_runtime_event(
                        "runtime.tool_completed",
                        tool="shell_exec",
                        tool_call_id="call-expand",
                        status="ok",
                        content=long_content,
                        display={
                            "kind": "shell",
                            "title": "Shell",
                            "summary": "seq 25",
                        },
                    ),
                )
            )
        )
        app.on_stream_completed(StreamCompleted("completed"))
        await pilot.pause()

        app._handle_slash_command("/expand call-expand")
        await pilot.pause()

        log = app.query_one("#transcript-log")
        plain = "\n".join("".join(seg.text for seg in line) for line in log.lines)
        assert "row 25" in plain
        assert app.query_one("#transcript-log Collapsible", Collapsible).collapsed is False


@pytest.mark.anyio
async def test_tui_tool_progress_coalesces_same_stream_chunks(app_class: Any) -> None:
    VoidCodeTUI, StreamChunkReceived, StreamCompleted = app_class

    mock_runtime = _mock_runtime()
    app = VoidCodeTUI(workspace=Path("."), runtime=mock_runtime)

    async with app.run_test() as pilot:
        for chunk_text in ("Hello, ", "world", "!"):
            app.on_stream_chunk_received(
                StreamChunkReceived(
                    _make_chunk(
                        status="running",
                        event=_runtime_event(
                            "runtime.tool_progress",
                            tool="shell_exec",
                            tool_call_id="call-prog",
                            stream="stdout",
                            chunk=chunk_text,
                        ),
                    )
                )
            )
        app.on_stream_chunk_received(
            StreamChunkReceived(
                _make_chunk(
                    status="running",
                    event=_runtime_event(
                        "runtime.tool_completed",
                        tool="shell_exec",
                        tool_call_id="call-prog",
                        status="ok",
                        content="done",
                        display={
                            "kind": "shell",
                            "title": "Shell",
                            "summary": "echo",
                        },
                    ),
                )
            )
        )
        app.on_stream_completed(StreamCompleted("completed"))
        await pilot.pause()

        log = app.query_one("#transcript-log")
        plain = "\n".join("".join(seg.text for seg in line) for line in log.lines)
        assert "Hello, world!" in plain
        assert "stdout" in plain


@pytest.mark.anyio
async def test_tui_context_panel_updates_from_metadata(app_class: Any) -> None:
    VoidCodeTUI, StreamChunkReceived, _ = app_class

    mock_runtime = _mock_runtime()
    app = VoidCodeTUI(workspace=Path("."), runtime=mock_runtime)

    async with app.run_test() as pilot:
        assert app.query_one("#context-panel").renderable == "Unknown"

        mock_session = _StubSession(
            session=_StubSessionRef(id="test-session"),
            status="running",
            metadata={
                "context_window": {
                    "retained_tool_result_count": 5,
                    "token_budget": 120,
                }
            },
        )
        chunk = _StubChunk(kind="event", session=mock_session, event=_runtime_event("test"))
        app.on_stream_chunk_received(StreamChunkReceived(chunk))
        await pilot.pause()

        assert app.query_one("#context-panel").renderable == "5 results\n[Budget: 120 tokens]"

        mock_session_compacted = _StubSession(
            session=_StubSessionRef(id="test-session"),
            status="running",
            metadata={
                "context_window": {
                    "retained_tool_result_count": 10,
                    "token_budget": 240,
                    "compacted": True,
                    "compaction_reason": "token limit",
                }
            },
        )
        chunk_compacted = _StubChunk(kind="event", session=mock_session_compacted, event=_runtime_event("test"))
        app.on_stream_chunk_received(StreamChunkReceived(chunk_compacted))
        await pilot.pause()

        assert app.query_one("#context-panel").renderable == "10 results\n[Budget: 240 tokens]\n[Compacted: token limit]"


@pytest.mark.anyio
async def test_tui_context_update_targets_root_screen_while_question_modal_is_open(app_class: Any) -> None:
    VoidCodeTUI, StreamChunkReceived, _ = app_class

    app = VoidCodeTUI(workspace=Path("."), runtime=_mock_runtime())
    async with app.run_test() as pilot:
        app.on_stream_chunk_received(
            StreamChunkReceived(
                _make_chunk(
                    status="waiting",
                    event=_runtime_event(
                        "runtime.question_requested",
                        request_id="question-context-race",
                        tool="question",
                        question_count=1,
                        questions=[
                            {
                                "header": "Choice",
                                "question": "Continue?",
                                "multiple": False,
                                "options": [{"label": "Yes", "description": "Continue"}],
                            }
                        ],
                    ),
                )
            )
        )
        await pilot.pause()

        from voidcode.tui.messages import ContextPanelUpdated
        from voidcode.tui.screens import QuestionModal

        assert isinstance(app.screen, QuestionModal)
        app.on_context_panel_updated(ContextPanelUpdated("7 results\n[Budget: 256 tokens]"))
        await pilot.pause()

        assert app.screen_stack[0].query_one("#context-panel").renderable == "7 results\n[Budget: 256 tokens]"


@pytest.mark.anyio
async def test_tui_tab_completes_slash_command(app_class: Any) -> None:
    VoidCodeTUI, _, _ = app_class

    mock_runtime = _mock_runtime()
    app = VoidCodeTUI(workspace=Path("."), runtime=mock_runtime)

    async with app.run_test() as pilot:
        await pilot.pause()
        composer = app.query_one("#composer-input", Input)
        composer.focus()
        composer.value = "/exp"
        await pilot.press("tab")
        await pilot.pause()

        assert composer.value == "/expand"


@pytest.mark.anyio
async def test_tui_tab_unknown_slash_prefix_does_not_complete(app_class: Any) -> None:
    """Tab on a slash prefix with no matching command leaves the input unchanged."""
    VoidCodeTUI, _, _ = app_class

    mock_runtime = _mock_runtime()
    app = VoidCodeTUI(workspace=Path("."), runtime=mock_runtime)

    async with app.run_test() as pilot:
        await pilot.pause()
        composer = app.query_one("#composer-input", Input)
        composer.focus()
        composer.value = "/xyz"
        await pilot.press("tab")
        await pilot.pause()

        assert composer.value == "/xyz"
        assert app.current_state == "Idle"


@pytest.mark.anyio
async def test_tui_tab_ambiguous_slash_prefix_does_not_complete(app_class: Any) -> None:
    """Tab on a slash prefix matching multiple commands never guesses."""
    VoidCodeTUI, _, _ = app_class

    mock_runtime = _mock_runtime()
    app = VoidCodeTUI(workspace=Path("."), runtime=mock_runtime)

    with patch("voidcode.tui.app._SLASH_COMMANDS", ("/expand", "/export")):
        async with app.run_test() as pilot:
            await pilot.pause()
            composer = app.query_one("#composer-input", Input)
            composer.focus()
            composer.value = "/exp"
            await pilot.press("tab")
            await pilot.pause()

            assert composer.value == "/exp"


@pytest.mark.anyio
async def test_tui_slash_commands_populated_on_mount(app_class: Any) -> None:
    """The composer's slash command list is populated and usable right after mount."""
    VoidCodeTUI, _, _ = app_class

    mock_runtime = _mock_runtime()
    app = VoidCodeTUI(workspace=Path("."), runtime=mock_runtime)

    async with app.run_test() as pilot:
        await pilot.pause()

        import voidcode.tui.app as tui_app_module

        assert "/expand" in tui_app_module._SLASH_COMMANDS

        composer = app.query_one("#composer-input", Input)
        composer.focus()
        composer.value = "/exp"
        await pilot.press("tab")
        await pilot.pause()

        assert composer.value == "/expand"


@pytest.mark.anyio
async def test_tui_shift_tab_exits_composer(app_class: Any) -> None:
    VoidCodeTUI, _, _ = app_class

    mock_runtime = _mock_runtime()
    app = VoidCodeTUI(workspace=Path("."), runtime=mock_runtime)

    async with app.run_test() as pilot:
        await pilot.pause()
        composer = app.query_one("#composer-input", Input)
        composer.focus()
        await pilot.press("shift+tab")
        await pilot.pause()

        assert composer.has_focus is False


@pytest.mark.anyio
async def test_tui_widgets_have_tooltips(app_class: Any) -> None:
    VoidCodeTUI, _, _ = app_class

    mock_runtime = _mock_runtime()
    app = VoidCodeTUI(workspace=Path("."), runtime=mock_runtime)

    async with app.run_test() as pilot:
        await pilot.pause()

        for selector in (
            "#composer-input",
            "#transcript-log",
            "#status-panel",
            "#session-panel",
        ):
            widget = app.query_one(selector)
            assert widget.tooltip, f"{selector} missing accessibility tooltip"


@pytest.mark.anyio
async def test_tui_transcript_line_cap_prevents_unbounded_growth(app_class: Any) -> None:
    """Perf regression: the transcript log must cap retained lines."""
    VoidCodeTUI, _, _ = app_class

    mock_runtime = _mock_runtime()
    app = VoidCodeTUI(workspace=Path("."), runtime=mock_runtime)

    async with app.run_test() as pilot:
        await pilot.pause()
        log = app.query_one("#transcript-log", TimelineView)

        assert log.max_lines == 2000

        for i in range(3000):
            log.write(f"line {i}")
        await pilot.pause()

        assert len(log.lines) <= log.max_lines


@pytest.mark.anyio
async def test_tui_content_block_enforces_max_lines(app_class: Any) -> None:
    """Large tool results are truncated to the head preview plus a hint line."""
    VoidCodeTUI, _, _ = app_class

    mock_runtime = _mock_runtime()
    app = VoidCodeTUI(workspace=Path("."), runtime=mock_runtime)

    async with app.run_test() as pilot:
        await pilot.pause()
        log = app.query_one("#transcript-log", TimelineView)
        before = len(log.lines)
        long_content = "\n".join(f"line {i}" for i in range(1, 501))

        app._write_content_block(log, long_content, "call-cap")
        await pilot.pause()

        written = len(log.lines) - before
        assert written <= 10


@pytest.mark.anyio
async def test_tui_integration_smoke_mount_run_tool_and_approval(app_class: Any) -> None:
    """End-to-end smoke: mount, submit, stream output, tool display, approval modal."""
    VoidCodeTUI, StreamChunkReceived, _ = app_class

    stream = iter(
        (
            _make_chunk(status="running", output="Hello from the stream\n"),
            _make_chunk(
                status="running",
                event=_runtime_event(
                    "graph.tool_request_created",
                    tool="read_file",
                    display={
                        "kind": "read",
                        "title": "Read",
                        "summary": "src/app.py",
                        "copyable": {"path": "src/app.py"},
                    },
                ),
            ),
        )
    )

    mock_runtime = _mock_runtime()
    mock_runtime.run_stream.return_value = stream
    app = VoidCodeTUI(workspace=Path("."), runtime=mock_runtime)

    async with app.run_test() as pilot:
        await pilot.press("a", "b", "c", "enter")
        await pilot.pause()
        await pilot.pause()

        mock_runtime.run_stream.assert_called_once()
        assert app.session_id == "demo-session"
        first_request = mock_runtime.run_stream.call_args.args[0]
        assert first_request.session_id is None
        assert first_request.allocate_session_id is True

        log = app.query_one("#transcript-log")
        plain = "\n".join("".join(seg.text for seg in line) for line in log.lines)
        assert "Hello from the stream" in plain
        assert "▶ Read: src/app.py" in plain

        app.on_stream_chunk_received(
            StreamChunkReceived(
                _make_chunk(
                    status="waiting",
                    event=_runtime_event(
                        "runtime.approval_requested",
                        request_id="req-smoke",
                        tool="write_file",
                        target_summary="sample.txt",
                    ),
                )
            )
        )
        await pilot.pause()

        from voidcode.tui.screens import ApprovalModal

        assert app.current_state == "Waiting approval"
        assert app.pending_request_id == "req-smoke"
        base_screen = app.screen_stack[0]
        assert base_screen.query_one("#composer-input").disabled is False

        approval_modal = app.screen
        assert isinstance(approval_modal, ApprovalModal)
        assert approval_modal.event.payload["tool"] == "write_file"

        plain = "\n".join("".join(seg.text for seg in line) for line in log.lines)
        assert "⚠ Approval requested for tool: write_file" in plain


@pytest.mark.anyio
async def test_tui_question_modal_submits_answers_and_resumes_stream(app_class: Any) -> None:
    VoidCodeTUI, StreamChunkReceived, _ = app_class

    mock_runtime = _mock_runtime()
    mock_runtime.answer_question_stream.return_value = iter((_make_chunk(session_id="question-session", status="completed", output="continued"),))
    app = VoidCodeTUI(workspace=Path("."), runtime=mock_runtime)

    async with app.run_test() as pilot:
        app.on_stream_chunk_received(
            StreamChunkReceived(
                _make_chunk(
                    session_id="question-session",
                    status="waiting",
                    event=_runtime_event(
                        "runtime.question_requested",
                        request_id="question-1",
                        tool="question",
                        question_count=1,
                        questions=[
                            {
                                "header": "Approach",
                                "question": "Which approach?",
                                "multiple": False,
                                "options": [
                                    {"label": "Minimal", "description": "Small change"},
                                    {"label": "Complete", "description": "Full flow"},
                                ],
                            }
                        ],
                    ),
                )
            )
        )
        await pilot.pause()

        from voidcode.tui.screens import QuestionModal

        modal = app.screen
        assert isinstance(modal, QuestionModal)
        assert app.current_state == "Waiting input"
        options = modal.query_one("#question-options", OptionList)
        options.highlighted = 2
        await pilot.press("enter")
        custom = modal.query_one("#question-custom", Input)
        assert custom.display is True
        custom.value = "Complete"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()

        log = app.screen_stack[0].query_one("#transcript-log")
        plain = "\n".join("".join(segment.text for segment in line) for line in log.lines)
        assert "✓ Answered" in plain
        assert "Approach" in plain

    mock_runtime.answer_question_stream.assert_called_once()
    call = mock_runtime.answer_question_stream.call_args
    assert call.args == ("question-session",)
    assert call.kwargs["question_request_id"] == "question-1"
    assert call.kwargs["responses"][0].header == "Approach"
    assert call.kwargs["responses"][0].answers == ("Complete",)


@pytest.mark.anyio
async def test_tui_question_wizard_handles_multi_select_pages_and_review(app_class: Any) -> None:
    VoidCodeTUI, StreamChunkReceived, _ = app_class

    mock_runtime = _mock_runtime()
    mock_runtime.answer_question_stream.return_value = iter((_make_chunk(session_id="wizard-session", status="completed", output="continued"),))
    app = VoidCodeTUI(workspace=Path("."), runtime=mock_runtime)

    async with app.run_test() as pilot:
        app.on_stream_chunk_received(
            StreamChunkReceived(
                _make_chunk(
                    session_id="wizard-session",
                    status="waiting",
                    event=_runtime_event(
                        "runtime.question_requested",
                        request_id="question-wizard",
                        tool="question",
                        question_count=2,
                        questions=[
                            {
                                "header": "Features",
                                "question": "Which features?",
                                "multiple": True,
                                "options": [
                                    {"label": "Tests", "description": "Add coverage"},
                                    {"label": "Docs", "description": "Update documentation"},
                                ],
                            },
                            {
                                "header": "Mode",
                                "question": "Which mode?",
                                "multiple": False,
                                "options": [
                                    {"label": "Fast", "description": "Fewer checks"},
                                    {"label": "Safe", "description": "Full verification"},
                                ],
                            },
                        ],
                    ),
                )
            )
        )
        await pilot.pause()

        from voidcode.tui.screens import QuestionModal

        modal = app.screen
        assert isinstance(modal, QuestionModal)
        options = modal.query_one("#question-options", OptionList)
        options.highlighted = 0
        await pilot.press("space")
        options.highlighted = 1
        await pilot.press("enter")
        assert "Tests" in modal.answers[0].selected
        assert "Docs" in modal.answers[0].selected

        await pilot.click("#question-next")
        assert modal.page_index == 1
        options.highlighted = 1
        await pilot.press("enter")
        assert modal.query_one("#question-review").display is True
        review = modal.query_one("#question-review")
        review_plain = str(review.renderable)
        assert "Features" in review_plain
        assert "Mode" in review_plain

        await pilot.click("#question-submit")
        await pilot.pause()
        await pilot.pause()

    responses = mock_runtime.answer_question_stream.call_args.kwargs["responses"]
    assert responses[0].answers == ("Tests", "Docs")
    assert responses[1].answers == ("Safe",)


@pytest.mark.anyio
async def test_tui_reuses_session_id_for_follow_up_prompt(app_class: Any) -> None:
    VoidCodeTUI, StreamChunkReceived, _ = app_class

    mock_runtime = _mock_runtime()
    mock_runtime.run_stream.side_effect = [
        iter((_make_chunk(session_id="session-1", status="completed", output="first"),)),
        iter((_make_chunk(session_id="session-1", status="completed", output="second"),)),
    ]
    app = VoidCodeTUI(workspace=Path("."), runtime=mock_runtime)

    async with app.run_test() as pilot:
        await pilot.press("a", "enter")
        await pilot.pause()
        await pilot.pause()
        assert app.session_id == "session-1"

        await pilot.press("b", "enter")
        await pilot.pause()
        await pilot.pause()

    requests = [call.args[0] for call in mock_runtime.run_stream.call_args_list]
    assert requests[0].session_id is None
    assert requests[0].allocate_session_id is True
    assert requests[1].session_id == "session-1"
    assert requests[1].allocate_session_id is False
