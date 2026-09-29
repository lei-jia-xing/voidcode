"""Mid-run todo nudge: stale todo list between provider calls.

Upstream ``takeMidRunNudge`` semantics: while a turn is still running, at least 12
mutation-tool results since the last todo touch earn one per-call nudge, at most
2 per cycle, suppressed for plan mode / no todo tool / parked loops. The nudge
rides the existing reminder channel (``per_call=True`` tail segment): never
persisted, never in the cache prefix.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from voidcode.runtime.config import RuntimeRemindersConfig
from voidcode.runtime.contracts import RuntimeRequest
from voidcode.runtime.events import RUNTIME_REMINDER_INJECTED
from voidcode.runtime.permission import PermissionPolicy
from voidcode.runtime.reminders import (
    TODO_MID_RUN_KIND,
    TODO_MID_RUN_MUTATION_THRESHOLD,
    ReminderSuppression,
    TodoMidRunState,
    decide_todo_mid_run_nudge,
    todo_mid_run_state_from_metadata,
)
from voidcode.runtime.service import ToolRegistry, VoidCodeRuntime
from voidcode.runtime.storage import SqliteSessionStore
from voidcode.tools.contracts import ToolCall
from voidcode.tools.glob import GlobTool
from voidcode.tools.todo import TodoTool
from voidcode.tools.write import WriteTool

from .test_todo_reminder import (
    _TODO_INIT_CALL,
    SESSION_ID,
    _provider_engine_config,
    _reminder_segments,
    _ScriptedGraph,
    _Step,
)

#: ``write`` is the allowlisted mutating builtin (``read_only=False``), so the
#: mutation counter is exercised through real permission-semantics metadata
#: rather than a test-only tool outside the manifest allowlist.
MUTATING_TOOL = "write"


def _mutations(start: int, count: int = TODO_MID_RUN_MUTATION_THRESHOLD) -> list[_Step]:
    return [
        _Step(tool_call=ToolCall(tool_name=MUTATING_TOOL, arguments={"path": f"probe-{index}.txt", "content": f"x{index}"}))
        for index in range(start, start + count)
    ]


class _NudgeGraph(_ScriptedGraph):
    """Scripted graph that keeps serving its last step: extra provider calls are tolerated."""

    def step(self, request: Any, tool_results: tuple[Any, ...], *, session: Any) -> Any:
        self.seen_segments.append(tuple(request.assembled_context.segments))
        if not self._script:
            raise AssertionError("the scripted graph ran out of steps")
        return self._script[0] if len(self._script) == 1 else self._script.pop(0)


def _run(tmp_path: Path, script: list[_Step], *, tools: tuple[Any, ...] = (TodoTool(), GlobTool(), WriteTool())) -> tuple[list[Any], _ScriptedGraph]:
    graph = _NudgeGraph(script)
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        tool_registry=ToolRegistry.from_tools(list(tools)),
        graph=graph,
        config=_provider_engine_config(reminders=RuntimeRemindersConfig()),
        permission_policy=PermissionPolicy(mode="yolo"),
    )
    chunks = list(runtime.run_stream(RuntimeRequest(prompt="go", session_id=SESSION_ID)))
    return chunks, graph


def _events(chunks: list[Any], event_type: str) -> list[Any]:
    return [chunk.event for chunk in chunks if chunk.kind == "event" and chunk.event is not None and chunk.event.event_type == event_type]


def _nudges(chunks: list[Any]) -> list[Any]:
    return [event for event in _events(chunks, RUNTIME_REMINDER_INJECTED) if event.payload["reminder_type"] == TODO_MID_RUN_KIND]


def _nudge_segments(context: Any) -> list[Any]:
    return [segment for segment in _reminder_segments(context) if (segment.metadata or {}).get("reminder_type") == TODO_MID_RUN_KIND]


def _mutation_script(mutations: int, *, tail: list[_Step]) -> list[_Step]:
    return [_Step(tool_call=_TODO_INIT_CALL), *_mutations(0, mutations), *tail]


def _stored_state(tmp_path: Path) -> TodoMidRunState:
    stored = SqliteSessionStore().load_session(workspace=tmp_path, session_id=SESSION_ID)
    return todo_mid_run_state_from_metadata(stored.session.metadata)


# --- trigger -----------------------------------------------------------------


def test_stale_todos_earn_one_mid_run_nudge_without_ending_the_turn(tmp_path: Path) -> None:
    chunks, graph = _run(
        tmp_path,
        _mutation_script(TODO_MID_RUN_MUTATION_THRESHOLD, tail=[_Step(output="done", is_finished=True)]),
    )

    nudges = _nudges(chunks)
    assert len(nudges) == 1
    payload = nudges[0].payload
    assert payload["attempt"] == 1
    assert payload["max_attempts"] == 2
    assert payload["mutation_count"] >= TODO_MID_RUN_MUTATION_THRESHOLD
    assert payload["incomplete_todo_count"] == 2
    # Exactly one provider call carried the nudge -- a later call than the first,
    # because the mutation window had to accumulate first -- and the run went on to
    # its own terminal step instead of ending.
    carrying_calls = [index for index, context in enumerate(graph.seen_segments) if _nudge_segments(context)]
    assert len(carrying_calls) == 1
    assert carrying_calls[0] > 1
    text = _nudge_segments(graph.seen_segments[carrying_calls[0]])[0].content or ""
    assert "todo_stale" in text
    assert f"{payload['mutation_count']} mutating tool call(s)" in text
    assert chunks[-1].session.status == "completed"
    assert _stored_state(tmp_path).attempts == 1


def test_below_the_threshold_no_nudge(tmp_path: Path) -> None:
    chunks, _graph = _run(
        tmp_path,
        _mutation_script(TODO_MID_RUN_MUTATION_THRESHOLD - 1, tail=[_Step(output="done", is_finished=True)]),
    )

    assert _nudges(chunks) == []


def test_the_nudge_is_budgeted_per_cycle(tmp_path: Path) -> None:
    """Two windows of 12 mutations earn two nudges; the third window earns none."""
    chunks, _graph = _run(
        tmp_path,
        [_Step(tool_call=_TODO_INIT_CALL), *_mutations(0), *_mutations(12), *_mutations(24), _Step(output="done", is_finished=True)],
    )

    assert [event.payload["attempt"] for event in _nudges(chunks)] == [1, 2]
    assert _stored_state(tmp_path).attempts == 2


def test_a_todo_touch_resets_the_mutation_counter(tmp_path: Path) -> None:
    """A successful todo call clears the counter, so the next window starts over."""
    chunks, _graph = _run(
        tmp_path,
        [
            _Step(tool_call=_TODO_INIT_CALL),
            *_mutations(0),
            _Step(tool_call=ToolCall(tool_name="todo", arguments={"op": "view"})),
            *_mutations(12),
            _Step(output="done", is_finished=True),
        ],
    )

    assert [event.payload["attempt"] for event in _nudges(chunks)] == [1, 2]
    assert _stored_state(tmp_path).mutations == 0


# --- suppression --------------------------------------------------------------


def test_no_active_todo_tool_means_no_nudge() -> None:
    """The suppression predicate covers the active-tool-set case (upstream ``getActiveToolNames``)."""

    decision = decide_todo_mid_run_nudge(
        state=TodoMidRunState(cycle_run_id="run-1"),
        run_id="run-1",
        mutations=TODO_MID_RUN_MUTATION_THRESHOLD,
        incomplete_count=2,
        suppression=ReminderSuppression(todo_tool_available=False),
    )

    assert decision.injects is False


def test_plan_mode_means_no_nudge(tmp_path: Path) -> None:
    """Plan mode suppresses the nudge (upstream ``planModeEnabled``)."""

    graph = _NudgeGraph([_Step(tool_call=_TODO_INIT_CALL), _Step(output="planned", is_finished=True)])
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        tool_registry=ToolRegistry.from_tools([TodoTool(), GlobTool()]),
        graph=graph,
        config=_provider_engine_config(reminders=RuntimeRemindersConfig()),
        permission_policy=PermissionPolicy(mode="yolo"),
    )

    chunks = list(runtime.run_stream(RuntimeRequest(prompt="go", session_id=SESSION_ID, metadata={"mode": "plan"})))

    assert _nudges(chunks) == []


# --- channel semantics --------------------------------------------------------


def test_nudge_text_stays_out_of_the_persisted_transcript(tmp_path: Path) -> None:
    chunks, _graph = _run(
        tmp_path,
        _mutation_script(TODO_MID_RUN_MUTATION_THRESHOLD, tail=[_Step(output="done", is_finished=True)]),
    )

    stored = SqliteSessionStore().load_session(workspace=tmp_path, session_id=SESSION_ID)
    persisted = json.dumps({"events": [event.payload for event in stored.events], "metadata": stored.session.metadata}, sort_keys=True)
    assert "<system-reminder" not in persisted
    assert "todo_stale" not in persisted
    assert _stored_state(tmp_path).attempts == 1


# --- independence from the completion reminder --------------------------------


def test_both_reminder_kinds_count_independently(tmp_path: Path) -> None:
    """One run: the mid-run nudge and the terminal completion reminder each keep their own budget."""
    chunks, _graph = _run(
        tmp_path,
        _mutation_script(TODO_MID_RUN_MUTATION_THRESHOLD, tail=[_Step(output="stopping", is_finished=True), _Step(output="done", is_finished=True)]),
    )

    kinds = [event.payload["reminder_type"] for event in _events(chunks, RUNTIME_REMINDER_INJECTED)]
    assert kinds == [TODO_MID_RUN_KIND, "todo"]
    stored = SqliteSessionStore().load_session(workspace=tmp_path, session_id=SESSION_ID)
    runtime_state = stored.session.metadata["runtime_state"]["reminders"]
    assert set(runtime_state) == {"todo", "todo_mid_run"}
    assert runtime_state["todo"]["attempts"] == 1
    assert runtime_state["todo_mid_run"]["attempts"] == 1
