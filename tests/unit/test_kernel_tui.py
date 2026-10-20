"""Unit tests for the generic kernel TUI client and 8 finite views."""

from __future__ import annotations

from typing import Any

import pytest

from voidcode.kernel.tui.keys import Key
from voidcode.kernel.tui.views import (
    InteractiveChoices,
    InteractiveConfirmation,
    InteractiveForm,
    UnsupportedViewError,
    render_plugin_view,
)


def test_unsupported_view_raises_error() -> None:
    """A view kind outside the eight finite kinds fails visibly, never silently."""
    with pytest.raises(UnsupportedViewError):
        render_plugin_view({"view_type": "invalid_magic_type", "data": {}}, 80)


def test_interactive_choices_navigation_and_selection() -> None:
    choices = InteractiveChoices(
        {
            "action": "select_target",
            "argument_name": "target_id",
            "options": [
                {"id": "a", "label": "Option A"},
                {"id": "b", "label": "Option B"},
            ],
        }
    )

    # Navigate down
    choices.handle_key(Key("down"))
    assert choices.selected_index == 1

    # Enter selects Option B
    outcome = choices.handle_key(Key("enter"))
    assert outcome == {"action": "select_target", "arguments": {"target_id": "b"}}

    # Esc cancels
    assert choices.handle_key(Key("esc")) == {"cancel": True}


def test_interactive_confirmation_key_handling() -> None:
    conf = InteractiveConfirmation(
        {
            "request_id": "req-abc",
            "message": "Are you sure?",
            "actions": ["allow", "deny"],
        }
    )
    assert conf.selected_index == 0

    # Enter on first action allows
    assert conf.handle_key(Key("enter")) == {"request_id": "req-abc", "decision": "allow"}

    # Navigate right to deny
    conf.handle_key(Key("right"))
    assert conf.selected_index == 1
    assert conf.handle_key(Key("enter")) == {"request_id": "req-abc", "decision": "deny"}

    # Esc automatically denies
    assert conf.handle_key(Key("esc")) == {"request_id": "req-abc", "decision": "deny"}


def test_interactive_form_field_editing_and_submission() -> None:
    form = InteractiveForm(
        {
            "action": "save_config",
            "fields": [
                {"name": "title", "label": "Title", "field_type": "text", "required": True},
                {"name": "count", "label": "Count", "field_type": "integer", "required": False},
                {"name": "enabled", "label": "Enabled", "field_type": "boolean", "required": False},
            ],
            "initial_values": {"title": "Init", "count": 5, "enabled": False},
        }
    )

    assert form.values["title"] == "Init"
    assert form.values["count"] == 5
    assert form.values["enabled"] is False

    # Boolean cycle is unset -> true -> false -> unset, so an explicit False
    # steps to unset and then to True.
    form.active_index = 2
    form.handle_key(Key("space"))
    assert form.values["enabled"] is None
    form.handle_key(Key("space"))
    assert form.values["enabled"] is True

    # Typing appends raw text; nothing is coerced while editing.
    form.active_index = 1
    form.handle_key(Key("3", text="3"))
    assert form.values["count"] == "53"

    # Type text into title
    form.active_index = 0
    form.handle_key(Key("!", text="!"))
    assert form.values["title"] == "Init!"

    # Submit coerces at the boundary: integer text becomes an int, boolean stays bool.
    outcome = form.handle_key(Key("ctrl+s"))
    assert outcome == {
        "action": "save_config",
        "arguments": {
            "title": "Init!",
            "count": 53,
            "enabled": True,
        },
    }


def test_interactive_form_never_fabricates_numbers_and_omits_blank_optionals() -> None:
    form = InteractiveForm(
        {
            "action": "set_limits",
            "fields": [
                {"name": "budget", "label": "Budget", "field_type": "integer", "required": False},
                {"name": "ratio", "label": "Ratio", "field_type": "number", "required": False},
            ],
            "initial_values": {},
        }
    )

    # Absent optionals submit as omitted keys, never as 0 or null.
    assert form.handle_key(Key("ctrl+s")) == {"action": "set_limits", "arguments": {}}

    # "1e3" must not silently become 1: visible error, nothing submitted.
    form.active_index = 0
    for char in "1e3":
        form.handle_key(Key(char, text=char))
    assert form.handle_key(Key("ctrl+s")) is None
    assert form.validation_error

    # A real integer and a real float are submitted verbatim.
    form.values["budget"] = "-12"
    form.values["ratio"] = "2.5e-2"
    assert form.handle_key(Key("ctrl+s")) == {
        "action": "set_limits",
        "arguments": {"budget": -12, "ratio": 0.025},
    }

    # Numeric candidates are trimmed for parsing, text stays verbatim.
    form.values["budget"] = " 12 "
    assert form.handle_key(Key("ctrl+s")) == {
        "action": "set_limits",
        "arguments": {"budget": 12, "ratio": 0.025},
    }


def test_interactive_form_validates_required_fields() -> None:
    form = InteractiveForm(
        {
            "action": "create_item",
            "fields": [
                {"name": "req_field", "label": "Required Field", "field_type": "text", "required": True},
            ],
        }
    )
    # Submit with empty value should fail validation and return None
    outcome = form.handle_key(Key("ctrl+s"))
    assert outcome is None
    assert form.validation_error


def test_interactive_form_load_action_prefills_without_overwriting_dirty_edits() -> None:
    form = InteractiveForm(
        {
            "action": "edit_item",
            "load_action": "get_item",
            "fields": [
                {"name": "desc", "label": "Description", "field_type": "text"},
                {"name": "notes", "label": "Notes", "field_type": "text"},
            ],
            "initial_values": {},
        }
    )

    # User starts typing into desc
    form.active_index = 0
    form.handle_key(Key("x", text="x"))
    assert "desc" in form.dirty_fields
    assert form.values["desc"] == "x"

    # Async load returns data for both desc and notes
    form.populate_from_load_action({"desc": "Original Desc", "notes": "Loaded Notes"})

    # User's dirty edit on desc is preserved, while clean field notes is populated!
    assert form.values["desc"] == "x"
    assert form.values["notes"] == "Loaded Notes"


def test_form_nullable_field_clears_instead_of_omitting() -> None:
    """A field the command declares nullable submits null, so a value can be cleared."""
    form = InteractiveForm(
        {
            "action": "set_nullable",
            "fields": [{"name": "budget", "label": "Budget", "field_type": "integer", "required": False}],
            "initial_values": {"budget": 42},
        },
        action_schema={
            "type": "object",
            "properties": {"budget": {"anyOf": [{"type": "integer"}, {"type": "null"}]}},
        },
    )
    form.values["budget"] = ""
    assert form.handle_key(Key("ctrl+s")) == {"action": "set_nullable", "arguments": {"budget": None}}


def test_form_boolean_missing_optional_nullable_and_explicit_values_stay_distinct() -> None:
    """Missing, null, false and true are four different submissions."""
    schema = {
        "type": "object",
        "properties": {
            "flag": {"type": "boolean"},
            "maybe": {"anyOf": [{"type": "boolean"}, {"type": "null"}]},
        },
    }
    form = InteractiveForm(
        {
            "action": "set_flags",
            "fields": [
                {"name": "flag", "label": "Flag", "field_type": "boolean", "required": False},
                {"name": "maybe", "label": "Maybe", "field_type": "boolean", "required": False},
            ],
        },
        action_schema=schema,
    )

    # Absent fields are omitted: an untouched form never clears a nullable value.
    assert form.handle_key(Key("ctrl+s")) == {"action": "set_flags", "arguments": {}}

    # A present, deliberate blank on a nullable field is an explicit clear.
    form.values["maybe"] = ""
    assert form.handle_key(Key("ctrl+s")) == {
        "action": "set_flags",
        "arguments": {"maybe": None},
    }

    # An explicit false is submitted as false, not dropped.
    form.values["flag"] = False
    assert form.handle_key(Key("ctrl+s")) == {
        "action": "set_flags",
        "arguments": {"flag": False, "maybe": None},
    }

    # An explicit true is submitted as true.
    form.values["flag"] = True
    assert form.handle_key(Key("ctrl+s")) == {
        "action": "set_flags",
        "arguments": {"flag": True, "maybe": None},
    }


def test_form_missing_required_nullable_is_refused_until_provided() -> None:
    form = InteractiveForm(
        {
            "action": "set_budget",
            "fields": [{"name": "budget", "label": "Budget", "field_type": "integer", "required": True}],
        },
        action_schema={
            "type": "object",
            "properties": {"budget": {"anyOf": [{"type": "integer"}, {"type": "null"}]}},
        },
    )
    # Absent: refused, even though null would be legal once the user acts on it.
    assert form.handle_key(Key("ctrl+s")) is None
    assert form.validation_error

    # Clearing then re-entering a numeric works: the editor never sees the
    # literal text of a present null.
    form.values["budget"] = None
    form.handle_key(Key("7", text="7"))
    form.handle_key(Key("5", text="5"))
    assert form.values["budget"] == "75"
    assert form.handle_key(Key("ctrl+s")) == {"action": "set_budget", "arguments": {"budget": 75}}


def test_form_required_boolean_missing_is_a_validation_error() -> None:
    form = InteractiveForm(
        {
            "action": "set_flag",
            "fields": [{"name": "flag", "label": "Flag", "field_type": "boolean", "required": True}],
        }
    )
    assert form.handle_key(Key("ctrl+s")) is None
    assert form.validation_error


def test_form_optional_non_nullable_blank_is_omitted() -> None:
    form = InteractiveForm(
        {
            "action": "set_limits",
            "fields": [{"name": "ratio", "label": "Ratio", "field_type": "number", "required": False}],
        },
        action_schema={"type": "object", "properties": {"ratio": {"type": "number"}}},
    )
    assert form.handle_key(Key("ctrl+s")) == {"action": "set_limits", "arguments": {}}


def test_form_preserves_typed_text_verbatim() -> None:
    """Non-empty text keeps its whitespace; only blanks are normalised."""
    form = InteractiveForm(
        {
            "action": "submit_form",
            "fields": [{"name": "summary", "label": "Summary", "field_type": "multiline", "required": True}],
        }
    )
    form.values["summary"] = "  keep my spacing  \n"
    outcome = form.handle_key(Key("ctrl+s"))
    assert outcome == {"action": "submit_form", "arguments": {"summary": "  keep my spacing  \n"}}


class _StubTerminal:
    """Commit sink only: records settled rows, never touches a real terminal."""

    width = 100

    def __init__(self) -> None:
        self.committed: list[str] = []

    def commit_rows(self, rows: list[str]) -> None:
        self.committed.extend(rows)


class _StubClient:
    """A collaborator that behaves like the server surface the app consumes.

    Connections are phased: the first exposes only the pre-restart prefix,
    then closes with the restart watermark; later connections filter the FULL
    durable log by `after_sequence` exactly like `kernel.events_after`. A
    worker that adopted a lying watermark fetches past the unseen rows and
    they never reach the commit sink — the loss is consumer-visible.
    """

    def __init__(self) -> None:
        self.health_calls = 0
        self.prefix_events: list[dict[str, object]] = []
        self.full_events: list[dict[str, object]] = []
        self.restart_cursor = 0
        self._connections = 0
        self._stop: Any = None

    def health(self) -> dict[str, object]:
        self.health_calls += 1
        return {"fenced": False, "stage": "serving", "generation": 2, "kernel_version": "t", "image_digest": "d"}

    def command_catalog(self) -> tuple[str, list[dict[str, object]]]:
        return "rev-1", []

    def views(self, session_id: str, *, client_capabilities: str = "") -> tuple[str, list[dict[str, object]]]:
        return "rev-1", []

    def get_session(self, session_id: str) -> dict[str, object]:
        return {"session_id": session_id, "status": "idle"}

    def events_stream(self, session_id: str, *, after_sequence: int = 0, follow: bool = True):  # type: ignore[no-untyped-def]
        self._connections += 1
        if self._connections == 1:
            # Pre-restart connection: only the settled prefix exists yet.
            backlog = [ev for ev in self.prefix_events if int(ev["sequence"]) > after_sequence]
            if backlog:
                yield {"events": backlog}
            yield {"kind": "server_restart", "cursor": self.restart_cursor}
            return
        # Post-restart connections: the full log, filtered by the resume cursor.
        backlog = [ev for ev in self.full_events if int(ev["sequence"]) > after_sequence]
        if backlog:
            yield {"events": backlog}
        # Hold the connection open like a live follow stream until the test
        # stops the worker (never hot-loop on instant EOF).
        import time as _time

        while self._stop is None or not self._stop.is_set():
            _time.sleep(0.01)


class _FencedClient(_StubClient):
    """Every business mutation answers the contracted fenced refusal."""

    def __init__(self) -> None:
        super().__init__()
        from voidcode.kernel.tui.client import KernelClientError

        self._refusal = KernelClientError("kernel is draining for an upgrade; retry after it restarts", status_code=503, code="upgrade_pending")

    def resume(self, session_id: str, **kwargs: object) -> dict[str, object]:
        raise self._refusal

    def confirm(self, session_id: str, request_id: str, decision: str, **kwargs: object) -> dict[str, object]:
        raise self._refusal


def _event(sequence: int, event_type: str = "kernel.user_message", **payload: object) -> dict[str, object]:
    return {"sequence": sequence, "type": event_type, "revision_id": "rev-1", "payload": dict(payload)}


def _drain_until(app: Any, terminal: Any, marker: str, *, timeout: float = 5.0) -> None:
    """Pump the UI-thread drain until a settled row appears (explicit sync)."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app._drain_events()
        if any(marker in row for row in terminal.committed):
            return
        time.sleep(0.01)
    raise AssertionError(f"settled row never appeared: {marker!r} in {terminal.committed!r}")


def test_reconnect_replays_unseen_rows_despite_ahead_restart_watermark() -> None:
    """Real reader-thread path: rows the first connection never exposed must
    still settle after a restart frame with an ahead watermark.

    Connection 1 exposes ONLY rows 1-2, then closes with watermark 99. Later
    connections filter the full 1-4 log by the resume cursor. A worker that
    adopted 99 would fetch after 99 and rows 3-4 would never settle — so the
    commit sink alone proves the fix. Only settled visible rows are asserted.
    """
    from voidcode.kernel.tui.terminal_app import KernelTuiApp

    client = _FencedClient()
    client.prefix_events = [_event(1, content="one"), _event(2, content="two")]
    client.full_events = [
        _event(1, content="one"),
        _event(2, content="two"),
        _event(3, content="three"),
        _event(4, content="four"),
    ]
    client.restart_cursor = 99
    terminal = _StubTerminal()
    app = KernelTuiApp(client, session_id="s1", terminal=terminal)  # type: ignore[arg-type]
    client._stop = app._stop_event
    # No reconcile thread in this cursor-specific test: stub the async refresh
    # so the join below covers only the owned stream worker.
    app._reconcile_connection_async = lambda: None  # type: ignore[method-assign]
    app._start_sse_stream()
    assert app._sse_thread is not None
    try:
        # Rows 1-2 settle from the first connection; the restart frame breaks
        # the worker's iterator and it reconnects from the applied cursor.
        _drain_until(app, terminal, "two")
        # Rows 3-4 were never exposed before the restart: they settle from the
        # reconnected fetch — only if the worker did not jump to 99.
        _drain_until(app, terminal, "four")
        for marker in ("one", "two", "three", "four"):
            assert sum(marker in row for row in terminal.committed) == 1, (marker, terminal.committed)
    finally:
        app._stop_event.set()
        app._sse_thread.join(timeout=5.0)
        assert not app._sse_thread.is_alive()


def test_real_503_handlers_keep_both_gates_then_decided_facts_close_each_once() -> None:
    """Real app handlers + a 503 collaborator: the gates stay actionable with
    the same ids/revision, then the canonical decided facts close each gate
    exactly once. A late acceptance for an OLD id must not close a NEW gate.

    This fails on the old early-clear implementation (gates are gone before the
    worker answers) and passes now.
    """
    import time

    from voidcode.kernel.tui.terminal_app import KernelTuiApp
    from voidcode.kernel.tui.views import InteractiveConfirmation

    client = _FencedClient()
    terminal = _StubTerminal()
    app = KernelTuiApp(client, session_id="s1", terminal=terminal)  # type: ignore[arg-type]
    app._session_cursors["s1"] = 10
    app.pending_approval = {"request_id": "appr-1", "tool_name": "write", "arguments": {}}
    app.active_overlay = InteractiveConfirmation({"request_id": "conf-9", "title": "t", "detail": "d"}, revision_id="rev-1")
    # Real handlers, real worker threads, real 503 answers. The barrier is the
    # workers' own follow-up envelopes: each failed submission ends with a
    # decision_done frame that clears its in-flight marker, so both markers
    # clearing means both workers answered. No production thread registry, no
    # global thread scans.
    app._resolve_approval("allow")
    app._resolve_confirmation("allow")
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        app._drain_events()
        if "appr-1" not in app._in_flight and "conf-9" not in app._in_flight:
            break
        time.sleep(0.01)
    assert "appr-1" not in app._in_flight and "conf-9" not in app._in_flight, "decision workers never answered"
    app._drain_events()
    # Same ids and revision remain actionable; nothing was auto-resent.
    assert app.pending_approval is not None and app.pending_approval["request_id"] == "appr-1"
    assert isinstance(app.active_overlay, InteractiveConfirmation) and app.active_overlay.request_id == "conf-9"
    assert app.active_overlay.revision_id == "rev-1"
    assert "appr-1" not in app._in_flight and "conf-9" not in app._in_flight
    # A NEW approval parks while the old gate is still mounted; a late
    # acceptance envelope for the OLD id must not close the new gate.
    app.pending_approval = {"request_id": "appr-2", "tool_name": "write", "arguments": {}}
    app.event_queue.put(
        {"kind": "approval_accepted", "request_id": "appr-1", "decision": "allow", "status": "completed", "generation": 0, "session_id": "s1"}
    )
    app._drain_events()
    assert app.pending_approval is not None and app.pending_approval["request_id"] == "appr-2"
    # The canonical decided facts close each gate exactly once.
    app.event_queue.put(
        {"kind": "event", "event": _event(11, "kernel.approval_decided", decision="allow", request_id="appr-2"), "generation": 0, "session_id": "s1"}
    )
    app.event_queue.put(
        {"kind": "event", "event": _event(12, "kernel.confirmation_decided", request_id="conf-9"), "generation": 0, "session_id": "s1"}
    )
    app._drain_events()
    assert app.pending_approval is None
    assert "conf-9" in app.resolved_confirmations
    assert app.active_overlay is None
    # Replayed decided facts never resurrect either gate.
    app.event_queue.put(
        {"kind": "event", "event": _event(12, "kernel.confirmation_decided", request_id="conf-9"), "generation": 0, "session_id": "s1"}
    )
    app._drain_events()
    assert app.active_overlay is None
    assert list(app.queued_confirmations) == []
