"""Catalog acquisition failures stay visible and recoverable across every UI entry point."""

from __future__ import annotations

import re
from typing import Any

from voidcode.sdk.client import CommandCatalog, SessionRow
from voidcode.sdk.commands import CommandDescriptor
from voidcode.sdk.views import ChoiceOption, ChoicesViewData, FormField, FormViewData, PluginViewDescriptor, ViewsSnapshot
from voidcode.tui import catalog
from voidcode.tui.keys import Key
from voidcode.tui.terminal_app import CoreTuiApp
from voidcode.tui.views import InteractiveChoices, InteractiveForm


class _StubTerminal:
    width = 100
    content_width = 100
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


class _FlakyClient:
    def __init__(self) -> None:
        self.fail = True
        self.descriptors: tuple[CommandDescriptor, ...] = ()

    def command_catalog(self) -> CommandCatalog:
        if self.fail:
            raise RuntimeError("server unavailable")
        return CommandCatalog(revision_id="rev-2", commands=self.descriptors)

    def list_sessions(self, limit: int = 20) -> list[SessionRow]:
        return [
            SessionRow(
                session_id="s1",
                workspace="/ws",
                status="idle",
                active_revision_id="rev-1",
                run_id=None,
                turn=0,
                last_event_sequence=0,
            )
        ]

    def views(self, session_id: str) -> ViewsSnapshot:
        view = PluginViewDescriptor(
            owner="t",
            name="form",
            title="Form",
            view_type="form",
            data={"fields": [{"name": "value", "label": "Value"}], "action": "demo.set"},
        )
        return ViewsSnapshot(revision_id="rev-1", views=(view,))


def _plain(rows: list[str]) -> str:
    return "\n".join(re.sub(r"\x1b\[[0-9;]*m", "", row) for row in rows)


def _app(client: _FlakyClient | None = None) -> tuple[CoreTuiApp, _StubTerminal, _FlakyClient]:
    terminal = _StubTerminal()
    stub = client or _FlakyClient()
    app = CoreTuiApp(stub, session_id="s1", terminal=terminal)  # type: ignore[arg-type]
    return app, terminal, stub


def _assert_unavailable(app: CoreTuiApp) -> None:
    assert app.status_line == "Catalog unavailable: server unavailable (retry)"


def test_failed_slash_submission_keeps_the_exact_draft() -> None:
    app, terminal, _ = _app()
    app.composer.set_text("/status now")
    app._handle_composer_key(Key("enter"))
    assert app.composer.value == "/status now"
    _assert_unavailable(app)


def test_failed_completion_reports_without_touching_the_draft() -> None:
    app, terminal, _ = _app()
    app.composer.set_text("/he")
    app._sync_slash_completion()
    assert app.composer.value == "/he"
    _assert_unavailable(app)


def test_failed_catalog_shortcut_reports_instead_of_doing_nothing() -> None:
    app, terminal, _ = _app()
    app._handle_composer_key(Key("alt+g"))
    _assert_unavailable(app)


def test_failed_client_syntax_and_schema_are_visible() -> None:
    app, terminal, _ = _app()
    assert app._client_syntax_command("!ls") is None
    _assert_unavailable(app)
    app.status_line = ""
    assert app._action_schema("demo.set") is None
    assert app.status_line.startswith("Catalog unavailable:")


def test_failed_form_load_keeps_the_form_and_reports_inline() -> None:
    app, terminal, _ = _app()
    form = InteractiveForm(FormViewData(fields=(FormField(name="value", label="Value"),), action="demo.set", load_action="demo.load"))
    app._maybe_load_form_data(form)
    assert form.load_error is not None and "Catalog unavailable" in form.load_error
    _assert_unavailable(app)


def test_failed_overlay_prepare_input_keeps_the_overlay() -> None:
    app, terminal, _ = _app()
    overlay = InteractiveChoices(
        ChoicesViewData(options=(ChoiceOption(id="one", label="One"),), action="demo.set", prepare_input=True),
        revision_id="rev-1",
    )
    app.active_overlay = overlay
    app._handle_overlay_key(Key("enter"))
    assert app.active_overlay is overlay
    _assert_unavailable(app)


def _settle(app: CoreTuiApp, *, timeout: float = 5.0) -> None:
    assert app._work.wait_idle(timeout=timeout)
    for _ in range(20):
        app._drain_events()
        if app._work.pending == 0 and app.event_queue.empty():
            return
    raise AssertionError("background TUI frames did not settle")


def test_sessions_and_views_report_catalog_failure_without_raising() -> None:
    app, terminal, _ = _app()
    app._show_sessions()
    _settle(app)
    assert app.status_line.startswith("Catalog unavailable:")
    app.status_line = ""
    app._show_views()
    _settle(app)
    assert app.status_line.startswith("Catalog unavailable:")
    assert isinstance(app.active_overlay, InteractiveForm)


def test_successful_reload_clears_the_failure_and_restores_dispatch() -> None:
    app, terminal, client = _app()
    assert catalog.get(app, retry=True) == ()
    _assert_unavailable(app)
    client.fail = False
    client.descriptors = (CommandDescriptor(name="status", owner="core", description="Show status"),)
    assert [entry.name for entry in catalog.get(app, retry=True)] == ["status"]
    assert app.catalog_revision == "rev-2"
    assert app._catalog_error is None
    app.status_line = ""
    app._show_help()
    text = _plain(terminal.committed)
    assert "Catalog unavailable:" not in text
    assert "/status" in text
