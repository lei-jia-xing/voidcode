"""Statusline segments: session id, run-state + spinner, theme name."""

from __future__ import annotations

import re
from typing import Any

from voidcode.sdk.client import CommandCatalog
from voidcode.tui.terminal_app import CoreTuiApp


class _StubTerminal:
    width = 100
    eof = False
    color_system = None

    def query_default_background_rgb(self) -> None:
        return None

    def __init__(self) -> None:
        self.committed: list[str] = []
        self.frames: list[list[str]] = []

    def commit_rows(self, rows: list[str]) -> None:
        self.committed.extend(rows)

    def paint_frame(self, frame: Any) -> None:
        self.frames.append(list(frame.rows))


class _StubClient:
    def command_catalog(self) -> CommandCatalog:
        return CommandCatalog(revision_id="rev-1", commands=())


def _frame_text(terminal: _StubTerminal) -> str:
    return "\n".join(re.sub(r"\x1b\[[0-9;]*m", "", row) for row in terminal.frames[-1])


def test_segments_reflect_session_run_state_and_theme() -> None:
    terminal = _StubTerminal()
    app = CoreTuiApp(_StubClient(), session_id="session-abcdef", terminal=terminal)  # type: ignore[arg-type]
    app.status_line = ""

    app._render_frame()
    text = _frame_text(terminal)
    assert "session-" in text  # short session id
    assert "session-abcdef" not in text
    assert "idle" in text
    assert app.theme.name in text

    app._run_active = True
    app._render_frame()
    assert "running" in _frame_text(terminal)

    app._run_local_control("theme", "voidcode-light")
    app._render_frame()
    assert "voidcode-light" in _frame_text(terminal)


def test_spinner_advances_while_running_static_when_idle() -> None:
    terminal = _StubTerminal()
    app = CoreTuiApp(_StubClient(), session_id="s1", terminal=terminal)  # type: ignore[arg-type]
    app.status_line = ""

    app._render_frame()
    idle_first = list(terminal.frames[-1])
    app._render_frame()
    assert terminal.frames[-1] == idle_first

    app._run_active = True
    app._render_frame()
    running_first = list(terminal.frames[-1])
    app._render_frame()
    assert terminal.frames[-1] != running_first


def test_notice_appends_to_segments() -> None:
    terminal = _StubTerminal()
    app = CoreTuiApp(_StubClient(), session_id="s1", terminal=terminal)  # type: ignore[arg-type]
    app.status_line = "Dispatching /help..."
    app._render_frame()
    text = _frame_text(terminal)
    assert "Dispatching /help..." in text
    assert "idle" in text
    assert app.theme.name in text
