"""Unit tests for the Todo plugin: state, ops, tool, commands, context, and reminder policy."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Literal

import pytest

from voidcode.plugins.todo.todo_plugin import (
    DEFAULT_TODO_REMINDER_MAX_PER_CYCLE,
    SCHEMA_VERSION,
    TODO_MID_RUN_KIND,
    TODO_MID_RUN_MUTATION_THRESHOLD,
    TODO_REMINDER_KIND,
    TodoClearCommand,
    TodoMidRunState,
    TodoRemindersConfig,
    TodoReminderState,
    TodoState,
    TodoStatusCommand,
    TodoTool,
    create_plugin,
    decide_todo_mid_run_nudge,
    decide_todo_reminder,
    incomplete_todo_phases,
    migrate_todo_state,
    parse_todo_config,
    render_provider_state,
    todo_context,
    todo_mid_run_nudge_text,
    todo_mutation_count,
    todo_observer,
    todo_reminder_text,
    todo_summary,
)
from voidcode.sdk.commands import CommandInvocation
from voidcode.sdk.hooks import FactPage, InvocationFinishedFact, PluginFact, RunFinishedFact, RunRequestedFact
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.services import PluginBudget, PluginServices, SessionUsageReport
from voidcode.sdk.tools import ToolCall, ToolFailure, ToolSuccess


def _request(sequence: int, run_id: str, *, resumed: bool = False) -> RunRequestedFact:
    return RunRequestedFact(
        session_id="session-1",
        run_id=run_id,
        revision_id="rev-1",
        logical_turn="",
        event_sequence=sequence,
        resumed=resumed,
    )


class MockServices(PluginServices):
    """In-memory PluginServices for fast unit testing."""

    def __init__(self) -> None:
        self.state: dict[tuple[str, str], tuple[Mapping[str, object] | None, int]] = {}
        self.staged_events: list[tuple[str, Mapping[str, object]]] = []
        self.staged_continuations: list[tuple[Mapping[str, object], str | None, str]] = []
        self.facts: list[PluginFact] = []
        self.schema_version = 0

    def get_state(self, scope: Literal["session", "workspace"] = "session") -> tuple[Mapping[str, object] | None, int]:
        return self.state.get((scope, ""), (None, 0))

    def get_state_schema_version(self, scope: Literal["session", "workspace"] = "session") -> int:
        _ = scope
        return self.schema_version

    def replace_state(
        self,
        state: Mapping[str, object],
        expected_version: int,
        scope: Literal["session", "workspace"] = "session",
    ) -> int:
        _cur, ver = self.get_state(scope)
        if ver != expected_version:
            raise RuntimeError(f"CAS mismatch: expected {expected_version}, was {ver}")
        new_ver = expected_version + 1
        self.state[(scope, "")] = (dict(state), new_ver)
        return new_ver

    def stage_event(self, event_type: str, payload: Mapping[str, object]) -> None:
        self.staged_events.append((event_type, dict(payload)))

    def enqueue_continuation(
        self,
        payload: Mapping[str, object],
        *,
        idempotency_key: str | None = None,
        role: Literal["system", "user"] = "system",
    ) -> str:
        self.staged_continuations.append((dict(payload), idempotency_key, role))
        return f"continuation-{len(self.staged_continuations)}"

    def read_facts(self, *, after_sequence: int = 0, limit: int = 100) -> FactPage:
        remaining = tuple(fact for fact in self.facts if fact.event_sequence > after_sequence)
        page = remaining[:limit]
        return FactPage(page, page[-1].event_sequence if page else after_sequence, len(remaining) > limit)

    def cancel_continuations(self, *, continuation_id: str | None = None) -> None:
        raise AssertionError("todo does not cancel continuations")

    def get_usage(self, *, since_sequence: int = 0, until_sequence: int | None = None) -> SessionUsageReport:
        raise AssertionError("todo does not read usage")

    def get_budget(self) -> PluginBudget | None:
        raise AssertionError("todo has no budget")

    def set_budget(self, **_kwargs: object) -> None:
        raise AssertionError("todo sets no budget")

    def clear_budget(self) -> None:
        raise AssertionError("todo clears no budget")


def _context(workspace: Path) -> ToolContext:
    return ToolContext(
        workspace=workspace,
        session_id="session-1",
        run_id="run-1",
        invocation_id="inv-1",
        revision_id="rev-1",
        actor="model",
        logical_turn="turn-1",
    )


def _call(arguments: Mapping[str, object], *, name: str = "todo") -> ToolCall:
    return ToolCall(tool_name=name, arguments=arguments, tool_call_id="call-1")


def _invoke(services: MockServices, tmp_path: Path, arguments: Mapping[str, object]):
    return TodoTool().invoke(_call(arguments), context=_context(tmp_path), services=services)


def _raw_state(phases: object, *, revision: int) -> dict[str, object]:
    """Build one stored v2 state document exactly as the plugin writes it."""
    parsed = tuple(
        {"name": phase["name"], "tasks": [dict(task) for task in phase["tasks"]]}
        for phase in phases  # type: ignore[union-attr]
    )
    return {
        "schema_version": 2,
        "revision": revision,
        "phases": [{"name": phase["name"], "tasks": [dict(task) for task in phase["tasks"]]} for phase in parsed],
        "summary": todo_summary(parsed),
        "reminders": {
            "todo": {"attempts": 0, "awaiting_progress": False, "progress_watermark": 0, "cycle_run_id": None},
            "todo_mid_run": {"attempts": 0, "mutations": 0, "mutation_baseline": 0, "cycle_run_id": None},
            "invocations": 0,
            "cursor_sequence": 0,
            "cycle_run_id": None,
        },
    }


def _stored(services: MockServices) -> TodoState:
    raw, _version = services.get_state("session")
    return TodoState.from_payload(raw)


def _phases(services: MockServices) -> list[dict[str, object]]:
    return [{"name": phase["name"], "tasks": [dict(task) for task in phase["tasks"]]} for phase in _stored(services).phases]


# --- State contract ---


def test_state_round_trip_and_summary_projection() -> None:
    state = TodoState(revision=3, phases=({"name": "Build", "tasks": [{"content": "compile", "status": "in_progress"}]},))
    payload = state.to_payload()
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["summary"] == {
        "total": 1,
        "pending": 0,
        "in_progress": 1,
        "completed": 0,
        "abandoned": 0,
        "blocked": 0,
        "active": 1,
    }
    assert TodoState.from_payload(payload) == state
    assert TodoState.from_payload(None) == TodoState()


@pytest.mark.parametrize(
    "payload",
    [
        {**_raw_state((), revision=0), "unknown": 1},
        {key: value for key, value in _raw_state((), revision=0).items() if key != "phases"},
        {**_raw_state((), revision=0), "schema_version": 1},
        {**_raw_state((), revision=0), "revision": -1},
        {**_raw_state((), revision=0), "revision": False},
        {**_raw_state((), revision=0), "summary": {"total": 5}},
    ],
)
def test_state_parse_rejects_invalid_documents(payload: Mapping[str, object]) -> None:
    with pytest.raises(ValueError):
        TodoState.from_payload(payload)


@pytest.mark.parametrize(
    "phases",
    [
        [{"name": "", "tasks": []}],
        [{"name": "A", "tasks": []}, {"name": "A", "tasks": []}],
        [{"name": "A", "tasks": [{"content": "x", "status": "pending"}, {"content": "x", "status": "pending"}]}],
        [{"name": "A", "tasks": [{"content": "x", "status": "unknown"}]}],
        [{"name": "A", "tasks": [{"content": "x", "status": "pending", "blocker": "why"}]}],
        [{"name": "A", "tasks": [{"content": "x", "status": "blocked", "blocker": "  "}]}],
        [
            {"name": "A", "tasks": [{"content": "x", "status": "in_progress"}]},
            {"name": "B", "tasks": [{"content": "y", "status": "in_progress"}]},
        ],
    ],
)
def test_state_parse_rejects_invalid_phases(phases: list[dict[str, object]]) -> None:
    payload = {**_raw_state((), revision=0), "phases": phases}
    with pytest.raises(ValueError):
        TodoState.from_payload(payload)


def test_blocked_reason_is_whitespace_collapsed() -> None:
    phases = [{"name": "A", "tasks": [{"content": "x", "status": "blocked", "blocker": "  waiting   on\tci  "}]}]
    parsed = TodoState.from_payload(_raw_state(phases, revision=1))
    assert parsed.phases[0]["tasks"][0]["blocker"] == "waiting on ci"


# --- Op engine ---


def test_init_list_and_items_normalize_first_pending(tmp_path: Path) -> None:
    services = MockServices()
    res = _invoke(services, tmp_path, {"op": "init", "list": [{"phase": "Build", "items": ["compile", "test"]}]})
    assert isinstance(res, ToolSuccess)
    assert _phases(services) == [
        {"name": "Build", "tasks": [{"content": "compile", "status": "in_progress"}, {"content": "test", "status": "pending"}]}
    ]
    assert res.output.text == "Todo init applied.\nBuild:\n- [in_progress] compile\n- [pending] test"
    assert res.body.as_payload()["mutated"] is True

    res_items = _invoke(services, tmp_path, {"op": "init", "items": ["a"], "phase": "Tasks"})
    assert isinstance(res_items, ToolSuccess)
    assert _phases(services) == [{"name": "Tasks", "tasks": [{"content": "a", "status": "in_progress"}]}]


def test_init_rejects_ambiguous_and_duplicate_input(tmp_path: Path) -> None:
    services = MockServices()
    both = _invoke(services, tmp_path, {"op": "init", "list": [{"phase": "A", "items": ["x"]}], "items": ["x"]})
    assert isinstance(both, ToolFailure)
    assert both.error == "init accepts list or items, not both"

    empty = _invoke(services, tmp_path, {"op": "init"})
    assert isinstance(empty, ToolFailure)
    assert empty.error == "init requires a non-empty list or items"

    dup = _invoke(services, tmp_path, {"op": "init", "items": ["x", "x"]})
    assert isinstance(dup, ToolFailure)
    assert dup.error == 'Duplicate task "x" in init list'
    assert services.get_state("session") == (None, 0)


def test_start_blocked_and_terminal_rules(tmp_path: Path) -> None:
    services = MockServices()
    _invoke(services, tmp_path, {"op": "init", "items": ["one", "two"]})
    _invoke(services, tmp_path, {"op": "block", "task": "two", "reason": "waiting on  ci"})
    blocked = _invoke(services, tmp_path, {"op": "start", "task": "two"})
    assert isinstance(blocked, ToolFailure)
    assert blocked.error == 'Task "two" is blocked; unblock it before starting'

    _invoke(services, tmp_path, {"op": "unblock", "task": "two"})
    started = _invoke(services, tmp_path, {"op": "start", "task": "two"})
    assert isinstance(started, ToolSuccess)
    tasks = {task["content"]: task["status"] for phase in _phases(services) for task in phase["tasks"]}  # type: ignore[union-attr]
    assert tasks == {"one": "pending", "two": "in_progress"}

    _invoke(services, tmp_path, {"op": "done", "task": "two"})
    restart = _invoke(services, tmp_path, {"op": "start", "task": "two"})
    assert isinstance(restart, ToolFailure)
    assert restart.error == 'Task "two" is already completed and cannot be started'


def test_done_drop_terminal_transitions_and_phase_targeting(tmp_path: Path) -> None:
    services = MockServices()
    _invoke(services, tmp_path, {"op": "init", "list": [{"phase": "A", "items": ["x", "y"]}, {"phase": "B", "items": ["z"]}]})
    noop = _invoke(services, tmp_path, {"op": "done", "task": "nonexistent"})
    assert isinstance(noop, ToolFailure)
    assert noop.error == 'Task "nonexistent" not found'

    phase_drop = _invoke(services, tmp_path, {"op": "drop", "phase": "A"})
    assert isinstance(phase_drop, ToolSuccess)
    statuses = {task["content"]: task["status"] for phase in _phases(services) for task in phase["tasks"]}  # type: ignore[union-attr]
    # Dropping every task in A leaves B's pending task as the promoted active one.
    assert statuses == {"x": "abandoned", "y": "abandoned", "z": "in_progress"}

    conflict = _invoke(services, tmp_path, {"op": "done", "task": "x"})
    assert isinstance(conflict, ToolFailure)
    assert conflict.error == 'Task "x" is already abandoned and cannot become completed'

    repeat = _invoke(services, tmp_path, {"op": "drop", "task": "x"})
    assert isinstance(repeat, ToolSuccess)
    assert repeat.body.as_payload()["mutated"] is False


def test_append_rm_and_view_semantics(tmp_path: Path) -> None:
    services = MockServices()
    _invoke(services, tmp_path, {"op": "init", "items": ["keep"]})
    appended = _invoke(services, tmp_path, {"op": "append", "phase": "More", "items": ["extra"]})
    assert isinstance(appended, ToolSuccess)
    assert [phase["name"] for phase in _phases(services)] == ["Tasks", "More"]

    dup = _invoke(services, tmp_path, {"op": "append", "phase": "More", "items": ["keep"]})
    assert isinstance(dup, ToolFailure)
    assert dup.error == 'Task "keep" already exists'

    missing_phase = _invoke(services, tmp_path, {"op": "append", "items": ["x"]})
    assert isinstance(missing_phase, ToolFailure)
    assert missing_phase.error == "append requires phase"

    both_targets = _invoke(services, tmp_path, {"op": "rm", "task": "keep", "phase": "Tasks"})
    assert isinstance(both_targets, ToolFailure)
    assert both_targets.error == "rm accepts either task or phase, not both"

    _invoke(services, tmp_path, {"op": "rm", "phase": "Tasks"})
    assert _phases(services) == [{"name": "Tasks", "tasks": []}, {"name": "More", "tasks": [{"content": "extra", "status": "in_progress"}]}]

    cleared = _invoke(services, tmp_path, {"op": "rm"})
    assert isinstance(cleared, ToolSuccess)
    assert all(phase["tasks"] == [] for phase in _phases(services))
    # A clearing mutation still reports the changed tasks, like any other op.
    assert cleared.output.text.startswith("Todo rm applied.\n- [removed] extra (More)\nNext active: none\nSummary: 0 total;")


def test_view_is_read_only_and_does_not_normalize(tmp_path: Path) -> None:
    services = MockServices()
    phases = [{"name": "A", "tasks": [{"content": "x", "status": "pending"}, {"content": "y", "status": "pending"}]}]
    services.replace_state(_raw_state(phases, revision=4), expected_version=0)
    res = _invoke(services, tmp_path, {"op": "view"})
    assert isinstance(res, ToolSuccess)
    assert res.body.as_payload()["mutated"] is False
    assert _stored(services).revision == 4
    assert _phases(services) == phases
    assert res.output.text.startswith("Todo view applied.\nA:\n- [pending] x")


def test_mutation_bumps_revision_and_stages_event(tmp_path: Path) -> None:
    services = MockServices()
    _invoke(services, tmp_path, {"op": "init", "items": ["one"]})
    assert _stored(services).revision == 1
    event_type, payload = services.staged_events[-1]
    assert event_type == "todo_updated"
    assert payload["todo_count"] == 1
    assert payload["active_count"] == 1
    assert payload["revision"] == 1

    _invoke(services, tmp_path, {"op": "done", "task": "one"})
    assert _stored(services).revision == 2
    assert services.staged_events[-1][1]["completed_count"] == 1


def test_render_mutation_diff_and_summary_text(tmp_path: Path) -> None:
    services = MockServices()
    _invoke(services, tmp_path, {"op": "init", "items": ["one", "two"]})
    res = _invoke(services, tmp_path, {"op": "done", "task": "one"})
    assert isinstance(res, ToolSuccess)
    assert res.output.text == (
        "Todo done applied.\n"
        "- [completed] one (Tasks)\n"
        "- [in_progress] two (Tasks)\n"
        "Next active: two\n"
        "Summary: 2 total; 0 pending; 1 in progress; 1 completed; 0 abandoned; 0 blocked."
    )


def test_invalid_arguments_fail_without_state_write(tmp_path: Path) -> None:
    services = MockServices()
    res = _invoke(services, tmp_path, {"op": "explode"})
    assert isinstance(res, ToolFailure)
    assert "todo op must be one of" in res.error
    assert services.get_state("session") == (None, 0)


# --- Commands ---


def test_status_command_renders_table_and_data(tmp_path: Path) -> None:
    services = MockServices()
    _invoke(services, tmp_path, {"op": "init", "items": ["one", "two"]})
    _invoke(services, tmp_path, {"op": "block", "task": "two", "reason": "waiting"})
    invocation = CommandInvocation(context=_context(tmp_path), name="todo", arguments={})
    res = TodoStatusCommand().invoke(invocation, services)
    assert res.status == "ok"
    assert res.output == "Tasks:\n- [in_progress] one\n- [blocked] two — waiting"
    assert res.data is not None
    assert res.data["summary"]["total"] == 2
    assert res.data["revision"] == 2

    empty = TodoStatusCommand().invoke(invocation, MockServices())
    assert empty.output == "Todo list is empty."


def test_clear_command_is_explicit_and_idempotent(tmp_path: Path) -> None:
    services = MockServices()
    _invoke(services, tmp_path, {"op": "init", "items": ["one"]})
    invocation = CommandInvocation(context=_context(tmp_path), name="todo.clear", arguments={})
    cleared = TodoClearCommand().invoke(invocation, services)
    assert cleared.status == "ok"
    assert cleared.output == "Todo list cleared."
    assert cleared.data == {"cleared": True}
    assert _stored(services).phases == ()
    assert services.staged_events[-1][0] == "todo_cleared"

    again = TodoClearCommand().invoke(invocation, services)
    assert again.output == "No todo list to clear."
    assert again.data == {"cleared": False}


# --- Context ---


def test_context_plan_truth_text_is_verbatim(tmp_path: Path) -> None:
    services = MockServices()
    assert todo_context(_context(tmp_path), services) is None

    _invoke(services, tmp_path, {"op": "init", "items": ["one", "two"]})
    _invoke(services, tmp_path, {"op": "block", "task": "two", "reason": "waiting on ci"})
    contribution = todo_context(_context(tmp_path), services)
    assert contribution is not None
    assert contribution.content == (
        "Runtime-managed todo state is active for this session.\n"
        "Use this as the current plan truth; do not recreate it from older tool results.\n"
        "Use one todo operation at a time; view is read-only and mutations are persisted by the runtime.\n"
        "Current active todos:\n"
        "Tasks:\n"
        "- [in_progress] one\n"
        "- [blocked] two (blocked: waiting on ci)"
    )

    _invoke(services, tmp_path, {"op": "done", "task": "one"})
    _invoke(services, tmp_path, {"op": "done", "task": "two"})
    assert todo_context(_context(tmp_path), services) is None


def test_context_hides_completed_and_abandoned_tasks(tmp_path: Path) -> None:
    services = MockServices()
    _invoke(services, tmp_path, {"op": "init", "items": ["one", "two"]})
    _invoke(services, tmp_path, {"op": "done", "task": "one"})
    text = render_provider_state(_stored(services).phases)
    assert text is not None
    assert "- [completed] one" not in text
    assert "- [in_progress] two" in text


# --- Reminder policy ---


def _fact(tool: str, *, status: str = "ok", effects: tuple[str, ...] = ("write",)) -> InvocationFinishedFact:
    return InvocationFinishedFact(
        session_id="session-1",
        run_id="run-1",
        revision_id="rev-1",
        logical_turn="turn-1",
        event_sequence=1,
        tool_name=tool,
        tool_call_id="c1",
        status=status,  # type: ignore[arg-type]
        effects=effects,
    )


def test_mutation_count_uses_declared_effects_and_todo_touch(tmp_path: Path) -> None:
    _ = tmp_path
    facts = [
        _fact("write"),
        _fact("read", effects=("read",)),
        _fact("todo", effects=("session",)),
        _fact("write"),
        _fact("write", status="error"),
        _fact("glob", effects=("read",)),
        _fact("write"),
    ]
    assert todo_mutation_count(facts, own_tool_names=frozenset({"todo"})) == 2

    no_touch = [_fact("write"), _fact("read", effects=("read",)), _fact("write")]
    assert todo_mutation_count(no_touch, own_tool_names=frozenset({"todo"})) == 2


def test_mid_run_nudge_threshold_budget_and_reset() -> None:
    silent = decide_todo_mid_run_nudge(
        state=TodoMidRunState(cycle_run_id="run-1"),
        run_id="run-1",
        mutations=TODO_MID_RUN_MUTATION_THRESHOLD - 1,
        incomplete_count=3,
    )
    assert silent.injects is False

    first = decide_todo_mid_run_nudge(
        state=TodoMidRunState(cycle_run_id="run-1"),
        run_id="run-1",
        mutations=TODO_MID_RUN_MUTATION_THRESHOLD,
        incomplete_count=3,
    )
    assert first.injects is True
    assert first.state == TodoMidRunState(attempts=1, mutations=0, mutation_baseline=TODO_MID_RUN_MUTATION_THRESHOLD, cycle_run_id="run-1")
    assert first.text == (
        '<system-reminder reason="todo_stale">\n'
        "12 mutating tool call(s) have run since the last todo update, and 3 todo item(s) are still open.\n"
        "Reconcile the list before continuing: mark finished work done and record what is left.\n"
        "(Mid-run nudge 1/2)\n"
        "</system-reminder>"
    )

    second = decide_todo_mid_run_nudge(
        state=first.state,
        run_id="run-1",
        mutations=TODO_MID_RUN_MUTATION_THRESHOLD * 2,
        incomplete_count=3,
    )
    assert second.injects is True
    assert second.state.attempts == 2
    capped = decide_todo_mid_run_nudge(
        state=second.state,
        run_id="run-1",
        mutations=TODO_MID_RUN_MUTATION_THRESHOLD * 3,
        incomplete_count=3,
    )
    assert capped.injects is False
    assert capped.state.attempts == 2
    fresh_cycle = decide_todo_mid_run_nudge(
        state=second.state,
        run_id="run-2",
        mutations=TODO_MID_RUN_MUTATION_THRESHOLD,
        incomplete_count=3,
    )
    assert fresh_cycle.injects is True
    assert fresh_cycle.state.attempts == 1


def test_mid_run_nudge_text_matches_legacy_shape() -> None:
    assert todo_mid_run_nudge_text(mutation_count=12, incomplete_count=1, attempt=2, max_attempts=2).startswith(
        '<system-reminder reason="todo_stale">'
    )


def test_completion_reminder_budget_watermark_and_cycle() -> None:
    phases = (("Tasks", ("one", "two")),)
    first = decide_todo_reminder(
        max_per_cycle=DEFAULT_TODO_REMINDER_MAX_PER_CYCLE,
        state=TodoReminderState(cycle_run_id="run-1"),
        run_id="run-1",
        incomplete_phases=phases,
        tool_result_count=5,
    )
    assert first.injects is True
    assert first.state == TodoReminderState(attempts=1, awaiting_progress=True, progress_watermark=5, cycle_run_id="run-1")
    assert first.text == (
        "<system-reminder>\n"
        "You stopped with 2 incomplete todo item(s):\n"
        "- Tasks\n"
        "  - one\n"
        "  - two\n"
        "\n"
        "Please continue working on these tasks or mark them complete if finished.\n"
        "(Reminder 1/3)\n"
        "</system-reminder>"
    )

    stalled = decide_todo_reminder(
        max_per_cycle=DEFAULT_TODO_REMINDER_MAX_PER_CYCLE,
        state=first.state,
        run_id="run-1",
        incomplete_phases=phases,
        tool_result_count=5,
    )
    assert stalled.injects is False

    progressed = decide_todo_reminder(
        max_per_cycle=DEFAULT_TODO_REMINDER_MAX_PER_CYCLE,
        state=first.state,
        run_id="run-1",
        incomplete_phases=phases,
        tool_result_count=9,
    )
    assert progressed.injects is True
    assert progressed.state.attempts == 2
    assert progressed.state.progress_watermark == 9

    finished = decide_todo_reminder(
        max_per_cycle=DEFAULT_TODO_REMINDER_MAX_PER_CYCLE,
        state=progressed.state,
        run_id="run-1",
        incomplete_phases=(),
        tool_result_count=12,
    )
    assert finished.injects is False
    assert finished.state == TodoReminderState(cycle_run_id="run-1")

    exhausted_state = TodoReminderState(attempts=DEFAULT_TODO_REMINDER_MAX_PER_CYCLE, cycle_run_id="run-1")
    exhausted = decide_todo_reminder(
        max_per_cycle=DEFAULT_TODO_REMINDER_MAX_PER_CYCLE,
        state=exhausted_state,
        run_id="run-1",
        incomplete_phases=phases,
        tool_result_count=20,
    )
    assert exhausted.injects is False
    assert exhausted.state.attempts == DEFAULT_TODO_REMINDER_MAX_PER_CYCLE


def test_completion_reminder_incomplete_projection() -> None:
    phases = (("A", ("x",)), ("B", ("y", "z")))
    assert incomplete_todo_phases(({"name": "A", "tasks": [{"content": "done", "status": "completed"}]},)) == ()
    assert (
        incomplete_todo_phases(
            tuple({"name": name, "tasks": [{"content": content, "status": "pending"} for content in contents]} for name, contents in phases)
        )
        == phases
    )

    assert todo_reminder_text((("A", ("x",)),), attempt=1, max_attempts=3).startswith(
        "<system-reminder>\nYou stopped with 1 incomplete todo item(s):"
    )


# --- Plugin registration ---


def test_create_plugin_registers_tool_commands_and_context() -> None:
    plugin = create_plugin({})
    assert [tool.definition.name for tool in plugin.tools] == ["todo"]
    assert [command.definition.name for command in plugin.commands] == ["todo", "todo.clear"]
    assert plugin.context is todo_context
    assert plugin.observer is not None
    assert plugin.migrate_state is migrate_todo_state


# --- Reminder wiring ---


def _invocation_fact(tool: str, *, status: str = "ok", effects: tuple[str, ...] = ("write",), owner: str = "") -> InvocationFinishedFact:
    return InvocationFinishedFact(
        session_id="session-1",
        run_id="run-1",
        revision_id="rev-1",
        logical_turn="turn-1",
        event_sequence=1,
        tool_name=tool,
        tool_call_id="call-1",
        status=status,  # type: ignore[arg-type]
        owner=owner,
        effects=effects,
    )


def _run_fact(*, status: str = "completed", stop_cause: str | None = "completed") -> RunFinishedFact:
    return RunFinishedFact(
        session_id="session-1",
        run_id="run-1",
        revision_id="rev-1",
        logical_turn="turn-1",
        event_sequence=2,
        status=status,  # type: ignore[arg-type]
        stop_cause=stop_cause,  # type: ignore[arg-type]
    )


def test_todo_config_defaults_and_validation() -> None:
    assert parse_todo_config(None) == TodoRemindersConfig(enabled=True, max_per_cycle=3)
    parsed = parse_todo_config({"reminders": {"enabled": False, "todo": {"max_per_cycle": 5}}})
    assert parsed == TodoRemindersConfig(enabled=False, max_per_cycle=5)
    with pytest.raises(ValueError, match="field 'other' is not supported"):
        parse_todo_config({"other": 1})
    with pytest.raises(ValueError, match="reminders.enabled must be a boolean"):
        parse_todo_config({"reminders": {"enabled": "yes"}})
    with pytest.raises(ValueError, match="max_per_cycle must be a non-negative integer"):
        parse_todo_config({"reminders": {"todo": {"max_per_cycle": -1}}})


def test_state_v2_round_trip_and_migration_from_v1() -> None:
    payload = _raw_state(({"name": "A", "tasks": [{"content": "x", "status": "pending"}]},), revision=2)
    state = TodoState.from_payload(payload)
    assert state.cycle_run_id is None
    assert state.invocations == 0
    assert TodoState.from_payload(state.to_payload()) == state

    v1 = {
        "schema_version": 1,
        "revision": 2,
        "phases": payload["phases"],
        "summary": payload["summary"],
    }
    migrated = migrate_todo_state(v1, 1, 2)
    assert migrated["schema_version"] == 2
    assert migrated["reminders"]["todo"]["attempts"] == 0
    assert TodoState.from_payload(migrated).revision == 2
    with pytest.raises(ValueError, match="supports 1 -> 2"):
        migrate_todo_state(v1, 1, 3)


def test_reminder_state_parse_rejects_unknown_and_missing_fields() -> None:
    payload = _raw_state((), revision=0)
    payload["reminders"]["todo"]["extra"] = 1
    with pytest.raises(ValueError, match="reminders.todo field 'extra' is not supported"):
        TodoState.from_payload(payload)

    payload = _raw_state((), revision=0)
    del payload["reminders"]["invocations"]
    with pytest.raises(ValueError, match="reminders is missing field"):
        TodoState.from_payload(payload)

    payload = _raw_state((), revision=0)
    payload["reminders"]["todo"]["awaiting_progress"] = "yes"
    with pytest.raises(ValueError, match="'awaiting_progress' must be a boolean"):
        TodoState.from_payload(payload)


def test_observer_mid_run_nudge_fires_once_at_the_threshold(tmp_path: Path) -> None:
    services = MockServices()
    services.facts = [_request(1, "run-1")]
    _invoke(services, tmp_path, {"op": "init", "items": ["one", "two"]})

    for _ in range(TODO_MID_RUN_MUTATION_THRESHOLD - 1):
        todo_observer(_invocation_fact("write"), _context(tmp_path), services)
    assert services.staged_continuations == []

    todo_observer(_invocation_fact("write"), _context(tmp_path), services)
    assert len(services.staged_continuations) == 1
    payload, key, role = services.staged_continuations[0]
    assert role == "user"
    assert payload["kind"] == TODO_MID_RUN_KIND
    assert payload["attempt"] == 1
    assert payload["mutation_count"] == TODO_MID_RUN_MUTATION_THRESHOLD
    assert payload["incomplete_count"] == 2
    assert payload["text"] == todo_mid_run_nudge_text(mutation_count=TODO_MID_RUN_MUTATION_THRESHOLD, incomplete_count=2, attempt=1, max_attempts=2)
    assert key == "todo:todo_mid_run:run-1:1"
    assert services.staged_events[-1][0] == "reminder_injected"
    assert services.staged_events[-1][1]["reminder_type"] == TODO_MID_RUN_KIND

    # The counter resets at the nudge: another threshold must pass before the next one.
    todo_observer(_invocation_fact("write"), _context(tmp_path), services)
    assert len(services.staged_continuations) == 1
    stored = _stored(services)
    assert stored.mid_run.attempts == 1
    assert stored.invocations == TODO_MID_RUN_MUTATION_THRESHOLD + 1


def test_observer_nudge_ignores_read_tier_and_resets_on_todo_touch(tmp_path: Path) -> None:
    services = MockServices()
    services.facts = [_request(1, "run-1")]
    _invoke(services, tmp_path, {"op": "init", "items": ["one"]})

    for _ in range(20):
        todo_observer(_invocation_fact("read", effects=("read",)), _context(tmp_path), services)
    assert services.staged_continuations == [], "read-tier calls are not mutations"

    for _ in range(TODO_MID_RUN_MUTATION_THRESHOLD):
        todo_observer(_invocation_fact("write"), _context(tmp_path), services)
    todo_observer(_invocation_fact("todo", effects=("session",), owner="todo"), _context(tmp_path), services)
    stored = _stored(services)
    assert stored.mid_run.mutations == 0
    assert stored.mid_run.mutation_baseline == 0

    for _ in range(TODO_MID_RUN_MUTATION_THRESHOLD - 1):
        todo_observer(_invocation_fact("edit"), _context(tmp_path), services)
    assert len(services.staged_continuations) == 1, "a todo touch restarts the mutation count"


def test_observer_completion_reminder_is_a_user_role_continuation(tmp_path: Path) -> None:
    services = MockServices()
    services.facts = [_request(1, "run-1")]
    _invoke(services, tmp_path, {"op": "init", "items": ["one", "two"]})
    _invoke(services, tmp_path, {"op": "block", "task": "two", "reason": "waiting"})
    todo_observer(_invocation_fact("write"), _context(tmp_path), services)

    todo_observer(_run_fact(), _context(tmp_path), services)
    assert len(services.staged_continuations) == 1
    payload, key, role = services.staged_continuations[0]
    assert role == "user"
    assert payload["kind"] == TODO_REMINDER_KIND
    assert payload["attempt"] == 1
    # Only pending/in_progress count as incomplete; the blocked task is listed by neither.
    assert payload["incomplete_count"] == 1
    assert payload["text"] == todo_reminder_text((("Tasks", ("one",)),), attempt=1, max_attempts=3)
    assert key == "todo:todo:run-1:1"

    # Same watermark + awaiting progress: a second finish without new work stays silent.
    todo_observer(_run_fact(), _context(tmp_path), services)
    assert len(services.staged_continuations) == 1

    # Fresh work moves the watermark and earns the next reminder.
    todo_observer(_invocation_fact("write"), _context(tmp_path), services)
    todo_observer(_run_fact(), _context(tmp_path), services)
    assert len(services.staged_continuations) == 2
    assert services.staged_continuations[-1][0]["attempt"] == 2


def test_observer_skips_reminders_for_unfinished_runs_and_finished_lists(tmp_path: Path) -> None:
    services = MockServices()
    services.facts = [_request(1, "run-1")]
    _invoke(services, tmp_path, {"op": "init", "items": ["one"]})

    todo_observer(_run_fact(status="failed", stop_cause="error"), _context(tmp_path), services)
    todo_observer(_run_fact(status="interrupted", stop_cause="user_stop"), _context(tmp_path), services)
    assert services.staged_continuations == []

    _invoke(services, tmp_path, {"op": "done", "task": "one"})
    todo_observer(_run_fact(), _context(tmp_path), services)
    assert services.staged_continuations == []
    assert _stored(services).todo_reminder.attempts == 0


def test_new_user_prompt_starts_a_fresh_reminder_cycle(tmp_path: Path) -> None:
    services = MockServices()
    services.facts = [_request(1, "run-1")]
    _invoke(services, tmp_path, {"op": "init", "items": ["one"]})
    config = TodoRemindersConfig(max_per_cycle=1)

    todo_observer(_run_fact(), _context(tmp_path), services, config=config)
    assert len(services.staged_continuations) == 1
    todo_observer(_run_fact(), _context(tmp_path), services, config=config)
    assert len(services.staged_continuations) == 1, "the per-cycle budget is exhausted"

    services.facts.append(_request(2, "run-2"))
    todo_observer(_run_fact(), _context(tmp_path), services, config=config)
    assert len(services.staged_continuations) == 2, "a new user prompt opens a new cycle"
    assert _stored(services).cycle_run_id == "run-2"
    assert services.staged_continuations[-1][0]["attempt"] == 1

    services.facts.append(replace(_run_fact(), event_sequence=3, run_id="run-3"))
    todo_observer(_run_fact(), _context(tmp_path), services, config=config)
    assert len(services.staged_continuations) == 2, "a continuation run stays in the same cycle"
    services.facts.append(_request(4, "run-2", resumed=True))
    todo_observer(_run_fact(), _context(tmp_path), services, config=config)
    assert len(services.staged_continuations) == 2, "an explicit resume cannot replenish the cycle budget"
    assert _stored(services).cycle_run_id == "run-2"
    assert _stored(services).todo_reminder.attempts == 1


def test_disabled_reminders_config_never_enqueues(tmp_path: Path) -> None:
    services = MockServices()
    services.facts = [_request(1, "run-1")]
    _invoke(services, tmp_path, {"op": "init", "items": ["one"]})
    config = TodoRemindersConfig(enabled=False)
    for _ in range(TODO_MID_RUN_MUTATION_THRESHOLD + 1):
        todo_observer(_invocation_fact("write"), _context(tmp_path), services, config=config)
    todo_observer(_run_fact(), _context(tmp_path), services, config=config)
    assert services.staged_continuations == []


def test_cycle_scan_advances_through_empty_source_pages_without_resetting_on_resume(tmp_path: Path):
    services = MockServices()
    services.facts = [_request(1, "run-1")]
    _invoke(services, tmp_path, {"op": "init", "items": ["one"]})
    config = TodoRemindersConfig(max_per_cycle=1)
    todo_observer(_run_fact(), _context(tmp_path), services, config=config)
    assert len(services.staged_continuations) == 1

    pages = iter((FactPage((), 201, True), FactPage((_request(202, "run-1", resumed=True),), 401, True), FactPage((), 405, False)))
    cursors = []

    def read_facts(*, after_sequence, limit):
        cursors.append(after_sequence)
        return next(pages)

    services.read_facts = read_facts
    todo_observer(_run_fact(), _context(tmp_path), services, config=config)
    assert cursors == [1, 201, 401]
    assert _stored(services).cursor_sequence == 405
    assert _stored(services).cycle_run_id == "run-1"
    assert len(services.staged_continuations) == 1
