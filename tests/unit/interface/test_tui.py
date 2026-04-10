from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


@dataclass(frozen=True)
class _StubEvent:
    sequence: int
    event_type: str
    source: str
    payload: dict[str, object]


@dataclass(frozen=True)
class _StubSessionRef:
    id: str


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

    with patch("voidcode.tui.app.load_runtime_config", autospec=True, return_value=MagicMock()):
        with patch("voidcode.tui.app.VoidCodeRuntime", autospec=True) as runtime_class:
            runtime = runtime_class.return_value
            runtime.run_stream.return_value = waiting_stream
            app = VoidCodeTUI(workspace=Path("."))

            async with app.run_test() as pilot:
                await pilot.press("a", "b", "c", "enter")
                await pilot.pause()
                await pilot.pause()

                assert app.current_state == "Waiting approval"
                assert app.pending_request_id == "req-1"
                assert app.query_one("#status-panel").content == "Waiting approval"
                assert app.query_one("#composer-input").disabled is True


@pytest.mark.anyio
async def test_tui_ignores_submission_while_stream_active(app_class: Any) -> None:
    VoidCodeTUI, _, _ = app_class

    with patch("voidcode.tui.app.load_runtime_config", autospec=True, return_value=MagicMock()):
        with patch("voidcode.tui.app.VoidCodeRuntime", autospec=True) as runtime_class:
            runtime = runtime_class.return_value
            app = VoidCodeTUI(workspace=Path("."))

            async with app.run_test() as pilot:
                app._stream_active = True
                app.query_one("#composer-input").disabled = True
                await pilot.press("x", "enter")
                await pilot.pause()

            runtime.run_stream.assert_not_called()


@pytest.mark.anyio
async def test_tui_renders_output_literally_without_markup(app_class: Any) -> None:
    VoidCodeTUI, StreamChunkReceived, StreamCompleted = app_class

    with patch("voidcode.tui.app.load_runtime_config", autospec=True, return_value=MagicMock()):
        with patch("voidcode.tui.app.VoidCodeRuntime", autospec=True):
            app = VoidCodeTUI(workspace=Path("."))

            async with app.run_test() as pilot:
                app.on_stream_chunk_received(
                    StreamChunkReceived(
                        _make_chunk(status="completed", output="[bold]literal[/bold]")
                    )
                )
                app.on_stream_completed(StreamCompleted("completed"))
                await pilot.pause()

                log = app.query_one("#transcript-log")
                last_line = log.lines[-1]
                plain_text = "".join(segment.text for segment in last_line)
                assert plain_text == "[bold]literal[/bold]"
                assert app.current_state == "Idle"


@pytest.mark.anyio
async def test_tui_failed_stream_stays_failed(app_class: Any) -> None:
    VoidCodeTUI, StreamChunkReceived, StreamCompleted = app_class

    with patch("voidcode.tui.app.load_runtime_config", autospec=True, return_value=MagicMock()):
        with patch("voidcode.tui.app.VoidCodeRuntime", autospec=True):
            app = VoidCodeTUI(workspace=Path("."))

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
                assert app.query_one("#status-panel").content == "Failed"


@pytest.mark.anyio
async def test_tui_sidebar_updates_on_mount(app_class: Any) -> None:
    VoidCodeTUI, _, _ = app_class

    mock_config = MagicMock()
    mock_config.tui.leader_key = "alt+y"

    with patch("voidcode.tui.app.load_runtime_config", autospec=True, return_value=mock_config):
        with patch("voidcode.tui.app.VoidCodeRuntime", autospec=True) as runtime_class:
            runtime = runtime_class.return_value

            # mock lsp
            mock_lsp = MagicMock()
            mock_lsp.mode = "managed"
            mock_server = MagicMock()
            mock_server.status = "running"
            mock_lsp.servers = {"pylsp": mock_server}
            runtime.current_lsp_state.return_value = mock_lsp

            # mock acp
            mock_acp = MagicMock()
            mock_acp.mode = "managed"
            mock_acp.status = "connected"
            runtime.current_acp_state.return_value = mock_acp

            app = VoidCodeTUI(workspace=Path("/fake/workspace"))

            async with app.run_test() as pilot:
                await pilot.pause()

                assert app.query_one("#workspace-panel").content == "workspace"
                assert app.query_one("#lsp-panel").content == "Active: 1"
                assert app.query_one("#acp-panel").content == "Connected"


@pytest.mark.anyio
async def test_tui_command_palette_new_session(app_class: Any) -> None:
    VoidCodeTUI, _, _ = app_class

    mock_config = MagicMock()
    mock_config.tui.leader_key = "alt+x"

    with patch("voidcode.tui.app.load_runtime_config", autospec=True, return_value=mock_config):
        with patch("voidcode.tui.app.VoidCodeRuntime", autospec=True):
            app = VoidCodeTUI(workspace=Path("."))
            app.session_id = "old-session"

            async with app.run_test() as pilot:
                await pilot.press("alt+x")
                await pilot.pause()

                # We should be in command palette
                from voidcode.tui.screens import CommandPalette

                assert isinstance(app.screen, CommandPalette)

                await pilot.press("enter")  # Selects 'session: new' since it's first
                await pilot.pause()

                assert app.session_id is None
                assert app.query_one("#session-panel").content == "None"


@pytest.mark.anyio
async def test_tui_command_palette_resume_session(app_class: Any) -> None:
    VoidCodeTUI, _, _ = app_class

    mock_config = MagicMock()
    mock_config.tui.leader_key = "alt+x"

    with patch("voidcode.tui.app.load_runtime_config", autospec=True, return_value=mock_config):
        with patch("voidcode.tui.app.VoidCodeRuntime", autospec=True) as runtime_class:
            runtime = runtime_class.return_value

            # Create a mock StoredSessionSummary
            from voidcode.runtime.session import SessionRef, StoredSessionSummary

            mock_session = StoredSessionSummary(
                session=SessionRef(id="session-test-id"),
                status="completed",
                turn=2,
                prompt="test prompt",
                updated_at=0,
            )
            runtime.list_sessions.return_value = (mock_session,)

            waiting_stream = iter(
                (
                    _make_chunk(
                        session_id="session-test-id",
                        status="running",
                        output="resumed output line 1",
                    ),
                    _make_chunk(
                        session_id="session-test-id",
                        status="completed",
                        output="resumed output line 2",
                    ),
                )
            )
            runtime.resume_stream.return_value = waiting_stream

            app = VoidCodeTUI(workspace=Path("."))

            async with app.run_test() as pilot:
                await pilot.press("alt+x")
                await pilot.pause()

                await pilot.press("down", "enter")  # Selects 'session: resume'
                await pilot.pause()

                from voidcode.tui.screens import SessionListModal

                assert isinstance(app.screen, SessionListModal)

                await pilot.press("enter")
                await pilot.pause()
                await pilot.pause()

                assert app.session_id == "session-test-id"
                assert "test-id" in app.query_one("#session-panel").content
                assert "test prompt" in app.query_one("#session-panel").content

                runtime.resume_stream.assert_called_once_with(session_id="session-test-id")

                log = app.query_one("#transcript-log")
                plain_text = "\\n".join(
                    "".join(segment.text for segment in line) for line in log.lines
                )
                assert "resumed output line 1" in plain_text
                assert "resumed output line 2" in plain_text


@pytest.mark.anyio
async def test_tui_filters_transcript_events(app_class: Any) -> None:
    VoidCodeTUI, StreamChunkReceived, StreamCompleted = app_class

    with patch("voidcode.tui.app.load_runtime_config", autospec=True, return_value=MagicMock()):
        with patch("voidcode.tui.app.VoidCodeRuntime", autospec=True):
            app = VoidCodeTUI(workspace=Path("."))

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
                plain_text = "\\n".join(
                    "".join(segment.text for segment in line) for line in log.lines
                )

                assert "graph.tool_request_created" in plain_text
                assert "runtime.internal_spam" not in plain_text
