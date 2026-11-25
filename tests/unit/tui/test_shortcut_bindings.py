"""Catalog shortcut bindings: cached chords, conflicts, and help discovery."""

from __future__ import annotations

import re
from typing import Any

from voidcode.sdk.client import ClientCapabilities, CommandCatalog
from voidcode.sdk.commands import CommandDescriptor
from voidcode.tui import catalog
from voidcode.tui.formatting import shortcut_bindings as _shortcut_bindings
from voidcode.tui.keys import Key
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

    def __init__(self, descriptors: tuple[CommandDescriptor, ...] = ()) -> None:
        self.descriptors = descriptors
        self.inputs: list[tuple[str, str, dict[str, object]]] = []

    def command_catalog(self) -> CommandCatalog:
        return CommandCatalog(revision_id="rev-1", commands=self.descriptors)

    def command_input(self, session_id: str, name: str, expected_revision: str, *, arguments=None):  # type: ignore[no-untyped-def]
        raise AssertionError("must not block the UI thread")


def _plain(rows: list[str]) -> str:
    return "\n".join(re.sub(r"\x1b\[[0-9;]*m", "", row) for row in rows)


def _app(descriptors: tuple[CommandDescriptor, ...]) -> tuple[CoreTuiApp, _StubTerminal, _StubClient]:
    terminal = _StubTerminal()
    client = _StubClient(descriptors)
    app = CoreTuiApp(client, session_id="s1", terminal=terminal)  # type: ignore[arg-type]
    catalog.cache(app, descriptors, "rev-1")
    return app, terminal, client


def test_single_claimant_binds_and_reserved_keys_never_bind() -> None:
    bindings, conflicts = _shortcut_bindings(
        (
            CommandDescriptor(name="monitor", owner="p", description="Monitor", has_input=True, default_shortcut="alt+a"),
            CommandDescriptor(name="steal", owner="p", description="Steal", default_shortcut="ctrl+c"),
        )
    )
    assert set(bindings) == {"alt+a"}
    assert bindings["alt+a"].name == "monitor"
    assert conflicts == {}


def test_duplicate_chord_disables_all_claimants() -> None:
    bindings, conflicts = _shortcut_bindings(
        (
            CommandDescriptor(name="one", owner="p", description="One", default_shortcut="alt+a"),
            CommandDescriptor(name="two", owner="p", description="Two", default_shortcut="alt+a"),
        )
    )
    assert bindings == {}
    assert conflicts == {"alt+a": ("one", "two")}


def test_shortcut_routes_through_command_input_without_touching_draft() -> None:
    descriptor = CommandDescriptor(name="monitor", owner="p", description="Monitor", has_input=True, default_shortcut="alt+a")
    app, _, _ = _app((descriptor,))
    app.composer.set_text("keep me")
    caret = app.composer.cursor
    calls: list[tuple[str, dict[str, object]]] = []
    app._prepare_command_input_async = lambda entry, **kwargs: calls.append((entry.name, dict(kwargs.get("arguments") or {})))  # type: ignore[method-assign]
    app._handle_composer_key(Key("alt+a"))
    assert calls == [("monitor", {})]
    assert app.composer.value == "keep me"
    assert app.composer.cursor == caret


def test_shortcut_ignored_under_gates_and_overlays() -> None:
    from voidcode.sdk.views import FormField, FormViewData
    from voidcode.tui.views import InteractiveForm

    descriptor = CommandDescriptor(name="monitor", owner="p", description="Monitor", has_input=True, default_shortcut="alt+a")
    app, _, _ = _app((descriptor,))
    calls: list[str] = []
    app._prepare_command_input_async = lambda entry, **kwargs: calls.append(entry.name)  # type: ignore[method-assign]
    app._overlays.show(InteractiveForm(FormViewData(fields=(FormField(name="f", label="F"),), action="edit"), revision_id="rev-1"))
    # Overlay branch owns the key: the composer shortcut path never runs.
    app._handle_overlay_key(Key("alt+a"))
    assert calls == []
    assert isinstance(app.active_overlay, InteractiveForm)


def test_conflict_chord_reports_claimants_without_dispatch() -> None:
    app, terminal, _ = _app(
        (
            CommandDescriptor(name="one", owner="p", description="One", default_shortcut="alt+a"),
            CommandDescriptor(name="two", owner="p", description="Two", default_shortcut="alt+a"),
        )
    )
    calls: list[str] = []
    app._prepare_command_input_async = lambda entry, **kwargs: calls.append(entry.name)  # type: ignore[method-assign]
    app._handle_composer_key(Key("alt+a"))
    assert calls == []
    assert "alt+a" in _plain(terminal.committed)
    assert "/one" in _plain(terminal.committed)
    assert "/two" in _plain(terminal.committed)


def test_help_hides_non_slash_commands_and_lists_shortcuts() -> None:
    app, terminal, _ = _app(
        (
            CommandDescriptor(name="monitor", owner="p", description="Monitor", has_input=True, slash_visible=False, default_shortcut="alt+a"),
            CommandDescriptor(name="visible", owner="p", description="Visible"),
        )
    )
    app._show_help()
    text = _plain(terminal.committed)
    assert "/monitor" not in text
    assert "/visible" in text
    assert "alt+a" in text
    assert "Monitor" in text


def test_help_catalog_failure_renders_local_controls_without_raising() -> None:
    class _BrokenClient(_StubClient):
        def command_catalog(self) -> CommandCatalog:
            raise RuntimeError("server unavailable")

    terminal = _StubTerminal()
    app = CoreTuiApp(_BrokenClient(), session_id="s1", terminal=terminal)  # type: ignore[arg-type]
    app._show_help()
    text = _plain(terminal.committed)
    assert "Catalog unavailable:" in text
    assert "(retry)" in text
    assert "server unavailable" in text
    assert "/tui help" in text


def test_bindings_rebuild_and_conflicts_show_in_help() -> None:
    app, terminal, _ = _app((CommandDescriptor(name="monitor", owner="p", description="Monitor", default_shortcut="alt+a"),))
    assert set(app._shortcut_bindings) == {"alt+a"}
    catalog.cache(
        app,
        (
            CommandDescriptor(name="one", owner="p", description="One", default_shortcut="alt+a"),
            CommandDescriptor(name="two", owner="p", description="Two", default_shortcut="alt+a"),
        ),
        "rev-2",
    )
    assert app._shortcut_bindings == {}
    app._show_help()
    text = _plain(terminal.committed)
    assert "conflicts" in text
    assert "/one" in text and "/two" in text
