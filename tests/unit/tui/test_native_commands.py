from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

from voidcode.runtime.contracts import ReviewFileDiff
from voidcode.tui.app import TuiApp
from voidcode.tui.term import Terminal
from voidcode.tui.transcript import DiffBlock, NoticeBlock


class _NativeRuntime:
    def __init__(self) -> None:
        self.compact_calls: list[str] = []
        self.diff_calls = 0
        self.compact_started = threading.Event()
        self.compact_release = threading.Event()
        self.compact_finished = threading.Event()

        self.shell_calls: list[tuple[str | None, str]] = []
        self.shell_started = threading.Event()
        self.shell_release = threading.Event()
        self.shell_finished = threading.Event()

        self.btw_calls: list[tuple[str, str]] = []
        self.btw_release = threading.Event()
        self.btw_release.set()

    def run_user_shell(self, *, session_id: str | None, command: str) -> dict[str, object]:
        self.shell_calls.append((session_id, command))
        self.shell_started.set()
        assert self.shell_release.wait(5)
        self.shell_finished.set()
        resolved = session_id or "shell-1"
        return {
            "session_id": resolved,
            "command": command,
            "exit_code": 0,
            "status": "completed",
            "duration_seconds": 0.01,
            "output": "ok\n",
            "truncated": False,
            "timed_out": False,
            "event_sequence": 1,
        }

    def current_lsp_state(self) -> SimpleNamespace:
        return SimpleNamespace(mode="disabled", servers={})

    def provider_readiness(self, *, session_id: str | None) -> SimpleNamespace:
        return SimpleNamespace(context_window=None)

    def review_diff_all(self) -> ReviewFileDiff:
        self.diff_calls += 1
        return ReviewFileDiff(root="/workspace", path=".", state="changed", diff="@@ -1 +1 @@\n-old\n+new\n")

    def compact_session(self, *, session_id: str) -> dict[str, object]:
        self.compact_calls.append(session_id)
        self.compact_started.set()
        assert self.compact_release.wait(5)
        self.compact_finished.set()
        return {"session_id": session_id, "compacted": True}

    def ask_side_question(self, *, session_id: str, question: str) -> dict[str, object]:
        self.btw_calls.append((session_id, question))
        assert self.btw_release.wait(5)
        return {"session_id": session_id, "question": question, "answer": f"answer: {question}"}

    def __exit__(self, *_args: object) -> None:
        return None


def _app(tmp_path: Path, runtime: _NativeRuntime) -> tuple[TuiApp, Terminal, object]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    app = TuiApp(workspace, runtime=runtime)
    output = (tmp_path / "terminal.log").open("wb", buffering=0)
    terminal = Terminal(output_fd=output.fileno(), is_tty=False, size=(80, 24), color_system=None)
    app._term = terminal
    app._setup(terminal)
    return app, terminal, output


def test_native_diff_uses_review_runtime_and_does_not_submit_a_turn(tmp_path: Path) -> None:
    runtime = _NativeRuntime()
    app, terminal, output = _app(tmp_path, runtime)
    try:
        app._submit("/diff")

        assert runtime.diff_calls == 1
        assert any(isinstance(block, DiffBlock) for block in app._view.transcript().blocks)
        assert app._session_id is None
    finally:
        app._shutdown(terminal)
        terminal.close()
        output.close()


def test_native_compact_runs_off_loop_and_gates_prompts(tmp_path: Path) -> None:
    runtime = _NativeRuntime()
    app, terminal, output = _app(tmp_path, runtime)
    submitted: list[str] = []
    app._start_prompt = submitted.append
    try:
        app._session_id = "session-1"
        app._submit("/compact")

        assert runtime.compact_started.wait(2)
        assert app._compact_pending
        app._submit("message while compacting")
        assert submitted == []
        runtime.compact_release.set()
        assert runtime.compact_finished.wait(2)
        assert app._compact_thread is not None
        app._compact_thread.join(2)
        app._drain_queue()

        assert runtime.compact_calls == ["session-1"]
        assert not app._compact_pending
        assert any(isinstance(block, NoticeBlock) for block in app._view.transcript().blocks)
    finally:
        runtime.compact_release.set()
        app._shutdown(terminal)
        terminal.close()
        output.close()


def test_native_command_arguments_never_fall_through_to_provider(tmp_path: Path) -> None:
    runtime = _NativeRuntime()
    app, terminal, output = _app(tmp_path, runtime)
    submitted: list[str] = []
    app._start_prompt = submitted.append
    try:
        app._submit("/diff please review this")
        app._submit("/compact now")

        assert submitted == []
        assert runtime.diff_calls == 0
        assert runtime.compact_calls == []
    finally:
        app._shutdown(terminal)
        terminal.close()
        output.close()


def test_shell_escape_dispatches_before_slash_routing(tmp_path: Path) -> None:
    runtime = _NativeRuntime()
    runtime.shell_release.set()
    app, terminal, output = _app(tmp_path, runtime)
    submitted: list[str] = []
    app._start_prompt = submitted.append
    try:
        app._session_id = "session-1"
        app._submit("!echo hi")

        assert runtime.shell_started.wait(2)
        assert runtime.shell_finished.wait(2)
        app._drain_queue()

        assert runtime.shell_calls == [("session-1", "echo hi")]
        assert submitted == []
        assert any(isinstance(block, NoticeBlock) is False for block in app._view.transcript().blocks)
    finally:
        app._shutdown(terminal)
        terminal.close()
        output.close()


def test_shell_escape_bang_bang_passes_through_verbatim(tmp_path: Path) -> None:
    runtime = _NativeRuntime()
    runtime.shell_release.set()
    app, terminal, output = _app(tmp_path, runtime)
    try:
        app._session_id = "session-1"
        app._submit("!!history")

        assert runtime.shell_started.wait(2)
        assert runtime.shell_finished.wait(2)

        assert runtime.shell_calls == [("session-1", "!history")]
    finally:
        app._shutdown(terminal)
        terminal.close()
        output.close()


def test_shell_escape_bare_bang_shows_help_without_executing(tmp_path: Path) -> None:
    runtime = _NativeRuntime()
    app, terminal, output = _app(tmp_path, runtime)
    submitted: list[str] = []
    app._start_prompt = submitted.append
    try:
        app._submit("!")
        app._submit("!   ")

        assert runtime.shell_calls == []
        assert submitted == []
        notices = [block for block in app._view.transcript().blocks if isinstance(block, NoticeBlock)]
        assert len(notices) == 2
        assert "Prefix a command with !" in notices[0].text
    finally:
        app._shutdown(terminal)
        terminal.close()
        output.close()


def test_shell_escape_works_while_streaming_and_mid_native(tmp_path: Path) -> None:
    runtime = _NativeRuntime()
    runtime.shell_release.set()
    app, terminal, output = _app(tmp_path, runtime)
    steered: list[str] = []
    app._steer = steered.append
    try:
        app._session_id = "session-1"
        app._streaming = True
        app._compact_pending = True
        app._submit("!echo mid-turn")

        assert runtime.shell_started.wait(2)
        assert runtime.shell_finished.wait(2)

        assert runtime.shell_calls == [("session-1", "echo mid-turn")]
        assert steered == []
    finally:
        app._shutdown(terminal)
        terminal.close()
        output.close()


def test_shell_escape_result_renders_user_and_tool_blocks(tmp_path: Path) -> None:
    from voidcode.tui.transcript import ToolBlock, UserBlock

    runtime = _NativeRuntime()
    runtime.shell_release.set()
    app, terminal, output = _app(tmp_path, runtime)
    try:
        app._session_id = "session-1"
        app._submit("!echo hi")

        assert runtime.shell_finished.wait(2)
        app._drain_queue()

        blocks = app._view.transcript().blocks
        assert any(isinstance(block, UserBlock) and block.text == "!echo hi" for block in blocks)
        tool_blocks = [block for block in blocks if isinstance(block, ToolBlock)]
        assert len(tool_blocks) == 1
        assert tool_blocks[0].state == "success"
        assert "Exit code: 0" in "\n".join(tool_blocks[0].body)
    finally:
        app._shutdown(terminal)
        terminal.close()
        output.close()


def test_composer_bash_mode_and_esc_exit(tmp_path: Path) -> None:
    from voidcode.tui.composer import Composer, ComposerAction
    from voidcode.tui.keys import Key
    from voidcode.tui.theme import resolve_theme

    runtime = _NativeRuntime()
    app, terminal, output = _app(tmp_path, runtime)
    try:
        theme = resolve_theme(mode="dark", color_system=None)
        composer = Composer(theme=theme, width=80)
        assert composer.is_bash_mode is False
        composer.handle_key(Key("!", text="!"))
        assert composer.is_bash_mode is True
        outcome = composer.handle_key(Key("escape"))
        assert outcome.action is not ComposerAction.CANCEL_TURN
        assert composer.value == ""
        assert composer.is_bash_mode is False
    finally:
        app._shutdown(terminal)
        terminal.close()
        output.close()


def test_btw_opens_overlay_and_appends_nothing_to_transcript(tmp_path: Path) -> None:
    from voidcode.tui.keys import Key
    from voidcode.tui.overlay import OverlayOutcomeKind, SideQuestionOverlay

    runtime = _NativeRuntime()
    app, terminal, output = _app(tmp_path, runtime)
    submitted: list[str] = []
    app._start_prompt = submitted.append
    try:
        app._session_id = "session-1"
        before = len(app._view.transcript().blocks)
        app._submit("/btw what is this?")

        for _ in range(200):
            app._drain_queue()
            if isinstance(app._overlay, SideQuestionOverlay):
                break
            import time

            time.sleep(0.01)
        assert isinstance(app._overlay, SideQuestionOverlay)
        assert runtime.btw_calls == [("session-1", "what is this?")]
        assert submitted == []
        # Nothing appended to the transcript: overlay only.
        assert len(app._view.transcript().blocks) == before
        rows = "\n".join(app._overlay.render(80))
        assert "what is this?" in rows
        assert "answer: what is this?" in rows
        # Scroll + dismiss semantics.
        assert app._overlay.handle_key(Key("down")).kind is OverlayOutcomeKind.PENDING
        assert app._overlay.handle_key(Key("space", " ")).kind is OverlayOutcomeKind.CANCELLED
        assert SideQuestionOverlay(question="q", answer="a").handle_key(Key("enter")).kind is OverlayOutcomeKind.CANCELLED
        assert SideQuestionOverlay(question="q", answer="a").handle_key(Key("escape")).kind is OverlayOutcomeKind.CANCELLED
    finally:
        app._shutdown(terminal)
        terminal.close()
        output.close()


def test_btw_bare_reshows_recent_or_usage(tmp_path: Path) -> None:
    from voidcode.tui.overlay import SideQuestionOverlay

    runtime = _NativeRuntime()
    app, terminal, output = _app(tmp_path, runtime)
    try:
        app._session_id = "session-1"
        app._submit("/btw")
        notices = [block for block in app._view.transcript().blocks if isinstance(block, NoticeBlock)]
        assert notices and "Usage: /btw <your question>" in notices[-1].text
        assert runtime.btw_calls == []

        app._submit("/btw first question")
        for _ in range(200):
            app._drain_queue()
            if isinstance(app._overlay, SideQuestionOverlay):
                break
            import time

            time.sleep(0.01)
        assert isinstance(app._overlay, SideQuestionOverlay)
        app._close_overlay()

        app._submit("/btw")
        assert isinstance(app._overlay, SideQuestionOverlay)
        assert "first question" in "\n".join(app._overlay.render(80))
        assert len(runtime.btw_calls) == 1
    finally:
        app._shutdown(terminal)
        terminal.close()
        output.close()


def test_btw_runs_while_streaming(tmp_path: Path) -> None:
    from voidcode.tui.overlay import SideQuestionOverlay

    runtime = _NativeRuntime()
    app, terminal, output = _app(tmp_path, runtime)
    steered: list[str] = []
    app._steer = steered.append
    try:
        app._session_id = "session-1"
        app._streaming = True
        app._compact_pending = True
        app._submit("/btw mid-turn question")

        for _ in range(200):
            app._drain_queue()
            if isinstance(app._overlay, SideQuestionOverlay):
                break
            import time

            time.sleep(0.01)
        assert isinstance(app._overlay, SideQuestionOverlay)
        assert runtime.btw_calls == [("session-1", "mid-turn question")]
        assert steered == []
    finally:
        app._shutdown(terminal)
        terminal.close()
        output.close()
