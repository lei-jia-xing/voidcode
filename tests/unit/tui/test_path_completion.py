"""Async ``@path`` completion: latest reply only, draft/caret preserved, failures visible."""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Mapping
from typing import Any

import pytest

from voidcode.capabilities.files.path_queries import PathCompletionEntry, PathCompletionResult
from voidcode.sdk.client import CommandCatalog, CoreClientError
from voidcode.sdk.services import QueryContractError, QueryUnavailable
from voidcode.tui import catalog
from voidcode.tui.client import CoreRpcClient
from voidcode.tui.events import AbortToken, PathCompletionEvent
from voidcode.tui.keys import Key
from voidcode.tui.terminal_app import CoreTuiApp


class _Terminal:
    width = 100
    height = 24
    eof = False
    color_system = None

    def query_default_background_rgb(self) -> None:
        return None

    def __init__(self) -> None:
        self.committed: list[str] = []

    def commit_rows(self, rows: list[str]) -> None:
        self.committed.extend(rows)

    def paint_frame(self, frame: Any) -> None:
        return None


def _plain(rows: list[str]) -> str:
    return "\n".join(re.sub(r"\x1b\[[0-9;]*m", "", row) for row in rows)


class _PathClient:
    def __init__(self, answers: Mapping[str, object] | None = None, *, error: Exception | None = None) -> None:
        self.answers = answers or {}
        self.error = error
        self.queries: list[str] = []
        self.releases: dict[str, threading.Event] = {}
        self.started: dict[str, threading.Event] = {}
        self.block = True

    def command_catalog(self) -> CommandCatalog:
        return CommandCatalog(revision_id="rev-1", commands=())

    def complete_paths(self, session_id: str, arguments: Mapping[str, object]) -> Mapping[str, object] | None:
        del session_id
        query = str(arguments.get("query", ""))
        self.queries.append(query)
        event = threading.Event()
        self.releases[query] = event
        self.started.setdefault(query, threading.Event()).set()
        if self.block:
            assert event.wait(5), f"query {query!r} was never released"
        if self.error is not None:
            raise self.error
        answer = self.answers.get(query)
        if answer is None:
            answer = PathCompletionResult(
                query=query,
                entries=(PathCompletionEntry(path="src/", kind="directory"), PathCompletionEntry(path="README.md", kind="file")),
                truncated=False,
            ).as_payload()
        return answer  # type: ignore[return-value]


def _app(client: _PathClient) -> tuple[CoreTuiApp, _Terminal]:
    terminal = _Terminal()
    app = CoreTuiApp(client, session_id="s1", terminal=terminal)  # type: ignore[arg-type]
    catalog.cache(app, (), "rev-1")
    return app, terminal


def _type(app: CoreTuiApp, text: str) -> None:
    for char in text:
        app._handle_composer_key(Key(char, text=char))


def _drain_until(app: CoreTuiApp, predicate: Any, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app._drain_events()
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("path completion did not reach the expected state")


def _wait_started(app: CoreTuiApp, client: _PathClient, query: str, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app._drain_events()
        if query in client.started:
            return
        time.sleep(0.005)
    raise AssertionError(f"query {query!r} never started; got {client.queries!r}")


def _settle_all(app: CoreTuiApp, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app._drain_events()
        if app._work.pending == 0 and app.event_queue.empty():
            return
        time.sleep(0.005)
    raise AssertionError("path completion frames did not settle")


def test_completion_is_async_latest_reply_wins_and_draft_is_untouched() -> None:
    client = _PathClient()
    app, terminal = _app(client)
    _type(app, "@")
    assert app.composer.value == "@"
    assert client.started[""].wait(5)
    assert app.composer.completion == ()
    client.releases[""].set()
    _drain_until(app, lambda: bool(app.composer.completion))
    assert app.composer.value == "@"
    assert app.composer.cursor == 1
    assert app.composer.completion == ("src/", "README.md")

    _type(app, "s")
    _wait_started(app, client, "s")
    _type(app, "r")
    assert app.composer.value == "@sr"
    client.releases["s"].set()
    _wait_started(app, client, "sr")
    app._drain_events()
    assert app.composer.completion == ()
    assert app.composer.value == "@sr"
    client.releases["sr"].set()
    _drain_until(app, lambda: bool(app.composer.completion))
    assert app.composer.completion == ("src/", "README.md")
    assert app.composer.value == "@sr"
    assert not _plain(terminal.committed)


@pytest.mark.parametrize("token", ["@sr", "@src/**/never"])
def test_unavailable_query_is_visible_and_leaves_the_draft_alone(token: str) -> None:
    client = _PathClient(error=QueryUnavailable("no admitted plugin answers 'workspace.path_completion'"))
    client.block = False
    app, terminal = _app(client)
    _type(app, token)
    _settle_all(app)
    assert app.composer.completion == ()
    assert app.composer.value == token
    assert _plain(terminal.committed).count("@path completion unavailable") == 1
    # A second failure with identical text does not spam the transcript.
    _type(app, "c")
    _settle_all(app)
    assert _plain(terminal.committed).count("@path completion unavailable") == 1


@pytest.mark.parametrize("token", ["@", "@src/**/never"])
def test_contract_failure_is_typed_and_visible(token: str) -> None:
    client = _PathClient(error=QueryContractError("answer named another query"))
    client.block = False
    app, terminal = _app(client)
    _type(app, token)
    _settle_all(app)
    assert "contract failure" in _plain(terminal.committed).lower()
    assert app.composer.completion == ()
    assert app.composer.value == token


@pytest.mark.parametrize(
    ("token", "query", "depth"),
    [
        ("src/**", "src/", 6),
        ("src/**/ma", "src/ma", 6),
        ("**/srm", "srm", 6),
        ("**", "", 6),
        ("src/ma", "src/ma", 1),
    ],
)
def test_path_completion_request_uses_the_explicit_recursive_trigger(token: str, query: str, depth: int) -> None:
    request = CoreTuiApp._path_request(token)
    assert (request.query, request.depth, request.limit, request.mode) == (query, depth, 50, "prefix")


def test_dismissed_completion_drops_the_inflight_reply() -> None:
    client = _PathClient()
    app, _terminal = _app(client)
    _type(app, "@")
    assert client.started[""].wait(5)
    app._handle_composer_key(Key("escape"))
    assert app.composer.value == "@"
    assert app.composer.completion == ()
    client.releases[""].set()
    time.sleep(0.05)
    app._drain_events()
    assert app.composer.completion == ()
    assert app.composer.value == "@"


def test_out_of_order_reply_with_matching_draft_is_still_rejected() -> None:
    client = _PathClient()
    app, _terminal = _app(client)
    _type(app, "@")
    assert client.started[""].wait(5)
    client.releases[""].set()
    _drain_until(app, lambda: bool(app.composer.completion))
    # Simulate an older serial delivering a plausible answer after a newer key.
    app.composer.set_text("@s")
    app._path_request_serial += 1
    stale = PathCompletionEvent(
        token=AbortToken(),
        request_id=1,
        target_range=(0, 1),
        draft="@s",
        cursor=2,
        result=PathCompletionResult(query="", entries=(PathCompletionEntry(path="stale.txt", kind="file"),), truncated=False),
    )
    rows: list[str] = []
    app._apply_path_completion(stale, rows)
    assert app.composer.completion == ()
    assert rows == []
    assert app.composer.value == "@s"


@pytest.mark.parametrize(
    ("code", "error_type"),
    [
        ("query_unavailable", QueryUnavailable),
        ("query_contract", QueryContractError),
    ],
)
def test_rpc_completion_error_codes_survive_as_typed_errors(monkeypatch: pytest.MonkeyPatch, code: str, error_type: type[Exception]) -> None:
    client = CoreRpcClient("http://127.0.0.1:1")

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise CoreClientError("refused", status_code=404, code=code)

    monkeypatch.setattr(client, "_call", refuse)
    with pytest.raises(error_type):
        client.complete_paths("session", {"query": ""})
