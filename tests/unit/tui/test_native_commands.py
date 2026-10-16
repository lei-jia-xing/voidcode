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
