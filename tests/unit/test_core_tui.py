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


def _many_options(count: int) -> ChoicesViewData:
    options = tuple(
        ChoiceOption(id=f"child-{index:02d}", label=f"child {index:02d} with a very long status label that wraps narrow terminals")
        for index in range(count)
    )
    return ChoicesViewData(options=options, action="monitor.open", argument_name="target", prepare_input=True)


def test_choices_filter_types_printables_and_keeps_stable_selection() -> None:
    choices = InteractiveChoices(_many_options(4))
    for char in "child 0":
        assert choices.handle_key(Key(char, text=char)) is None
    assert choices.filter == "child 0"
    assert [opt.id for opt in choices.filtered_options] == [f"child-{index:02d}" for index in range(4)]
    choices.handle_key(Key("down"))
    for char in "2":
        choices.handle_key(Key(char, text=char))
    assert [opt.id for opt in choices.filtered_options] == ["child-02"]
    assert choices.selected_id == "child-02"
    # j/k are filter text, never navigation: a name containing them still filters.
    choices = InteractiveChoices(ChoicesViewData(options=(ChoiceOption(id="jk", label="jakarta"),), action="open", argument_name="target"))
    choices.handle_key(Key("j", text="j"))
    assert choices.filter == "j"
    assert choices.selected_id == "jk"


def test_choices_backspace_deletes_grapheme_and_esc_clears_first() -> None:
    choices = InteractiveChoices(_many_options(2))
    choices.handle_key(Key("e", text="e"))
    choices.handle_key(Key("́", text="é"))
    assert choices.handle_key(Key("backspace")) is None
    assert choices.filter == "e"
    assert choices.handle_key(Key("esc")) is None
    assert choices.filter == ""
    assert choices.handle_key(Key("esc")) == {"cancel": True}


def test_choices_enter_on_empty_matches_does_nothing_and_reports_prepare_flag() -> None:
    choices = InteractiveChoices(_many_options(2))
    for char in "zzz-no-match":
        choices.handle_key(Key(char, text=char))
    assert choices.filtered_options == ()
    assert choices.handle_key(Key("enter")) is None
    choices.handle_key(Key("esc"))
    outcome = choices.handle_key(Key("enter"))
    assert outcome == {"action": "monitor.open", "arguments": {"target": "child-00"}, "prepare_input": True}


def test_choices_viewport_keeps_selected_visible_at_40x18() -> None:
    import re

    from voidcode.tui.styles import Styles

    choices = InteractiveChoices(_many_options(20))
    for _ in range(19):
        choices.handle_key(Key("down"))
    rows = choices.render_rows(40, Styles(), max_rows=12)
    plain = "\n".join(re.sub(r"\x1b\[[0-9;]*m", "", row) for row in rows)
    assert len(rows) <= 12
    assert "child-19" in plain or "child 19" in plain
    assert "↑" in plain
    from voidcode.tui.term import visible_width

    assert all(visible_width(row) <= 40 for row in rows)
    first = InteractiveChoices(_many_options(20))
    rows = first.render_rows(40, Styles(), max_rows=12)
    plain = "\n".join(re.sub(r"\x1b\[[0-9;]*m", "", row) for row in rows)
    assert "child 00" in plain
    assert "↓" in plain


def test_choices_update_view_preserves_filter_and_stable_id() -> None:
    choices = InteractiveChoices(_many_options(3))
    choices.handle_key(Key("down"))
    assert choices.selected_id == "child-01"
    choices.update_view(
        ChoicesViewData(
            options=(ChoiceOption(id="child-01", label="child 01 refreshed"), ChoiceOption(id="child-03", label="child 03 new")),
            action="monitor.open",
            argument_name="target",
            prepare_input=True,
        )
    )
    assert choices.selected_id == "child-01"
    choices.update_view(ChoicesViewData(options=(), action="monitor.open", argument_name="target", prepare_input=True))
    assert choices.selected_id is None
    assert choices.handle_key(Key("enter")) is None


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
    assert outcome == {"action": "select_target", "arguments": {"target_id": "b"}, "prepare_input": False}

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
    assert app.status_line == ""
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
    assert app.status_line == ""


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


def test_run_failure_is_rendered_once_from_the_durable_event() -> None:
    """The durable outcome owns the error; the response must not repeat it."""
    from voidcode.tui.terminal_app import CoreTuiApp

    terminal = _StubTerminal()
    app = CoreTuiApp(_RunClient(), session_id="s1", terminal=terminal)
    app._stream_ready = True

    app._dispatch_user_prompt("hello")
    import time

    deadline = time.monotonic() + 2
    while app.event_queue.empty() and time.monotonic() < deadline:
        time.sleep(0.01)
    app._drain_events()
    assert terminal.committed == []

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
    assert app.status_line == ""


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
    assert any("hi" in row for row in plain)
    assert any("hello" in row for row in plain)
    assert any("why" in row for row in plain)
    assert any("boom" in row for row in plain)


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
    assert any("read" in row for row in plain)
    assert any('"path"' in row for row in plain)
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
    assert "exit 1" in text and "exec" in text
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
        _drain_until(app, terminal, "sequence=3")
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


@pytest.mark.parametrize("event_first", [False, True])
def test_run_failure_receipt_and_event_share_one_notice(event_first: bool) -> None:
    from voidcode.tui.terminal_app import CoreTuiApp

    terminal = _StubTerminal()
    app = CoreTuiApp(_RunClient(), session_id="s1", terminal=terminal)
    app._stream_ready = True
    receipt = {"kind": "run_status", "status": "failed", "error": "boom", "run_id": "run-1"}
    event = {"kind": "stream_event", "frame": EventFrame(session_id="s1", event=_event(1, "core.run_failed", error="boom"))}
    for frame in [event, receipt] if event_first else [receipt, event]:
        app.event_queue.put(frame)
        app._drain_events()
    assert sum("boom" in row for row in terminal.committed) == 1
    assert app.status_line == ""
    assert not app._run_active


def test_escape_cancels_run_but_completion_consumes_escape(monkeypatch: pytest.MonkeyPatch) -> None:
    from voidcode.tui.terminal_app import CoreTuiApp

    app = CoreTuiApp(_RunClient(), session_id="s1", terminal=_StubTerminal())
    cancelled: list[bool] = []
    monkeypatch.setattr(app, "_cancel_active_run", lambda: cancelled.append(True))
    app.composer.set_text("/sh")
    app.composer.set_completion(("/show", "/shell"))
    app._handle_composer_key(Key("escape"))
    assert not cancelled
    assert app.composer.value == "/sh"
    app._handle_composer_key(Key("escape"))
    assert cancelled == [True]


def test_ctrl_c_clears_input_then_quits_without_cancelling_run(monkeypatch: pytest.MonkeyPatch) -> None:
    from voidcode.tui.terminal_app import CoreTuiApp

    terminal = _StubTerminal()
    app = CoreTuiApp(_RunClient(), session_id="s1", terminal=terminal)
    app.running = True
    app._run_active = True
    app.composer.set_text("draft")
    cancelled: list[bool] = []
    monkeypatch.setattr(app, "_cancel_active_run", lambda: cancelled.append(True))
    monkeypatch.setattr(terminal, "read_bytes", lambda **kwargs: b"\x03")
    app._process_input()
    assert app.running
    assert app.composer.value == ""
    app._render_frame()
    assert any("Ctrl+C again" in row for row in terminal.frames[-1])
    app._process_input()
    assert not app.running
    assert not cancelled


def test_idle_cancel_is_silent() -> None:
    from voidcode.tui.terminal_app import CoreTuiApp

    terminal = _StubTerminal()
    app = CoreTuiApp(_RunClient(), session_id="s1", terminal=terminal)
    app.status_line = ""
    app._cancel_active_run()
    assert app.status_line == ""
    assert not terminal.committed
    assert not app._cancel_pending


def test_multiline_user_message_preserves_line_breaks() -> None:
    from voidcode.tui.styles import Styles
    from voidcode.tui.terminal_app import _content_rows

    text = "first\n\n第二行 <literal>"
    rows = _content_rows(ProviderMessage("user", text), 80, Styles(None))
    assert "\n".join(row.rstrip() for row in rows).strip() == text


def test_system_context_is_hidden_without_replaying_visible_messages() -> None:
    from voidcode.tui.terminal_app import CoreTuiApp

    app = CoreTuiApp(_CommandClient(), session_id="s1", terminal=_StubTerminal())
    rows: list[str] = []
    context = _event(1, "core.continuation_started", content=ProviderMessage("system", "private model context"))
    app._handle_stream_event(context, rows)
    assert rows == []
    assert app._session_cursors["s1"] == 1
    app._handle_stream_event(_user_event(2, "visible prompt"), rows)
    app._handle_stream_event(context, rows)
    assert sum("visible prompt" in row for row in rows) == 1
    assert not any("private model context" in row for row in rows)
    assert app._session_cursors["s1"] == 2


def test_busy_run_keeps_new_prompt_as_draft() -> None:
    from voidcode.tui.terminal_app import CoreTuiApp

    app = CoreTuiApp(_RunClient(), session_id="s1", terminal=_StubTerminal())
    app._run_active = True
    app._dispatch_user_prompt("next question")
    assert app.composer.value == "next question"
    assert app.event_queue.empty()


def test_short_terminal_reserves_input_and_bounds_completion() -> None:
    from voidcode.tui.terminal_app import CoreTuiApp

    terminal = _StubTerminal()
    terminal.height = 10
    app = CoreTuiApp(_RunClient(), session_id="s1", terminal=terminal)
    app.status_line = ""
    app.composer.set_completion(tuple(f"/command-{i}" for i in range(30)))
    app._stream_dispatch = "dispatch"
    app._stream_buffers["dispatch"] = ["stream\n" * 30]
    app._render_frame()
    assert len(terminal.frames[-1]) <= terminal.height
    assert terminal.frames[-1][-3].strip() == ""


def test_ctrl_d_edits_nonempty_draft(monkeypatch: pytest.MonkeyPatch) -> None:
    from voidcode.tui.terminal_app import CoreTuiApp

    terminal = _StubTerminal()
    app = CoreTuiApp(_RunClient(), session_id="s1", terminal=terminal)
    app.running = True
    app.composer.set_text("draft")
    app.composer.handle_key(Key("home"))
    monkeypatch.setattr(terminal, "read_bytes", lambda **kwargs: b"\x04")
    app._process_input()
    assert app.running
    assert app.composer.value == "raft"


def test_ctrl_c_does_not_decide_pending_approval(monkeypatch: pytest.MonkeyPatch) -> None:
    from voidcode.tui.terminal_app import CoreTuiApp
    from voidcode.tui.views import InteractiveApproval

    terminal = _StubTerminal()
    app = CoreTuiApp(_RunClient(), session_id="s1", terminal=terminal)
    app.running = True
    approval = InteractiveApproval(_approval_gate())
    app.pending_approval = approval
    monkeypatch.setattr(terminal, "read_bytes", lambda **kwargs: b"\x03")
    app._process_input()
    assert app.running
    assert app.pending_approval is approval
    assert not app._in_flight
    app._process_input()
    assert not app.running
    assert app.pending_approval is approval


def test_ctrl_c_exit_window_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    from voidcode.tui.terminal_app import CoreTuiApp

    terminal = _StubTerminal()
    app = CoreTuiApp(_RunClient(), session_id="s1", terminal=terminal)
    app.running = True
    monkeypatch.setattr(terminal, "read_bytes", lambda **kwargs: b"\x03")
    app._process_input()
    app._quit_armed_at -= 2
    app._process_input()
    assert app.running


def test_rejected_run_preserves_prompt_and_stops_spinner() -> None:
    from voidcode.tui.terminal_app import CoreTuiApp

    class Rejected(_RunClient):
        def run(self, session_id: str, spec: RunSpec) -> RunResult:
            raise CoreClientError("upgrade in progress", status_code=503, code="upgrade_pending")

    terminal = _StubTerminal()
    app = CoreTuiApp(Rejected(), session_id="s1", terminal=terminal)
    app._dispatch_user_prompt("keep this prompt")
    _drain_until(app, terminal, "was not dispatched")
    assert app.composer.value == "keep this prompt"
    assert not app._run_active
    assert not any("Run failed" in row for row in terminal.committed)


def test_spaced_slash_matches_longest_path_and_invokes_dotted_id() -> None:
    """`/goal set x` reaches goal.set with the verbatim tail; `/goal` alone reaches goal."""
    import time

    from voidcode.tui.terminal_app import CoreTuiApp

    catalog = [
        CommandDescriptor(name="goal", owner="p", description="g", argument_schema={"type": "object", "properties": {}}, effects=(ToolEffect.READ,)),
        CommandDescriptor(
            name="goal.set",
            owner="p",
            description="gs",
            argument_schema={"type": "object", "properties": {"objective": {"type": "string"}}, "required": ["objective"]},
            effects=(ToolEffect.WRITE,),
        ),
    ]
    terminal = _StubTerminal()
    client = _CommandClient(catalog=catalog, results={})
    app = CoreTuiApp(client, session_id="s1", terminal=terminal)

    app._dispatch_slash_command("/goal set my objective here")
    deadline = time.monotonic() + 5
    while not client.invoked and time.monotonic() < deadline:
        time.sleep(0.01)
    assert [(name, args, rev) for _, name, args, rev in client.invoked] == [("goal.set", {"objective": "my objective here"}, "rev-1")]
    assert app.status_line == "Dispatching /goal set..."

    client.invoked.clear()
    app._dispatch_slash_command("/goal")
    deadline = time.monotonic() + 5
    while not client.invoked and time.monotonic() < deadline:
        time.sleep(0.01)
    assert [(name, args) for _, name, args, _ in client.invoked] == [("goal", {})]


def test_dotted_legacy_slash_makes_no_mutation() -> None:
    """`/goal.set x` is refused as unknown; nothing is invoked and no parent runs instead."""
    import time

    from voidcode.tui.terminal_app import CoreTuiApp

    catalog = [
        CommandDescriptor(name="goal", owner="p", description="g", argument_schema={"type": "object", "properties": {}}, effects=(ToolEffect.READ,)),
        CommandDescriptor(
            name="goal.set",
            owner="p",
            description="gs",
            argument_schema={"type": "object", "properties": {"objective": {"type": "string"}}, "required": ["objective"]},
            effects=(ToolEffect.WRITE,),
        ),
    ]
    terminal = _StubTerminal()
    client = _CommandClient(catalog=catalog, results={})
    app = CoreTuiApp(client, session_id="s1", terminal=terminal)

    app._dispatch_slash_command("/goal.set oops")
    time.sleep(0.2)
    app._drain_events()
    assert client.invoked == []
    assert any("Unknown command" in row for row in terminal.committed), terminal.committed


def test_spaced_slash_preserves_json_tail_and_refuses_non_object() -> None:
    """JSON with spaces/dots reaches the command intact; a non-object tail is refused without invoke."""
    import time

    from voidcode.tui.terminal_app import CoreTuiApp

    catalog = [
        CommandDescriptor(
            name="workflow.run",
            owner="p",
            description="w",
            argument_schema={"type": "object", "properties": {"workflow": {"type": "string"}, "task": {}}, "required": ["workflow"]},
            effects=(ToolEffect.EXECUTE,),
            client_syntax="/workflow run",
        ),
    ]
    terminal = _StubTerminal()
    client = _CommandClient(catalog=catalog, results={})
    app = CoreTuiApp(client, session_id="s1", terminal=terminal)

    app._dispatch_slash_command('/workflow   run\t {"workflow": "a.b", "task": "x  y"}')
    deadline = time.monotonic() + 5
    while not client.invoked and time.monotonic() < deadline:
        time.sleep(0.01)
    assert [args for _, _, args, _ in client.invoked] == [{"workflow": "a.b", "task": "x  y"}]

    client.invoked.clear()
    app._dispatch_slash_command("/workflow run [1, 2]")
    time.sleep(0.2)
    app._drain_events()
    assert client.invoked == []
    assert any("JSON object" in row for row in terminal.committed), terminal.committed


def test_ambiguous_public_path_fails_without_invoke() -> None:
    """Two descriptors projecting onto one public path refuse visibly; neither runs."""
    import time

    from voidcode.tui.terminal_app import CoreTuiApp

    catalog = [
        CommandDescriptor(
            name="foo.bar", owner="p", description="1", argument_schema={"type": "object", "properties": {}}, effects=(ToolEffect.READ,)
        ),
        CommandDescriptor(
            name="other",
            owner="p",
            description="2",
            argument_schema={"type": "object", "properties": {}},
            effects=(ToolEffect.READ,),
            client_syntax="/foo bar",
        ),
    ]
    terminal = _StubTerminal()
    client = _CommandClient(catalog=catalog, results={})
    app = CoreTuiApp(client, session_id="s1", terminal=terminal)

    app._dispatch_slash_command("/foo bar")
    time.sleep(0.2)
    app._drain_events()
    assert client.invoked == []
    assert any("Unknown command" in row for row in terminal.committed), terminal.committed


def test_dotted_syntax_hint_normalizes_to_spaced_path() -> None:
    """An explicit `/foo.bar` hint projects to `/foo bar`; the dotted UI form stays rejected."""
    import time

    from voidcode.tui.terminal_app import CoreTuiApp

    catalog = [
        CommandDescriptor(
            name="other",
            owner="p",
            description="2",
            argument_schema={"type": "object", "properties": {}},
            effects=(ToolEffect.READ,),
            client_syntax="/foo.bar",
        ),
    ]
    terminal = _StubTerminal()
    client = _CommandClient(catalog=catalog, results={})
    app = CoreTuiApp(client, session_id="s1", terminal=terminal)

    app._dispatch_slash_command("/foo bar")
    deadline = time.monotonic() + 5
    while not client.invoked and time.monotonic() < deadline:
        time.sleep(0.01)
    assert [name for _, name, _, _ in client.invoked] == ["other"]

    client.invoked.clear()
    app._dispatch_slash_command("/foo.bar")
    time.sleep(0.2)
    app._drain_events()
    assert client.invoked == []
    assert any("Unknown command" in row for row in terminal.committed), terminal.committed


def test_shell_syntax_stays_reachable_as_slash_and_help_lists_spaced_names() -> None:
    """`!`-syntax commands keep their derived `/shell` path; help lists spaced names only."""
    import time

    from voidcode.tui.terminal_app import CoreTuiApp

    catalog = [
        CommandDescriptor(
            name="shell",
            owner="p",
            description="Run a shell command locally (the `!` escape).",
            argument_schema={"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
            effects=(ToolEffect.EXECUTE,),
            client_syntax="!",
        ),
        CommandDescriptor(
            name="goal.set", owner="p", description="gs", argument_schema={"type": "object", "properties": {}}, effects=(ToolEffect.READ,)
        ),
    ]
    terminal = _StubTerminal()
    client = _CommandClient(catalog=catalog, results={})
    app = CoreTuiApp(client, session_id="s1", terminal=terminal)

    app._dispatch_slash_command("/shell echo hi")
    deadline = time.monotonic() + 5
    while not client.invoked and time.monotonic() < deadline:
        time.sleep(0.01)
    assert [(name, args) for _, name, args, _ in client.invoked] == [("shell", {"command": "echo hi"})]

    terminal.committed.clear()
    app._dispatch_slash_command("/tui help")
    assert any("/goal set" in row for row in terminal.committed), terminal.committed
    assert not any("goal.set" in row for row in terminal.committed), terminal.committed


def test_welcome_shows_once_on_empty_session_only() -> None:
    """The welcome mark commits once per empty visit; resumed sessions and banner=None stay quiet."""
    from rich.text import Text

    from voidcode.config import BannerConfig
    from voidcode.tui.terminal_app import CoreTuiApp

    terminal = _StubTerminal()
    app = CoreTuiApp(_CommandClient(), session_id="s1", terminal=terminal, banner=BannerConfig(text="◆", colors=("cyan",)))
    app._maybe_welcome()
    assert [Text.from_ansi(row).plain.strip() for row in terminal.committed] == ["◆"]
    app._maybe_welcome()
    assert [Text.from_ansi(row).plain.strip() for row in terminal.committed] == ["◆"]

    resumed_terminal = _StubTerminal()
    resumed = CoreTuiApp(_CommandClient(), session_id="s1", terminal=resumed_terminal)
    resumed._session_cursors["s1"] = 3
    resumed._maybe_welcome()
    assert resumed_terminal.committed == []

    class _ResumedClient(_CommandClient):
        def get_session(self, session_id: str) -> SessionRow:
            row = super().get_session(session_id)
            return SessionRow(
                session_id=row.session_id,
                workspace=row.workspace,
                status=row.status,
                active_revision_id=row.active_revision_id,
                run_id=row.run_id,
                turn=row.turn,
                last_event_sequence=2,
            )

    durable_terminal = _StubTerminal()
    durable = CoreTuiApp(_ResumedClient(), session_id="s1", terminal=durable_terminal)
    durable._maybe_welcome()
    assert durable_terminal.committed == []

    off_terminal = _StubTerminal()
    off = CoreTuiApp(_CommandClient(), session_id="s1", terminal=off_terminal, banner=None)
    off._maybe_welcome()
    assert off_terminal.committed == []


def test_welcome_rows_clip_center_and_sample_colors() -> None:
    from rich.color import ColorSystem
    from rich.style import Style
    from rich.text import Text

    from voidcode.config import BannerConfig
    from voidcode.tui.styles import Styles
    from voidcode.tui.term import visible_width
    from voidcode.tui.terminal_app import _banner_rows

    styles = Styles(None)
    rows = _banner_rows(BannerConfig(text="◆\n  界\x07", colors=("red",)), styles, width=20)
    assert rows[1].index("界") - rows[0].index("◆") == 2
    assert all(visible_width(row) <= 20 for row in rows)
    assert "\x07" not in "".join(rows)
    assert abs(rows[0].index("◆") - (20 - max(visible_width(row) for row in rows))) <= 1

    separated = _banner_rows(BannerConfig(text="◆\n\n  ◆", colors=("red",)), styles, width=20)
    assert len(separated) == 3 and separated[1].strip() == ""
    assert separated[2].index("◆") - separated[0].index("◆") == 2

    colored = _banner_rows(
        BannerConfig(text="◆\n  ◆", colors=("#010203", "#405060", "#a0b0c0")),
        Styles(ColorSystem.TRUECOLOR),
        width=20,
    )
    for row, expected in zip(colored, [(1, 2, 3), (160, 176, 192)], strict=True):
        style = Text.from_ansi(row).spans[0].style
        assert isinstance(style, Style) and style.color is not None
        assert style.color.get_truecolor() == expected

    narrow = _banner_rows(BannerConfig(text="界" * 8, colors=("red",)), styles, width=5)
    assert narrow == ["界界"]
    assert _banner_rows(BannerConfig(), styles, width=0) == []
    assert _banner_rows(None, styles, width=80) == []
    assert _banner_rows(BannerConfig(text="  \n \n", colors=("red",)), styles, width=80) == []


def test_command_validation_failure_preserves_form_draft_for_correction() -> None:
    from voidcode.tui.terminal_app import CoreTuiApp

    client = _CommandClient(results={"goal.set": CommandResult(status="error", error="budget must be positive")})
    terminal = _StubTerminal()
    app = CoreTuiApp(client, session_id="s1", terminal=terminal)  # type: ignore[arg-type]
    form = InteractiveForm(FormViewData(fields=(FormField(name="objective", label="Objective"),), action="goal.set"), revision_id="rev-1")
    form.values["objective"] = "Keep this draft"
    app.active_overlay = form
    app._handle_overlay_key(Key("ctrl+s"))
    _drain_until(app, terminal, "budget must be positive")
    assert app.active_overlay is form
    assert form.values["objective"] == "Keep this draft"
    assert "invoke:goal.set@rev-1" not in app._in_flight


def test_command_choices_open_only_on_live_reply_and_dispatch_displayed_revision() -> None:
    import time

    from voidcode.sdk.commands import CommandPresentation
    from voidcode.tui.terminal_app import CoreTuiApp

    result = CommandResult(
        status="ok",
        output="Explore",
        presentation=CommandPresentation(
            title="Profiles",
            view_type="choices",
            data={"options": [{"id": "explore", "label": "Explore"}], "action": "demo.show", "argument_name": "name"},
        ),
    )
    client = _CommandClient(results={"demo": result})
    terminal = _StubTerminal()
    app = CoreTuiApp(client, session_id="s1", terminal=terminal)  # type: ignore[arg-type]
    app.catalog_revision = "rev-1"
    rows: list[str] = []
    app._handle_stream_event(_event(1, "core.command_completed", name="demo", result=result.as_payload()), rows)
    assert app.active_overlay is None
    assert rows == ["Explore"]
    app._invoke_async("demo", {}, "rev-1")
    deadline = time.monotonic() + 5
    while not isinstance(app.active_overlay, InteractiveChoices) and time.monotonic() < deadline:
        app._drain_events()
        time.sleep(0.01)
    assert isinstance(app.active_overlay, InteractiveChoices)
    app._handle_overlay_key(Key("enter"))
    deadline = time.monotonic() + 5
    while len(client.invoked) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert client.invoked[-1] == ("s1", "demo.show", {"name": "explore"}, "rev-1")


def test_late_command_reply_never_closes_or_reopens_an_editor_after_cancel() -> None:
    from threading import Event

    from voidcode.sdk.commands import CommandPresentation
    from voidcode.tui.terminal_app import CoreTuiApp

    started, release = Event(), Event()

    class DelayedClient(_CommandClient):
        def invoke_command(self, *args, **kwargs):
            started.set()
            assert release.wait(timeout=3)
            return CommandResult(
                status="ok",
                presentation=CommandPresentation(
                    title="Late editor", view_type="form", data={"action": "edit", "fields": [{"name": "text", "label": "Text"}]}
                ),
            )

    app = CoreTuiApp(DelayedClient(), session_id="s1", terminal=_StubTerminal())  # type: ignore[arg-type]
    app.catalog_revision = "rev-1"
    old = InteractiveForm(FormViewData(fields=(FormField(name="text", label="Text"),), action="edit"), revision_id="rev-1")
    old.values["text"] = "Old"
    app.active_overlay = old
    app._handle_overlay_key(Key("ctrl+s"))
    assert started.wait(timeout=2)
    app._handle_overlay_key(Key("esc"))
    newer = InteractiveForm(old.view, revision_id="rev-1")
    newer.values["text"] = "New draft"
    app.active_overlay = newer
    release.set()
    frames = []
    while True:
        frame = app.event_queue.get(timeout=3)
        frames.append(frame)
        if frame["kind"] == "command_presentation":
            break
    for frame in frames:
        app.event_queue.put(frame)
    app._drain_events()
    assert app.active_overlay is newer
    assert newer.values["text"] == "New draft"


def test_markdown_detail_scrolls_with_decoded_keys_and_bounds_action_rows() -> None:
    from voidcode.sdk.views import MarkdownViewData
    from voidcode.tui.keys import KeyDecoder
    from voidcode.tui.styles import Styles
    from voidcode.tui.term import visible_width
    from voidcode.tui.views import InteractiveMarkdown

    data = MarkdownViewData.from_payload(
        {
            "content": "\n\n".join(f"Detail line {index}" for index in range(50)),
            "actions": [
                {"label": "Next page " * 20, "action": "skills.show", "arguments": {"name": "guide", "offset": 100}},
                {"label": "Choose skill", "action": "skills.show", "prepare_input": True},
            ],
        }
    )
    overlay = InteractiveMarkdown(data, revision_id="shown-revision", title="Guide")
    for width, height in [(80, 20), (40, 12), (20, 6)]:
        rows = overlay.render_rows(width, Styles(None), max_rows=height)
        assert len(rows) <= height
        assert all(visible_width(row) <= width for row in rows)
    for key in KeyDecoder().feed(b"\x1b[6~"):
        overlay.handle_key(key)
    assert overlay.scroll > 0
    overlay.handle_key(Key("home"))
    assert overlay.scroll == 0
    assert overlay.handle_key(Key("enter")) == {"action": "skills.show", "arguments": {"name": "guide", "offset": 100}, "prepare_input": False}
    overlay.handle_key(Key("tab"))
    assert overlay.handle_key(Key("enter")) == {"action": "skills.show", "arguments": {}, "prepare_input": True}
    assert overlay.handle_key(Key("esc")) == {"cancel": True}


def test_markdown_action_history_is_passive_but_explicit_presentation_opens_detail() -> None:
    from voidcode.sdk.commands import CommandPresentation
    from voidcode.tui.terminal_app import CoreTuiApp
    from voidcode.tui.views import InteractiveMarkdown

    presentation = CommandPresentation(
        title="Details",
        view_type="markdown",
        data={"content": "Task status", "actions": [{"label": "Update task", "action": "todo.edit", "arguments": {"task": "one"}}]},
    )
    app = CoreTuiApp(_CommandClient(), session_id="s1", terminal=_StubTerminal())  # type: ignore[arg-type]
    rows = []
    app._handle_stream_event(
        _event(1, "core.command_completed", name="todo", result=CommandResult(status="ok", presentation=presentation).as_payload()), rows
    )
    assert app.active_overlay is None
    assert "Task status" in "\n".join(rows)
    view = PluginViewDescriptor(owner="todo", name="todo", title="Details", view_type="markdown", data=presentation.data)
    assert app._mount_interactive_view(view, "shown-revision")
    assert isinstance(app.active_overlay, InteractiveMarkdown)
    assert app.active_overlay.revision_id == "shown-revision"


def test_tui_sessions_selector_mount_and_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    from voidcode.tui.terminal_app import CoreTuiApp
    from voidcode.tui.views import InteractiveChoices

    class _SessionsClient(_StubClient):
        def __init__(self, sessions: list[SessionRow]) -> None:
            super().__init__()
            self._sessions = list(sessions)

        def list_sessions(self, limit: int = 100) -> list[SessionRow]:
            return list(self._sessions)

    s1 = _session_row("s1-abcde", status="idle", turn=1)
    s2 = _session_row("s2-fghij", status="running", turn=2)
    client = _SessionsClient([s1, s2])
    terminal = _StubTerminal()
    app = CoreTuiApp(client, session_id="s1-abcde", terminal=terminal)  # type: ignore[arg-type]

    switched: list[str] = []
    monkeypatch.setattr(app, "_switch_session", lambda target: switched.append(target))

    # 1. /tui sessions mounts InteractiveChoices with sessions list
    app._dispatch_slash_command("/tui sessions")
    assert isinstance(app.active_overlay, InteractiveChoices)
    assert app.active_overlay.title == "Active Sessions"
    assert app.active_overlay.action == "tui.switch_session"
    assert app.active_overlay.argument_name == "session_id"
    assert app.active_overlay.selected_index == 0
    assert len(app.active_overlay.filtered_options) == 2
    assert " [current]" in app.active_overlay.filtered_options[0].label
    assert " [current]" not in app.active_overlay.filtered_options[1].label

    # 2. Esc dismisses overlay without switching
    app._handle_overlay_key(Key("esc"))
    assert app.active_overlay is None
    assert switched == []

    # 3. Selecting the current session dismisses overlay without switching
    app._dispatch_slash_command("/tui sessions")
    assert isinstance(app.active_overlay, InteractiveChoices)
    assert app.active_overlay.selected_index == 0
    app._handle_overlay_key(Key("enter"))
    assert app.active_overlay is None
    assert switched == []

    # 4. Selecting a different session calls _switch_session
    app._dispatch_slash_command("/tui sessions")
    assert isinstance(app.active_overlay, InteractiveChoices)
    app._handle_overlay_key(Key("down"))
    assert app.active_overlay.selected_index == 1
    app._handle_overlay_key(Key("enter"))
    assert app.active_overlay is None
    assert switched == ["s2-fghij"]

    # 5. Empty session list commits warning without overlay
    empty_app = CoreTuiApp(_SessionsClient([]), session_id="s1", terminal=_StubTerminal())  # type: ignore[arg-type]
    empty_app._dispatch_slash_command("/tui sessions")
    assert empty_app.active_overlay is None
    assert any("No active sessions." in row for row in empty_app.terminal.committed)
