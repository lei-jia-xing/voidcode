"""Session, view, and reload RPCs never block the UI thread; stale replies are dropped."""

from __future__ import annotations

import re
import threading
import time
from pathlib import Path
from typing import Any

from voidcode.sdk.client import CommandCatalog, CommandResult, SessionRow
from voidcode.sdk.views import PluginViewDescriptor, ViewsSnapshot
from voidcode.tui import catalog
from voidcode.tui.local_client import CoreLocalClient
from voidcode.tui.terminal_app import CoreTuiApp
from voidcode.tui.views import InteractiveChoices, InteractiveForm


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


def _plain(rows: list[str]) -> str:
    return "\n".join(re.sub(r"\x1b\[[0-9;]*m", "", row) for row in rows)


def _row(session_id: str = "s1") -> SessionRow:
    return SessionRow(
        session_id=session_id,
        workspace="/ws",
        status="idle",
        active_revision_id="rev-1",
        run_id=None,
        turn=0,
        last_event_sequence=0,
    )


class _BlockingClient:
    def __init__(self) -> None:
        self.release = threading.Event()
        self.started = threading.Event()
        self.calls: list[tuple[str, str]] = []

    def command_catalog(self) -> CommandCatalog:
        return CommandCatalog(revision_id="rev-1", commands=())

    def list_sessions(self, limit: int = 100) -> list[SessionRow]:
        self.calls.append(("sessions", "start"))
        self.started.set()
        self.release.wait(5)
        self.calls.append(("sessions", "done"))
        return [_row()]

    def create_session(self) -> SessionRow:
        self.calls.append(("create", "start"))
        self.started.set()
        self.release.wait(5)
        self.calls.append(("create", "done"))
        return _row("s2")

    def views(self, session_id: str) -> ViewsSnapshot:
        self.calls.append(("views", "start"))
        self.started.set()
        self.release.wait(5)
        self.calls.append(("views", "done"))
        view = PluginViewDescriptor(
            owner="t",
            name="form",
            title="Form",
            view_type="form",
            data={"fields": [{"name": "value", "label": "Value"}], "action": "demo.set"},
        )
        return ViewsSnapshot(revision_id="rev-1", views=(view,))


def _app(client: Any) -> tuple[CoreTuiApp, _StubTerminal]:
    terminal = _StubTerminal()
    app = CoreTuiApp(client, session_id="s1", terminal=terminal)
    catalog.cache(app, (), "rev-1")
    return app, terminal


def _settle(app: CoreTuiApp, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app._drain_events()
        if app._work.pending == 0 and app.event_queue.empty():
            return
        time.sleep(0.005)
    raise AssertionError("background frames did not settle")


def test_loading_status_clears_by_kind_even_when_the_text_differs() -> None:
    app, _ = _app(_BlockingClient())

    app.set_loading_status("Loading the sessions palette...")
    app.clear_loading_status()
    assert app.status_line == ""

    app.status_line = "Error: keep me"
    app.clear_loading_status()
    assert app.status_line == "Error: keep me"


def test_followup_status_clears_by_kind_even_when_the_text_differs() -> None:
    app, _ = _app(_BlockingClient())

    app.set_followup_queued_status("Queued status with arbitrary wording")
    app.clear_followup_queued_status()
    assert app.status_line == ""

    app.status_line = "Error: keep me"
    app.clear_followup_queued_status()
    assert app.status_line == "Error: keep me"


def test_show_sessions_returns_before_the_rpc_and_mounts_afterward() -> None:
    client = _BlockingClient()
    app, _ = _app(client)
    app._show_sessions()
    assert client.started.wait(5)
    assert client.calls == [("sessions", "start")]
    assert app.status_line == "Loading sessions..."
    client.release.set()
    _settle(app)
    assert isinstance(app.active_overlay, InteractiveChoices)
    assert [entry.id for entry in app.active_overlay.view.options] == ["s1"]


def test_create_session_returns_before_the_rpc_and_switches_afterward(monkeypatch) -> None:
    client = _BlockingClient()
    app, _ = _app(client)
    switched: list[str] = []
    monkeypatch.setattr(app, "_switch_session", lambda target: switched.append(target))
    app._create_session()
    assert client.started.wait(5)
    assert client.calls == [("create", "start")]
    assert switched == []
    client.release.set()
    _settle(app)
    assert switched == ["s2"]


def test_show_views_returns_before_the_rpc_and_mounts_the_form() -> None:
    client = _BlockingClient()
    app, _ = _app(client)
    app._show_views()
    assert client.started.wait(5)
    assert client.calls == [("views", "start")]
    client.release.set()
    _settle(app)
    assert isinstance(app.active_overlay, InteractiveForm)


def test_stale_session_frames_never_mount_after_switching() -> None:
    client = _BlockingClient()
    app, _ = _app(client)
    app._show_sessions()
    client.release.set()
    assert app._work.wait_idle(5.0)
    app._follower.new_epoch()
    app._drain_events()
    assert app.active_overlay is None


class _FakeLocalClient(CoreLocalClient):
    def __init__(self) -> None:
        self.release = threading.Event()
        self.calls: list[tuple[str, str, str]] = []

    def invoke_command(self, session_id: str, name: str, arguments: Any, expected_revision: str) -> CommandResult:
        self.calls.append((session_id, name, expected_revision))
        self.release.wait(5)
        return CommandResult(status="ok", data={"to_revision_id": "rev-2"})


def test_model_switch_rewrites_and_reloads_off_the_ui_thread(tmp_path: Path) -> None:
    client = _FakeLocalClient()
    terminal = _StubTerminal()
    app = CoreTuiApp(client, session_id="s1", terminal=terminal, workspace_dir=tmp_path)
    catalog.cache(app, (), "rev-1")
    app._tui_switch_model("model", "openai/gpt-5.2")
    assert app.status_line == "Switching model..."
    assert not any("Model switched" in row for row in terminal.committed)
    client.release.set()
    _settle(app)
    assert client.calls == [("s1", "core.reload", "rev-1")]
    assert (tmp_path / "voidcode.toml").exists()
    assert "Model switched: revision rev-2 admitted" in _plain(terminal.committed)


def test_model_switch_rolls_back_config_and_clears_status_on_reload_failure(tmp_path: Path) -> None:
    from voidcode.sdk.client import CoreClientError

    config_path = tmp_path / "voidcode.toml"
    original = 'schema_version = 1\n\n[model]\nmodel = "old/model"\n'
    config_path.write_text(original, encoding="utf-8")

    class _FailingReloadClient(_FakeLocalClient):
        def invoke_command(self, session_id: str, name: str, arguments: Any, expected_revision: str) -> CommandResult:
            self.calls.append((session_id, name, expected_revision))
            raise CoreClientError("reload rejected", code="reload_failed")

    client = _FailingReloadClient()
    terminal = _StubTerminal()
    app = CoreTuiApp(client, session_id="s1", terminal=terminal, workspace_dir=tmp_path)
    catalog.cache(app, (), "rev-1")
    app._tui_switch_model("model", "openai/gpt-5.2")
    _settle(app)
    assert config_path.read_text(encoding="utf-8") == original
    assert app.status_line == ""
    assert "Model switch failed" in _plain(terminal.committed)


def test_model_switch_removes_newly_created_config_on_reload_failure(tmp_path: Path) -> None:
    config_path = tmp_path / "voidcode.toml"

    class _BadStatusReloadClient(_FakeLocalClient):
        def invoke_command(self, session_id: str, name: str, arguments: Any, expected_revision: str) -> CommandResult:
            return CommandResult(status="error", error="bad reload")

    app = CoreTuiApp(_BadStatusReloadClient(), session_id="s1", terminal=_StubTerminal(), workspace_dir=tmp_path)
    catalog.cache(app, (), "rev-1")
    app._tui_switch_model("model", "openai/gpt-5.2")
    _settle(app)
    assert not config_path.exists()
    assert app.status_line == ""


def test_upgrade_pending_invoke_decrements_commands_in_flight(tmp_path: Path) -> None:
    from voidcode.sdk.client import CoreClientError

    class _FencedClient(_FakeLocalClient):
        def invoke_command(self, session_id: str, name: str, arguments: Any, expected_revision: str) -> CommandResult:
            raise CoreClientError("upgrade pending", code="upgrade_pending")

    terminal = _StubTerminal()
    app = CoreTuiApp(_FencedClient(), session_id="s1", terminal=terminal, workspace_dir=tmp_path)
    app._invoke_async("status", {}, "rev-1")
    _settle(app)
    assert app._commands_in_flight == 0
    assert "draining for an upgrade" in _plain(terminal.committed)
