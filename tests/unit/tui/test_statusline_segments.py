"""Footer context row: workspace · model · effort, plus a separate notice row."""

from __future__ import annotations

import re
from typing import Any

from voidcode.sdk.client import ClientCapabilities, CommandCatalog, HealthModel, HealthStatus
from voidcode.tui.terminal_app import CoreTuiApp


class _StubTerminal:
    width = 100
    content_width = 100
    height = 24
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
    capabilities = ClientCapabilities()

    def command_catalog(self) -> CommandCatalog:
        return CommandCatalog(revision_id="rev-1", commands=())


def _health(workspace: str = "/srv/ws", model: HealthModel | None = None) -> HealthStatus:
    return HealthStatus(
        status="ok",
        stage="serving",
        pid=1,
        workspace=workspace,
        revision_id="rev-1",
        launch_protocol=1,
        core_version="test",
        web_bundle=None,
        generation=1,
        active_upgrade=None,
        model=model,
    )


def _model(**fields: Any) -> HealthModel:
    base: dict[str, Any] = {
        "raw": None,
        "provider": None,
        "model": None,
        "reasoning_effort": None,
        "reasoning_levels": (),
        "context_window": None,
    }
    base.update(fields)
    return HealthModel(**base)


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


def test_footer_context_window_is_typed_and_unknown_stays_honest() -> None:
    terminal = _StubTerminal()
    app = CoreTuiApp(_StubClient(), session_id="s1", terminal=terminal)  # type: ignore[arg-type]
    app.upgrade = _health(model=_model(raw="openai/gpt-5.2", provider="openai", model="gpt-5.2", reasoning_effort="high", context_window=200000))
    app.latest_input_tokens = 5200
    app.status_line = ""
    app._render_frame()
    assert "5.2k/200k" in _frame_text(terminal)

    app.upgrade = _health(model=_model(raw="openai/gpt-5.2", provider="openai", model="gpt-5.2", reasoning_effort="high"))
    app._render_frame()
    text = _frame_text(terminal)
    assert "5.2k ctx" in text
    assert "5.2k/" not in text


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


def test_default_composer_is_compact_and_has_no_prompt_or_placeholder() -> None:
    terminal = _StubTerminal()
    app = CoreTuiApp(_StubClient(), session_id="s1", terminal=terminal)  # type: ignore[arg-type]
    app.status_line = ""
    app._render_frame()
    rows = terminal.frames[-1]
    assert len(rows) == 4
    assert rows[0] == rows[2] == "─" * terminal.width
    assert rows[1].strip() == ""
    assert app.composer.cursor_position == (1, 0)

    app.composer.set_text("hello\nworld")
    app._render_frame()
    assert terminal.frames[-1][1].rstrip() == "hello"
    assert terminal.frames[-1][2].rstrip() == "world"
    assert len(terminal.frames[-1]) == 5


def test_borderless_composer_has_no_vertical_padding() -> None:
    terminal = _StubTerminal()
    app = CoreTuiApp(_StubClient(), terminal=terminal, border="borderless", footer="off")  # type: ignore[arg-type]
    app.status_line = ""
    app._render_frame()
    assert len(terminal.frames[-1]) == 1
    assert terminal.frames[-1][0].strip() == ""
    assert app.composer.cursor_position == (0, 0)


def test_footer_uses_terminal_default_foreground_at_standard_color_depth() -> None:
    from rich.color import ColorSystem

    from voidcode.tui.styles import Styles

    terminal = _StubTerminal()
    app = CoreTuiApp(_StubClient(), session_id="s1", terminal=terminal)  # type: ignore[arg-type]
    app.styles = Styles(ColorSystem.STANDARD)
    app.upgrade = _health(model=_model(provider="local", model="test-model"))
    app._render_frame()
    row = next(row for row in terminal.frames[-1] if "local/test-model" in row)
    assert "\x1b[39m" in row
    assert "\x1b[90m" not in row


def test_footer_styles_context_warning_and_error_thresholds() -> None:
    from rich.color import ColorSystem

    from voidcode.tui.rendering import footer_context
    from voidcode.tui.styles import Styles

    terminal = _StubTerminal()
    app = CoreTuiApp(_StubClient(), session_id="s1", terminal=terminal)  # type: ignore[arg-type]
    app.styles = Styles(ColorSystem.STANDARD)
    app.upgrade = _health(model=_model(provider="local", model="test-model", context_window=100_000))

    app.latest_input_tokens = 50_000
    low = footer_context(app, 120)
    assert "\x1b[33m" not in low
    assert "\x1b[91m" not in low

    app.latest_input_tokens = 80_000
    warn = footer_context(app, 120)
    assert "\x1b[33m80.0k/100k\x1b[39m" in warn

    app.latest_input_tokens = 95_000
    crit = footer_context(app, 120)
    assert "\x1b[91m95.0k/100k\x1b[39m" in crit


def test_border_status_includes_elapsed_time_and_parallel_tool_names() -> None:
    import time

    from voidcode.tui.rendering import ActiveToolProgress, border_status

    terminal = _StubTerminal()
    app = CoreTuiApp(_StubClient(), session_id="s1", terminal=terminal)  # type: ignore[arg-type]
    app._mark_run_active()
    app._run_started_at = time.monotonic() - 65.2
    working = border_status(app)
    assert "working" in working
    assert "1m05s" in working

    for idx, name in enumerate(("read", "grep", "find", "edit")):
        app._active_tools[f"inv-{idx}"] = ActiveToolProgress(
            invocation_id=f"inv-{idx}",
            tool_name=name,
            icon=None,
            start_time=time.monotonic() - 2.0,
            session_id="s1",
            run_id="r1",
            revision_id="rev-1",
            generation=0,
        )
    tools_status = border_status(app)
    assert "running read, grep, find +1" in tools_status
    assert "1m05s" in tools_status
