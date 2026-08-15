"""Terminal-seal and shutdown-drain tests for the runtime session lifecycle.

Covers the four lifecycle requirements:

1. Terminal seal: once a session is terminal (``completed`` / ``failed`` /
   ``interrupted`` with no active run), late events (tool results, provider
   deltas, background-task completions, steer/follow-up) are rejected or
   dropped, never applied.
2. Shutdown drain: runtime teardown joins background-task workers so every
   child/background-task result is durable before teardown.
3. The three concurrency races: cancel vs in-flight tool result, approval vs
   steer interleave, parent vs child completion.
4. Bundle/replay round-trip of the seal semantics (imported terminal sessions
   reject late events; replay cannot re-activate them).
"""

from __future__ import annotations

import importlib
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest

from voidcode.graph.contracts import GraphEvent, GraphRunRequest
from voidcode.runtime.config import RuntimeConfig
from voidcode.runtime.contracts import RuntimeRequest, RuntimeResponse
from voidcode.runtime.events import RUNTIME_BACKGROUND_TASK_COMPLETED, EventEnvelope
from voidcode.runtime.permission import PermissionPolicy
from voidcode.runtime.service import (
    RuntimeStreamChunk,
    SessionState,
    ToolRegistry,
    VoidCodeRuntime,
)
from voidcode.runtime.session import SessionRef
from voidcode.runtime.storage import SessionSealedError, SqliteSessionStore
from voidcode.runtime.task import (
    BackgroundTaskRef,
    BackgroundTaskRequestSnapshot,
    BackgroundTaskState,
    is_background_task_terminal,
)
from voidcode.tools.contracts import ToolCall, ToolDefinition, ToolResult

pytestmark = pytest.mark.usefixtures("force_deterministic_engine_default")


@pytest.fixture
def force_deterministic_engine_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOIDCODE_EXECUTION_ENGINE", "deterministic")
    config_module = importlib.import_module("voidcode.runtime.config")
    monkeypatch.setattr(
        config_module,
        "_default_runtime_mcp_config",
        lambda: config_module.RuntimeMcpConfig(enabled=False),
    )
    monkeypatch.setattr(config_module, "_default_runtime_mcp_servers", lambda: {})


@dataclass(slots=True)
class _StubStep:
    tool_call: ToolCall | None = None
    output: str | None = None
    is_finished: bool = False


def _delegated_request(prompt: str, *, parent_session_id: str = "leader-session") -> RuntimeRequest:
    return RuntimeRequest(
        prompt=prompt,
        parent_session_id=parent_session_id,
        metadata={
            "delegation": {
                "mode": "background",
                "subagent_type": "worker",
                "selected_preset": "worker",
                "selected_execution_engine": "provider",
            }
        },
    )


class _SuccessGraph:
    """Top-level runs finish immediately; delegated children call submit_result."""

    def step(
        self,
        request: GraphRunRequest,
        tool_results: tuple[object, ...],
        *,
        session: SessionState,
    ) -> _StubStep:
        _ = tool_results
        if session.session.parent_id is not None:
            return _StubStep(tool_call=ToolCall(tool_name="submit_result", arguments={"summary": request.prompt}))
        return _StubStep(output=request.prompt, is_finished=True)


def _seed_child_session_and_task(
    store: SqliteSessionStore,
    *,
    workspace: Path,
    task_id: str,
    parent_session_id: str,
    child_session_id: str,
) -> None:
    """Persist a completed child session + running task row the way a worker would."""
    store.save_interrupted_checkpoint(
        workspace=workspace,
        session_id=child_session_id,
        prompt="child probe",
        session_metadata={
            "background_run": True,
            "background_task_id": task_id,
        },
        tool_results=(),
        last_event_sequence=0,
        create_if_missing=True,
    )
    store.append_session_events(
        workspace=workspace,
        session_id=child_session_id,
        events=(
            ("runtime.request_received", "runtime", {"prompt": "child probe"}, None),
            ("graph.response_ready", "graph", {"summary": "child done"}, None),
        ),
    )
    store.save_run(
        workspace=workspace,
        request=RuntimeRequest(
            prompt="child probe",
            session_id=child_session_id,
            parent_session_id=parent_session_id,
            metadata={
                "background_run": True,
                "background_task_id": task_id,
            },
        ),
        response=RuntimeResponse(
            session=SessionState(
                session=SessionRef(id=child_session_id, parent_id=parent_session_id),
                status="completed",
                turn=1,
                metadata={
                    "background_run": True,
                    "background_task_id": task_id,
                },
            ),
            events=(
                EventEnvelope(
                    session_id=child_session_id,
                    sequence=1,
                    event_type="runtime.request_received",
                    source="runtime",
                    payload={"prompt": "child probe"},
                ),
                EventEnvelope(
                    session_id=child_session_id,
                    sequence=2,
                    event_type="graph.response_ready",
                    source="graph",
                    payload={"summary": "child done"},
                ),
            ),
            output="child done",
        ),
    )
    store.create_background_task(
        workspace=workspace,
        task=BackgroundTaskState(
            task=BackgroundTaskRef(id=task_id),
            status="running",
            request=BackgroundTaskRequestSnapshot(
                prompt="child probe",
                parent_session_id=parent_session_id,
            ),
            session_id=child_session_id,
            created_at=1,
            updated_at=1,
            started_at=1,
        ),
    )


# ---------------------------------------------------------------------------
# Race 1: cancel vs tool-result arriving simultaneously
# ---------------------------------------------------------------------------


class _BlockingThenResultTool:
    """Tool that blocks until released, then returns a real result."""

    definition = ToolDefinition(
        name="write",
        description="Probe that blocks until released and then returns a real result",
        input_schema={"type": "object"},
        read_only=False,
    )

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.invoke_count = 0

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        _ = call, workspace
        self.invoke_count += 1
        self.started.set()
        if not self.release.wait(timeout=5.0):
            raise RuntimeError("blocking tool was not released")
        return ToolResult(
            tool_name=self.definition.name,
            status="ok",
            content="real late result",
            data={"tool_call_id": "late-call", "arguments": {}},
        )


class _ToolThenNothingGraph:
    def step(
        self,
        request: GraphRunRequest,
        tool_results: tuple[object, ...],
        *,
        session: SessionState,
    ) -> _StubStep:
        _ = request, tool_results, session
        return _StubStep(tool_call=ToolCall(tool_name="write", arguments={}))


def test_cancel_lands_while_tool_result_in_flight_drops_late_result(tmp_path: Path) -> None:
    tool = _BlockingThenResultTool()
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        graph=_ToolThenNothingGraph(),
        tool_registry=ToolRegistry.from_tools([tool]),
        config=RuntimeConfig(approval_mode="allow", execution_engine="deterministic"),
        permission_policy=PermissionPolicy(mode="allow"),
    )
    store = runtime._session_store
    chunks: list[RuntimeStreamChunk] = []
    errors: list[BaseException] = []

    def _consume_stream() -> None:
        try:
            chunks.extend(runtime.run_stream(RuntimeRequest(prompt="race tool", session_id="race-1")))
        except BaseException as exc:  # pragma: no cover - asserted via errors list
            errors.append(exc)

    consumer = threading.Thread(target=_consume_stream)
    consumer.start()

    # Deterministic: the tool is in flight when the cancel lands.
    assert tool.started.wait(timeout=5.0)
    result = runtime.cancel_session("race-1", reason="cancel while tool in flight")
    assert result.interrupted is True
    tool.release.set()
    consumer.join(timeout=5.0)

    assert consumer.is_alive() is False
    assert errors == []
    assert tool.invoke_count == 1

    # The real tool result arrived AFTER the interrupt and is a late event: it
    # must be dropped, not persisted.
    persisted = store.load_session(workspace=tmp_path, session_id="race-1")
    completed_events = [event for event in persisted.events if event.event_type == "runtime.tool_completed"]
    assert completed_events == []
    # The terminal failure chunk records the interruption as session truth.
    failed_events = [event for event in persisted.events if event.event_type == "runtime.failed"]
    assert failed_events
    assert failed_events[-1].payload["kind"] == "interrupted"
    assert failed_events[-1].payload["cancelled"] is True
    assert failed_events[-1].payload["reason"] == "cancel while tool in flight"
    assert persisted.session.status == "failed"

    # The sealed session rejects late events through BOTH append paths.
    with pytest.raises(SessionSealedError):
        store.append_session_events(
            workspace=tmp_path,
            session_id="race-1",
            events=(("runtime.tool_completed", "tool", {"tool": "write", "status": "ok", "content": "late"}, None),),
        )
    with pytest.raises(SessionSealedError):
        store.append_session_event(
            workspace=tmp_path,
            session_id="race-1",
            event_type="graph.response_ready",
            source="graph",
            payload={"summary": "late"},
        )


def test_cancel_mid_provider_stream_drops_remaining_deltas(tmp_path: Path) -> None:
    class _StreamingGraph:
        def __init__(self) -> None:
            self.deltas_seen = threading.Event()
            self.release = threading.Event()

        def stream_step(self, request: GraphRunRequest, tool_results: tuple, *, session: SessionState):
            _ = request, tool_results
            for index in range(3):
                yield GraphEvent(
                    event_type="graph.provider_stream",
                    source="graph",
                    payload={"kind": "delta", "channel": "text", "text": f"delta-{index}"},
                )
            self.deltas_seen.set()
            if not self.release.wait(timeout=5.0):
                raise RuntimeError("streaming graph was not released")
            for index in range(3, 10):
                yield GraphEvent(
                    event_type="graph.provider_stream",
                    source="graph",
                    payload={"kind": "delta", "channel": "text", "text": f"delta-{index}"},
                )
            yield _StubStep(output="done", is_finished=True)

        def step(self, request: GraphRunRequest, tool_results: tuple, *, session: SessionState) -> _StubStep:
            raise AssertionError("streaming graph must not call step")

    graph = _StreamingGraph()
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        graph=graph,  # type: ignore[arg-type]
        config=RuntimeConfig(approval_mode="allow", execution_engine="deterministic"),
    )
    streamed_deltas: list[str] = []

    def _consume_stream() -> None:
        for chunk in runtime.run_stream(RuntimeRequest(prompt="stream race", session_id="race-stream", metadata={"provider_stream": True})):
            if chunk.event is not None and chunk.event.event_type == "graph.provider_stream":
                text = chunk.event.payload.get("text")
                if isinstance(text, str):
                    streamed_deltas.append(text)

    consumer = threading.Thread(target=_consume_stream)
    consumer.start()
    # Deterministic: the cancel lands while the provider stream is mid-flight
    # (three deltas delivered, the rest blocked on ``release``).
    assert graph.deltas_seen.wait(timeout=5.0)
    result = runtime.cancel_session("race-stream", reason="cancel mid stream")
    assert result.interrupted is True
    graph.release.set()
    consumer.join(timeout=5.0)

    assert consumer.is_alive() is False
    # Deltas after the interrupt are late events and are dropped.
    assert streamed_deltas == ["delta-0", "delta-1", "delta-2"]
    persisted = runtime._session_store.load_session(workspace=tmp_path, session_id="race-stream")
    failed_events = [event for event in persisted.events if event.event_type == "runtime.failed"]
    assert failed_events
    assert failed_events[-1].payload["kind"] == "interrupted"
    assert failed_events[-1].payload["reason"] == "cancel mid stream"
    # Live provider deltas are client-only: nothing was persisted from the stream.
    assert all(event.event_type != "graph.provider_stream" for event in persisted.events)


# ---------------------------------------------------------------------------
# Race 2: approval vs steer interleave
# ---------------------------------------------------------------------------


class _ApprovalThenDoneGraph:
    def step(
        self,
        request: GraphRunRequest,
        tool_results: tuple[object, ...],
        *,
        session: SessionState,
    ) -> _StubStep:
        _ = session
        if not tool_results and "pre-seal steer" not in request.prompt:
            return _StubStep(
                tool_call=ToolCall(
                    tool_name="write",
                    arguments={"path": "alpha.txt", "content": "1"},
                )
            )
        return _StubStep(output="done", is_finished=True)


def _waiting_approval_request_id(response: RuntimeResponse) -> str:
    approval_events = [event for event in response.events if event.event_type == "runtime.approval_requested"]
    assert approval_events
    request_id = approval_events[-1].payload.get("request_id")
    assert isinstance(request_id, str)
    return request_id


def test_steer_landing_after_approval_resolution_is_rejected(tmp_path: Path) -> None:
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        graph=_ApprovalThenDoneGraph(),
        config=RuntimeConfig(approval_mode="ask", execution_engine="deterministic"),
        permission_policy=PermissionPolicy(mode="ask"),
    )
    waiting = runtime.run(RuntimeRequest(prompt="approval steer", session_id="steer-1"))
    assert waiting.session.status == "waiting"

    # The steer arrives while the pending approval is being resolved: the
    # resolution wins and seals the session, so the steer is a late event.
    approval_request_id = _waiting_approval_request_id(waiting)
    resolved = runtime.resume(
        "steer-1",
        approval_request_id=approval_request_id,
        approval_decision="allow",
    )
    assert resolved.session.status == "completed"

    with pytest.raises(SessionSealedError, match="terminal"):
        runtime.queue_steering("steer-1", "late steer after approval resolution")

    stored = runtime._load_stored_response(session_id="steer-1")
    assert stored.session.status == "completed"
    assert "pending_messages" not in stored.session.metadata


def test_steer_queued_while_waiting_is_delivered_on_next_run_without_reactivating(
    tmp_path: Path,
) -> None:
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        graph=_ApprovalThenDoneGraph(),
        config=RuntimeConfig(approval_mode="ask", execution_engine="deterministic"),
        permission_policy=PermissionPolicy(mode="ask"),
    )
    waiting = runtime.run(RuntimeRequest(prompt="approval steer pre", session_id="steer-2"))
    assert waiting.session.status == "waiting"

    # A steer that lands BEFORE the seal is accepted (the session is resumable,
    # not terminal) and must be delivered on the next run — the interleave is
    # decided deterministically by the guard at enqueue time.
    queued = runtime.queue_steering("steer-2", "pre-seal steer")
    assert any(item.get("kind") == "steering" and item.get("content") == "pre-seal steer" for item in queued)

    approval_request_id = _waiting_approval_request_id(waiting)
    resolved = runtime.resume(
        "steer-2",
        approval_request_id=approval_request_id,
        approval_decision="allow",
    )
    assert resolved.session.status == "completed"

    # The follow-up run drains the pre-seal steering into its prompt.
    followup = runtime.run(RuntimeRequest(prompt="follow up", session_id="steer-2"))
    assert followup.session.status == "completed"
    request_received = next(event for event in followup.events if event.event_type == "runtime.request_received")
    assert "pre-seal steer" in cast(str, request_received.payload["prompt"])


def test_steer_queued_while_run_active_is_accepted(tmp_path: Path) -> None:
    class _ImmediateDoneGraph:
        def step(
            self,
            request: GraphRunRequest,
            tool_results: tuple[object, ...],
            *,
            session: SessionState,
        ) -> _StubStep:
            _ = request, tool_results, session
            return _StubStep(output="done", is_finished=True)

    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        graph=_ImmediateDoneGraph(),  # type: ignore[arg-type]
        config=RuntimeConfig(approval_mode="allow", execution_engine="deterministic"),
    )
    stream = runtime.run_stream(RuntimeRequest(prompt="active steer", session_id="steer-active"))
    first_chunk = next(stream)
    assert first_chunk.session.status == "running"

    queued = runtime.queue_steering("steer-active", "steer while running")
    assert any(item.get("kind") == "steering" and item.get("content") == "steer while running" for item in queued)

    remaining = list(stream)
    assert remaining[-1].session.status == "completed"
    stored = runtime._load_stored_response(session_id="steer-active")
    assert stored.session.status == "completed"


def test_steer_rejected_on_interrupted_session_without_active_run(tmp_path: Path) -> None:
    store = SqliteSessionStore()
    store.save_interrupted_checkpoint(
        workspace=tmp_path,
        session_id="interrupted-steer",
        prompt="interrupted probe",
        session_metadata={},
        tool_results=(),
        last_event_sequence=0,
        create_if_missing=True,
    )
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        session_store=store,
        config=RuntimeConfig(approval_mode="allow", execution_engine="deterministic"),
    )

    # An ``interrupted`` row with no active run is sealed: the run that left it
    # has ended, so a steer arriving now is a late event and is rejected.
    with pytest.raises(SessionSealedError, match="interrupted"):
        runtime.queue_steering("interrupted-steer", "late steer on interrupted session")
    stored = store.load_session(workspace=tmp_path, session_id="interrupted-steer")
    assert "pending_messages" not in stored.session.metadata


# ---------------------------------------------------------------------------
# Race 3: parent vs child completion
# ---------------------------------------------------------------------------


def test_child_background_completion_cannot_mutate_sealed_parent(tmp_path: Path) -> None:
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        graph=_SuccessGraph(),  # type: ignore[arg-type]
        config=RuntimeConfig(approval_mode="allow", execution_engine="deterministic"),
    )
    parent = runtime.run(RuntimeRequest(prompt="leader", session_id="leader-session"))
    assert parent.session.status == "completed"

    store = runtime._session_store
    _seed_child_session_and_task(
        store,
        workspace=tmp_path,
        task_id="task-race-3",
        parent_session_id="leader-session",
        child_session_id="child-race-3",
    )

    # Persist the child completion through the runtime's finalization path —
    # the same path a background-task worker executes.
    supervisor = runtime._background_task_supervisor
    task = store.load_background_task(workspace=tmp_path, task_id="task-race-3")
    child_response = supervisor.load_background_task_child_response(task=task)
    assert child_response is not None
    supervisor.finalize_background_task_from_session_response(session_response=child_response)

    # Child truth is durable regardless of the parent's seal.
    finalized = store.load_background_task(workspace=tmp_path, task_id="task-race-3")
    assert finalized.status == "completed"

    # The parent stays terminal; the sanctioned lifecycle notification may be
    # appended, but nothing else may mutate the sealed parent's truth.
    parent_after = store.load_session(workspace=tmp_path, session_id="leader-session")
    assert parent_after.session.status == "completed"
    assert any(event.event_type == RUNTIME_BACKGROUND_TASK_COMPLETED for event in parent_after.events)
    with pytest.raises(SessionSealedError):
        store.append_session_events(
            workspace=tmp_path,
            session_id="leader-session",
            events=(("runtime.tool_completed", "tool", {"tool": "write", "status": "ok", "content": "late"}, None),),
        )
    with pytest.raises(SessionSealedError):
        store.append_session_event(
            workspace=tmp_path,
            session_id="leader-session",
            event_type="graph.response_ready",
            source="graph",
            payload={"summary": "late"},
        )

    # Replay of the sealed parent is read-only: it cannot be re-activated.
    replayed = runtime.resume("leader-session")
    assert replayed.session.status == "completed"
    assert [event.sequence for event in replayed.events] == sorted(event.sequence for event in replayed.events)


# ---------------------------------------------------------------------------
# Shutdown drain
# ---------------------------------------------------------------------------


def test_runtime_shutdown_drains_background_worker_results_before_teardown(
    tmp_path: Path,
) -> None:
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        graph=_SuccessGraph(),  # type: ignore[arg-type]
        config=RuntimeConfig(approval_mode="allow", execution_engine="deterministic"),
    )
    _ = runtime.run(RuntimeRequest(prompt="leader", session_id="leader-session"))

    started = runtime.start_background_task(_delegated_request("drain child"))

    # Shutdown joins the worker; the worker's finalization (task terminal row +
    # parent notification) is durable by the time shutdown returns.
    runtime.shutdown_background_tasks(timeout_seconds=5.0)

    task = runtime._session_store.load_background_task(workspace=tmp_path, task_id=started.task.id)
    assert is_background_task_terminal(task.status)
    assert runtime._background_task_supervisor.threads == {}
    leader = runtime._session_store.load_session(workspace=tmp_path, session_id="leader-session")
    assert any(event.event_type == RUNTIME_BACKGROUND_TASK_COMPLETED for event in leader.events)
