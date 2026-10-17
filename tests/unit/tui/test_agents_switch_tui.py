from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

from voidcode.runtime.contracts import (
    AgentSessionRow,
    AgentSummary,
    AgentTaskRow,
    SessionAgentsOverview,
)
from voidcode.runtime.session import SessionRef, StoredSessionSummary
from voidcode.tui.app import TuiApp
from voidcode.tui.keys import Key
from voidcode.tui.overlay import OverlayOutcomeKind, SessionPickerOverlay
from voidcode.tui.term import Terminal
from voidcode.tui.transcript import NoticeBlock, SessionMarkerBlock


class _AgentsSwitchMockRuntime:
    def __init__(self) -> None:
        self.sessions: list[StoredSessionSummary] = [
            StoredSessionSummary(
                session=SessionRef(id="session-parent-1"),
                status="idle",
                turn=3,
                prompt="parent prompt",
                updated_at=1000,
                title="Parent Session",
            ),
            StoredSessionSummary(
                session=SessionRef(id="session-child-2", parent_id="session-parent-1"),
                status="completed",
                turn=2,
                prompt="subtask prompt",
                updated_at=2000,
                title="Child Worker",
            ),
            StoredSessionSummary(
                session=SessionRef(id="session-other-3"),
                status="idle",
                turn=1,
                prompt="other prompt",
                updated_at=3000,
                title="Other Session",
            ),
        ]
        self.resumed_streams: list[str] = []
        self.agents_calls: list[str] = []

    def list_sessions(self) -> list[StoredSessionSummary]:
        return list(self.sessions)

    def session_forest(self) -> tuple[()]:
        return ()

    def run_config(self, session_id: str) -> SimpleNamespace:
        return SimpleNamespace(approval_mode="ask")

    def resume_stream(self, session_id: str):
        self.resumed_streams.append(session_id)
        return iter(())

    def session_agents_overview(self, *, session_id: str) -> SessionAgentsOverview:
        self.agents_calls.append(session_id)
        rows = tuple(
            AgentSessionRow(
                session_id=s.session.id,
                title=s.title,
                status=s.status,
                turn=s.turn,
                kind="delegation" if s.session.parent_id else "main",
            )
            for s in self.sessions
        )
        tasks = (
            AgentTaskRow(
                task_id="task-child-2",
                status="completed",
                child_session_id="session-child-2",
            ),
        )
        presets = (AgentSummary(id="default", label="Default"),)
        return SessionAgentsOverview(
            session_id=session_id,
            current_session_id=session_id,
            sessions=rows,
            tasks=tasks,
            presets=presets,
        )

    def current_lsp_state(self) -> SimpleNamespace:
        return SimpleNamespace(mode="disabled", servers={})

    def provider_readiness(self, *, session_id: str | None) -> SimpleNamespace:
        return SimpleNamespace(context_window=None)

    def cancel_session(self, session_id: str, reason: str = "") -> SimpleNamespace:
        return SimpleNamespace(interrupted=True)

    def __exit__(self, *_args: object) -> None:
        return None


def _last_notice(app: TuiApp) -> str:
    notices = [b.text for b in app._view.transcript().blocks if isinstance(b, NoticeBlock)]
    return notices[-1] if notices else ""


def _make_app(tmp_path: Path, runtime: _AgentsSwitchMockRuntime) -> tuple[TuiApp, Terminal, object]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    app = TuiApp(workspace, runtime=runtime)
    output = (tmp_path / "terminal.log").open("wb", buffering=0)
    terminal = Terminal(output_fd=output.fileno(), is_tty=False, size=(80, 24), color_system=None)
    app._term = terminal
    app._setup(terminal)
    return app, terminal, output


def _drain_until(app: TuiApp, predicate, limit: int = 200) -> None:
    for _ in range(limit):
        app._drain_queue()
        if predicate():
            return
        time.sleep(0.01)


def test_agents_overview_opens_picker_and_transcript_notice(tmp_path: Path) -> None:
    runtime = _AgentsSwitchMockRuntime()
    app, terminal, output = _make_app(tmp_path, runtime)
    try:
        app._session_id = "session-parent-1"
        app._submit("/agents")
        _drain_until(app, lambda: len(runtime.agents_calls) > 0)
        app._drain_queue()

        # Transcript notice block is populated
        notice = _last_notice(app)
        assert "Agents" in notice
        assert "Sessions:" in notice
        assert "session-parent-1" in notice
        assert "(current)" in notice
        assert "session-child-2" in notice
        assert "task-child-2: completed → session-child-2" in notice

        # SessionPickerOverlay opened with switch action
        assert isinstance(app._overlay, SessionPickerOverlay)
        assert app._overlay._title == "Active Agents"
        assert app._overlay._action == "switch"
        assert len(app._overlay._sessions) == 3
        # Match expected row contents
        session_ids = [s[0] for s in app._overlay._sessions]
        assert session_ids == ["session-parent-1", "session-child-2", "session-other-3"]
    finally:
        app._shutdown(terminal)
        terminal.close()
        output.close()


def test_agents_switch_reuses_resume_mechanics(tmp_path: Path) -> None:
    runtime = _AgentsSwitchMockRuntime()
    app, terminal, output = _make_app(tmp_path, runtime)
    try:
        app._session_id = "session-parent-1"
        app._session_name = "Parent Session"
        app._submit("/agents")
        _drain_until(app, lambda: len(runtime.agents_calls) > 0)
        app._drain_queue()

        assert isinstance(app._overlay, SessionPickerOverlay)
        # Select other session (down, down, enter)
        app._handle_overlay_key(Key("down"))
        app._handle_overlay_key(Key("down"))
        app._handle_overlay_key(Key("enter"))

        # Swapped to session-other-3 reusing _open_resumed_session
        assert app._overlay is None
        assert app._session_id == "session-other-3"
        assert app._session_name == "Other Session"
        assert runtime.resumed_streams == ["session-other-3"]

        markers = [b for b in app._view.transcript().blocks if isinstance(b, SessionMarkerBlock)]
        assert any("Resumed Session" in m.label for m in markers)
    finally:
        app._shutdown(terminal)
        terminal.close()
        output.close()


def test_agents_switch_to_child_delegation_session(tmp_path: Path) -> None:
    runtime = _AgentsSwitchMockRuntime()
    app, terminal, output = _make_app(tmp_path, runtime)
    try:
        app._session_id = "session-parent-1"
        app._session_name = "Parent Session"
        app._submit("/agents")
        _drain_until(app, lambda: len(runtime.agents_calls) > 0)
        app._drain_queue()

        assert isinstance(app._overlay, SessionPickerOverlay)
        # Move down to session-child-2 (child delegation session)
        app._handle_overlay_key(Key("down"))
        app._handle_overlay_key(Key("enter"))

        # Swapped to child delegation session
        assert app._overlay is None
        assert app._session_id == "session-child-2"
        assert app._session_name == "Child Worker"
        assert runtime.resumed_streams == ["session-child-2"]
    finally:
        app._shutdown(terminal)
        terminal.close()
        output.close()


def test_agents_switch_current_session_is_noop(tmp_path: Path) -> None:
    runtime = _AgentsSwitchMockRuntime()
    app, terminal, output = _make_app(tmp_path, runtime)
    try:
        app._session_id = "session-parent-1"
        app._submit("/agents")
        _drain_until(app, lambda: len(runtime.agents_calls) > 0)
        app._drain_queue()

        assert isinstance(app._overlay, SessionPickerOverlay)
        # First row is session-parent-1 (current session); press enter
        app._handle_overlay_key(Key("enter"))

        assert app._overlay is None
        assert app._session_id == "session-parent-1"
        assert _last_notice(app) == "This session is already active."
        # resume_stream was not triggered again
        assert runtime.resumed_streams == []
    finally:
        app._shutdown(terminal)
        terminal.close()
        output.close()


def test_agents_switch_busy_rule_rejects_mid_stream(tmp_path: Path) -> None:
    runtime = _AgentsSwitchMockRuntime()
    app, terminal, output = _make_app(tmp_path, runtime)
    try:
        app._session_id = "session-parent-1"
        # Simulate active stream running on the current session
        app._streaming = True

        # /agents inspection is NOT gated by busy stream (upstream parity)
        app._submit("/agents")
        _drain_until(app, lambda: len(runtime.agents_calls) > 0)
        app._drain_queue()

        # Overview opened properly despite busy stream
        assert isinstance(app._overlay, SessionPickerOverlay)

        # Attempt to switch to another session while streaming
        app._handle_overlay_key(Key("down"))
        app._handle_overlay_key(Key("down"))
        app._handle_overlay_key(Key("enter"))

        # Switch is rejected: in-flight stream is not corrupted
        assert app._overlay is None
        assert app._session_id == "session-parent-1"
        assert _last_notice(app) == "Session switch is disabled while a task is in progress."
        # No session swap or resume_stream occurred
        assert runtime.resumed_streams == []
    finally:
        app._streaming = False
        app._shutdown(terminal)
        terminal.close()
        output.close()


def test_agents_picker_escape_cancels(tmp_path: Path) -> None:
    runtime = _AgentsSwitchMockRuntime()
    app, terminal, output = _make_app(tmp_path, runtime)
    try:
        app._session_id = "session-parent-1"
        app._submit("/agents")
        _drain_until(app, lambda: len(runtime.agents_calls) > 0)
        app._drain_queue()

        assert isinstance(app._overlay, SessionPickerOverlay)
        outcome = app._overlay.handle_key(Key("escape"))
        assert outcome.kind is OverlayOutcomeKind.CANCELLED
        app._close_overlay()
        assert app._overlay is None
        assert app._session_id == "session-parent-1"
        assert runtime.resumed_streams == []
    finally:
        app._shutdown(terminal)
        terminal.close()
        output.close()
