from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

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
                assert app.current_state == "Idle"


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
async def test_tui_accumulates_tokens_from_model_turn(app_class: Any) -> None:
    VoidCodeTUI, StreamChunkReceived, _ = app_class

    with patch("voidcode.tui.app.load_runtime_config", autospec=True, return_value=object()):
        with patch("voidcode.tui.app.VoidCodeRuntime", autospec=True):
            app = VoidCodeTUI(workspace=Path("."))

            async with app.run_test() as pilot:
                app.on_stream_chunk_received(
                    StreamChunkReceived(
                        _make_chunk(
                            status="running",
                            event=_runtime_event(
                                "graph.model_turn",
                                input_tokens=500,
                                output_tokens=300,
                            ),
                        )
                    )
                )
                app.on_stream_chunk_received(
                    StreamChunkReceived(
                        _make_chunk(
                            status="running",
                            event=_runtime_event(
                                "graph.model_turn",
                                input_tokens=200,
                                output_tokens=100,
                            ),
                        )
                    )
                )
                await pilot.pause()

                assert app._tokens_in == 700
                assert app._tokens_out == 400


@pytest.mark.anyio
async def test_tui_status_bar_shows_workspace_and_session(app_class: Any) -> None:
    VoidCodeTUI, StreamChunkReceived, _ = app_class

    with patch("voidcode.tui.app.load_runtime_config", autospec=True, return_value=object()):
        with patch("voidcode.tui.app.VoidCodeRuntime", autospec=True) as runtime_class:
            runtime = runtime_class.return_value
            runtime.current_lsp_state.side_effect = Exception("no lsp")
            runtime.current_acp_state.side_effect = Exception("no acp")
            app = VoidCodeTUI(workspace=Path("/home/user/myproject"))

            async with app.run_test() as pilot:
                app.on_stream_chunk_received(
                    StreamChunkReceived(
                        _make_chunk(
                            session_id="abcdef1234567890",
                            status="running",
                            event=_runtime_event("graph.model_turn"),
                        )
                    )
                )
                await pilot.pause()

                bar = app.query_one("#runtime-status-bar")
                bar_text = str(bar.content)
                assert "myproject" in bar_text
                assert "abcdef12" in bar_text


@pytest.mark.anyio
async def test_tui_ctrl_p_opens_runtime_detail_screen(app_class: Any) -> None:
    VoidCodeTUI, _, _ = app_class

    with patch("voidcode.tui.app.load_runtime_config", autospec=True, return_value=object()):
        with patch("voidcode.tui.app.VoidCodeRuntime", autospec=True) as runtime_class:
            runtime = runtime_class.return_value
            runtime.list_sessions.return_value = ()
            runtime.current_lsp_state.side_effect = Exception("no lsp")
            runtime.current_acp_state.side_effect = Exception("no acp")
            app = VoidCodeTUI(workspace=Path("."))

            async with app.run_test() as pilot:
                await pilot.press("ctrl+p")
                await pilot.pause()

                from voidcode.tui.screens import RuntimeDetailScreen

                assert isinstance(app.screen, RuntimeDetailScreen)


@pytest.mark.anyio
async def test_tui_ctrl_x_opens_runtime_detail_screen(app_class: Any) -> None:
    VoidCodeTUI, _, _ = app_class

    with patch("voidcode.tui.app.load_runtime_config", autospec=True, return_value=object()):
        with patch("voidcode.tui.app.VoidCodeRuntime", autospec=True) as runtime_class:
            runtime = runtime_class.return_value
            runtime.list_sessions.return_value = ()
            runtime.current_lsp_state.side_effect = Exception("no lsp")
            runtime.current_acp_state.side_effect = Exception("no acp")
            app = VoidCodeTUI(workspace=Path("."))

            async with app.run_test() as pilot:
                await pilot.press("ctrl+x")
                await pilot.pause()

                from voidcode.tui.screens import RuntimeDetailScreen

                assert isinstance(app.screen, RuntimeDetailScreen)
