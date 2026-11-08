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
                CommandDescriptor(name="show.more", owner="core", description="more"),
                CommandDescriptor(name="shell", owner="core", description="shell", client_syntax="!"),
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
    assert app.composer.value == "/show "
    assert app.composer.completion == ("/show more",)
    _type(app, "x")
    assert app.composer.value == "/show x"
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
    calls: list[str] = []
    app._dispatch_slash_command = lambda line: calls.append(line)  # type: ignore[method-assign]
    _type(app, "/show ")
    assert app.composer.completion == ("/show more",)
    app._handle_composer_key(Key("enter"))
    assert calls == []
    assert app.composer.value == "/show more"
    assert app.composer.completion == ()


def test_args_multiline_plain_and_midline_never_open() -> None:
    app, _ = _app()
    _type(app, "/show ")
    assert app.composer.completion == ("/show more",)
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


def test_root_groups_and_tab_enter_children_without_dispatch() -> None:
    app, _ = _app()
    _type(app, "/")
    assert "/tui" in app.composer.completion
    assert "/show more" not in app.composer.completion
    app.composer.set_text("/show")
    app._sync_slash_completion()
    app._handle_composer_key(Key("tab"))
    assert app.composer.value == "/show "
    assert app.composer.completion == ("/show more",)
    app.composer.set_text("/tui")
    app._sync_slash_completion()
    app._handle_composer_key(Key("enter"))
    assert app.composer.value == "/tui "
    assert len(app.composer.completion) == 10


def test_required_arguments_open_revision_bound_form_without_invocation() -> None:
    from voidcode.tui.views import InteractiveForm

    app, _ = _app()
    descriptor = CommandDescriptor(
        name="goal.set",
        owner="goal",
        description="Create a goal",
        argument_schema={
            "type": "object",
            "properties": {"objective": {"type": "string"}, "token_budget": {"type": "integer"}},
            "required": ["objective"],
            "additionalProperties": False,
        },
    )
    app.catalog_revision = "shown-revision"
    app._dispatch_catalog_command(descriptor, "")
    assert isinstance(app.active_overlay, InteractiveForm)
    assert app.active_overlay.revision_id == "shown-revision"
    assert app.active_overlay.action == "goal.set"
    assert [(field.name, field.required) for field in app.active_overlay.view.fields] == [("objective", True), ("token_budget", False)]
    assert app.active_overlay.values == {}
    assert app.active_overlay.handle_key(Key("ctrl+s")) is None
    assert app.active_overlay.validation_error
    app.active_overlay.handle_key(Key("paste", text="Ship the feature"))
    assert app.active_overlay.handle_key(Key("ctrl+s")) == {"action": "goal.set", "arguments": {"objective": "Ship the feature"}}
    app.active_overlay.handle_key(Key("tab"))
    app.active_overlay.handle_key(Key("paste", text="2000"))
    assert app.active_overlay.handle_key(Key("ctrl+s")) == {
        "action": "goal.set",
        "arguments": {"objective": "Ship the feature", "token_budget": 2000},
    }


def test_completion_rows_include_catalog_description() -> None:
    app, _ = _app()
    _type(app, "/he")
    assert any("/help — help" in row for row in app.composer.completion_rows())


def test_live_command_presentation_mounts_under_its_revision_only() -> None:
    from voidcode.sdk.views import PluginViewDescriptor
    from voidcode.tui.views import InteractiveChoices

    app, _ = _app()
    app.catalog_revision = "rev-1"
    view = PluginViewDescriptor(
        owner="subagents",
        name="test_view",
        title="Profiles",
        view_type="choices",
        data={"options": [{"id": "explore", "label": "Explore"}], "action": "test.show", "argument_name": "name"},
    )
    app.event_queue.put({"kind": "command_presentation", "view": view, "revision_id": "rev-old", "serial": app._interaction_serial})
    app._drain_events()
    assert app.active_overlay is None
    app.event_queue.put({"kind": "command_presentation", "view": view, "revision_id": "rev-1", "serial": app._interaction_serial})
    app._drain_events()
    assert isinstance(app.active_overlay, InteractiveChoices)
    assert app.active_overlay.revision_id == "rev-1"
    assert app.active_overlay.handle_key(Key("enter")) == {"action": "test.show", "arguments": {"name": "explore"}, "prepare_input": False}


def test_history_receipt_renders_passive_presentation_without_opening_interaction() -> None:
    from voidcode.sdk.commands import CommandPresentation, CommandResult
    from voidcode.tui.terminal_app import _command_result_rows

    result = CommandResult(
        status="ok",
        output="fallback",
        presentation=CommandPresentation(title="Profile", view_type="markdown", data={"content": "**Explore**\n\nRead the workspace."}),
    )
    text = "\n".join(_command_result_rows("test.show", result, Styles(None), 40))
    assert "Explore" in text and "Read the workspace." in text
    assert "**" not in text and "fallback" not in text


def test_prepared_input_uses_dynamic_options_without_invoking_until_submit() -> None:
    from threading import Event

    from voidcode.sdk.commands import CommandInputSnapshot, CommandPresentation, CommandResult
    from voidcode.tui.views import InteractiveForm

    called = Event()

    class Client(_StubClient):
        def __init__(self):
            self.invoked = []

        def command_input(self, session_id, name, expected_revision, *, arguments=None):
            assert (session_id, name, expected_revision) == ("s1", "test.input", "rev-1")
            return CommandInputSnapshot(
                revision_id="rev-1",
                presentation=CommandPresentation(
                    title="Select agent",
                    view_type="form",
                    data={
                        "action": "test.input",
                        "fields": [
                            {
                                "name": "name",
                                "label": "Agent",
                                "options": [{"id": "explore", "label": "Explore"}, {"id": "review", "label": "Review"}],
                            }
                        ],
                    },
                ),
            )

        def invoke_command(self, session_id, name, arguments, expected_revision):
            self.invoked.append((name, arguments, expected_revision))
            called.set()
            return CommandResult(status="ok")

    app, _ = _app()
    app.client = client = Client()
    app.catalog_revision = "rev-1"
    descriptor = CommandDescriptor(name="test.input", owner="subagents", description="Inspect", has_input=True)
    app._dispatch_catalog_command(descriptor, "")
    frame = app.event_queue.get(timeout=2)
    assert client.invoked == []
    app.event_queue.put(frame)
    app._drain_events()
    assert isinstance(app.active_overlay, InteractiveForm)
    app._handle_overlay_key(Key("right"))
    app._handle_overlay_key(Key("right"))
    app._handle_overlay_key(Key("ctrl+s"))
    assert called.wait(timeout=2)
    assert client.invoked == [("test.input", {"name": "review"}, "rev-1")]


def test_natural_multi_field_arguments_preserve_text_and_explicit_json() -> None:
    app, _ = _app()
    calls = []
    app._invoke_async = lambda name, arguments, revision, **kwargs: calls.append((name, arguments))
    descriptor = CommandDescriptor(
        name="goal.set",
        owner="goal",
        description="Set",
        text_argument="objective",
        argument_schema={
            "type": "object",
            "properties": {"objective": {"type": "string"}, "token_budget": {"type": "integer"}},
            "required": ["objective"],
        },
    )
    app._dispatch_catalog_command(descriptor, "Ship it")
    app._dispatch_catalog_command(descriptor, '{"objective":"With budget","token_budget":2000}')
    assert calls == [("goal.set", {"objective": "Ship it"}), ("goal.set", {"objective": "With budget", "token_budget": 2000})]


def test_internal_command_helpers_do_not_create_discovery_namespaces() -> None:
    app, _ = _app()
    app.catalog_cache = (
        CommandDescriptor(name="help", owner="core", description="Help"),
        CommandDescriptor(name="internal.only", owner="plugin", description="Helper", slash_visible=False),
    )
    _type(app, "/")
    assert "/internal" not in app.composer.completion
    calls = []
    app._invoke_async = lambda name, arguments, revision, **kwargs: calls.append(name)
    app._dispatch_slash_command("/internal only")
    assert calls == ["internal.only"]
