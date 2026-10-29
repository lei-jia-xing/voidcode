"""Automatic slash-name suggestions (catalog + /tui controls)."""

from __future__ import annotations

from typing import Any

from voidcode.sdk.client import CommandCatalog
from voidcode.sdk.commands import CommandDescriptor
from voidcode.tui.composer import Composer, ComposerAction
from voidcode.tui.keys import Key
from voidcode.tui.styles import Styles, symbol
from voidcode.tui.terminal_app import CoreTuiApp


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


def test_typing_slash_opens_and_filters_without_tab() -> None:
    app, _ = _app()
    app._handle_composer_key(Key("/", text="/"))
    assert app.composer.completion
    _type(app, "he")
    assert app.composer.completion == ("/help",)
    app._handle_composer_key(Key("backspace"))
    assert app.composer.value == "/h"
    assert app.composer.completion == ("/help",)


def test_no_match_closes_and_editing_reopens() -> None:
    app, _ = _app()
    _type(app, "/zzz")
    assert app.composer.completion == ()
    for _ in range(3):
        app._handle_composer_key(Key("backspace"))
    assert app.composer.value == "/"
    assert app.composer.completion


def test_arrows_move_tab_accepts_and_esc_keeps_draft() -> None:
    app, _ = _app()
    _type(app, "/sh")
    assert app.composer.completion == ("/shell", "/show")
    app._handle_composer_key(Key("down"))
    assert app.composer.completion_index == 1
    app._handle_composer_key(Key("up"))
    assert app.composer.completion_index == 0
    app._handle_composer_key(Key("down"))
    app._handle_composer_key(Key("tab"))
    assert app.composer.value == "/show"
    assert app.composer.completion == ()
    _type(app, "x")
    assert app.composer.value == "/showx"
    assert app.composer.completion == ()
    app.composer.clear()
    _type(app, "/sh")
    app._handle_composer_key(Key("escape"))
    assert app.composer.completion == ()
    assert app.composer.value == "/sh"
    app._handle_composer_key(Key("escape"))
    assert app.composer.completion == ()
    assert app.composer.value == "/sh"
    app._handle_composer_key(Key("o", text="o"))
    assert app.composer.value == "/sho"
    assert app.composer.completion == ("/show",)


def test_partial_enter_fills_without_running_exact_enter_submits() -> None:
    app, _ = _app()
    calls: list[str] = []
    app._dispatch_slash_command = lambda line: calls.append(line)  # type: ignore[method-assign]
    _type(app, "/sh")
    app._handle_composer_key(Key("down"))
    app._handle_composer_key(Key("enter"))
    assert app.composer.value == "/show"
    assert app.composer.completion == ()
    assert calls == []
    app._handle_composer_key(Key("enter"))
    assert calls == ["/show"]
    assert app.composer.value == ""

    _type(app, "/help")
    assert app.composer.completion == ("/help",)
    app._handle_composer_key(Key("enter"))
    assert calls == ["/show", "/help"]
    assert app.composer.value == ""


def test_moved_highlight_enter_fills_instead_of_running_typed_exact() -> None:
    app, _ = _app()
    app.catalog_cache = (*app.catalog_cache, CommandDescriptor(name="show.more", owner="core", description="more"))
    calls: list[str] = []
    app._dispatch_slash_command = lambda line: calls.append(line)  # type: ignore[method-assign]
    _type(app, "/show")
    assert app.composer.completion == ("/show", "/show.more")
    app._handle_composer_key(Key("down"))
    app._handle_composer_key(Key("enter"))
    assert calls == []
    assert app.composer.value == "/show.more"
    assert app.composer.completion == ()


def test_args_multiline_plain_and_midline_never_open() -> None:
    app, _ = _app()
    _type(app, "/show ")
    assert app.composer.completion == ()
    _type(app, "x")
    assert app.composer.completion == ()
    app.composer.clear()
    _type(app, "hello")
    assert app.composer.completion == ()
    app.composer.clear()
    _type(app, "/sh")
    assert app.composer.completion
    app._handle_composer_key(Key("shift+enter"))
    assert "\n" in app.composer.value
    assert app.composer.completion == ()
    app.composer.clear()
    _type(app, "/sh")
    app._handle_composer_key(Key("left"))
    assert app.composer.completion == ()


def test_tui_subcommand_filters_and_cjk_plain_text_safe() -> None:
    app, _ = _app()
    app._handle_composer_key(Key("paste", text="/tui v"))
    assert app.composer.completion == ("/tui views",)
    app._handle_composer_key(Key("tab"))
    assert app.composer.value == "/tui views"
    assert app.composer.completion == ()
    app.composer.clear()
    _type(app, "/tui ")
    assert len(app.composer.completion) == 10
    _type(app, "views ")
    assert app.composer.completion == ()
    app.composer.clear()
    _type(app, "\u65e5\u672c\u8a9e")
    assert app.composer.completion == ()
    assert app.composer.value == "\u65e5\u672c\u8a9e"


def test_live_catalog_refresh_updates_open_menu_and_submit_history_intact() -> None:
    app, _ = _app()
    _type(app, "/sh")
    assert app.composer.completion == ("/shell", "/show")
    app.catalog_cache = (CommandDescriptor(name="show", owner="core", description="show"),)
    app._sync_slash_completion()
    assert app.composer.completion == ("/show",)
    app.composer.clear()
    prompts: list[str] = []
    app._dispatch_user_prompt = lambda prompt: prompts.append(prompt)  # type: ignore[method-assign]
    _type(app, "hello")
    app._handle_composer_key(Key("enter"))
    assert prompts == ["hello"]
    app._handle_composer_key(Key("up"))
    assert app.composer.value == "hello"


def test_composer_popup_keys_use_arrows_and_unicode_mark() -> None:
    composer = Composer(styles=Styles(None), width=40)
    composer.set_text("/sh")
    composer.set_completion(("/shell", "/show"))
    composer.handle_key(Key("down"))
    assert composer.completion_index == 1
    composer.handle_key(Key("tab"))
    assert composer.value == "/show"
    assert composer.completion == ()
    composer.set_completion(("/shell", "/show"))
    rows = composer.completion_rows()
    assert rows[0].startswith(symbol("nav.cursor"))
    assert composer.handle_key(Key("escape")).action == ComposerAction.NONE
    assert composer.completion == ()
