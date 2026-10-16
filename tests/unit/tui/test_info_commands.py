from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

from voidcode.runtime.contracts import ContextUsageSegment, CopyTarget, SessionContextUsage, SessionExport, SessionStatusPanel
from voidcode.tui.app import TuiApp
from voidcode.tui.keys import Key
from voidcode.tui.overlay import (
    CopyPickerOverlay,
    ExportPathOverlay,
    ExportPickerOverlay,
    OverlayOutcomeKind,
)
from voidcode.tui.term import Terminal
from voidcode.tui.transcript import NoticeBlock


class _InfoRuntime:
    def __init__(self) -> None:
        self.status_calls: list[str] = []
        self.export_calls: list[str] = []
        self.write_calls: list[tuple[str, str]] = []
        self.copy_calls: list[str] = []
        self.context_calls: list[tuple[str, bool]] = []

    def current_lsp_state(self) -> SimpleNamespace:
        return SimpleNamespace(mode="disabled", servers={})

    def provider_readiness(self, *, session_id: str | None) -> SimpleNamespace:
        return SimpleNamespace(context_window=None)

    def session_status_panel(self, *, session_id: str) -> SessionStatusPanel:
        self.status_calls.append(session_id)
        return SessionStatusPanel(
            session_id=session_id,
            title="demo",
            status="completed",
            turn=2,
            model="gpt-4o",
            provider="openai",
            reasoning_effort="medium",
            approval_mode="suggest",
            collaboration_mode="default",
            goal_objective="ship",
            goal_status="active",
            input_tokens=100,
            output_tokens=50,
            cost_usd=0.001,
            workspace="/workspace",
            base_url="https://api.openai.com/v1",
            forked_from_session_id=None,
        )

    def export_session_markdown(self, *, session_id: str) -> SessionExport:
        self.export_calls.append(session_id)
        return SessionExport(session_id=session_id, filename=f"voidcode-session-{session_id}.md", markdown="# Session\n")

    def write_session_export(self, *, session_id: str, path: str) -> dict[str, object]:
        self.write_calls.append((session_id, path))
        return {"session_id": session_id, "path": path, "filename": Path(path).name}

    def copy_targets(self, *, session_id: str) -> tuple[CopyTarget, ...]:
        self.copy_calls.append(session_id)
        return (
            CopyTarget(label="Whole response", preview="intro", text="intro"),
            CopyTarget(label="python code", preview="print(1)", text="print(1)\n"),
        )

    def session_context_usage(self, *, session_id: str, expand_items: bool = False) -> SessionContextUsage:
        self.context_calls.append((session_id, expand_items))
        return SessionContextUsage(
            session_id=session_id,
            model="openai/gpt-4o",
            used_tokens=1000,
            total_tokens=8000,
            percent=12.5,
            over_budget=False,
            estimated=False,
            segments=(ContextUsageSegment(label="System prompt", tokens=1000, items=()),),
        )

    def __exit__(self, *_args: object) -> None:
        return None


def _app(tmp_path: Path, runtime: _InfoRuntime) -> tuple[TuiApp, Terminal, object]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    app = TuiApp(workspace, runtime=runtime)
    output = (tmp_path / "terminal.log").open("wb", buffering=0)
    terminal = Terminal(output_fd=output.fileno(), is_tty=False, size=(80, 24), color_system=None)
    app._term = terminal
    app._setup(terminal)
    return app, terminal, output


def _drain_until(app: TuiApp, predicate, limit: int = 200) -> None:  # type: ignore[no-untyped-def]
    for _ in range(limit):
        app._drain_queue()
        if predicate():
            return
        time.sleep(0.01)


def test_status_renders_panel_and_runs_mid_stream(tmp_path: Path) -> None:
    runtime = _InfoRuntime()
    app, terminal, output = _app(tmp_path, runtime)
    steered: list[str] = []
    app._steer = steered.append
    try:
        app._session_id = "session-1"
        app._streaming = True
        app._submit("/status")
        _drain_until(app, lambda: runtime.status_calls != [])
        app._drain_queue()
        notices = [block for block in app._view.transcript().blocks if isinstance(block, NoticeBlock)]
        assert notices and "session-1" in notices[-1].text
        assert "openai/gpt-4o" in notices[-1].text
        assert steered == []
    finally:
        app._shutdown(terminal)
        terminal.close()
        output.close()


def test_export_idle_gate_and_path_save(tmp_path: Path) -> None:
    runtime = _InfoRuntime()
    app, terminal, output = _app(tmp_path, runtime)
    try:
        app._session_id = "session-1"
        app._streaming = True
        app._submit("/export")
        assert runtime.export_calls == []
        notices = [block for block in app._view.transcript().blocks if isinstance(block, NoticeBlock)]
        assert notices and "idle active session" in notices[-1].text

        app._streaming = False
        app._submit("/export out.md")
        assert runtime.write_calls == [("session-1", "out.md")]
        notices = [block for block in app._view.transcript().blocks if isinstance(block, NoticeBlock)]
        assert notices and "out.md" in notices[-1].text
    finally:
        app._shutdown(terminal)
        terminal.close()
        output.close()


def test_export_bare_opens_picker_then_filename(tmp_path: Path) -> None:
    runtime = _InfoRuntime()
    app, terminal, output = _app(tmp_path, runtime)
    try:
        app._session_id = "session-1"
        app._submit("/export")
        _drain_until(app, lambda: isinstance(app._overlay, ExportPickerOverlay))
        assert isinstance(app._overlay, ExportPickerOverlay)
        # Choose "Save to file" (second row).
        app._handle_overlay_key(Key("down"))
        app._handle_overlay_key(Key("enter"))
        assert isinstance(app._overlay, ExportPathOverlay)
        rows = "\n".join(app._overlay.render(80))
        assert "voidcode-session-session-1.md" in rows
        app._handle_overlay_key(Key("enter"))
        assert runtime.write_calls == [("session-1", "voidcode-session-session-1.md")]
    finally:
        app._shutdown(terminal)
        terminal.close()
        output.close()


def test_copy_opens_picker_and_copies_choice(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import voidcode.tui.app as app_module

    copied: list[str] = []
    monkeypatch.setattr(app_module, "copy_to_clipboard", copied.append)
    runtime = _InfoRuntime()
    app, terminal, output = _app(tmp_path, runtime)
    try:
        app._session_id = "session-1"
        app._streaming = True  # no gate: works mid-stream
        app._submit("/copy")
        _drain_until(app, lambda: isinstance(app._overlay, CopyPickerOverlay))
        assert isinstance(app._overlay, CopyPickerOverlay)
        app._handle_overlay_key(Key("down"))
        app._handle_overlay_key(Key("enter"))
        assert copied == ["print(1)\n"]
        notices = [block for block in app._view.transcript().blocks if isinstance(block, NoticeBlock)]
        assert notices and "python code" in notices[-1].text
    finally:
        app._shutdown(terminal)
        terminal.close()
        output.close()


def test_copy_single_target_skips_picker(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import voidcode.tui.app as app_module

    copied: list[str] = []
    monkeypatch.setattr(app_module, "copy_to_clipboard", copied.append)
    runtime = _InfoRuntime()
    runtime.copy_targets = lambda *, session_id: (CopyTarget(label="Whole response", preview="x", text="x"),)  # type: ignore[method-assign]
    app, terminal, output = _app(tmp_path, runtime)
    try:
        app._session_id = "session-1"
        app._submit("/copy")
        _drain_until(app, lambda: copied != [])
        assert app._overlay is None
        assert copied == ["x"]
    finally:
        app._shutdown(terminal)
        terminal.close()
        output.close()


def test_copy_failure_surfaces_error(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import voidcode.tui.app as app_module

    def _fail(_text: str) -> None:
        raise OSError("no terminal")

    monkeypatch.setattr(app_module, "copy_to_clipboard", _fail)
    runtime = _InfoRuntime()
    runtime.copy_targets = lambda *, session_id: (CopyTarget(label="Whole response", preview="x", text="x"),)  # type: ignore[method-assign]
    app, terminal, output = _app(tmp_path, runtime)
    try:
        app._session_id = "session-1"
        app._submit("/copy")
        _drain_until(
            app,
            lambda: any(isinstance(block, NoticeBlock) and "Copy failed" in block.text for block in app._view.transcript().blocks),
        )
        notices = [block for block in app._view.transcript().blocks if isinstance(block, NoticeBlock)]
        assert notices and "Copy failed" in notices[-1].text
    finally:
        app._shutdown(terminal)
        terminal.close()
        output.close()


def test_context_renders_usage_and_all_flag(tmp_path: Path) -> None:
    runtime = _InfoRuntime()
    app, terminal, output = _app(tmp_path, runtime)
    steered: list[str] = []
    app._steer = steered.append
    try:
        app._session_id = "session-1"
        app._streaming = True  # no gate: works mid-stream
        app._submit("/context all")
        _drain_until(app, lambda: runtime.context_calls != [])
        app._drain_queue()
        assert runtime.context_calls == [("session-1", True)]
        notices = [block for block in app._view.transcript().blocks if isinstance(block, NoticeBlock)]
        assert notices and "1000 / 8000" in notices[-1].text
        assert "System prompt" in notices[-1].text
        assert steered == []

        app._submit("/context bogus")
        notices = [block for block in app._view.transcript().blocks if isinstance(block, NoticeBlock)]
        assert notices and "only 'all'" in notices[-1].text
    finally:
        app._shutdown(terminal)
        terminal.close()
        output.close()


def test_picker_escape_cancels(tmp_path: Path) -> None:
    assert CopyPickerOverlay(options=(("0", "Whole response"),)).handle_key(Key("escape")).kind is OverlayOutcomeKind.CANCELLED
    assert ExportPickerOverlay().handle_key(Key("escape")).kind is OverlayOutcomeKind.CANCELLED
    assert ExportPathOverlay().handle_key(Key("escape")).kind is OverlayOutcomeKind.CANCELLED


def test_osc52_sequence_shape() -> None:
    import base64
    import io

    import voidcode.tui.term as term_module

    buffer = io.StringIO()
    old_stdout = term_module.sys.stdout
    term_module.sys.stdout = buffer  # type: ignore[assignment]
    try:
        term_module.copy_to_clipboard("hi", tmux=False)
    finally:
        term_module.sys.stdout = old_stdout
    assert buffer.getvalue() == f"\x1b]52;c;{base64.b64encode(b'hi').decode()}\x07"
