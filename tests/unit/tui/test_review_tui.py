from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from voidcode.runtime.contracts import ReviewCommitLogEntry, ReviewTarget
from voidcode.tui.app import TuiApp
from voidcode.tui.keys import Key
from voidcode.tui.overlay import (
    ReviewBranchPickerOverlay,
    ReviewCommitPickerOverlay,
    ReviewCustomPromptOverlay,
    ReviewPresetPickerOverlay,
)
from voidcode.tui.term import Terminal


class _ReviewTuiRuntime:
    def __init__(self) -> None:
        self.review_calls: list[ReviewTarget] = []

    def current_lsp_state(self) -> SimpleNamespace:
        return SimpleNamespace(mode="disabled", servers={})

    def provider_readiness(self, *, session_id: str | None) -> SimpleNamespace:
        return SimpleNamespace(context_window=None)

    def review_branches(self) -> tuple[str, ...]:
        return ("main", "feature-x")

    def review_current_branch(self) -> str | None:
        return "main"

    def review_recent_commits(self, limit: int = 100) -> tuple[ReviewCommitLogEntry, ...]:
        return (
            ReviewCommitLogEntry(sha="abcdef1234567890", timestamp=1700000000, subject="Initial commit"),
            ReviewCommitLogEntry(sha="123456abcdef7890", timestamp=1700000100, subject="Add feature"),
        )

    def review_session(self, *, session_id: str, focus: str | None = None, target: ReviewTarget | None = None) -> SimpleNamespace:
        if target is not None:
            self.review_calls.append(target)
        elif focus is not None:
            self.review_calls.append(ReviewTarget(type="custom", instructions=focus))
        else:
            self.review_calls.append(ReviewTarget(type="uncommitted"))
        return SimpleNamespace(output="Review done")

    def __exit__(self, *_args: object) -> None:
        return None


def _setup_app(tmp_path: Path, runtime: _ReviewTuiRuntime) -> tuple[TuiApp, Terminal, object]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    app = TuiApp(workspace, runtime=runtime)
    output = (tmp_path / "terminal.log").open("wb", buffering=0)
    terminal = Terminal(output_fd=output.fileno(), is_tty=False, size=(80, 24), color_system=None)
    app._term = terminal
    app._setup(terminal)
    app._session_id = "session-1"
    return app, terminal, output


def test_tui_bare_review_opens_preset_picker_with_4_items(tmp_path: Path) -> None:
    runtime = _ReviewTuiRuntime()
    app, terminal, output = _setup_app(tmp_path, runtime)
    try:
        app._command_native_review("")
        assert isinstance(app._overlay, ReviewPresetPickerOverlay)
        assert len(app._overlay._options) == 4
        assert app._overlay._options[0][0] == "base_branch"
        assert app._overlay._options[1][0] == "uncommitted"
        assert app._overlay._options[2][0] == "commit"
        assert app._overlay._options[3][0] == "custom"
    finally:
        app._shutdown(terminal)
        terminal.close()
        output.close()


def test_tui_inline_review_bypasses_picker_to_custom(tmp_path: Path) -> None:
    runtime = _ReviewTuiRuntime()
    app, terminal, output = _setup_app(tmp_path, runtime)
    try:
        app._command_native_review("check memory safety")
        assert app._overlay is None
        assert app._review_thread is not None
        app._review_thread.join(2)
        assert len(runtime.review_calls) == 1
        call = runtime.review_calls[0]
        assert call.type == "custom"
        assert call.instructions == "check memory safety"
    finally:
        app._shutdown(terminal)
        terminal.close()
        output.close()


def test_tui_preset_picker_uncommitted_flow(tmp_path: Path) -> None:
    runtime = _ReviewTuiRuntime()
    app, terminal, output = _setup_app(tmp_path, runtime)
    try:
        app._command_native_review("")
        assert isinstance(app._overlay, ReviewPresetPickerOverlay)
        # Move down to 'uncommitted' (index 1) and press enter
        app._handle_overlay_key(Key("down"))
        app._handle_overlay_key(Key("enter"))
        assert app._overlay is None
        assert app._review_thread is not None
        app._review_thread.join(2)
        assert len(runtime.review_calls) == 1
        assert runtime.review_calls[0].type == "uncommitted"
    finally:
        app._shutdown(terminal)
        terminal.close()
        output.close()


def test_tui_preset_picker_base_branch_flow(tmp_path: Path) -> None:
    runtime = _ReviewTuiRuntime()
    app, terminal, output = _setup_app(tmp_path, runtime)
    try:
        app._command_native_review("")
        assert isinstance(app._overlay, ReviewPresetPickerOverlay)
        # Select base_branch (index 0)
        app._handle_overlay_key(Key("enter"))
        assert isinstance(app._overlay, ReviewBranchPickerOverlay)
        assert app._overlay._branches == ("main", "feature-x")
        # Select first branch ('main')
        app._handle_overlay_key(Key("enter"))
        assert app._overlay is None
        assert app._review_thread is not None
        app._review_thread.join(2)
        assert len(runtime.review_calls) == 1
        assert runtime.review_calls[0].type == "base_branch"
        assert runtime.review_calls[0].branch == "main"
    finally:
        app._shutdown(terminal)
        terminal.close()
        output.close()


def test_tui_preset_picker_commit_flow(tmp_path: Path) -> None:
    runtime = _ReviewTuiRuntime()
    app, terminal, output = _setup_app(tmp_path, runtime)
    try:
        app._command_native_review("")
        # Move to 'commit' (index 2)
        app._handle_overlay_key(Key("down"))
        app._handle_overlay_key(Key("down"))
        app._handle_overlay_key(Key("enter"))
        assert isinstance(app._overlay, ReviewCommitPickerOverlay)
        assert len(app._overlay._commits) == 2
        # Select first commit
        app._handle_overlay_key(Key("enter"))
        assert app._overlay is None
        assert app._review_thread is not None
        app._review_thread.join(2)
        assert len(runtime.review_calls) == 1
        assert runtime.review_calls[0].type == "commit"
        assert runtime.review_calls[0].sha == "abcdef1234567890"
        assert runtime.review_calls[0].title == "Initial commit"
    finally:
        app._shutdown(terminal)
        terminal.close()
        output.close()


def test_tui_preset_picker_custom_prompt_flow(tmp_path: Path) -> None:
    runtime = _ReviewTuiRuntime()
    app, terminal, output = _setup_app(tmp_path, runtime)
    try:
        app._command_native_review("")
        # Move to 'custom' (index 3)
        app._handle_overlay_key(Key("down"))
        app._handle_overlay_key(Key("down"))
        app._handle_overlay_key(Key("down"))
        app._handle_overlay_key(Key("enter"))
        assert isinstance(app._overlay, ReviewCustomPromptOverlay)
        # Type instructions and press enter
        for char in "audit tests":
            app._handle_overlay_key(Key(char, text=char))
        app._handle_overlay_key(Key("enter"))
        assert app._overlay is None
        assert app._review_thread is not None
        app._review_thread.join(2)
        assert len(runtime.review_calls) == 1
        assert runtime.review_calls[0].type == "custom"
        assert runtime.review_calls[0].instructions == "audit tests"
    finally:
        app._shutdown(terminal)
        terminal.close()
        output.close()
