from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from voidcode.runtime.config import (
    RuntimeTuiPreferences,
    RuntimeTuiReadingPreferences,
    RuntimeTuiThemePreferences,
)


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


def _mock_runtime_config(
    *, leader_key: str = "alt+x", preferences: RuntimeTuiPreferences | None = None
) -> MagicMock:
    config = MagicMock()
    config.tui = MagicMock()
    config.tui.leader_key = leader_key
    config.tui.keymap = None
    config.tui.preferences = preferences
    return config


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

    with patch(
        "voidcode.tui.app.load_runtime_config",
        autospec=True,
        return_value=_mock_runtime_config(),
    ):
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

    with patch(
        "voidcode.tui.app.load_runtime_config",
        autospec=True,
        return_value=_mock_runtime_config(),
    ):
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
async def test_tui_renders_output_as_markdown(app_class: Any) -> None:
    VoidCodeTUI, StreamChunkReceived, StreamCompleted = app_class

    with patch(
        "voidcode.tui.app.load_runtime_config",
        autospec=True,
        return_value=_mock_runtime_config(),
    ):
        with patch("voidcode.tui.app.VoidCodeRuntime", autospec=True):
            app = VoidCodeTUI(workspace=Path("."))

            async with app.run_test() as pilot:
                app.on_stream_chunk_received(
                    StreamChunkReceived(_make_chunk(status="completed", output="**bold**"))
                )
                app.on_stream_completed(StreamCompleted("completed"))
                await pilot.pause()

                log = app.query_one("#transcript-log")
                last_line = log.lines[-1]
                plain_text = "".join(segment.text for segment in last_line)
                assert "bold" in plain_text
                assert "**" not in plain_text
                assert app.current_state == "Idle"


@pytest.mark.anyio
async def test_tui_failed_stream_stays_failed(app_class: Any) -> None:
    VoidCodeTUI, StreamChunkReceived, StreamCompleted = app_class

    with patch(
        "voidcode.tui.app.load_runtime_config",
        autospec=True,
        return_value=_mock_runtime_config(),
    ):
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

    mock_config = _mock_runtime_config(leader_key="alt+y")

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

            app = VoidCodeTUI(workspace=Path("/fake/workspace"))

            async with app.run_test() as pilot:
                await pilot.pause()

                assert app.query_one("#workspace-panel").content == "workspace"
                assert app.query_one("#lsp-panel").content == "Active: 1"


@pytest.mark.anyio
async def test_tui_command_palette_new_session(app_class: Any) -> None:
    VoidCodeTUI, _, _ = app_class

    mock_config = _mock_runtime_config()

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

    mock_config = _mock_runtime_config()

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

                await pilot.press(
                    "r", "e", "s", "u", "m", "e", "enter"
                )  # Selects 'session: resume'
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
async def test_tui_command_palette_theme_switch(app_class: Any) -> None:
    VoidCodeTUI, _, _ = app_class

    mock_config = _mock_runtime_config()

    with patch("voidcode.tui.app.load_runtime_config", autospec=True, return_value=mock_config):
        with patch(
            "voidcode.tui.app.load_global_tui_preferences",
            autospec=True,
            return_value=None,
        ):
            with patch(
                "voidcode.tui.app.load_workspace_tui_preferences",
                autospec=True,
                return_value=None,
            ):
                with patch("voidcode.tui.app.VoidCodeRuntime", autospec=True):
                    with patch("voidcode.tui.app.save_global_tui_preferences") as mock_save:
                        app = VoidCodeTUI(workspace=Path("."))

                        async with app.run_test() as pilot:
                            await pilot.press("alt+x")
                            await pilot.pause()

                            # session: new, session: resume, theme: switch
                            await pilot.press("s", "w", "i", "t", "c", "h", "enter")
                            await pilot.pause()

                            from voidcode.tui.screens import ThemePickerModal

                            assert isinstance(app.screen, ThemePickerModal)

                            # select theme
                            await pilot.press("enter")
                            await pilot.pause()

                            mock_save.assert_called_once()
                            assert app._tui_preferences.theme is not None
                            saved_preferences = mock_save.call_args.args[0]
                            assert saved_preferences.theme == RuntimeTuiThemePreferences(
                                name=app._tui_preferences.theme.name,
                                mode=None,
                            )
                            assert saved_preferences.reading is None


@pytest.mark.anyio
async def test_tui_command_palette_view_wrap(app_class: Any) -> None:
    VoidCodeTUI, _, _ = app_class

    mock_config = _mock_runtime_config(
        preferences=RuntimeTuiPreferences(
            theme=RuntimeTuiThemePreferences(name="textual-dark", mode="auto"),
            reading=RuntimeTuiReadingPreferences(sidebar_collapsed=True),
        )
    )

    with patch("voidcode.tui.app.load_runtime_config", autospec=True, return_value=mock_config):
        with patch(
            "voidcode.tui.app.load_global_tui_preferences",
            autospec=True,
            return_value=RuntimeTuiPreferences(
                theme=RuntimeTuiThemePreferences(name="textual-dark", mode="auto"),
                reading=RuntimeTuiReadingPreferences(wrap=True, sidebar_collapsed=True),
            ),
        ):
            with patch(
                "voidcode.tui.app.load_workspace_tui_preferences",
                autospec=True,
                return_value=None,
            ):
                with patch("voidcode.tui.app.VoidCodeRuntime", autospec=True):
                    with patch("voidcode.tui.app.save_global_tui_preferences") as mock_save:
                        app = VoidCodeTUI(workspace=Path("."))

                        async with app.run_test() as pilot:
                            await pilot.press("alt+x")
                            await pilot.pause()

                            # view: wrap is 5th item
                            await pilot.press("w", "r", "a", "p", "enter")
                            await pilot.pause()

                            mock_save.assert_called_once()
                            assert app._tui_preferences.reading is not None
                            assert app._tui_preferences.reading.wrap is False
                            saved_preferences = mock_save.call_args.args[0]
                            assert saved_preferences.reading == RuntimeTuiReadingPreferences(
                                wrap=False, sidebar_collapsed=True
                            )
                            assert saved_preferences.theme == RuntimeTuiThemePreferences(
                                name="textual-dark", mode="auto"
                            )


@pytest.mark.anyio
async def test_tui_wrap_toggle_does_not_snapshot_inherited_global_theme(app_class: Any) -> None:
    VoidCodeTUI, _, _ = app_class

    mock_config = _mock_runtime_config(
        preferences=RuntimeTuiPreferences(
            theme=RuntimeTuiThemePreferences(name="tokyo-night", mode="dark"),
            reading=RuntimeTuiReadingPreferences(wrap=True, sidebar_collapsed=True),
        )
    )

    with patch("voidcode.tui.app.load_runtime_config", autospec=True, return_value=mock_config):
        with patch(
            "voidcode.tui.app.load_global_tui_preferences",
            autospec=True,
            return_value=RuntimeTuiPreferences(
                theme=RuntimeTuiThemePreferences(name="tokyo-night", mode="dark"),
                reading=RuntimeTuiReadingPreferences(wrap=True, sidebar_collapsed=True),
            ),
        ):
            with patch(
                "voidcode.tui.app.load_workspace_tui_preferences",
                autospec=True,
                return_value=None,
            ):
                with patch("voidcode.tui.app.VoidCodeRuntime", autospec=True):
                    with patch("voidcode.tui.app.save_global_tui_preferences") as mock_save:
                        app = VoidCodeTUI(workspace=Path("."))

                        async with app.run_test() as pilot:
                            await pilot.press("alt+x")
                            await pilot.pause()
                            await pilot.press("w", "r", "a", "p", "enter")
                            await pilot.pause()

                            saved_preferences = mock_save.call_args.args[0]
                            assert saved_preferences == RuntimeTuiPreferences(
                                theme=RuntimeTuiThemePreferences(name="tokyo-night", mode="dark"),
                                reading=RuntimeTuiReadingPreferences(
                                    wrap=False, sidebar_collapsed=True
                                ),
                            )


@pytest.mark.anyio
async def test_tui_default_preference_changes_write_global_not_workspace(app_class: Any) -> None:
    VoidCodeTUI, _, _ = app_class

    mock_config = _mock_runtime_config(
        preferences=RuntimeTuiPreferences(
            theme=RuntimeTuiThemePreferences(name="textual-dark", mode="auto"),
            reading=RuntimeTuiReadingPreferences(wrap=True, sidebar_collapsed=False),
        )
    )

    with patch("voidcode.tui.app.load_runtime_config", autospec=True, return_value=mock_config):
        with patch(
            "voidcode.tui.app.load_global_tui_preferences",
            autospec=True,
            return_value=RuntimeTuiPreferences(
                theme=RuntimeTuiThemePreferences(name="textual-dark", mode="auto"),
                reading=RuntimeTuiReadingPreferences(wrap=True, sidebar_collapsed=False),
            ),
        ):
            with patch(
                "voidcode.tui.app.load_workspace_tui_preferences",
                autospec=True,
                return_value=RuntimeTuiPreferences(
                    reading=RuntimeTuiReadingPreferences(sidebar_collapsed=True)
                ),
            ):
                with patch("voidcode.tui.app.VoidCodeRuntime", autospec=True):
                    with patch("voidcode.tui.app.save_global_tui_preferences") as mock_global_save:
                        app = VoidCodeTUI(workspace=Path("."))

                        async with app.run_test() as pilot:
                            await pilot.press("alt+x")
                            await pilot.pause()
                            await pilot.press("w", "r", "a", "p", "enter")
                            await pilot.pause()

                            mock_global_save.assert_called_once()


@pytest.mark.anyio
async def test_tui_global_save_does_not_snapshot_workspace_only_override(app_class: Any) -> None:
    VoidCodeTUI, _, _ = app_class

    mock_config = _mock_runtime_config(
        preferences=RuntimeTuiPreferences(
            theme=RuntimeTuiThemePreferences(name="textual-dark", mode="auto"),
            reading=RuntimeTuiReadingPreferences(wrap=True, sidebar_collapsed=True),
        )
    )

    with patch("voidcode.tui.app.load_runtime_config", autospec=True, return_value=mock_config):
        with patch(
            "voidcode.tui.app.load_global_tui_preferences",
            autospec=True,
            return_value=RuntimeTuiPreferences(
                theme=RuntimeTuiThemePreferences(name="textual-dark", mode="auto"),
                reading=RuntimeTuiReadingPreferences(wrap=True, sidebar_collapsed=False),
            ),
        ):
            with patch(
                "voidcode.tui.app.load_workspace_tui_preferences",
                autospec=True,
                return_value=RuntimeTuiPreferences(
                    reading=RuntimeTuiReadingPreferences(sidebar_collapsed=True)
                ),
            ):
                with patch("voidcode.tui.app.VoidCodeRuntime", autospec=True):
                    with patch("voidcode.tui.app.save_global_tui_preferences") as mock_save:
                        app = VoidCodeTUI(workspace=Path("."))

                        async with app.run_test() as pilot:
                            await pilot.press("alt+x")
                            await pilot.pause()
                            await pilot.press("w", "r", "a", "p", "enter")
                            await pilot.pause()

                            saved_preferences = mock_save.call_args.args[0]
                            assert saved_preferences == RuntimeTuiPreferences(
                                theme=RuntimeTuiThemePreferences(name="textual-dark", mode="auto"),
                                reading=RuntimeTuiReadingPreferences(
                                    wrap=False, sidebar_collapsed=False
                                ),
                            )


@pytest.mark.anyio
async def test_tui_transcript_log_wraps(app_class: Any) -> None:
    VoidCodeTUI, _, _ = app_class

    with patch(
        "voidcode.tui.app.load_runtime_config",
        autospec=True,
        return_value=_mock_runtime_config(),
    ):
        with patch("voidcode.tui.app.VoidCodeRuntime", autospec=True):
            app = VoidCodeTUI(workspace=Path("."))
            async with app.run_test() as pilot:
                await pilot.pause()
                log = app.query_one("#transcript-log")
                assert getattr(log, "wrap", False) is True


@pytest.mark.anyio
async def test_tui_filters_transcript_events(app_class: Any) -> None:
    VoidCodeTUI, StreamChunkReceived, StreamCompleted = app_class

    with patch(
        "voidcode.tui.app.load_runtime_config",
        autospec=True,
        return_value=_mock_runtime_config(),
    ):
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

                assert "read" in plain_text
                assert "runtime.internal_spam" not in plain_text


@pytest.mark.anyio
async def test_tui_context_panel_updates_from_metadata(app_class: Any) -> None:
    VoidCodeTUI, StreamChunkReceived, _ = app_class

    with patch(
        "voidcode.tui.app.load_runtime_config",
        autospec=True,
        return_value=_mock_runtime_config(),
    ):
        with patch("voidcode.tui.app.VoidCodeRuntime", autospec=True):
            app = VoidCodeTUI(workspace=Path("."))

            async with app.run_test() as pilot:
                assert app.query_one("#context-panel").content == "Unknown"

                mock_session = _StubSession(
                    session=_StubSessionRef(id="test-session"),
                    status="running",
                    metadata={
                        "context_window": {
                            "retained_tool_result_count": 5,
                            "max_tool_result_count": 10,
                        }
                    },
                )
                chunk = _StubChunk(kind="event", session=mock_session, event=_runtime_event("test"))
                app.on_stream_chunk_received(StreamChunkReceived(chunk))
                await pilot.pause()

                assert app.query_one("#context-panel").content == "5 / 10 results (50%)"

                mock_session_compacted = _StubSession(
                    session=_StubSessionRef(id="test-session"),
                    status="running",
                    metadata={
                        "context_window": {
                            "retained_tool_result_count": 10,
                            "max_tool_result_count": 10,
                            "compacted": True,
                            "compaction_reason": "token limit",
                        }
                    },
                )
                chunk_compacted = _StubChunk(
                    kind="event", session=mock_session_compacted, event=_runtime_event("test")
                )
                app.on_stream_chunk_received(StreamChunkReceived(chunk_compacted))
                await pilot.pause()

                assert (
                    app.query_one("#context-panel").content
                    == "10 / 10 results (100%)\n[Compacted: token limit]"
                )
