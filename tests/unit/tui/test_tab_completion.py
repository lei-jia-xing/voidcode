"""Tab completion over slash-command names (catalog + /tui controls)."""

from __future__ import annotations

from typing import Any

from voidcode.sdk.client import CommandCatalog
from voidcode.sdk.commands import CommandDescriptor
from voidcode.tui.composer import Composer, ComposerAction, ComposerOutcome
from voidcode.tui.keys import Key
from voidcode.tui.terminal_app import CoreTuiApp
from voidcode.tui.theme import resolve_theme


class _StubTerminal:
    width = 100
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
    def command_catalog(self) -> CommandCatalog:
        return CommandCatalog(
            revision_id="rev-1",
            commands=(
                CommandDescriptor(name="help", owner="core", description="help"),
                CommandDescriptor(name="show", owner="core", description="show"),
                CommandDescriptor(name="shell", owner="core", description="shell"),
            ),
        )


def _app() -> tuple[CoreTuiApp, _StubTerminal]:
    terminal = _StubTerminal()
    app = CoreTuiApp(_StubClient(), session_id="s1", terminal=terminal)  # type: ignore[arg-type]
    app.catalog_cache = _StubClient().command_catalog().commands
    return app, terminal


def _type(app: CoreTuiApp, text: str) -> None:
    for char in text:
        app._handle_composer_key(Key(char, text=char))


def test_completion_is_pure_candidates_in_outcome_out() -> None:
    """The composer completes from app-supplied candidates; Enter accepts, Esc cancels."""
    composer = Composer(theme=resolve_theme(color_system=None), width=40)
    composer.set_text("/sh")
    assert composer.handle_key(Key("tab")) == ComposerOutcome()  # no candidates: no-op

    composer.set_completion(("/show", "/shell"))
    assert composer.completion == ("/show", "/shell")
    composer.handle_key(Key("tab"))
    assert composer.completion_index == 1
    composer.handle_key(Key("enter"))
    assert composer.value == "/shell"
    assert composer.completion == ()

    composer.set_completion(("/show", "/shell"))
    assert composer.handle_key(Key("escape")) == ComposerOutcome()
    assert composer.completion == ()
    assert composer.value == "/shell"
    assert composer.handle_key(Key("escape")).action == ComposerAction.CANCEL_TURN


def test_single_match_completes_inline() -> None:
    app, terminal = _app()
    _type(app, "/he")
    app._handle_composer_key(Key("tab"))
    assert app.composer.value == "/help"
    assert app.composer.completion == ()
    app._render_frame()
    assert terminal.committed == []


def test_multi_popup_cycles_and_accepts_without_committing() -> None:
    app, terminal = _app()
    _type(app, "/sh")
    app._handle_composer_key(Key("tab"))
    app._render_frame()
    last = terminal.frames[-1]
    assert any("/show" in row for row in last)
    assert any("/shell" in row for row in last)

    app._handle_composer_key(Key("tab"))
    assert app.composer.completion_index == 1
    app._handle_composer_key(Key("enter"))
    assert app.composer.value == "/show"
    assert app.composer.completion == ()
    assert terminal.committed == []


def test_tui_controls_complete_from_bare_prefix() -> None:
    app, _ = _app()
    _type(app, "/tui t")
    app._handle_composer_key(Key("tab"))
    assert app.composer.value == "/tui theme"


def test_escape_cancels_popup_keeps_draft_and_plain_text_never_completes() -> None:
    app, _ = _app()
    _type(app, "/sh")
    app._handle_composer_key(Key("tab"))
    assert app.composer.completion != ()
    app._handle_composer_key(Key("escape"))
    assert app.composer.completion == ()
    assert app.composer.value == "/sh"

    app.composer.set_text("hello")
    app._handle_composer_key(Key("tab"))
    assert app.composer.completion == ()
    assert app.composer.value == "hello"
