from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from voidcode.runtime.config import (
    RuntimeTuiConfig,
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
async def test_tui_strips_runtime_failed_prefix_from_error_lines(app_class: Any) -> None:
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
async def test_tui_closes_runtime_on_unmount(app_class: Any) -> None:
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
                await pilot.pause()

            runtime.__exit__.assert_called_once_with(None, None, None)


@pytest.mark.anyio
async def test_session_new_via_keybinding(app_class: Any) -> None:
    VoidCodeTUI, _, _ = app_class

    mock_config = _mock_runtime_config(keymap={"ctrl+n": "session_new"})

    with patch("voidcode.tui.app.load_runtime_config", autospec=True, return_value=mock_config):
        with patch("voidcode.tui.app.VoidCodeRuntime", autospec=True):
            app = VoidCodeTUI(workspace=Path("."))
            app.session_id = "old-session"

            async with app.run_test() as pilot:
                await pilot.press("ctrl+n")
                await pilot.pause()

                assert app.session_id is None
                assert app.query_one("#session-panel").content == "None"


@pytest.mark.anyio
async def test_session_resume_via_keybinding(app_class: Any) -> None:
    VoidCodeTUI, _, _ = app_class

    mock_config = _mock_runtime_config(keymap={"ctrl+r": "session_resume"})

    with patch("voidcode.tui.app.load_runtime_config", autospec=True, return_value=mock_config):
        with patch("voidcode.tui.app.VoidCodeRuntime", autospec=True) as runtime_class:
            runtime = runtime_class.return_value

            from voidcode.runtime.session import SessionRef, StoredSessionSummary

            mock_session = StoredSessionSummary(
                session=SessionRef(id="session-test-id"),
                status="completed",
                turn=2,
                prompt="test prompt",
                updated_at=0,
            )
            runtime.list_sessions.return_value = (mock_session,)

            app = VoidCodeTUI(workspace=Path("."))

            async with app.run_test() as pilot:
                await pilot.press("ctrl+r")
                await pilot.pause()

                from voidcode.tui.screens import SessionListModal

                assert isinstance(app.screen, SessionListModal)


@pytest.mark.anyio
async def test_theme_switch_via_keybinding(app_class: Any) -> None:
    VoidCodeTUI, _, _ = app_class
    TestTUI = _make_keybind_tui(VoidCodeTUI)

    mock_config = _mock_runtime_config(keymap={"ctrl+t": "theme_switch"})

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
                    app = TestTUI(workspace=Path("."))

                    async with app.run_test() as pilot:
                        await pilot.press("ctrl+t")
                        await pilot.pause()

                        from voidcode.tui.screens import ThemePickerModal

                        assert isinstance(app.screen, ThemePickerModal)


@pytest.mark.anyio
async def test_tui_command_palette_view_wrap(app_class: Any) -> None:
    VoidCodeTUI, _, _ = app_class
    TestTUI = _make_keybind_tui(VoidCodeTUI)

    mock_config = _mock_runtime_config(
        keymap={"f2": "view_wrap"},
        preferences=RuntimeTuiPreferences(
            theme=RuntimeTuiThemePreferences(name="textual-dark", mode="auto"),
            reading=RuntimeTuiReadingPreferences(sidebar_collapsed=True),
        ),
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
                        app = TestTUI(workspace=Path("."))

                        async with app.run_test() as pilot:
                            await pilot.press("f2")
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
    TestTUI = _make_keybind_tui(VoidCodeTUI)

    mock_config = _mock_runtime_config(
        keymap={"f2": "view_wrap"},
        preferences=RuntimeTuiPreferences(
            theme=RuntimeTuiThemePreferences(name="tokyo-night", mode="dark"),
            reading=RuntimeTuiReadingPreferences(wrap=True, sidebar_collapsed=True),
        ),
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
                        app = TestTUI(workspace=Path("."))

                        async with app.run_test() as pilot:
                            await pilot.press("f2")
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
    TestTUI = _make_keybind_tui(VoidCodeTUI)

    mock_config = _mock_runtime_config(
        keymap={"f2": "view_wrap"},
        preferences=RuntimeTuiPreferences(
            theme=RuntimeTuiThemePreferences(name="textual-dark", mode="auto"),
            reading=RuntimeTuiReadingPreferences(wrap=True, sidebar_collapsed=False),
        ),
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
                        app = TestTUI(workspace=Path("."))

                        async with app.run_test() as pilot:
                            await pilot.press("f2")
                            await pilot.pause()

                            mock_global_save.assert_called_once()


@pytest.mark.anyio
async def test_tui_global_save_does_not_snapshot_workspace_only_override(app_class: Any) -> None:
    VoidCodeTUI, _, _ = app_class
    TestTUI = _make_keybind_tui(VoidCodeTUI)

    mock_config = _mock_runtime_config(
        keymap={"f2": "view_wrap"},
        preferences=RuntimeTuiPreferences(
            theme=RuntimeTuiThemePreferences(name="textual-dark", mode="auto"),
            reading=RuntimeTuiReadingPreferences(wrap=True, sidebar_collapsed=True),
        ),
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
                        app = TestTUI(workspace=Path("."))

                        async with app.run_test() as pilot:
                            await pilot.press("f2")
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
async def test_tui_tool_display_summary_rendered(app_class: Any) -> None:
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
async def test_tui_tool_result_content_inline_for_short_results(app_class: Any) -> None:
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
async def test_tui_tool_result_truncated_shows_expand_hint(app_class: Any) -> None:
    VoidCodeTUI, StreamChunkReceived, StreamCompleted = app_class

    long_content = "\n".join(f"line {i}" for i in range(1, 26))

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

    diff_content = (
        "--- a/src/foo.py\n"
        "+++ b/src/foo.py\n"
        "@@ -1,2 +1,2 @@\n"
        "-old_line\n"
        "+new_line\n"
        " context\n"
    )

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
async def test_tui_expand_slash_command_writes_stored_content(app_class: Any) -> None:
    VoidCodeTUI, StreamChunkReceived, StreamCompleted = app_class

    long_content = "\n".join(f"row {i}" for i in range(1, 26))

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
                assert "/expand call-expand" in plain
                assert "row 25" in plain


@pytest.mark.anyio
async def test_tui_tool_progress_coalesces_same_stream_chunks(app_class: Any) -> None:
    VoidCodeTUI, StreamChunkReceived, StreamCompleted = app_class

    with patch(
        "voidcode.tui.app.load_runtime_config",
        autospec=True,
        return_value=_mock_runtime_config(),
    ):
        with patch("voidcode.tui.app.VoidCodeRuntime", autospec=True):
            app = VoidCodeTUI(workspace=Path("."))

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
                            "token_budget": 120,
                        }
                    },
                )
                chunk = _StubChunk(kind="event", session=mock_session, event=_runtime_event("test"))
                app.on_stream_chunk_received(StreamChunkReceived(chunk))
                await pilot.pause()

                assert app.query_one("#context-panel").content == "5 results\n[Budget: 120 tokens]"

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
                chunk_compacted = _StubChunk(
                    kind="event", session=mock_session_compacted, event=_runtime_event("test")
                )
                app.on_stream_chunk_received(StreamChunkReceived(chunk_compacted))
                await pilot.pause()

                assert (
                    app.query_one("#context-panel").content
                    == "10 results\n[Budget: 240 tokens]\n[Compacted: token limit]"
                )
