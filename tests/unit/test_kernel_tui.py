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
        self.frames: list[list[str]] = []

    def commit_rows(self, rows: list[str]) -> None:
        self.committed.extend(rows)

    def paint_frame(self, frame: Any) -> None:
        self.frames.append(list(frame.rows))


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

    def events_stream(self, session_id: str, *, after_sequence: int = 0, follow: bool = True, cancel: object = None):  # type: ignore[no-untyped-def]
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


class _CommandClient(_StubClient):
    """Catalog/views/invoke stub for the consumer-visible command-result flows."""

    def __init__(
        self,
        *,
        catalog: list[dict[str, object]] | None = None,
        view_list: list[dict[str, object]] | None = None,
        results: dict[str, dict[str, object]] | None = None,
    ) -> None:
        super().__init__()
        self.catalog = list(catalog or [])
        self.view_list = list(view_list or [])
        self.results = dict(results or {})
        self.invoked: list[tuple[str, str, dict[str, object], str]] = []

    def command_catalog(self) -> tuple[str, list[dict[str, object]]]:
        return "rev-1", list(self.catalog)

    def views(self, session_id: str, *, client_capabilities: str = "") -> tuple[str, list[dict[str, object]]]:
        return "rev-1", list(self.view_list)

    def invoke_command(
        self,
        session_id: str,
        name: str,
        arguments: dict[str, object] | None = None,
        *,
        expected_revision: str,
        client_capabilities: str = "",
    ) -> dict[str, object]:
        self.invoked.append((session_id, name, dict(arguments or {}), expected_revision))
        return dict(self.results.get(name, {"status": "ok"}))


def _event(sequence: int, event_type: str = "kernel.user_message", **payload: object) -> dict[str, object]:
    return {"sequence": sequence, "type": event_type, "revision_id": "rev-1", "payload": dict(payload)}


class _RunClient(_CommandClient):
    """Run returns the existing failed result; the durable event repeats the error."""

    def run(self, session_id: str, prompt: str, *, max_steps: int = 32) -> dict[str, object]:
        return {"status": "failed", "error": "boom"}


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


def _refresh_views(app: Any, *, timeout: float = 5.0) -> None:
    """Run one real automatic reconnect refresh and wait for it to apply."""
    import time

    target = app._reconcile_serial + 1
    app._reconcile_connection_async()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app._drain_events()
        if app._reconcile_applied >= target:
            return
        time.sleep(0.01)
    raise AssertionError(f"reconcile never applied: serial={target!r} applied={app._reconcile_applied!r}")


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


def test_automatic_view_refresh_renders_passive_views_without_mounting_form() -> None:
    """A reconnect refreshes views together but only an explicit request takes focus."""
    from voidcode.kernel.tui.terminal_app import KernelTuiApp
    from voidcode.kernel.tui.views import InteractiveForm

    status_view = {
        "name": "goal",
        "title": "Goal",
        "view_type": "status",
        "data": {"label": "Goal", "value": "No active goal"},
    }
    form_view = {
        "name": "goal.form",
        "title": "Set Goal",
        "view_type": "form",
        "data": {
            "fields": [{"name": "objective", "label": "Objective", "field_type": "multiline", "required": True}],
            "action": "goal.set",
            "initial_values": {},
        },
    }
    client = _CommandClient(view_list=[status_view, form_view])
    terminal = _StubTerminal()
    app = KernelTuiApp(client, session_id="s1", terminal=terminal)

    _refresh_views(app)
    app._render_frame()
    assert app.active_overlay is None
    assert any("No active goal" in row for row in terminal.committed)
    assert not any("Objective" in row for row in terminal.frames[-1]), terminal.frames[-1]
    app._handle_composer_key(Key("x", text="x"))
    assert app.composer.value == "x"

    app._show_views()
    app._render_frame()
    assert isinstance(app.active_overlay, InteractiveForm)
    assert any("Objective" in row for row in terminal.frames[-1]), terminal.frames[-1]


def test_activation_refresh_does_not_mount_a_form() -> None:
    """Activation is automatic too: catalog+views refresh, but no form steals focus."""
    from voidcode.kernel.tui.terminal_app import KernelTuiApp

    form_view = {
        "name": "goal.form",
        "title": "Set Goal",
        "view_type": "form",
        "data": {
            "fields": [{"name": "objective", "label": "Objective", "field_type": "multiline", "required": True}],
            "action": "goal.set",
            "initial_values": {},
        },
    }
    client = _CommandClient(view_list=[form_view])
    terminal = _StubTerminal()
    app = KernelTuiApp(client, session_id="s1", terminal=terminal)

    rows: list[str] = []
    app._handle_single_event(_event(1, "kernel.activation_committed", to_revision_id="rev-2"), rows)
    app._render_frame()
    assert app.active_overlay is None
    assert not any("Objective" in row for row in terminal.frames[-1]), terminal.frames[-1]


def test_durable_gates_survive_automatic_view_refresh() -> None:
    """Approval/confirmation gates come from durable facts, not views, and stay put."""
    from voidcode.kernel.tui.terminal_app import KernelTuiApp
    from voidcode.kernel.tui.views import InteractiveConfirmation

    status_view = {
        "name": "goal",
        "title": "Goal",
        "view_type": "status",
        "data": {"label": "Goal", "value": "No active goal"},
    }
    client = _CommandClient(view_list=[status_view])
    terminal = _StubTerminal()
    app = KernelTuiApp(client, session_id="s1", terminal=terminal)
    rows: list[str] = []

    app._handle_single_event(
        _event(1, "kernel.approval_requested", request_id="appr-1", call={"tool_name": "write", "arguments": {}}),
        rows,
    )
    assert app.pending_approval is not None and app.pending_approval["request_id"] == "appr-1"
    _refresh_views(app)
    assert app.pending_approval is not None and app.pending_approval["request_id"] == "appr-1"
    app._render_frame()
    assert any("Tool approval required" in row for row in terminal.frames[-1]), terminal.frames[-1]

    app.pending_approval = None
    app._handle_single_event(
        _event(
            2,
            "kernel.confirmation_requested",
            request_id="conf-1",
            owner="goal",
            command_name="goal.set",
            arguments={},
            ui_type="confirmation",
            ui_data={"request_id": "conf-1", "message": "Apply goal?"},
        ),
        rows,
    )
    assert isinstance(app.active_overlay, InteractiveConfirmation)
    _refresh_views(app)
    assert isinstance(app.active_overlay, InteractiveConfirmation)
    app._render_frame()
    assert any("Apply goal?" in row for row in terminal.frames[-1]), terminal.frames[-1]


def test_core_help_receipt_renders_multiline_and_clears_dispatching() -> None:
    """Bare /help is the core command; its direct receipt must become the visible list."""
    from voidcode.kernel.tui.terminal_app import KernelTuiApp

    catalog = [
        {
            "name": "help",
            "owner": "core",
            "description": "Show available commands and documentation",
            "effects": ["read"],
            "argument_schema": {"type": "object", "properties": {}},
        }
    ]
    client = _CommandClient(
        catalog=catalog,
        results={
            "help": {
                "status": "ok",
                "output": "Available commands:\n  /status — Show kernel and workspace status",
                "data": {"commands": [{"name": "status"}]},
            }
        },
    )
    terminal = _StubTerminal()
    app = KernelTuiApp(client, session_id="s1", terminal=terminal)

    app._dispatch_slash_command("/help")
    assert app.status_line == "Dispatching /help..."
    _drain_until(app, terminal, "Available commands:")
    assert any("/status" in row for row in terminal.committed), terminal.committed
    assert all("\\x0a" not in row for row in terminal.committed), terminal.committed
    assert app.status_line == "/help: ok"
    assert [entry[1] for entry in client.invoked] == ["help"]


def test_core_data_receipt_preserves_empty_mapping_and_falsy_values() -> None:
    """A data-only core receipt falls back to JSON without truthiness loss."""
    from voidcode.kernel.tui.terminal_app import KernelTuiApp

    catalog = [
        {
            "name": "show",
            "owner": "core",
            "description": "Show session state",
            "effects": ["read"],
            "argument_schema": {"type": "object", "properties": {}},
        }
    ]
    client = _CommandClient(
        catalog=catalog,
        results={"show": {"status": "ok", "data": {"empty": {}, "turn": 0, "ok": False, "none": None}}},
    )
    terminal = _StubTerminal()
    app = KernelTuiApp(client, session_id="s1", terminal=terminal)

    app._dispatch_slash_command("/show")
    _drain_until(app, terminal, '"empty"')
    text = "\n".join(terminal.committed)
    for expected in ('"empty": {}', '"turn": 0', '"ok": false', '"none": null'):
        assert expected in text, (expected, text)
    assert app.status_line == "/show: ok"


def test_plugin_command_event_renders_multiple_lines_with_error_precedence() -> None:
    """Plugin results arrive as durable events; lines stay lines and error wins."""
    from voidcode.kernel.tui.terminal_app import KernelTuiApp

    terminal = _StubTerminal()
    app = KernelTuiApp(_CommandClient(), session_id="s1", terminal=terminal)
    app._stream_ready = True

    app.event_queue.put(
        {
            "kind": "event",
            "event": _event(
                1,
                "kernel.command_completed",
                name="usage",
                result={"status": "ok", "output": "turns: 0\ninput tokens: 0"},
            ),
            "session_id": "s1",
            "generation": 0,
        }
    )
    app._drain_events()
    assert any("turns: 0" in row for row in terminal.committed), terminal.committed
    assert any("input tokens: 0" in row for row in terminal.committed), terminal.committed
    assert all("\\x0a" not in row for row in terminal.committed), terminal.committed

    app.event_queue.put(
        {
            "kind": "event",
            "event": _event(
                2,
                "kernel.command_completed",
                name="broken",
                result={"status": "error", "error": "boom", "output": "partial"},
            ),
            "session_id": "s1",
            "generation": 0,
        }
    )
    app._drain_events()
    assert any("boom" in row for row in terminal.committed), terminal.committed
    assert not any("partial" in row for row in terminal.committed), terminal.committed


def test_late_completion_does_not_overwrite_gate_or_fence_status() -> None:
    """A command finishing late may clear Dispatching, never a live gate/fence."""
    from voidcode.kernel.tui.terminal_app import KernelTuiApp

    terminal = _StubTerminal()
    app = KernelTuiApp(_CommandClient(), session_id="s1", terminal=terminal)

    app.pending_approval = {"request_id": "appr-1", "tool_name": "write", "arguments": {}}
    app.status_line = "Tool approval required: write"
    app.event_queue.put(
        {
            "kind": "command_done",
            "name": "status",
            "status": "ok",
            "result": None,
            "session_id": "s1",
            "generation": 0,
        }
    )
    app._drain_events()
    assert app.status_line == "Tool approval required: write"

    app.pending_approval = None
    app.active_overlay = None
    app.upgrade = {"fenced": True}
    app.status_line = "Kernel upgrade in progress: dispatch paused"
    app.event_queue.put(
        {
            "kind": "command_done",
            "name": "status",
            "status": "ok",
            "result": None,
            "session_id": "s1",
            "generation": 0,
        }
    )
    app._drain_events()
    assert app.status_line == "Kernel upgrade in progress: dispatch paused"


def test_run_failure_error_is_rendered_once_from_the_response_path() -> None:
    """The run response owns the error text; the durable event must not repeat it."""
    from voidcode.kernel.tui.terminal_app import KernelTuiApp

    terminal = _StubTerminal()
    app = KernelTuiApp(_RunClient(), session_id="s1", terminal=terminal)
    app._stream_ready = True

    app._dispatch_user_prompt("hello")
    _drain_until(app, terminal, "boom")
    assert sum("boom" in row for row in terminal.committed) == 1

    app.event_queue.put(
        {
            "kind": "event",
            "event": _event(1, "kernel.run_failed", error="boom", logical_turn="turn-1"),
            "session_id": "s1",
            "generation": 0,
        }
    )
    app._drain_events()
    assert sum("boom" in row for row in terminal.committed) == 1, terminal.committed
