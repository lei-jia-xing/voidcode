"""Unit tests for the generic core TUI client and 8 finite views."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import Any, Literal

import pytest

from voidcode.sdk.client import (
    CommandCatalog,
    CommandResult,
    CoreClientError,
    EventFrame,
    HeadFrame,
    HealthStatus,
    RestartFrame,
    ResumeSpec,
    RunResult,
    RunSpec,
    SessionFrame,
    SessionRow,
    StreamCancellation,
    StreamEvent,
)
from voidcode.sdk.commands import CommandDescriptor, ConfirmationRequest
from voidcode.sdk.gates import ApprovalGate
from voidcode.sdk.provider import ProviderMessage, ProviderTurnResult
from voidcode.sdk.tools import OutputBounds, TextOutput, ToolCall, ToolEffect, ToolFailure, ToolSuccess
from voidcode.sdk.transcript import TranscriptEntry, TranscriptPage, TranscriptRun, TranscriptTool
from voidcode.sdk.views import (
    ChoiceOption,
    ChoicesViewData,
    ConfirmationViewData,
    FormField,
    FormViewData,
    PluginViewDescriptor,
    ViewsSnapshot,
)
from voidcode.tui.keys import Key
from voidcode.tui.views import (
    InteractiveApproval,
    InteractiveChoices,
    InteractiveConfirmation,
    InteractiveForm,
    render_plugin_view,
)


def _session_row(session_id: str = "s1", status: str = "idle", turn: int = 0) -> SessionRow:
    return SessionRow(
        session_id=session_id,
        workspace="/tmp/ws",
        status=status,
        active_revision_id="rev-1",
        run_id=None,
        turn=turn,
        last_event_sequence=0,
    )


def _health(*, fenced: bool = False) -> HealthStatus:
    return HealthStatus(
        status="ok",
        stage="draining" if fenced else "serving",
        pid=1,
        workspace="/tmp/ws",
        revision_id="rev-1",
        launch_protocol=1,
        core_version="t",
        web_bundle=None,
        generation=2,
        active_upgrade=None,
    )


def _approval_gate(request_id: str = "appr-1", run_id: str = "run-1", tool_name: str = "write") -> ApprovalGate:
    return ApprovalGate(call=ToolCall(tool_name=tool_name, arguments={}), request_id=request_id, run_id=run_id, revision_id="rev-1")


def _event(
    sequence: int,
    event_type: str = "core.user_message",
    run_id: str = "run-1",
    revision_id: str = "rev-1",
    content: Any = None,
    **payload: object,
) -> StreamEvent:
    entry: TranscriptEntry | None = None
    if content is not None:
        entry = TranscriptEntry(sequence, run_id, revision_id, "kernel", content)
    return StreamEvent(
        session_id="s1",
        sequence=sequence,
        type=event_type,
        run_id=run_id,
        revision_id=revision_id,
        source="kernel",
        actor="user",
        payload=dict(payload),
        transcript=entry,
    )


def _user_event(sequence: int, text: str) -> StreamEvent:
    event = _event(sequence, "core.user_message", content=ProviderMessage("user", text))
    return StreamEvent(
        session_id=event.session_id,
        sequence=event.sequence,
        type=event.type,
        run_id=event.run_id,
        revision_id=event.revision_id,
        source=event.source,
        actor=event.actor,
        payload={"content": text},
        transcript=event.transcript,
    )


def test_unsupported_view_raises_error() -> None:
    """A view kind outside the eight finite kinds fails visibly, never silently."""
    with pytest.raises(ValueError, match="bogus"):
        PluginViewDescriptor(owner="o", name="n", title="t", view_type="bogus", data={})  # type: ignore[arg-type]


def test_fields_view_folds_long_cjk_value_without_clipping() -> None:
    """A long unbroken CJK value survives the fields table at 48 columns."""
    import re

    from voidcode.tui.term import visible_width

    value = "将遗留的导入器逐步迁移到新的" + "迁移" * 30 + "终尾"
    descriptor = PluginViewDescriptor(owner="o", name="n", title="t", view_type="fields", data={"rows": [{"label": "Objective", "value": value}]})
    rows = render_plugin_view(descriptor, 48)
    plain = [re.sub(r"\x1b\[[0-9;]*m", "", row) for row in rows]
    assert all(visible_width(row) <= 48 for row in plain), [visible_width(row) for row in plain]
    compact = "".join(char for row in plain for char in row if not char.isspace())
    assert value in compact
    assert "…" not in compact


def test_interactive_choices_navigation_and_selection() -> None:
    choices = InteractiveChoices(
        ChoicesViewData(
            options=(ChoiceOption(id="a", label="Option A"), ChoiceOption(id="b", label="Option B")),
            action="select_target",
            argument_name="target_id",
        )
    )

    # Navigate down
    choices.handle_key(Key("down"))
    assert choices.selected_index == 1

    # Enter selects Option B
    outcome = choices.handle_key(Key("enter"))
    assert outcome == {"action": "select_target", "arguments": {"target_id": "b"}}

    # Esc cancels
    assert choices.handle_key(Key("esc")) == {"cancel": True}

    conf = InteractiveConfirmation(ConfirmationViewData(request_id="req-abc", message="Are you sure?", actions=("allow", "deny")))
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
        FormViewData(
            fields=(
                FormField(name="title", label="Title", field_type="text", required=True),
                FormField(name="count", label="Count", field_type="integer", required=False),
                FormField(name="enabled", label="Enabled", field_type="boolean", required=False),
            ),
            action="save_config",
            initial_values={"title": "Init", "count": 5, "enabled": False},
        )
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

    form = InteractiveForm(
        FormViewData(
            fields=(
                FormField(name="budget", label="Budget", field_type="integer", required=False),
                FormField(name="ratio", label="Ratio", field_type="number", required=False),
            ),
            action="set_limits",
        )
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

    form = InteractiveForm(
        FormViewData(
            fields=(FormField(name="req_field", label="Required Field", field_type="text", required=True),),
            action="create_item",
        )
    )
    # Submit with empty value should fail validation and return None
    outcome = form.handle_key(Key("ctrl+s"))
    assert outcome is None
    assert form.validation_error

    form = InteractiveForm(
        FormViewData(
            fields=(
                FormField(name="desc", label="Description", field_type="text"),
                FormField(name="notes", label="Notes", field_type="text"),
            ),
            action="edit_item",
            load_action="get_item",
        )
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
    form = InteractiveForm(
        FormViewData(
            fields=(FormField(name="budget", label="Budget", field_type="integer", required=False),),
            action="set_nullable",
            initial_values={"budget": 42},
        ),
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
        FormViewData(
            fields=(
                FormField(name="flag", label="Flag", field_type="boolean", required=False),
                FormField(name="maybe", label="Maybe", field_type="boolean", required=False),
            ),
            action="set_flags",
        ),
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
        FormViewData(
            fields=(FormField(name="budget", label="Budget", field_type="integer", required=True),),
            action="set_budget",
        ),
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
        FormViewData(
            fields=(FormField(name="flag", label="Flag", field_type="boolean", required=True),),
            action="set_flag",
        )
    )
    assert form.handle_key(Key("ctrl+s")) is None
    assert form.validation_error


def test_form_optional_non_nullable_blank_is_omitted() -> None:
    form = InteractiveForm(
        FormViewData(
            fields=(FormField(name="ratio", label="Ratio", field_type="number", required=False),),
            action="set_limits",
        ),
        action_schema={"type": "object", "properties": {"ratio": {"type": "number"}}},
    )
    assert form.handle_key(Key("ctrl+s")) == {"action": "set_limits", "arguments": {}}


def test_form_preserves_typed_text_verbatim() -> None:
    """Non-empty text keeps its whitespace; only blanks are normalised."""
    form = InteractiveForm(
        FormViewData(
            fields=(FormField(name="summary", label="Summary", field_type="multiline", required=True),),
            action="submit_form",
        )
    )
    form.values["summary"] = "  keep my spacing  \n"
    outcome = form.handle_key(Key("ctrl+s"))
    assert outcome == {"action": "submit_form", "arguments": {"summary": "  keep my spacing  \n"}}


class _StubTerminal:
    """Commit sink only: records settled rows, never touches a real terminal."""

    width = 100
    eof = False
    color_system = None

    def query_default_background_rgb(self) -> None:
        return None

    def __init__(self) -> None:
        self.committed: list[str] = []
        self.frames: list[list[str]] = []

    def read_bytes(self, timeout: float = 0.0) -> bytes:
        return b""

    def commit_rows(self, rows: list[str]) -> None:
        self.committed.extend(rows)

    def paint_frame(self, frame: Any) -> None:
        self.frames.append(list(frame.rows))


class _StubClient:
    """A collaborator that behaves like the server surface the app consumes.

    Connections are phased: the first exposes only the pre-restart prefix,
    then closes with the restart watermark; later connections filter the FULL
    durable log by `after_sequence` exactly like `core.events_after`. A
    worker that adopted a lying watermark fetches past the unseen rows and
    they never reach the commit sink — the loss is consumer-visible.
    """

    def __init__(self) -> None:
        self.health_calls = 0
        self.prefix_events: list[StreamEvent] = []
        self.full_events: list[StreamEvent] = []
        self.restart_cursor = 0
        self._connections = 0
        self._stop: Any = None

    def health(self) -> HealthStatus:
        self.health_calls += 1
        return _health()

    def command_catalog(self) -> CommandCatalog:
        return CommandCatalog(revision_id="rev-1", commands=())

    def views(self, session_id: str) -> ViewsSnapshot:
        return ViewsSnapshot(revision_id="rev-1", views=())

    def get_session(self, session_id: str) -> SessionRow:
        return _session_row(session_id)

    def transcript(self, session_id: str, *, after_sequence: int = 0, limit: int = 100) -> TranscriptPage:
        del session_id
        ordered = sorted(self.full_events or self.prefix_events, key=lambda ev: ev.sequence)
        window = [ev for ev in ordered if ev.sequence > after_sequence][:limit]
        entries = tuple(ev.transcript for ev in window if ev.transcript is not None)
        consumed = len(window)
        nxt = window[-1].sequence if window else after_sequence
        remaining = [ev for ev in ordered if ev.sequence > nxt]
        return TranscriptPage(entries=entries, next_sequence=nxt, has_more=bool(remaining), source_events=consumed)

    async def events_stream(
        self,
        session_id: str,
        after_sequence: int = 0,
        *,
        follow: bool = True,
        cancel: StreamCancellation | None = None,
    ) -> AsyncIterator[SessionFrame]:
        self._connections += 1
        if self._connections == 1:
            # Pre-restart connection: only the settled prefix exists yet.
            backlog = [ev for ev in self.prefix_events if ev.sequence > after_sequence]
            if backlog:
                yield HeadFrame(session=_session_row(session_id), events=tuple(backlog))
            yield RestartFrame(session_id=session_id, cursor=self.restart_cursor)
            return
        # Post-restart connections: the full log, filtered by the resume cursor.
        backlog = [ev for ev in self.full_events if ev.sequence > after_sequence]
        if backlog:
            yield HeadFrame(session=_session_row(session_id), events=tuple(backlog))
        # Hold the connection open like a live follow stream until the test
        # stops the worker (never hot-loop on instant EOF).
        import asyncio as _asyncio

        while self._stop is None or not self._stop.is_set():
            await _asyncio.sleep(0.01)


class _FencedClient(_StubClient):
    """Every business mutation answers the contracted fenced refusal."""

    def __init__(self) -> None:
        super().__init__()
        self._refusal = CoreClientError("core is draining for an upgrade; retry after it restarts", status_code=503, code="upgrade_pending")

    def resume(self, session_id: str, spec: ResumeSpec | None = None) -> RunResult:
        raise self._refusal

    def confirm(self, session_id: str, request_id: str, decision: Literal["allow", "deny"], expected_revision: str = "") -> CommandResult:
        raise self._refusal


class _CommandClient(_StubClient):
    """Catalog/views/invoke stub for the consumer-visible command-result flows."""

    def __init__(
        self,
        *,
        catalog: list[CommandDescriptor] | None = None,
        view_list: list[PluginViewDescriptor] | None = None,
        results: dict[str, CommandResult] | None = None,
    ) -> None:
        super().__init__()
        self.catalog = list(catalog or [])
        self.view_list = list(view_list or [])
        self.results = dict(results or {})
        self.invoked: list[tuple[str, str, dict[str, object], str]] = []

    def command_catalog(self) -> CommandCatalog:
        return CommandCatalog(revision_id="rev-1", commands=tuple(self.catalog))

    def list_sessions(self, limit: int = 20) -> list[SessionRow]:
        return [_session_row("s1")]

    def views(self, session_id: str) -> ViewsSnapshot:
        return ViewsSnapshot(revision_id="rev-1", views=tuple(self.view_list))

    def invoke_command(
        self,
        session_id: str,
        name: str,
        arguments: Mapping[str, object] | None = None,
        expected_revision: str = "",
    ) -> CommandResult:
        self.invoked.append((session_id, name, dict(arguments or {}), expected_revision))
        return self.results.get(name, CommandResult(status="ok"))


class _RunClient(_CommandClient):
    """Run returns the existing failed result; the durable event repeats the error."""

    def run(self, session_id: str, spec: RunSpec) -> RunResult:
        return RunResult(session_id=session_id, run_id="run-1", revision_id="rev-1", status="failed", error="boom")


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


def _refresh_connection(app: Any, *, timeout: float = 5.0) -> None:
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
    from voidcode.tui.terminal_app import CoreTuiApp

    client = _FencedClient()
    client.prefix_events = [_user_event(1, "one"), _user_event(2, "two")]
    client.full_events = [
        _user_event(1, "one"),
        _user_event(2, "two"),
        _user_event(3, "three"),
        _user_event(4, "four"),
    ]
    client.restart_cursor = 99
    terminal = _StubTerminal()
    app = CoreTuiApp(client, session_id="s1", terminal=terminal)  # type: ignore[arg-type]
    client._stop = app._stop_event
    # No reconcile thread in this cursor-specific test: stub the async refresh
    # so the join below covers only the owned stream worker.
    app._reconcile_connection_async = lambda: None  # type: ignore[method-assign]
    app._start_event_stream()
    assert app._stream_thread is not None
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
        app._stream_thread.join(timeout=5.0)
        assert not app._stream_thread.is_alive()


def test_real_503_handlers_keep_both_gates_then_decided_facts_close_each_once() -> None:
    """Real app handlers + a 503 collaborator: the gates stay actionable with
    the same ids/revision, then the canonical decided facts close each gate
    exactly once. A late acceptance for an OLD id must not close a NEW gate.

    This fails on the old early-clear implementation (gates are gone before the
    worker answers) and passes now.
    """
    import time

    from voidcode.tui.terminal_app import CoreTuiApp
    from voidcode.tui.views import InteractiveConfirmation

    client = _FencedClient()
    terminal = _StubTerminal()
    app = CoreTuiApp(client, session_id="s1", terminal=terminal)  # type: ignore[arg-type]
    app._session_cursors["s1"] = 10
    app.pending_approval = InteractiveApproval(_approval_gate("appr-1"))
    app.active_overlay = InteractiveConfirmation(ConfirmationViewData(request_id="conf-9", message="Confirm?"), revision_id="rev-1")
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
    assert app.pending_approval is not None and app.pending_approval.request_id == "appr-1"
    assert isinstance(app.active_overlay, InteractiveConfirmation) and app.active_overlay.request_id == "conf-9"
    assert app.active_overlay.revision_id == "rev-1"
    assert "appr-1" not in app._in_flight and "conf-9" not in app._in_flight
    # A NEW approval parks while the old gate is still mounted; a late
    # acceptance envelope for the OLD id must not close the new gate.
    app.pending_approval = InteractiveApproval(_approval_gate("appr-2"))
    app.event_queue.put(
        {"kind": "approval_accepted", "request_id": "appr-1", "decision": "allow", "status": "completed", "generation": 0, "session_id": "s1"}
    )
    app._drain_events()
    assert app.pending_approval is not None and app.pending_approval.request_id == "appr-2"
    # The canonical decided facts close each gate exactly once.
    app.event_queue.put(
        {
            "kind": "stream_event",
            "frame": EventFrame(session_id="s1", event=_event(11, "core.approval_decided", decision="allow", request_id="appr-2")),
            "generation": 0,
            "session_id": "s1",
        }
    )
    app.event_queue.put(
        {
            "kind": "stream_event",
            "frame": EventFrame(session_id="s1", event=_event(12, "core.confirmation_decided", request_id="conf-9")),
            "generation": 0,
            "session_id": "s1",
        }
    )
    app._drain_events()
    assert app.pending_approval is None
    assert "conf-9" in app._gates.decided
    assert app.active_overlay is None
    # Replayed decided facts never resurrect either gate.
    app.event_queue.put(
        {
            "kind": "stream_event",
            "frame": EventFrame(session_id="s1", event=_event(12, "core.confirmation_decided", request_id="conf-9")),
            "generation": 0,
            "session_id": "s1",
        }
    )
    app._drain_events()
    assert app.active_overlay is None
    assert list(app.queued_confirmations) == []


def test_automatic_refresh_is_silent_until_views_are_explicitly_requested() -> None:
    """Reconnect refreshes only the catalog/status; optional views stay on demand."""
    from voidcode.tui.terminal_app import CoreTuiApp
    from voidcode.tui.views import InteractiveForm

    status_view = PluginViewDescriptor(owner="goal", name="goal", title="Goal", view_type="status", data={"label": "Goal", "value": "No active goal"})
    form_view = PluginViewDescriptor(
        owner="goal",
        name="goal.form",
        title="Set Goal",
        view_type="form",
        data={
            "fields": [{"name": "objective", "label": "Objective", "field_type": "multiline", "required": True}],
            "action": "goal.set",
            "initial_values": {},
        },
    )
    client = _CommandClient(view_list=[status_view, form_view])
    terminal = _StubTerminal()
    app = CoreTuiApp(client, session_id="s1", terminal=terminal)

    _refresh_connection(app)
    app._render_frame()
    assert app.active_overlay is None
    assert not any("No active goal" in row for row in terminal.committed)
    app._handle_composer_key(Key("x", text="x"))
    assert app.composer.value == "x"

    app._show_views()
    app._render_frame()
    assert isinstance(app.active_overlay, InteractiveForm)
    assert any("No active goal" in row for row in terminal.committed)
    assert any("Objective" in row for row in terminal.frames[-1]), terminal.frames[-1]


def test_activation_refresh_is_silent_for_optional_views() -> None:
    """Activation refreshes the catalog only: no passive row, no form, no revision id."""
    from voidcode.tui.terminal_app import CoreTuiApp

    status_view = PluginViewDescriptor(owner="goal", name="goal", title="Goal", view_type="status", data={"label": "Goal", "value": "No active goal"})
    form_view = PluginViewDescriptor(
        owner="goal",
        name="goal.form",
        title="Set Goal",
        view_type="form",
        data={
            "fields": [{"name": "objective", "label": "Objective", "field_type": "multiline", "required": True}],
            "action": "goal.set",
            "initial_values": {},
        },
    )
    client = _CommandClient(view_list=[status_view, form_view])
    terminal = _StubTerminal()
    app = CoreTuiApp(client, session_id="s1", terminal=terminal)

    rows: list[str] = []
    app._handle_stream_event(_event(1, "core.activation_committed", to_revision_id="rev-2"), rows)
    app._render_frame()
    assert app.active_overlay is None
    assert not any("No active goal" in row for row in rows)
    assert not any("rev-2" in row for row in rows), rows
    assert not any("Objective" in row for row in terminal.frames[-1]), terminal.frames[-1]


def test_idle_chrome_shows_status_segments() -> None:
    """Idle chrome shows session/run-state segments in the status dock."""
    from voidcode.tui.terminal_app import CoreTuiApp

    client = _CommandClient()
    terminal = _StubTerminal()
    app = CoreTuiApp(client, terminal=terminal)

    app._init_session()
    assert app.session_id == "s1"
    assert app.status_line == ""
    app._render_frame()
    frame_text = "\n".join(terminal.frames[-1])
    assert "s1" in frame_text
    assert "idle" in frame_text
    assert "voidcode-dark" not in frame_text

    app._show_sessions()
    assert any("s1" in row for row in terminal.committed), terminal.committed


def test_durable_gates_survive_automatic_view_refresh() -> None:
    """Approval/confirmation gates come from durable facts, not views, and stay put."""
    from voidcode.tui.terminal_app import CoreTuiApp
    from voidcode.tui.views import InteractiveConfirmation

    status_view = PluginViewDescriptor(owner="goal", name="goal", title="Goal", view_type="status", data={"label": "Goal", "value": "No active goal"})
    client = _CommandClient(view_list=[status_view])
    terminal = _StubTerminal()
    app = CoreTuiApp(client, session_id="s1", terminal=terminal)
    rows: list[str] = []

    app._handle_stream_event(
        _event(1, "core.approval_requested", request_id="appr-1", call={"tool_name": "write", "arguments": {}}),
        rows,
    )
    assert app.pending_approval is not None and app.pending_approval.request_id == "appr-1"
    _refresh_connection(app)
    assert app.pending_approval is not None and app.pending_approval.request_id == "appr-1"
    app._render_frame()
    assert any("Tool approval required" in row for row in terminal.frames[-1]), terminal.frames[-1]

    app.pending_approval = None
    app._handle_stream_event(
        _event(
            2,
            "core.confirmation_requested",
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
    _refresh_connection(app)
    assert isinstance(app.active_overlay, InteractiveConfirmation)
    app._render_frame()
    assert any("Apply goal?" in row for row in terminal.frames[-1]), terminal.frames[-1]


def test_core_help_receipt_renders_multiline_and_clears_dispatching() -> None:
    """Bare /help is the core command; its direct receipt must become the visible list."""
    from voidcode.tui.terminal_app import CoreTuiApp

    catalog = [
        CommandDescriptor(
            name="help",
            owner="core",
            description="Show available commands and documentation",
            argument_schema={"type": "object", "properties": {}},
            effects=(ToolEffect.READ,),
        )
    ]
    client = _CommandClient(
        catalog=catalog,
        results={
            "help": CommandResult(
                status="ok",
                output="Available commands:\n  /status — Show core and workspace status",
                data={"commands": [{"name": "status"}]},
            )
        },
    )
    terminal = _StubTerminal()
    app = CoreTuiApp(client, session_id="s1", terminal=terminal)

    app._dispatch_slash_command("/help")
    assert app.status_line == "Dispatching /help..."
    _drain_until(app, terminal, "Available commands:")
    assert any("/status" in row for row in terminal.committed), terminal.committed
    assert all("\\x0a" not in row for row in terminal.committed), terminal.committed
    assert app.status_line == "/help: ok"
    assert [entry[1] for entry in client.invoked] == ["help"]


def test_core_data_receipt_preserves_empty_mapping_and_falsy_values() -> None:
    """A data-only core receipt falls back to JSON without truthiness loss."""
    from voidcode.tui.terminal_app import CoreTuiApp

    catalog = [
        CommandDescriptor(
            name="show",
            owner="core",
            description="Show session state",
            argument_schema={"type": "object", "properties": {}},
            effects=(ToolEffect.READ,),
        )
    ]
    client = _CommandClient(
        catalog=catalog,
        results={"show": CommandResult(status="ok", data={"empty": {}, "turn": 0, "ok": False, "none": None})},
    )
    terminal = _StubTerminal()
    app = CoreTuiApp(client, session_id="s1", terminal=terminal)

    app._dispatch_slash_command("/show")
    _drain_until(app, terminal, '"empty"')
    text = "\n".join(terminal.committed)
    for expected in ('"empty": {}', '"turn": 0', '"ok": false', '"none": null'):
        assert expected in text, (expected, text)
    assert app.status_line == "/show: ok"


def test_long_settled_command_result_wraps_without_losing_content() -> None:
    """A long ANSI/CJK/newline command result reaches scrollback whole at 48 columns."""
    import re
    import tempfile

    from voidcode.tui.term import Terminal, visible_width
    from voidcode.tui.terminal_app import CoreTuiApp

    payload = "长" * 60 + "\nTAIL-END"
    with tempfile.TemporaryFile() as capture:
        terminal = Terminal(output_fd=capture.fileno(), is_tty=False, size=(48, 30), color_system=None)
        app = CoreTuiApp(_CommandClient(), session_id="s1", terminal=terminal)
        app._stream_ready = True
        app.event_queue.put(
            {
                "kind": "stream_event",
                "frame": EventFrame(
                    session_id="s1",
                    event=_event(1, "core.command_completed", name="providers", result={"status": "ok", "output": payload}),
                ),
                "session_id": "s1",
                "generation": 0,
            }
        )
        app._drain_events()
        terminal.close()
        capture.seek(0)
        raw = capture.read().decode("utf-8")

    plain_rows = [re.sub(r"\x1b\[[0-9;]*m", "", row) for row in raw.splitlines()]
    assert all(visible_width(row) <= 48 for row in plain_rows), [visible_width(row) for row in plain_rows]
    settled = "".join(row.rstrip() for row in plain_rows)
    assert settled.count("长") == 60
    assert "TAIL-END" in settled


def test_plugin_command_event_renders_multiple_lines_with_error_precedence() -> None:
    """Plugin results arrive as durable events; lines stay lines and error wins."""
    from voidcode.tui.terminal_app import CoreTuiApp

    terminal = _StubTerminal()
    app = CoreTuiApp(_CommandClient(), session_id="s1", terminal=terminal)
    app._stream_ready = True

    app.event_queue.put(
        {
            "kind": "stream_event",
            "frame": EventFrame(
                session_id="s1",
                event=_event(
                    1,
                    "core.command_completed",
                    name="usage",
                    result={"status": "ok", "output": "turns: 0\ninput tokens: 0"},
                ),
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
            "kind": "stream_event",
            "frame": EventFrame(
                session_id="s1",
                event=_event(
                    2,
                    "core.command_completed",
                    name="broken",
                    result={"status": "error", "error": "boom", "output": "partial"},
                ),
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
    from voidcode.tui.terminal_app import CoreTuiApp

    terminal = _StubTerminal()
    app = CoreTuiApp(_CommandClient(), session_id="s1", terminal=terminal)

    app.pending_approval = InteractiveApproval(_approval_gate("appr-1"))
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
    app.upgrade = _health(fenced=True)
    app.status_line = "Core upgrade in progress: dispatch paused"
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
    assert app.status_line == "Core upgrade in progress: dispatch paused"


def test_run_failure_error_is_rendered_once_from_the_response_path() -> None:
    """The run response owns the error text; the durable event must not repeat it."""
    from voidcode.tui.terminal_app import CoreTuiApp

    terminal = _StubTerminal()
    app = CoreTuiApp(_RunClient(), session_id="s1", terminal=terminal)
    app._stream_ready = True

    app._dispatch_user_prompt("hello")
    _drain_until(app, terminal, "boom")
    assert sum("boom" in row for row in terminal.committed) == 1

    app.event_queue.put(
        {
            "kind": "stream_event",
            "frame": EventFrame(session_id="s1", event=_event(1, "core.run_failed", error="boom", logical_turn="turn-1")),
            "session_id": "s1",
            "generation": 0,
        }
    )
    app._drain_events()
    assert sum("boom" in row for row in terminal.committed) == 1, terminal.committed


def test_rows_carry_no_hardcoded_colors() -> None:
    import re

    from voidcode.tui.terminal_app import CoreTuiApp

    terminal = _StubTerminal()
    app = CoreTuiApp(_CommandClient(), session_id="s1", terminal=terminal)
    app._stream_ready = True
    rows: list[str] = []
    app._handle_stream_event(_user_event(1, "hi"), rows)
    app._handle_stream_event(_event(2, "core.model_turn", content=ProviderTurnResult(output="hello", reasoning="why"), dispatch_sequence=2), rows)
    app._handle_stream_event(_event(3, "core.run_failed", content=TranscriptRun("failed"), error="boom"), rows)
    # color_system=None strips every SGR code: a hardcoded escape would survive the strip.
    assert not any("\x1b" in row for row in rows), rows
    plain = [re.sub(r"\x1b\[[0-9;]*m", "", row) for row in rows]
    assert any("User>" in row for row in plain)
    assert any("hello" in row for row in plain)
    assert any("Reasoning>" in row for row in plain)
    assert any("Run failed" in row for row in plain)


def test_model_turn_renders_markdown_and_structured_tool_rows() -> None:
    import re

    from voidcode.tui.terminal_app import CoreTuiApp

    terminal = _StubTerminal()
    app = CoreTuiApp(_CommandClient(), session_id="s1", terminal=terminal)
    rows: list[str] = []
    app._handle_stream_event(
        _event(
            1,
            "core.model_turn",
            content=ProviderTurnResult(
                output="# Title\n\nSome **bold** text",
                tool_calls=(ToolCall(tool_name="read", arguments={"path": "a.txt"}),),
            ),
            dispatch_sequence=1,
        ),
        rows,
    )
    app._handle_stream_event(
        _event(
            2,
            "core.tool_completed",
            content=TranscriptTool(
                ToolCall(tool_name="read", arguments={"path": "a.txt"}),
                ToolSuccess(tool_name="read", output=TextOutput(text="file body", bounds=OutputBounds(truncated=True, source="read:42"))),
                interrupted=False,
                batch_sequence=1,
                deferred=False,
            ),
        ),
        rows,
    )
    plain = [re.sub(r"\x1b\[[0-9;]*m", "", row) for row in rows]
    text = "\n".join(plain)
    assert "Title" in text and "bold" in text
    assert any("> Tool call read" in row for row in plain)
    assert any('"path"' in row for row in plain)
    assert any("[ok] Tool read" in row for row in plain)
    assert any("file body" in row for row in plain)
    assert any("truncated" in row for row in plain)


def test_tool_result_prefers_presentation_and_flags_failure() -> None:
    import re

    from voidcode.tui.terminal_app import CoreTuiApp

    terminal = _StubTerminal()
    app = CoreTuiApp(_CommandClient(), session_id="s1", terminal=terminal)
    rows: list[str] = []
    app._handle_stream_event(
        _event(
            1,
            "core.tool_completed",
            content=TranscriptTool(
                ToolCall(tool_name="exec", arguments={}),
                ToolFailure(tool_name="exec", error="exit 1", output=TextOutput(text="raw")),
                interrupted=False,
                batch_sequence=1,
                deferred=False,
            ),
        ),
        rows,
    )
    app._handle_stream_event(
        _event(
            2,
            "core.tool_completed",
            content=TranscriptTool(
                ToolCall(tool_name="read", arguments={}),
                ToolSuccess(tool_name="read", output=TextOutput(text="raw bytes", presentation="Image read successfully")),
                interrupted=False,
                batch_sequence=1,
                deferred=False,
            ),
        ),
        rows,
    )
    plain = [re.sub(r"\x1b\[[0-9;]*m", "", row) for row in rows]
    text = "\n".join(plain)
    assert "[failed] Tool exec" in text and "exit 1" in text
    assert "Image read successfully" in text and "raw bytes" not in text


def test_approval_gate_shows_full_args_with_diff_and_scrolls() -> None:
    import re

    from voidcode.tui.terminal_app import CoreTuiApp

    terminal = _StubTerminal()
    app = CoreTuiApp(_CommandClient(), session_id="s1", terminal=terminal)
    rows: list[str] = []
    long_value = "x" * 300
    app._handle_stream_event(
        _event(
            1,
            "core.approval_requested",
            request_id="appr-9",
            call={"tool_name": "edit", "arguments": {"path": "a.py", "oldString": "old", "newString": "new", "note": long_value}},
        ),
        rows,
    )
    assert app.pending_approval is not None
    app._render_frame()
    plain = [re.sub(r"\x1b\[[0-9;]*m", "", row) for row in terminal.frames[-1]]
    text = "".join(row.rstrip() for row in plain)
    # Nothing truncated: the 300-char argument and both diff sides are visible.
    assert long_value in text
    assert "old" in text and "new" in text
    assert "a.py" in text

    first = list(terminal.frames[-1])
    app.pending_approval.handle_key(Key("down"))
    app.pending_approval.handle_key(Key("down"))
    app._render_frame()
    assert terminal.frames[-1] != first
    assert any("allow" in row.lower() for row in terminal.frames[-1])


def test_approval_keys_decide_without_truncation_path() -> None:
    gate = ApprovalGate(call=ToolCall(tool_name="exec", arguments={"cmd": "ls"}), request_id="r1", run_id="run-1", revision_id="rev-1")
    approval = InteractiveApproval(gate)
    assert approval.handle_key(Key("a")) == {"request_id": "r1", "decision": "allow"}
    assert approval.handle_key(Key("enter")) == {"request_id": "r1", "decision": "allow"}
    assert approval.handle_key(Key("d")) == {"request_id": "r1", "decision": "deny"}
    assert approval.handle_key(Key("esc")) == {"request_id": "r1", "decision": "deny"}
    assert approval.handle_key(Key("down")) is None
    assert approval.scroll == 1


def test_approval_multi_edit_preview_and_nested_json_survive_owned_mappings() -> None:
    """Owned tuples render as JSON arrays with a joined multi-edit diff, never mappingproxy repr."""
    import re

    gate = ApprovalGate(
        call=ToolCall(
            tool_name="edit",
            arguments={
                "path": "a.py",
                "edits": ({"oldString": "old-one", "newString": "new-one"}, {"oldString": "old-two", "newString": "new-two"}),
                "nested": {"items": (1, {"deep": (True, None)}), "empty": {}},
            },
        ),
        request_id="r-owned",
        run_id="run-1",
        revision_id="rev-1",
    )
    approval = InteractiveApproval(gate)
    rows = approval.render_rows(100)
    plain = [re.sub(r"\x1b\[[0-9;]*m", "", row) for row in rows]
    text = "\n".join(plain)
    assert "old-one" in text and "new-two" in text
    assert '"deep"' in text and "mappingproxy" not in text and "mappingproxy" not in repr(text)
    assert '"items": [\n' in text or '"items": ["' in text or '"items"' in text


def test_provider_progress_buffers_partial_text_and_spinner_advances() -> None:
    """Progress fragments render as partial markdown in the live frame, never as rows."""
    import re

    from voidcode.tui.terminal_app import CoreTuiApp

    terminal = _StubTerminal()
    app = CoreTuiApp(_CommandClient(), session_id="s1", terminal=terminal)
    rows: list[str] = []
    app._handle_stream_event(
        _event(1, "core.provider_progress", run_id="r", dispatch_id="d1", dispatch_sequence=1, channel="text", text_delta="Hello "),
        rows,
    )
    app._handle_stream_event(
        _event(2, "core.provider_progress", run_id="r", dispatch_id="d1", dispatch_sequence=1, channel="text", text_delta="world"),
        rows,
    )
    assert rows == []
    app._render_frame()
    plain = [re.sub(r"\x1b\[[0-9;]*m", "", row) for row in terminal.frames[-1]]
    assert any("Hello world" in row for row in plain)
    first = list(terminal.frames[-1])
    app._render_frame()
    # The spinner advances every frame while a run is in flight.
    assert terminal.frames[-1] != first


def test_model_turn_settles_stream_buffer_to_committed_markdown() -> None:
    """The committed turn replaces the live buffer: rows settle once, the live region clears."""
    import re

    from voidcode.tui.terminal_app import CoreTuiApp

    terminal = _StubTerminal()
    app = CoreTuiApp(_CommandClient(), session_id="s1", terminal=terminal)
    rows: list[str] = []
    app._handle_stream_event(
        _event(1, "core.provider_progress", run_id="r", dispatch_id="d1", dispatch_sequence=1, channel="text", text_delta="partial"),
        rows,
    )
    app._handle_stream_event(
        _event(2, "core.model_turn", content=ProviderTurnResult(output="final answer"), dispatch_sequence=2, dispatch_id="d1"),
        rows,
    )
    assert any("final answer" in row for row in (" ".join(re.sub(r"\x1b\[[0-9;]*m", "", row) for row in rows)).split("\n"))
    assert app._stream_dispatch is None
    app._render_frame()
    plain = [re.sub(r"\x1b\[[0-9;]*m", "", row) for row in terminal.frames[-1]]
    assert not any("partial" in row for row in plain)


def test_reasoning_progress_does_not_pollute_the_text_buffer() -> None:
    """Reasoning fragments buffer separately; the live region shows answer text only."""
    import re

    from voidcode.tui.terminal_app import CoreTuiApp

    terminal = _StubTerminal()
    app = CoreTuiApp(_CommandClient(), session_id="s1", terminal=terminal)
    rows: list[str] = []
    app._handle_stream_event(
        _event(1, "core.provider_progress", run_id="r", dispatch_id="d1", dispatch_sequence=1, channel="reasoning", text_delta="secret plan"),
        rows,
    )
    app._handle_stream_event(
        _event(2, "core.provider_progress", run_id="r", dispatch_id="d1", dispatch_sequence=1, channel="text", text_delta="visible"),
        rows,
    )
    app._render_frame()
    plain = [re.sub(r"\x1b\[[0-9;]*m", "", row) for row in terminal.frames[-1]]
    text = "\n".join(plain)
    assert "visible" in text
    assert "secret plan" not in text


def test_terminated_run_cannot_resurrect_then_newer_run_keeps_its_gate() -> None:
    """A late pending receipt for a dead run never reopens; an old run end keeps a newer gate."""
    from voidcode.sdk.gates import ApprovalRequested, GateState, RunTerminated, reduce_gates

    first = _approval_gate("appr-old", run_id="run-old")
    state = reduce_gates(GateState(), ApprovalRequested(first))
    assert state.approval is not None and state.approval.request_id == "appr-old"
    state = reduce_gates(state, RunTerminated("run-old"))
    assert state.approval is None
    # A late duplicate for the terminated run is tombstoned: it never reopens.
    state = reduce_gates(state, ApprovalRequested(first))
    assert state.approval is None
    # A newer run parks; a late end for the old run preserves the newer gate.
    newer = _approval_gate("appr-new", run_id="run-new")
    state = reduce_gates(state, ApprovalRequested(newer))
    assert state.approval is not None and state.approval.request_id == "appr-new"
    state = reduce_gates(state, RunTerminated("run-old"))
    assert state.approval is not None and state.approval.request_id == "appr-new"


def test_activation_tombstones_old_revision_confirmation() -> None:
    """A late old-revision confirmation after activation never reopens the queue."""
    from voidcode.sdk.gates import ActivationCommitted, ConfirmationRequested, GateState, reduce_gates
    from voidcode.sdk.gates import ConfirmationGate as Gate

    request = ConfirmationRequest(
        request_id="conf-old",
        owner="goal",
        command_name="goal.set",
        arguments={},
        ui_type="confirmation",
        ui_data={"request_id": "conf-old", "message": "Apply goal?"},
    )
    gate = Gate.from_request(request, revision_id="rev-1")
    state = reduce_gates(GateState(), ConfirmationRequested(gate))
    assert [entry.request_id for entry in state.confirmations] == ["conf-old"]
    state = reduce_gates(state, ActivationCommitted("rev-2"))
    assert state.confirmations == ()
    state = reduce_gates(state, ConfirmationRequested(gate))
    assert state.confirmations == ()


def test_head_transcript_arrives_canonical_and_terminal_error_stops_the_worker() -> None:
    """Stream-carried transcript renders; a terminal error exits the worker without reconnect."""
    from voidcode.sdk.client import ErrorFrame
    from voidcode.tui.terminal_app import CoreTuiApp

    approval = _event(1, "core.approval_requested", request_id="appr-1", call={"tool_name": "write", "arguments": {}})
    prompt = _user_event(2, "hello")
    head = HeadFrame(session=_session_row("s1"), events=(approval, prompt))

    class _ErrorClient(_CommandClient):
        def transcript(self, session_id: str, *, after_sequence: int = 0, limit: int = 100) -> TranscriptPage:
            raise AssertionError("stream-carried transcript must not refetch")

        async def events_stream(
            self,
            session_id: str,
            after_sequence: int = 0,
            *,
            follow: bool = True,
            cancel: StreamCancellation | None = None,
        ) -> AsyncIterator[SessionFrame]:
            self._connections += 1
            yield head
            yield ErrorFrame(session_id=session_id, error="projection failed: sequence=3 type=core.tool_completed error=ValidationError")

    client = _ErrorClient()
    terminal = _StubTerminal()
    app = CoreTuiApp(client, session_id="s1", terminal=terminal)
    app._reconcile_connection_async = lambda: None  # type: ignore[method-assign]
    app._start_event_stream()
    assert app._stream_thread is not None
    try:
        _drain_until(app, terminal, "hello")
        _drain_until(app, terminal, "Core stream error")
        app._stream_thread.join(timeout=5.0)
        assert not app._stream_thread.is_alive()
        app._drain_events()
        assert client._connections == 1
        assert sum("hello" in row for row in terminal.committed) == 1
        assert app.pending_approval is not None and app.pending_approval.request_id == "appr-1"
        assert app._session_cursors["s1"] == 2
    finally:
        app._stop_event.set()
        app._stream_thread.join(timeout=5.0)
        assert not app._stream_thread.is_alive()
