"""Footer context row: workspace · model · effort, plus a separate notice row."""

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


def _health(workspace: str = "/srv/ws", model: Any = None) -> Any:
    # Backend owns HealthStatus.model; until it lands, a stub carrying the
    # frozen B DTO shape exercises the chrome's getattr-based degrade path.
    from types import SimpleNamespace

    return SimpleNamespace(status="ok", stage="serving", workspace=workspace, fenced=False, model=model)


def _model(**fields: Any) -> Any:
    from types import SimpleNamespace

    base: dict[str, Any] = {"raw": None, "provider": None, "model": None, "reasoning_effort": None, "reasoning_levels": []}
    base.update(fields)
    return SimpleNamespace(**base)


def _frame_text(terminal: _StubTerminal) -> str:
    return "\n".join(re.sub(r"\x1b\[[0-9;]*m", "", row) for row in terminal.frames[-1])


def test_footer_shows_server_workspace_model_effort_no_run_state() -> None:
    terminal = _StubTerminal()
    app = CoreTuiApp(_StubClient(), session_id="session-abcdef", terminal=terminal)  # type: ignore[arg-type]
    app.upgrade = _health(model=_model(raw="openai/gpt-5.2", provider="openai", model="gpt-5.2", reasoning_effort="xhigh"))
    app.status_line = ""

    app._render_frame()
    text = _frame_text(terminal)
    assert "/srv/ws · openai/gpt-5.2 · xhigh" in text
    assert "idle" not in text and "running" not in text
    assert "session-abcdef" not in text and "session-" not in text


def test_hidden_effort_and_no_model_degrade_honestly() -> None:
    terminal = _StubTerminal()
    app = CoreTuiApp(_StubClient(), session_id="s1", terminal=terminal)  # type: ignore[arg-type]
    app.upgrade = _health(model=_model(raw="openai/gpt-5.2", provider="openai", model="gpt-5.2", reasoning_effort=None))
    app.status_line = ""
    app._render_frame()
    assert "/srv/ws · openai/gpt-5.2" in _frame_text(terminal)
    assert "xhigh" not in _frame_text(terminal)

    app.upgrade = _health(model=None)
    app._render_frame()
    assert "no model" in _frame_text(terminal)


def test_narrow_width_drops_workspace_before_model() -> None:
    terminal = _StubTerminal()
    terminal.width = 40
    app = CoreTuiApp(_StubClient(), session_id="s1", terminal=terminal)  # type: ignore[arg-type]
    app.upgrade = _health(
        workspace="/very/long/workspace/path/that/keeps/going",
        model=_model(raw="openai/gpt-5.2", provider="openai", model="gpt-5.2", reasoning_effort="xhigh"),
    )
    app.status_line = ""
    app._render_frame()
    text = _frame_text(terminal)
    assert "openai/gpt-5.2" in text and "xhigh" in text

    terminal.width = 20
    app._render_frame()
    assert "openai/gpt-5.2" in _frame_text(terminal)


def test_compact_footer_is_model_effort_only() -> None:
    terminal = _StubTerminal()
    app = CoreTuiApp(_StubClient(), session_id="s1", terminal=terminal, footer="compact")  # type: ignore[arg-type]
    app.upgrade = _health(model=_model(raw="openai/gpt-5.2", provider="openai", model="gpt-5.2", reasoning_effort="xhigh"))
    app.status_line = ""
    app._render_frame()
    text = _frame_text(terminal)
    assert "openai/gpt-5.2 · xhigh" in text
    assert "/srv/ws" not in text


def test_footer_off_hides_context_but_notice_still_shows() -> None:
    terminal = _StubTerminal()
    app = CoreTuiApp(_StubClient(), session_id="s1", terminal=terminal, footer="off")  # type: ignore[arg-type]
    app.upgrade = _health(model=_model(raw="openai/gpt-5.2", provider="openai", model="gpt-5.2", reasoning_effort="xhigh"))
    app.status_line = "Tool approval required: write"
    app._render_frame()
    text = _frame_text(terminal)
    assert "Tool approval required: write" in text
    assert "/srv/ws" not in text and "openai/gpt-5.2" not in text


def test_border_spinner_animates_while_running_clean_when_idle() -> None:
    terminal = _StubTerminal()
    app = CoreTuiApp(_StubClient(), session_id="s1", terminal=terminal)  # type: ignore[arg-type]
    app.status_line = ""

    app._render_frame()
    idle_first = list(terminal.frames[-1])
    app._render_frame()
    assert terminal.frames[-1] == idle_first
    assert "working" not in _frame_text(terminal)

    app._run_active = True
    app._render_frame()
    running_first = list(terminal.frames[-1])
    app._render_frame()
    assert terminal.frames[-1] != running_first
    assert "working" in _frame_text(terminal)


def test_notice_is_a_separate_row_from_context() -> None:
    terminal = _StubTerminal()
    app = CoreTuiApp(_StubClient(), session_id="s1", terminal=terminal)  # type: ignore[arg-type]
    app.upgrade = _health(model=_model(raw="openai/gpt-5.2", provider="openai", model="gpt-5.2", reasoning_effort="xhigh"))
    app.status_line = "Dispatching /help..."
    app._render_frame()
    text = _frame_text(terminal)
    assert "Dispatching /help..." in text
    assert "/srv/ws · openai/gpt-5.2 · xhigh" in text
    assert "idle" not in text


def test_remote_workspace_is_never_home_shortened() -> None:
    import os

    terminal = _StubTerminal()
    app = CoreTuiApp(_StubClient(), session_id="s1", terminal=terminal)  # type: ignore[arg-type]
    home = os.path.expanduser("~")
    app.upgrade = _health(workspace=f"{home}/remote-ws", model=_model(raw="openai/gpt-5.2", provider="openai", model="gpt-5.2"))
    app.status_line = ""
    app._render_frame()
    # Remote arm (non-local client): the server path stays full, never "~".
    assert f"{home}/remote-ws" in _frame_text(terminal)
