"""Slash/path completion controller: narrow API, selection, staleness, replacements."""

from __future__ import annotations

import queue
from collections.abc import Mapping
from typing import Any

from voidcode.capabilities.files.path_queries import PathCompletionEntry, PathCompletionResult
from voidcode.sdk.commands import CommandDescriptor
from voidcode.tui.completion import CompletionCatalog, CompletionController
from voidcode.tui.composer import Composer
from voidcode.tui.events import AbortToken, PathCompletionEvent
from voidcode.tui.keys import Key
from voidcode.tui.styles import Styles
from voidcode.tui.work import WorkGroup

COMMANDS = (
    CommandDescriptor(name="help", owner="core", description="Show help"),
    CommandDescriptor(name="show", owner="core", description="Show data"),
    CommandDescriptor(name="show.more", owner="core", description="Show more"),
    CommandDescriptor(name="shout", owner="core", description="Shout"),
    CommandDescriptor(name="hidden", owner="core", description="Hidden", slash_visible=False),
)


class _Client:
    """Completion never calls the transport synchronously; any call is a bug."""

    def complete_paths(self, session_id: str, arguments: Mapping[str, object]) -> Mapping[str, object] | None:
        raise AssertionError(f"unexpected path query: {session_id} {arguments}")


def _controller(monkeypatch: Any | None = None) -> tuple[CompletionController, Composer, list[str], queue.Queue[Any]]:
    composer = Composer(styles=Styles(None), width=100)
    work = WorkGroup()
    events: queue.Queue[Any] = queue.Queue()
    ensured: list[str] = []

    def ensure() -> CompletionCatalog:
        ensured.append("ensure")
        return CompletionCatalog(revision="rev-1", commands=COMMANDS)

    controller = CompletionController(composer=composer, styles=Styles(None), work=work, event_queue=events, client=_Client())  # type: ignore[arg-type]
    if monkeypatch is not None:
        monkeypatch.setattr(work, "submit", lambda *_args, **_kwargs: None)
    return controller, composer, ensured, events


def _sync(controller: CompletionController, ensure: Any, *, dismissed: bool = False) -> None:
    controller.sync(
        dismissed=dismissed,
        catalog=CompletionCatalog(revision="rev-1", commands=COMMANDS),
        ensure_catalog=ensure,
        session_id="s1",
        generation=0,
    )


def _catalog_present() -> CompletionCatalog:
    raise AssertionError("a present catalog must not be re-ensured")


def test_slash_completion_filters_hides_hidden_and_preserves_selection(monkeypatch: Any) -> None:
    controller, composer, _ensured, _events = _controller(monkeypatch)
    composer.set_text("/sh")
    _sync(controller, _catalog_present)
    assert composer.completion == ("/shout", "/show")
    assert "/hidden" not in composer.completion

    composer.handle_key(Key("down"))
    assert composer.completion_index == 1
    _sync(controller, _catalog_present)
    assert composer.completion == ("/shout", "/show")
    assert composer.completion_index == 1

    composer.set_text("/show")
    _sync(controller, _catalog_present)
    assert composer.completion == ("/show",)
    composer.handle_key(Key("tab"))
    assert composer.value == "/show"
    composer.set_text("/show ")
    _sync(controller, _catalog_present)
    assert composer.completion == ("/show more",)


def test_builtin_tui_controls_complete_even_without_an_available_catalog(monkeypatch: Any) -> None:
    controller, composer, _ensured, _events = _controller(monkeypatch)
    ensured: list[str] = []

    def ensure() -> CompletionCatalog:
        ensured.append("ensure")
        return CompletionCatalog(revision="", commands=())

    composer.set_text("/tui v")
    controller.sync(
        catalog=CompletionCatalog(revision="", commands=()),
        ensure_catalog=ensure,
        session_id="s1",
        generation=0,
    )
    assert ensured == ["ensure"]
    assert composer.completion == ("/tui views",)


def test_catalog_ensure_runs_only_when_a_slash_token_exists(monkeypatch: Any) -> None:
    controller, composer, _ensured, _events = _controller(monkeypatch)
    ensured: list[str] = []

    def ensure() -> CompletionCatalog:
        ensured.append("ensure")
        return CompletionCatalog(revision="", commands=())

    composer.set_text("hello")
    controller.sync(catalog=CompletionCatalog(revision="", commands=()), ensure_catalog=ensure, session_id="s1", generation=0)
    assert ensured == []

    composer.set_text("/")
    controller.sync(catalog=CompletionCatalog(revision="", commands=()), ensure_catalog=ensure, session_id="s1", generation=0)
    assert ensured == ["ensure"]
    assert composer.completion == ("/tui",)


def test_dismissed_popup_clears_without_touching_the_draft(monkeypatch: Any) -> None:
    controller, composer, _ensured, _events = _controller(monkeypatch)
    composer.set_text("/he")
    _sync(controller, lambda: CompletionCatalog(revision="rev-1", commands=COMMANDS))
    assert composer.completion

    _sync(controller, lambda: CompletionCatalog(revision="rev-1", commands=COMMANDS), dismissed=True)
    assert composer.completion == ()
    assert composer.value == "/he"
    assert composer.completion_descriptions == {}


def test_path_reply_renders_candidates_and_replacements_only_for_the_live_draft(monkeypatch: Any) -> None:
    controller, composer, _ensured, _events = _controller(monkeypatch)
    composer.set_text("@s")
    _sync(controller, lambda: CompletionCatalog(revision="rev-1", commands=COMMANDS))
    result = PathCompletionResult(
        query="s",
        entries=(PathCompletionEntry(path="src/a.py", kind="file"), PathCompletionEntry(path="src/dir/", kind="directory")),
        truncated=False,
    )
    frame = PathCompletionEvent(
        token=AbortToken(),
        request_id=1,
        target_range=(0, 2),
        draft="@s",
        cursor=2,
        result=result,
    )

    controller.apply(frame, [])
    assert composer.completion == ("src/a.py", "src/dir/")
    assert controller.path_replacement("src/a.py") == "src/a.py "
    assert controller.path_replacement("src/dir/") == "src/dir/"

    stale = PathCompletionEvent(
        token=AbortToken(),
        request_id=1,
        target_range=(0, 2),
        draft="@old",
        cursor=4,
        result=result,
    )
    composer.set_text("@new")
    controller.apply(stale, [])
    assert composer.value == "@new"
    assert composer.completion == ()
