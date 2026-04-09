from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call, patch

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


@dataclass(frozen=True)
class _StubStoredSessionSummary:
    session: _StubSessionRef
    status: str
    turn: int
    prompt: str
    updated_at: int


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

    with patch("voidcode.tui.app.load_runtime_config", autospec=True, return_value=object()):
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

    with patch("voidcode.tui.app.load_runtime_config", autospec=True, return_value=object()):
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

    with patch("voidcode.tui.app.load_runtime_config", autospec=True, return_value=object()):
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
                assert app.current_state == "Completed"


@pytest.mark.anyio
async def test_tui_failed_stream_stays_failed(app_class: Any) -> None:
    VoidCodeTUI, StreamChunkReceived, StreamCompleted = app_class

    with patch("voidcode.tui.app.load_runtime_config", autospec=True, return_value=object()):
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
async def test_tui_lists_sessions_on_mount(app_class: Any) -> None:
    VoidCodeTUI, _, _ = app_class
    sessions = (
        _StubStoredSessionSummary(
            session=_StubSessionRef(id="session-2"),
            status="completed",
            turn=1,
            prompt="read beta.md",
            updated_at=2,
        ),
        _StubStoredSessionSummary(
            session=_StubSessionRef(id="session-1"),
            status="waiting",
            turn=1,
            prompt="write alpha.txt hi",
            updated_at=1,
        ),
    )

    with patch("voidcode.tui.app.load_runtime_config", autospec=True, return_value=object()):
        with patch("voidcode.tui.app.VoidCodeRuntime", autospec=True) as runtime_class:
            runtime = runtime_class.return_value
            runtime.list_sessions.return_value = sessions
            app = VoidCodeTUI(workspace=Path("."))

            async with app.run_test() as pilot:
                await pilot.pause()

                option_list = app.query_one("#session-list")
                prompts = [str(option.prompt) for option in option_list.options]
                assert prompts == ["[COMPLETED] read beta.md", "[WAITING] write alpha.txt hi"]


@pytest.mark.anyio
async def test_tui_loading_session_resets_transcript_and_replays_history(app_class: Any) -> None:
    VoidCodeTUI, _, _ = app_class
    sessions = (
        _StubStoredSessionSummary(
            session=_StubSessionRef(id="loaded-session"),
            status="completed",
            turn=1,
            prompt="read README.md",
            updated_at=1,
        ),
    )
    replay_stream = iter(
        (
            _make_chunk(
                session_id="loaded-session",
                status="completed",
                event=_runtime_event("runtime.request_received", prompt="read README.md"),
            ),
            _make_chunk(session_id="loaded-session", status="completed", output="README body"),
        )
    )

    with patch("voidcode.tui.app.load_runtime_config", autospec=True, return_value=object()):
        with patch("voidcode.tui.app.VoidCodeRuntime", autospec=True) as runtime_class:
            runtime = runtime_class.return_value
            runtime.list_sessions.return_value = sessions
            runtime.resume_stream.return_value = replay_stream
            app = VoidCodeTUI(workspace=Path("."))

            async with app.run_test() as pilot:
                app.query_one("#transcript-log").write("stale line")
                await pilot.pause()

                option_list = app.query_one("#session-list")
                option_list.highlighted = 0
                option_list.action_select()
                await pilot.pause()
                await pilot.pause()

                log = app.query_one("#transcript-log")
                lines = ["".join(segment.text for segment in line) for line in log.lines]
                assert "stale line" not in lines
                assert lines[-1] == "README body"
                assert app.session_id == "loaded-session"
                assert app.current_state == "Completed"
                runtime.resume_stream.assert_called_once_with(
                    session_id="loaded-session",
                    approval_request_id=None,
                    approval_decision=None,
                )


@pytest.mark.anyio
async def test_tui_loading_waiting_session_allows_approval_after_restart(app_class: Any) -> None:
    VoidCodeTUI, _, _ = app_class
    sessions = (
        _StubStoredSessionSummary(
            session=_StubSessionRef(id="waiting-session"),
            status="waiting",
            turn=1,
            prompt="write sample.txt hi",
            updated_at=1,
        ),
    )
    initial_replay = iter(
        (
            _make_chunk(
                session_id="waiting-session",
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
    resumed = iter(
        (
            _make_chunk(
                session_id="waiting-session",
                status="running",
                event=_runtime_event(
                    "runtime.approval_resolved",
                    request_id="req-1",
                    decision="allow",
                ),
            ),
            _make_chunk(
                session_id="waiting-session",
                status="completed",
                output="done",
            ),
        )
    )

    with patch("voidcode.tui.app.load_runtime_config", autospec=True, return_value=object()):
        with patch("voidcode.tui.app.VoidCodeRuntime", autospec=True) as runtime_class:
            runtime = runtime_class.return_value
            runtime.list_sessions.return_value = sessions
            runtime.resume_stream = MagicMock(side_effect=[initial_replay, resumed])
            app = VoidCodeTUI(workspace=Path("."))

            async with app.run_test() as pilot:
                option_list = app.query_one("#session-list")
                option_list.highlighted = 0
                option_list.action_select()
                await pilot.pause()
                await pilot.pause()

                assert app.current_state == "Waiting approval"
                assert app.pending_request_id == "req-1"

                app.screen.dismiss("allow")
                await pilot.pause()
                await pilot.pause()

                assert app.current_state == "Completed"
                assert app.pending_request_id is None
                assert runtime.resume_stream.call_args_list == [
                    call(
                        session_id="waiting-session",
                        approval_request_id=None,
                        approval_decision=None,
                    ),
                    call(
                        session_id="waiting-session",
                        approval_request_id="req-1",
                        approval_decision="allow",
                    ),
                ]


@pytest.mark.anyio
async def test_tui_new_session_clears_loaded_state(app_class: Any) -> None:
    VoidCodeTUI, _, _ = app_class
    sessions = (
        _StubStoredSessionSummary(
            session=_StubSessionRef(id="loaded-session"),
            status="completed",
            turn=1,
            prompt="read README.md",
            updated_at=1,
        ),
    )

    with patch("voidcode.tui.app.load_runtime_config", autospec=True, return_value=object()):
        with patch("voidcode.tui.app.VoidCodeRuntime", autospec=True) as runtime_class:
            runtime = runtime_class.return_value
            runtime.list_sessions.return_value = sessions
            app = VoidCodeTUI(workspace=Path("."))

            async with app.run_test() as pilot:
                app.session_id = "loaded-session"
                app.pending_request_id = "req-1"
                app.query_one("#transcript-log").write("old")
                app._set_state("Completed")
                await pilot.click("#new-session-btn")
                await pilot.pause()

                assert app.session_id is None
                assert app.pending_request_id is None
                assert app.current_state == "Idle"
                assert len(app.query_one("#transcript-log").lines) == 0
