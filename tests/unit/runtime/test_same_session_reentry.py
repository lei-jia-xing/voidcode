"""Same-session re-entry: what a second run / resume / steer may do while one run is in flight.

The invariant under test is the one written down in
``docs/contracts/execution-lifecycle.md`` → 「同一 session 的并发所有权」:

* a live run owns the session's event stream and appends to it incrementally;
* a fresh run on the same session is allowed to append (the terminal seal belongs
  to the last active run, so an older-finishing run leaves the row writable);
* a checkpoint resume re-executes a turn and rewrites the persisted tail, so it is
  refused while a run is in flight — history another run still owns is never
  truncated;
* steering is queued for the next run and never applied mid-run (covered by
  ``tests/unit/runtime/test_session_lifecycle_seal.py``).
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, cast

import pytest

from voidcode.core.tool_context import ToolContext
from voidcode.core.turns import FinalTurn, ToolTurn
from voidcode.runtime.config import RuntimeConfig, RuntimeMcpConfig
from voidcode.runtime.contracts import RuntimeRequest, RuntimeRequestError
from voidcode.runtime.permission import PermissionPolicy
from voidcode.runtime.service import ToolRegistry, VoidCodeRuntime
from voidcode.runtime.storage import SqliteSessionStore
from voidcode.tools.contracts import TextOutput, ToolCall, ToolDefinition, ToolEffect, ToolResult, ToolSuccess

SESSION_ID = "reentry-session"
_GATED_TOOL = "gated_tool"


class _GatedTool:
    """Mutating tool that blocks until the test releases it."""

    definition = ToolDefinition(name=_GATED_TOOL, description="Blocks until released.", effects=frozenset({ToolEffect.WRITE}))

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def invoke(self, call: ToolCall, *, context: ToolContext) -> ToolResult:
        _ = call, context
        self.started.set()
        assert self.release.wait(timeout=20.0), "the gated tool was never released"
        return ToolSuccess(tool_name=self.definition.name, output=TextOutput("gated done"))


class _SingleToolGraph:
    def __init__(self, tool_name: str, *, output: str = "done") -> None:
        self._tool_name = tool_name
        self._output = output

    def produce(self, request: object, tool_results: tuple[object, ...], *, session: object) -> FinalTurn | ToolTurn:
        _ = request, session
        if not tool_results:
            return ToolTurn(calls=(ToolCall(tool_name=self._tool_name, arguments={}),))
        return FinalTurn(output=self._output)


class _ImmediateGraph:
    def produce(self, request: object, tool_results: tuple[object, ...], *, session: object) -> FinalTurn:
        _ = request, tool_results, session
        return FinalTurn(output="second run done")


def _runtime(workspace: Path, *, tool: object | None, turn_producer: object) -> VoidCodeRuntime:
    tools = [] if tool is None else [tool]
    return VoidCodeRuntime(
        workspace=workspace,
        tool_registry=ToolRegistry.from_tools(tools),
        turn_producer=turn_producer,
        config=RuntimeConfig(mcp=RuntimeMcpConfig(enabled=False), execution_engine="deterministic"),
        permission_policy=PermissionPolicy(mode="yolo"),
    )


def _entries(workspace: Path, session_id: str = SESSION_ID) -> list[str]:
    stored = SqliteSessionStore().load_session(workspace=workspace, session_id=session_id)
    return [f"{event.sequence}:{event.event_type}" for event in stored.events]


def _status(workspace: Path, session_id: str = SESSION_ID) -> str:
    return SqliteSessionStore().load_session_status(workspace=workspace, session_id=session_id)


def _checkpoint(workspace: Path, session_id: str = SESSION_ID) -> dict[str, object] | None:
    return SqliteSessionStore().load_resume_checkpoint(workspace=workspace, session_id=session_id)


class _InFlightRun:
    """One run executed on its own thread, observable while it is in flight."""

    def __init__(self, runtime: VoidCodeRuntime, tool: _GatedTool | None) -> None:
        self.runtime = runtime
        self.tool = tool
        self.chunks: list[Any] = []
        self.errors: list[BaseException] = []
        self.thread = threading.Thread(target=self._run, name="reentry-first-run")
        self.thread.start()
        if tool is not None:
            assert tool.started.wait(timeout=20.0), "the first run never reached the gated tool"

    def _run(self) -> None:
        try:
            self.chunks.extend(self.runtime.run_stream(RuntimeRequest(prompt="first run", session_id=SESSION_ID)))
        except BaseException as exc:  # noqa: BLE001 — the test asserts on the recorded error
            self.errors.append(exc)

    def finish(self) -> None:
        if self.tool is not None:
            self.tool.release.set()
        self.thread.join(timeout=20.0)
        assert not self.thread.is_alive(), "the first run did not finish after being released"
        assert self.errors == [], f"the first run failed: {self.errors}"
        assert self.chunks[-1].session.status == "completed"


def test_a_second_run_appends_without_rewriting_the_in_flight_history(tmp_path: Path) -> None:
    """A concurrent fresh run appends; no event of the in-flight run is lost or resequenced.

    The second run cannot seal the row either: the seal belongs to the last
    active run, so the still-running first run keeps a writable row.
    """
    tool = _GatedTool()
    first = _InFlightRun(_runtime(tmp_path, tool=tool, turn_producer=_SingleToolGraph(_GATED_TOOL)), tool)
    in_flight_prompt = "steer while the first run is in flight"
    # ``queue_steering`` is the queued re-entry path: accepted while the run is
    # active, delivered to the next run, never injected mid-run.
    assert len(first.runtime.queue_steering(SESSION_ID, content=in_flight_prompt)) == 1
    before = _entries(tmp_path)
    assert _status(tmp_path) == "interrupted"

    second_chunks = list(
        _runtime(tmp_path, tool=None, turn_producer=_ImmediateGraph()).run_stream(RuntimeRequest(prompt="second run", session_id=SESSION_ID))
    )

    assert second_chunks[-1].session.status == "completed"
    after_second = _entries(tmp_path)
    # Nothing was rewritten: the in-flight run's prefix is still there in order.
    assert after_second[: len(before)] == before
    assert len(after_second) > len(before)
    # ...and the row is still writable for the run that is still in flight: the
    # seal belongs to the last active run, not to the first one to finish.
    assert _status(tmp_path) == "interrupted"

    first.finish()
    final = _entries(tmp_path)
    assert final[: len(before)] == before
    assert _status(tmp_path) == "completed"
    # Every event survived the other run's seal: the second run's appends are
    # still in the log and the tool result of the first run is durable once.
    assert set(after_second).issubset(final)
    assert len(final) == len(after_second) + 1
    assert sum(1 for entry in final if entry.endswith(":runtime.tool_completed")) == 1


def test_resume_is_refused_while_a_run_owns_the_session_and_rewrites_nothing(tmp_path: Path) -> None:
    """A checkpoint resume is refused while a run is in flight, and touches no truth.

    A resume truncates the persisted tail and re-executes the turn; doing that
    under a live run would delete events that run still owns.
    """
    tool = _GatedTool()
    first = _InFlightRun(_runtime(tmp_path, tool=tool, turn_producer=_SingleToolGraph(_GATED_TOOL)), tool)
    before = _entries(tmp_path)
    checkpoint_before = _checkpoint(tmp_path)
    assert before and _status(tmp_path) == "interrupted"

    resume_runtime = _runtime(tmp_path, tool=None, turn_producer=_ImmediateGraph())
    with pytest.raises(RuntimeRequestError, match="has a run in flight"):
        resume_runtime.resume(SESSION_ID)
    with pytest.raises(RuntimeRequestError, match="has a run in flight"):
        next(resume_runtime.resume_stream(SESSION_ID))

    # The refused resume rewrote nothing: same events, same checkpoint.
    assert _entries(tmp_path) == before
    assert _checkpoint(tmp_path) == checkpoint_before
    assert _status(tmp_path) == "interrupted"

    first.finish()
    final = _entries(tmp_path)
    assert final[: len(before)] == before
    assert _status(tmp_path) == "completed"
    assert sum(1 for entry in final if entry.endswith(":runtime.tool_completed")) == 1


class _PromptRecordingGraph:
    """One tool call, then finish — recording every prompt the loop hands it."""

    def __init__(self, tool_name: str) -> None:
        self._tool_name = tool_name
        self.prompts: list[str] = []
        self._lock = threading.Lock()

    def produce(self, request: Any, tool_results: tuple[object, ...], *, session: object) -> FinalTurn | ToolTurn:
        _ = session
        with self._lock:
            self.prompts.append(cast(str, request.prompt))
        if not tool_results:
            return ToolTurn(calls=(ToolCall(tool_name=self._tool_name, arguments={}),))
        return FinalTurn(output="done")


def test_steering_while_a_run_is_in_flight_is_queued_until_the_next_turn(tmp_path: Path) -> None:
    """Steering is an outbox entry applied at the next turn boundary, never a mid-run event.

    The active run keeps its own event stream: the queued message changes the
    next turn's prompt, and it is gone from the session's outbox once that turn
    has consumed it.
    """
    tool = _GatedTool()
    graph = _PromptRecordingGraph(_GATED_TOOL)
    first = _InFlightRun(_runtime(tmp_path, tool=tool, turn_producer=graph), tool)
    before = _entries(tmp_path)

    queued = first.runtime.queue_steering(SESSION_ID, content="queued steering")
    assert [cast(str, message["content"]) for message in queued] == ["queued steering"]
    stored = SqliteSessionStore().load_session(workspace=tmp_path, session_id=SESSION_ID)
    assert [cast(str, message["content"]) for message in cast(list[dict[str, object]], stored.session.metadata["pending_messages"])] == [
        "queued steering"
    ]
    # The run in flight is untouched: no appended event carries the message.
    assert _entries(tmp_path) == before

    first.finish()
    assert not any("queued steering" in entry for entry in _entries(tmp_path))
    # The message reached the live run's next turn as prompt content, and the
    # outbox was drained by that turn.
    assert "queued steering" not in graph.prompts[0]
    assert "queued steering" in graph.prompts[-1]
    reloaded = SqliteSessionStore().load_session(workspace=tmp_path, session_id=SESSION_ID)
    assert "pending_messages" not in reloaded.session.metadata
