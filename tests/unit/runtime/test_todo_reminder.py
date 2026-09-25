"""Todo completion reminders on the runtime's per-call reminder channel.

Contract (``docs/contracts/runtime-events.md`` → ``runtime.reminder_injected``):
a terminal assistant turn that still has ``pending``/``in_progress`` todos earns
one tail-appended per-call reminder — bounded per cycle, suspended while the
previous reminder still awaits progress, and skipped while the session is parked
on a user answer, has a background task that will re-wake the loop, or is a
delegated child. The reminder reaches the provider for that call only: the
session keeps counters, the client gets one event, the transcript keeps nothing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from voidcode.hook.percall import percall_messages_sha256
from voidcode.provider.config import ProviderConfigs, ProviderEndpointConfig
from voidcode.runtime.background.models import BackgroundTaskRef, StoredBackgroundTaskSummary
from voidcode.runtime.config import RuntimeConfig, RuntimeMcpConfig, RuntimeRemindersConfig
from voidcode.runtime.context.continuity import (
    replayed_conversation_segments_from_segments,
    verified_checkpoint_session_metadata,
)
from voidcode.runtime.context.percall import segments_to_percall_messages
from voidcode.runtime.contracts import RuntimeRequest
from voidcode.runtime.events import RUNTIME_REMINDER_INJECTED
from voidcode.runtime.permission import PermissionPolicy
from voidcode.runtime.reminders import (
    REMINDERS_RUNTIME_STATE_KEY,
    TODO_REMINDER_SOURCE,
    ReminderSuppression,
    TodoReminderState,
    decide_todo_reminder,
    incomplete_todo_phases,
    todo_reminder_state_from_metadata,
    todo_reminder_state_from_payload,
)
from voidcode.runtime.run_loop import _runtime_waits_for_user
from voidcode.runtime.service import SessionState, ToolRegistry, VoidCodeRuntime
from voidcode.runtime.session import SessionRef
from voidcode.runtime.storage import SqliteSessionStore
from voidcode.runtime.todos import runtime_todo_phases_from_payload, todo_state_payload
from voidcode.tools.contracts import ToolCall
from voidcode.tools.glob import GlobTool
from voidcode.tools.todo import TodoTool

SESSION_ID = "reminder-session"
MODEL_NAME = "model"
_PROGRESS_CALL = ToolCall(tool_name="glob", arguments={"pattern": "*"})
_TODO_INIT_CALL = ToolCall(
    tool_name="todo",
    arguments={"op": "init", "list": [{"phase": "Phase 1", "items": ["write the reminder", "prove it"]}]},
)
_TODO_DONE_CALL = ToolCall(tool_name="todo", arguments={"op": "done"})


@dataclass(slots=True)
class _Step:
    tool_call: ToolCall | None = None
    output: str | None = None
    is_finished: bool = False
    events: tuple[object, ...] = ()
    reasoning: str | None = None
    provider_usage: object | None = None


class _ScriptedGraph:
    """Hands out scripted steps in order and records every assembled context."""

    def __init__(self, script: list[_Step]) -> None:
        self._script = list(script)
        self.seen_segments: list[tuple[Any, ...]] = []

    def step(self, request: Any, tool_results: tuple[Any, ...], *, session: Any) -> _Step:
        _ = tool_results, session
        self.seen_segments.append(tuple(request.assembled_context.segments))
        if not self._script:
            raise AssertionError("the scripted graph ran out of steps")
        return self._script.pop(0)


class _PendingBackgroundTaskStore(SqliteSessionStore):
    """Every parent session owns one running child, so the loop will be re-woken."""

    def list_background_tasks_by_parent_session(self, *, workspace: Path, parent_session_id: str) -> tuple[Any, ...]:
        _ = workspace, parent_session_id
        return (
            StoredBackgroundTaskSummary(
                task=BackgroundTaskRef(id="task-1"),
                status="running",
                prompt="pending child",
                session_id=None,
                error=None,
                created_at=0,
                updated_at=0,
            ),
        )


def _provider_engine_config(*, reminders: RuntimeRemindersConfig) -> RuntimeConfig:
    """Provider-engine config resolved against a declared stand-in provider.

    The scripted graph replaces the model call, so the run is offline while the
    runtime still takes its provider-engine path (the reminder channel targets a
    model call, so it is gated on that engine).
    """

    return RuntimeConfig(
        mcp=RuntimeMcpConfig(enabled=False),
        execution_engine="provider",
        model=f"session/{MODEL_NAME}",
        providers=ProviderConfigs(custom={"session": ProviderEndpointConfig()}),
        reminders=reminders,
    )


def _runtime(
    tmp_path: Path,
    graph: _ScriptedGraph,
    *,
    reminders: RuntimeRemindersConfig | None = None,
    session_store: SqliteSessionStore | None = None,
    execution_engine: str = "provider",
) -> VoidCodeRuntime:
    config = (
        _provider_engine_config(reminders=reminders or RuntimeRemindersConfig())
        if execution_engine == "provider"
        else RuntimeConfig(
            mcp=RuntimeMcpConfig(enabled=False),
            execution_engine="deterministic",
            reminders=reminders or RuntimeRemindersConfig(),
        )
    )
    return VoidCodeRuntime(
        workspace=tmp_path,
        session_store=session_store,
        tool_registry=ToolRegistry.from_tools([TodoTool(), GlobTool()]),
        graph=graph,
        config=config,
        permission_policy=PermissionPolicy(mode="allow"),
    )


def graph_calls(graph: _ScriptedGraph) -> int:
    return len(graph.seen_segments)


def _reminder_segments(segments: tuple[Any, ...]) -> list[Any]:
    return [segment for segment in segments if (segment.metadata or {}).get("source") == TODO_REMINDER_SOURCE]


def _run(
    tmp_path: Path,
    graph: _ScriptedGraph,
    *,
    reminders: RuntimeRemindersConfig | None = None,
    session_store: SqliteSessionStore | None = None,
    execution_engine: str = "provider",
) -> list[Any]:
    runtime = _runtime(tmp_path, graph, reminders=reminders, session_store=session_store, execution_engine=execution_engine)
    return list(runtime.run_stream(RuntimeRequest(prompt="go", session_id=SESSION_ID)))


def _events(chunks: list[Any]) -> list[Any]:
    return [chunk.event for chunk in chunks if chunk.kind == "event" and chunk.event is not None]


def _reminder_events(chunks: list[Any]) -> list[Any]:
    return [event for event in _events(chunks) if event.event_type == RUNTIME_REMINDER_INJECTED]


def _stored_session(tmp_path: Path) -> Any:
    return SqliteSessionStore().load_session(workspace=tmp_path, session_id=SESSION_ID)


# --- (a) terminal turn with unfinished todos injects exactly one reminder ----


def test_terminal_turn_with_unfinished_todos_injects_one_per_call_reminder(tmp_path: Path) -> None:
    graph = _ScriptedGraph(
        [_Step(tool_call=_TODO_INIT_CALL), _Step(output="done for now", is_finished=True), _Step(output="finished", is_finished=True)]
    )

    chunks = _run(tmp_path, graph)

    reminders = _reminder_events(chunks)
    assert [
        (event.payload["reminder_type"], event.payload["attempt"], event.payload["max_attempts"], event.payload["incomplete_todo_count"])
        for event in reminders
    ] == [("todo", 1, 3, 2)]
    # The reminder is a per-call tail segment of the continuation turn, appended
    # after everything the prompt assembly produced.
    assert _reminder_segments(graph.seen_segments[0]) == []
    reminder_segments = _reminder_segments(graph.seen_segments[2])
    assert len(reminder_segments) == 1
    assert graph.seen_segments[2][-1].content == reminder_segments[0].content
    assert reminder_segments[0].content == (
        "<system-reminder>\n"
        "You stopped with 2 incomplete todo item(s):\n"
        "- Phase 1\n"
        "  - write the reminder\n"
        "  - prove it\n"
        "\n"
        "Please continue working on these tasks or mark them complete if finished.\n"
        "(Reminder 1/3)\n"
        "</system-reminder>"
    )
    # The continuation turn ended the run: the reminder is not re-injected.
    assert len(graph.seen_segments) == 3
    assert chunks[-1].session.status == "completed"
    assert todo_reminder_state_from_metadata(_stored_session(tmp_path).session.metadata) == TodoReminderState(
        attempts=1,
        awaiting_progress=True,
        progress_watermark=1,
        cycle_run_id=_stored_session(tmp_path).session.metadata["runtime_state"]["run_id"],
    )


# --- (b) the per-cycle budget stops further reminders ------------------------


def test_reminder_budget_stops_after_max_per_cycle_attempts(tmp_path: Path) -> None:
    script: list[_Step] = [_Step(tool_call=_TODO_INIT_CALL)]
    for _attempt in range(3):
        script.append(_Step(output="stopping", is_finished=True))
        script.append(_Step(tool_call=_PROGRESS_CALL))
    script.append(_Step(output="stopping", is_finished=True))
    graph = _ScriptedGraph(script)

    chunks = _run(tmp_path, graph)

    assert [event.payload["attempt"] for event in _reminder_events(chunks)] == [1, 2, 3]
    # The budget is spent: the fourth terminal turn ran to completion unreminded.
    assert graph_calls(graph) == 8
    assert todo_reminder_state_from_metadata(_stored_session(tmp_path).session.metadata).attempts == 3


def test_repeated_stop_without_progress_does_not_repeat_the_reminder(tmp_path: Path) -> None:
    """A reminder still awaiting agent action suppresses the next one."""

    graph = _ScriptedGraph(
        [_Step(tool_call=_TODO_INIT_CALL), _Step(output="stopping", is_finished=True), _Step(output="stopping again", is_finished=True)]
    )

    chunks = _run(tmp_path, graph)

    assert len(_reminder_events(chunks)) == 1
    assert todo_reminder_state_from_metadata(_stored_session(tmp_path).session.metadata).attempts == 1


def test_counters_from_a_previous_cycle_are_ignored() -> None:
    """The budget belongs to one user-prompt cycle (``runtime_state.run_id``).

    A spent budget carried over from an earlier run (resume replays the stored
    metadata, and every run/resume gets a new ``run_id``) must not silence the
    first reminder of the new cycle.
    """

    decision = _decide(state=TodoReminderState(attempts=3, awaiting_progress=True, progress_watermark=9, cycle_run_id="run-0"))

    assert decision.injects is True
    assert (decision.attempt, decision.max_attempts) == (1, 3)
    assert decision.state.cycle_run_id == "run-1"


# --- (c) a finished todo list never earns a reminder -------------------------


def test_completed_todos_never_earn_a_reminder(tmp_path: Path) -> None:
    graph = _ScriptedGraph(
        [
            _Step(tool_call=_TODO_INIT_CALL),
            _Step(tool_call=_TODO_DONE_CALL),
            _Step(output="all done", is_finished=True),
        ]
    )

    chunks = _run(tmp_path, graph)

    assert _reminder_events(chunks) == []
    assert chunks[-1].session.status == "completed"
    # Nothing to remind about: no attempt is ever spent.
    assert todo_reminder_state_from_metadata(_stored_session(tmp_path).session.metadata).attempts == 0


def test_disabled_reminders_never_inject(tmp_path: Path) -> None:
    graph = _ScriptedGraph([_Step(tool_call=_TODO_INIT_CALL), _Step(output="stopping", is_finished=True)])

    chunks = _run(tmp_path, graph, reminders=RuntimeRemindersConfig(enabled=False))

    assert _reminder_events(chunks) == []
    assert REMINDERS_RUNTIME_STATE_KEY not in _stored_session(tmp_path).session.metadata["runtime_state"]


def test_non_provider_engine_never_injects(tmp_path: Path) -> None:
    """The channel nudges a model call; a non-provider graph has none."""

    graph = _ScriptedGraph([_Step(tool_call=_TODO_INIT_CALL), _Step(output="stopping", is_finished=True)])

    chunks = _run(tmp_path, graph, execution_engine="deterministic")

    assert _reminder_events(chunks) == []
    assert graph_calls(graph) == 2


# --- (e) the reminder is per-call only --------------------------------------


def test_reminder_text_stays_out_of_the_persisted_transcript(tmp_path: Path) -> None:
    graph = _ScriptedGraph([_Step(tool_call=_TODO_INIT_CALL), _Step(output="stopping", is_finished=True), _Step(output="finished", is_finished=True)])

    _run(tmp_path, graph)

    stored = _stored_session(tmp_path)
    persisted = json.dumps({"events": [event.payload for event in stored.events], "metadata": stored.session.metadata}, sort_keys=True)
    assert "<system-reminder>" not in persisted
    assert "You stopped with" not in persisted
    # Only the counters survive, and the reminder is bound as a per-call message
    # so it cannot move the cache prefix either.
    reminder = _reminder_segments(graph.seen_segments[2])
    assert len(reminder) == 1
    bound = segments_to_percall_messages(graph.seen_segments[2])
    without_reminder = segments_to_percall_messages(tuple(segment for segment in graph.seen_segments[2] if segment.content != reminder[0].content))
    assert percall_messages_sha256(bound) == percall_messages_sha256(without_reminder)


def test_reminder_counters_survive_checkpoint_verification(tmp_path: Path) -> None:
    """A counter advanced after the checkpoint was captured must not block resume."""

    todos = todo_state_payload(({"name": "Phase 1", "tasks": [{"content": "work", "status": "pending"}]},), revision=1)
    checkpoint_metadata: dict[str, object] = {"runtime_state": {"run_id": "run-1", "todos": todos}}
    stored_metadata: dict[str, object] = {
        "runtime_state": {
            "run_id": "run-1",
            "todos": todos,
            "reminders": {"todo": TodoReminderState(attempts=1, awaiting_progress=True, progress_watermark=2, cycle_run_id="run-1").payload()},
        }
    }

    verified = verified_checkpoint_session_metadata(checkpoint_metadata=checkpoint_metadata, stored_metadata=stored_metadata)

    assert verified == checkpoint_metadata


# --- skip conditions and budget boundaries (decision level) ------------------


def _decide(**overrides: Any) -> Any:
    arguments: dict[str, Any] = {
        "max_per_cycle": 3,
        "state": TodoReminderState(cycle_run_id="run-1"),
        "run_id": "run-1",
        "incomplete_phases": (("Phase 1", ("work",)),),
        "tool_result_count": 1,
        "suppression": ReminderSuppression(),
    }
    # The suppression flags stay addressable by name in this file's cases.
    suppression_keys = {"runtime_waits_for_user", "pending_background_task", "delegated_child", "plan_mode", "todo_tool_available"}
    suppression_overrides = {key: overrides.pop(key) for key in list(overrides) if key in suppression_keys}
    arguments.update(overrides)
    if suppression_overrides:
        arguments["suppression"] = ReminderSuppression(**suppression_overrides)
    return decide_todo_reminder(**arguments)


@pytest.mark.parametrize(
    "skip_reason",
    ["runtime_waits_for_user", "pending_background_task", "delegated_child"],
)
def test_terminal_turn_skips_the_reminder_while_the_loop_is_already_parked(skip_reason: str) -> None:
    decision = _decide(**{skip_reason: True})

    assert decision.injects is False
    # A transient park is not progress: the spent budget carries over.
    assert decision.state == TodoReminderState(cycle_run_id="run-1")


def test_runtime_waits_for_user_reads_the_parked_plan_state() -> None:
    """The loop's skip flag is the session's own parked-state truth."""

    def session(plan_state: dict[str, object] | None) -> SessionState:
        metadata: dict[str, object] = {} if plan_state is None else {"plan_state": plan_state}
        return SessionState(session=SessionRef(id=SESSION_ID), status="running", turn=1, metadata=metadata)

    assert _runtime_waits_for_user(session({"status": "waiting_question"})) is True
    assert _runtime_waits_for_user(session({"status": "waiting_approval"})) is True
    assert _runtime_waits_for_user(session({"status": "in_progress"})) is False
    assert _runtime_waits_for_user(session(None)) is False


def test_reminder_segment_is_never_replayed_history(tmp_path: Path) -> None:
    graph = _ScriptedGraph([_Step(tool_call=_TODO_INIT_CALL), _Step(output="stopping", is_finished=True), _Step(output="finished", is_finished=True)])

    _run(tmp_path, graph)

    reminder = _reminder_segments(graph.seen_segments[2])
    assert len(reminder) == 1
    # Replay keeps only ``replayed_conversation`` segments, so the reminder can
    # never come back as prior history on the next run or resume.
    assert replayed_conversation_segments_from_segments(reminder) == ()


def test_pending_background_task_suppresses_the_reminder_end_to_end(tmp_path: Path) -> None:
    graph = _ScriptedGraph([_Step(tool_call=_TODO_INIT_CALL), _Step(output="stopping", is_finished=True)])

    chunks = _run(tmp_path, graph, session_store=_PendingBackgroundTaskStore())

    assert _reminder_events(chunks) == []
    assert chunks[-1].session.status == "completed"


def test_reminder_skipped_when_all_todos_finish_after_a_previous_attempt() -> None:
    decision = _decide(
        state=TodoReminderState(attempts=2, awaiting_progress=False, progress_watermark=5, cycle_run_id="run-1"),
        incomplete_phases=(),
        tool_result_count=7,
    )

    assert decision.injects is False
    assert decision.state == TodoReminderState(cycle_run_id="run-1")


def test_attempt_budget_boundary_is_exclusive() -> None:
    at_budget = _decide(state=TodoReminderState(attempts=3, awaiting_progress=False, cycle_run_id="run-1"))
    below_budget = _decide(state=TodoReminderState(attempts=2, awaiting_progress=False, cycle_run_id="run-1"))

    assert at_budget.injects is False
    assert below_budget.injects is True
    assert below_budget.attempt == 3


def test_incomplete_todo_phases_project_only_unfinished_tasks() -> None:
    phases = runtime_todo_phases_from_payload(
        [
            {
                "name": "Phase 1",
                "tasks": [
                    {"content": "pending task", "status": "pending"},
                    {"content": "running task", "status": "in_progress"},
                    {"content": "done task", "status": "completed"},
                ],
            },
            {"name": "Phase 2", "tasks": [{"content": "closed task", "status": "abandoned"}]},
        ]
    )

    assert incomplete_todo_phases(phases) == (("Phase 1", ("pending task", "running task")),)


def test_reminder_state_parser_rejects_a_drifted_payload() -> None:
    with pytest.raises(ValueError, match="is not supported"):
        todo_reminder_state_from_payload(
            {"attempts": 1, "awaiting_progress": True, "progress_watermark": 0, "cycle_run_id": "run-1", "reminder_text": "nope"}
        )
    with pytest.raises(ValueError, match="is missing field"):
        todo_reminder_state_from_payload({"attempts": 1})
