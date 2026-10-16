from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from voidcode.runtime.session import SessionRef, StoredSessionSummary
from voidcode.tui.app import TuiApp
from voidcode.tui.keys import Key
from voidcode.tui.overlay import RenameSessionOverlay, SessionPickerOverlay
from voidcode.tui.term import Terminal
from voidcode.tui.transcript import NoticeBlock, SessionMarkerBlock


class _TuiMockRuntime:
    def __init__(self) -> None:
        self.sessions: list[StoredSessionSummary] = [
            StoredSessionSummary(
                session=SessionRef(id="session-alpha123"),
                status="completed",
                turn=1,
                prompt="alpha prompt",
                updated_at=1000,
                title="Alpha Session",
            ),
            StoredSessionSummary(
                session=SessionRef(id="session-beta456"),
                status="completed",
                turn=1,
                prompt="beta prompt",
                updated_at=2000,
                title="Beta Project",
            ),
        ]
        self.renamed: list[tuple[str, str]] = []
        self.forked: list[str] = []

    def list_sessions(self) -> list[StoredSessionSummary]:
        return list(self.sessions)

    def session_forest(self) -> tuple[()]:
        return ()

    def cancel_session(self, session_id: str, reason: str = "") -> SimpleNamespace:
        return SimpleNamespace(interrupted=True)

    def run_config(self, session_id: str) -> SimpleNamespace:
        return SimpleNamespace(approval_mode="ask")

    def resume_stream(self, session_id: str):
        return iter(())

    def fork_session(self, session_id: str) -> StoredSessionSummary:
        self.forked.append(session_id)
        forked_summary = StoredSessionSummary(
            session=SessionRef(id=f"{session_id}-fork"),
            status="completed",
            turn=1,
            prompt="forked",
            updated_at=3000,
            title="Fork Title",
        )
        self.sessions.append(forked_summary)
        return forked_summary

    def rename_session(self, session_id: str, title: str) -> StoredSessionSummary:
        self.renamed.append((session_id, title))
        for s in self.sessions:
            if s.session.id == session_id:
                updated = StoredSessionSummary(
                    session=s.session,
                    status=s.status,
                    turn=s.turn,
                    prompt=s.prompt,
                    updated_at=4000,
                    title=title,
                )
                return updated
        return StoredSessionSummary(
            session=SessionRef(id=session_id),
            status="completed",
            turn=1,
            prompt="",
            updated_at=4000,
            title=title,
        )

    def provider_readiness(self, *, session_id: str | None) -> SimpleNamespace:
        return SimpleNamespace(context_window=None)

    def current_lsp_state(self) -> SimpleNamespace:
        return SimpleNamespace(mode="disabled", servers={})

    def __exit__(self, *_args: object) -> None:
        return None


def _last_notice(app: TuiApp) -> str:
    notices = [b.text for b in app._view.transcript().blocks if isinstance(b, NoticeBlock)]
    return notices[-1] if notices else ""


def _make_app(tmp_path: Path, runtime: _TuiMockRuntime) -> tuple[TuiApp, Terminal, object]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    app = TuiApp(workspace, runtime=runtime)
    output = (tmp_path / "terminal.log").open("wb", buffering=0)
    terminal = Terminal(output_fd=output.fileno(), is_tty=False, size=(80, 24), color_system=None)
    app._term = terminal
    app._setup(terminal)
    return app, terminal, output


def test_tui_submit_new_command(tmp_path: Path) -> None:
    runtime = _TuiMockRuntime()
    app, terminal, output = _make_app(tmp_path, runtime)
    try:
        app._session_id = "session-old"
        app._submit("/new My Fresh Task")

        assert app._session_id is None
        assert app._session_name == "My Fresh Task"
        markers = [b for b in app._view.transcript().blocks if isinstance(b, SessionMarkerBlock)]
        assert any("My Fresh Task" in m.label for m in markers)
    finally:
        app._shutdown(terminal)
        terminal.close()
        output.close()


def test_tui_submit_clear_command(tmp_path: Path) -> None:
    runtime = _TuiMockRuntime()
    app, terminal, output = _make_app(tmp_path, runtime)
    try:
        app._session_id = "session-old"
        app._submit("/clear Cleaned Task")

        assert app._session_id is None
        assert app._session_name == "Cleaned Task"
        markers = [b for b in app._view.transcript().blocks if isinstance(b, SessionMarkerBlock)]
        assert len(markers) == 1
        assert "Cleaned Task" in markers[0].label
    finally:
        app._shutdown(terminal)
        terminal.close()
        output.close()


def test_tui_submit_resume_command_bare_and_match(tmp_path: Path) -> None:
    runtime = _TuiMockRuntime()
    app, terminal, output = _make_app(tmp_path, runtime)
    try:
        # Bare /resume opens picker overlay
        app._submit("/resume")
        assert isinstance(app._overlay, SessionPickerOverlay)
        app._close_overlay()

        # /resume with matching title
        app._submit("/resume Alpha Session")
        assert app._session_id == "session-alpha123"
        app._streaming = False

        # /resume with matching id
        app._submit("/resume session-beta456")
        assert app._session_id == "session-beta456"
        app._streaming = False

        # /resume miss
        app._submit("/resume non-existent-chat")
        assert _last_notice(app) == "No saved chat found matching 'non-existent-chat'."
    finally:
        app._shutdown(terminal)
        terminal.close()
        output.close()


def test_tui_submit_fork_command(tmp_path: Path) -> None:
    runtime = _TuiMockRuntime()
    app, terminal, output = _make_app(tmp_path, runtime)
    try:
        app._session_id = "session-alpha123"
        app._submit("/fork Forked Alpha")

        assert runtime.forked == ["session-alpha123"]
        assert app._session_id == "session-alpha123-fork"
    finally:
        app._shutdown(terminal)
        terminal.close()
        output.close()


def test_tui_submit_rename_command_bare_and_arg(tmp_path: Path) -> None:
    runtime = _TuiMockRuntime()
    app, terminal, output = _make_app(tmp_path, runtime)
    try:
        app._session_id = "session-alpha123"

        # Bare /rename opens overlay; pressing enter on empty input emits error notice
        app._submit("/rename")
        assert isinstance(app._overlay, RenameSessionOverlay)
        app._handle_overlay_key(Key(name="enter"))
        assert _last_notice(app) == "Thread name cannot be empty."

        # /rename with valid argument
        app._submit("/rename Brand New Name")
        assert runtime.renamed == [("session-alpha123", "Brand New Name")]
        assert app._session_name == "Brand New Name"
    finally:
        app._shutdown(terminal)
        terminal.close()
        output.close()


def test_tui_busy_gate_matrix(tmp_path: Path) -> None:
    runtime = _TuiMockRuntime()
    app, terminal, output = _make_app(tmp_path, runtime)
    try:
        app._session_id = "session-alpha123"
        app._streaming = True

        # /new blocked
        app._submit("/new")
        assert _last_notice(app) == "'/new' is disabled while a task is in progress."

        # /clear blocked
        app._submit("/clear")
        assert _last_notice(app) == "'/clear' is disabled while a task is in progress."

        # /resume blocked
        app._submit("/resume")
        assert _last_notice(app) == "'/resume' is disabled while a task is in progress."

        # /fork blocked
        app._submit("/fork")
        assert _last_notice(app) == "'/fork' is disabled while a task is in progress."

        # /rename ALLOWED during task
        app._submit("/rename Allowed Rename")
        assert runtime.renamed == [("session-alpha123", "Allowed Rename")]
        assert app._session_name == "Allowed Rename"
    finally:
        app._shutdown(terminal)
        terminal.close()
        output.close()
