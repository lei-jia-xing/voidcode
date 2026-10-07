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
from pathlib import Path
from typing import cast

import pytest

from tests.runtime_composition import create_task, save_checkpoint
from tests.runtime_storage import repositories_for_test_store
from voidcode.core.questions import PendingQuestionOption, PendingQuestionPrompt
from voidcode.core.tool_context import ToolContext
from voidcode.core.turns import FinalTurn, ReportedCall, StreamFact, ToolTurn, TurnRequest, TurnSession
from voidcode.provider.protocol import ProviderStreamEvent
from voidcode.runtime.background.models import (
    BackgroundTaskRef,
    BackgroundTaskRequestSnapshot,
    BackgroundTaskState,
    is_background_task_terminal,
)
from voidcode.runtime.config import RuntimeBackgroundTaskConfig, RuntimeConfig
from voidcode.runtime.contracts import RuntimeRequest, RuntimeResponse
from voidcode.runtime.events import (
    RUNTIME_BACKGROUND_TASK_COMPLETED,
    RUNTIME_BACKGROUND_TASK_PROGRESS,
    RUNTIME_BACKGROUND_TASK_WAITING_APPROVAL,
    EventEnvelope,
)
from voidcode.runtime.execution.report_codec import report_payload
from voidcode.runtime.permission import PendingApproval, PermissionPolicy
from voidcode.runtime.question import PendingQuestion
from voidcode.runtime.service import (
    RuntimeStreamChunk,
    SessionState,
    VoidCodeRuntime,
)
from voidcode.runtime.session import SessionRef
from voidcode.runtime.storage import SessionSealedError, SqliteSessionStore
from voidcode.runtime.tool_registry import ToolRegistry
from voidcode.tools.contracts import TerminalYield, TextOutput, ToolCall, ToolDefinition, ToolEffect, ToolResult, ToolSuccess
from voidcode.tools.read import ReadTool

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
    """Top-level runs finish immediately; delegated children call yield."""

    def produce(
        self,
        request: TurnRequest,
        tool_results: tuple[object, ...],
        *,
        session: TurnSession,
    ) -> ToolTurn | FinalTurn:
        _ = tool_results
        if session.metadata.get("parent_session_id") is not None:
            return ToolTurn(calls=(ToolCall(tool_name="yield", arguments={"summary": request.prompt}),))
        return FinalTurn(output=request.prompt)


def _seed_child_session_and_task(
    store: SqliteSessionStore,
    *,
    workspace: Path,
    task_id: str,
    parent_session_id: str,
    child_session_id: str,
    capability_snapshot: dict[str, object] | None = None,
) -> None:
    """Persist a completed child session linked to its actual task owner."""
    metadata: dict[str, object] = {"background_run": True, "background_task_id": task_id}
    if capability_snapshot is not None:
        metadata["agent_capability_snapshot"] = capability_snapshot
        metadata["composition_ref"] = capability_snapshot["composition_ref"]
    task = create_task(
        store,
        workspace=workspace,
        task=BackgroundTaskState(
            task=BackgroundTaskRef(id=task_id),
            status="running",
            request=BackgroundTaskRequestSnapshot(prompt="child probe", parent_session_id=parent_session_id, metadata=metadata),
            session_id=child_session_id,
            created_at=1,
            updated_at=1,
            started_at=1,
        ),
    )
    metadata["composition_ref"] = task.request.metadata["composition_ref"]
    save_checkpoint(
        store,
        workspace=workspace,
        session_id=child_session_id,
        prompt="child probe",
        session_metadata=metadata,
        tool_results=(),
        last_event_sequence=0,
        create_if_missing=True,
    )
    events = (
        ("runtime.request_received", "runtime", {"prompt": "child probe"}, None),
        (
            "runtime.tool_completed",
            "tool",
            {
                "tool": "yield",
                "tool_call_id": "yield-call",
                "status": "ok",
                "handoff": {"summary": "child done", "data": {}},
                "reported_call": report_payload(
                    ReportedCall(
                        "yield-call", "yield", {}, ToolSuccess("yield", output=TextOutput("child done"), control=TerminalYield("child done", {}))
                    )
                ),
            },
            None,
        ),
        ("graph.response_ready", "graph", {"summary": "child done"}, None),
    )
    store.append_session_events(workspace=workspace, session_id=child_session_id, events=events)
    store.save_run(
        workspace=workspace,
        request=RuntimeRequest(prompt="child probe", session_id=child_session_id, parent_session_id=parent_session_id, metadata=metadata),
        response=RuntimeResponse(
            session=SessionState(session=SessionRef(id=child_session_id, parent_id=parent_session_id), status="completed", turn=1, metadata=metadata),
            events=tuple(
                EventEnvelope(session_id=child_session_id, sequence=index, event_type=event_type, source=source, payload=payload)
                for index, (event_type, source, payload, _) in enumerate(events, start=1)
            ),
            output="child done",
        ),
    )


def _seed_waiting_child_and_task(
    store: SqliteSessionStore,
    *,
    workspace: Path,
    task_id: str,
    parent_session_id: str,
    child_session_id: str,
    wait_kind: str,
    capability_snapshot: dict[str, object] | None = None,
) -> None:
    """Persist a running task and blocked child with a canonical task owner."""
    metadata: dict[str, object] = {
        "background_run": True,
        "background_task_id": task_id,
        "delegation": {
            "mode": "background",
            "subagent_type": "worker",
            "selected_preset": "worker",
            "selected_execution_engine": "provider",
        },
    }
    if capability_snapshot is not None:
        metadata["agent_capability_snapshot"] = capability_snapshot
        metadata["composition_ref"] = capability_snapshot["composition_ref"]
    request_id = f"{wait_kind}-request"
    task = create_task(
        store,
        workspace=workspace,
        task=BackgroundTaskState(
            task=BackgroundTaskRef(id=task_id),
            status="running",
            request=BackgroundTaskRequestSnapshot(
                prompt="waiting child",
                session_id=child_session_id,
                parent_session_id=parent_session_id,
                metadata=metadata,
            ),
            session_id=child_session_id,
            approval_request_id=request_id if wait_kind == "approval" else None,
            question_request_id=request_id if wait_kind == "question" else None,
            created_at=1,
            updated_at=1,
            started_at=1,
        ),
    )
    metadata["composition_ref"] = task.request.metadata["composition_ref"]
    request = RuntimeRequest(
        prompt="waiting child",
        session_id=child_session_id,
        parent_session_id=parent_session_id,
        metadata=metadata,
    )
    event_type = "runtime.approval_requested" if wait_kind == "approval" else "runtime.question_requested"
    event_payload: dict[str, object] = {"request_id": request_id}
    if wait_kind == "approval":
        event_payload.update({"tool": "write"})
    else:
        event_payload.update({"tool": "question", "question_count": 1, "questions": [{"header": "Proceed", "question": "Proceed?"}]})
    response = RuntimeResponse(
        session=SessionState(
            session=SessionRef(id=child_session_id, parent_id=parent_session_id),
            status="waiting",
            turn=1,
            metadata=metadata,
        ),
        events=(
            EventEnvelope(
                session_id=child_session_id,
                sequence=1,
                event_type="runtime.request_received",
                source="runtime",
                payload={"prompt": request.prompt},
            ),
            EventEnvelope(
                session_id=child_session_id,
                sequence=2,
                event_type=event_type,
                source="runtime",
                payload=event_payload,
            ),
        ),
    )
    save_checkpoint(
        store,
        workspace=workspace,
        session_id=child_session_id,
        prompt=request.prompt,
        session_metadata=metadata,
        tool_results=(),
        last_event_sequence=0,
        create_if_missing=True,
    )
    store.append_session_events(
        workspace=workspace,
        session_id=child_session_id,
        events=tuple((event.event_type, event.source, event.payload, None) for event in response.events),
    )
    if wait_kind == "approval":
        store.save_pending_approval(
            workspace=workspace,
            request=request,
            response=response,
            pending_approval=PendingApproval(
                request_id=request_id,
                tool_name="write",
                arguments={"path": "child.txt", "content": "x"},
                target_summary="write child.txt",
                reason="non-read-only tool invocation",
                policy_mode="ask",
                request_event_sequence=2,
                owner_session_id=child_session_id,
                owner_parent_session_id=parent_session_id,
                delegated_task_id=task_id,
                operation_class="write",
            ),
        )
    else:
        store.save_pending_question(
            workspace=workspace,
            request=request,
            response=response,
            pending_question=PendingQuestion(
                request_id=request_id,
                tool_name="question",
                arguments={},
                prompts=(
                    PendingQuestionPrompt(
                        question="Proceed?",
                        header="Proceed",
                        options=(PendingQuestionOption(label="yes"),),
                    ),
                ),
            ),
        )


# ---------------------------------------------------------------------------
class _BlockingThenResultTool:
    definition = ToolDefinition(
        name="write",
        description="Probe that blocks until released and then returns a real result",
        effects=frozenset({ToolEffect.WRITE}),
    )

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.invoke_count = 0

    def invoke(self, call: ToolCall, *, context: ToolContext) -> ToolResult:
        _ = call, context
        self.invoke_count += 1
        self.started.set()
        if not self.release.wait(timeout=5.0):
            raise RuntimeError("blocking tool was not released")
        return ToolSuccess(tool_name=self.definition.name, output=TextOutput("real late result"))


class _ToolThenNothingGraph:
    def produce(
        self,
        request: TurnRequest,
        tool_results: tuple[object, ...],
        *,
        session: TurnSession,
    ) -> ToolTurn | FinalTurn:
        _ = request, session
        if not tool_results:
            return ToolTurn(calls=(ToolCall(tool_name="write", arguments={}),))
        return FinalTurn(output="done")


def test_cancel_lands_while_tool_result_in_flight_drops_late_result(tmp_path: Path) -> None:
    tool = _BlockingThenResultTool()
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        turn_producer=_ToolThenNothingGraph(),
        tool_registry=ToolRegistry.from_tools([tool]),
        config=RuntimeConfig(approval_mode="yolo", execution_engine="deterministic"),
        permission_policy=PermissionPolicy(mode="yolo"),
    )
    sessions = runtime._repositories.sessions
    events = runtime._repositories.events
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
    persisted = sessions.load_session(workspace=tmp_path, session_id="race-1")
    completed_events = [event for event in persisted.events if event.event_type == "runtime.tool_completed"]
    assert completed_events == []
    # The terminal failure chunk records the interruption as session truth.
    failed_events = [event for event in persisted.events if event.event_type == "runtime.failed"]
    assert failed_events
    assert failed_events[-1].payload["kind"] == "interrupted"
    assert failed_events[-1].payload["cancelled"] is True
    assert failed_events[-1].payload["reason"] == "cancel while tool in flight"
    # A user-cancelled run terminates ``interrupted`` (not ``failed``): the
    # terminal-status derivation keys off the cancelled flag.
    assert persisted.session.status == "interrupted"

    # An ``interrupted`` row is the mid-run/un-sealed state by design: the
    # storage layer seals only {completed, failed} (``SESSION_STORAGE_SEAL_
    # STATUSES``) so a fresh run / resume can re-open it. Sealing of an
    # ``interrupted`` row with no active run is the runtime-level guard
    # (``VoidCodeRuntime._sealed_session_status``), which gates the
    # interaction queue — the storage append paths stay open for lifecycle
    # re-entry.
    assert events.append_session_events(
        workspace=tmp_path,
        session_id="race-1",
        events=(
            (
                "runtime.tool_completed",
                "tool",
                {
                    "tool": "write",
                    "status": "ok",
                    "content": "late",
                    "reported_call": report_payload(ReportedCall("write-call", "write", {}, ToolSuccess("write", output=TextOutput("late")))),
                },
                None,
            ),
        ),
    )
    assert (
        events.append_session_event(
            workspace=tmp_path,
            session_id="race-1",
            event_type="graph.response_ready",
            source="graph",
            payload={"summary": "late"},
        )
        is not None
    )
    # The runtime-level guard rejects late interaction-queue messages on the
    # sealed interrupted row (no active run), matching the pre-fix failed-row
    # behavior.
    with pytest.raises(SessionSealedError):
        runtime.queue_follow_up("race-1", "late follow-up")


def test_cancel_mid_provider_stream_drops_remaining_deltas(tmp_path: Path) -> None:
    class _StreamingGraph:
        def __init__(self) -> None:
            self.deltas_seen = threading.Event()
            self.release = threading.Event()

        def stream_produce(self, request: TurnRequest, tool_results: tuple, *, session: TurnSession):
            _ = request, tool_results
            for index in range(3):
                yield StreamFact(ProviderStreamEvent(kind="delta", channel="text", text=f"delta-{index}"))
            self.deltas_seen.set()
            if not self.release.wait(timeout=5.0):
                raise RuntimeError("streaming graph was not released")
            for index in range(3, 10):
                yield StreamFact(ProviderStreamEvent(kind="delta", channel="text", text=f"delta-{index}"))
            yield FinalTurn(output="done")

        def produce(self, request: TurnRequest, tool_results: tuple, *, session: TurnSession) -> ToolTurn | FinalTurn:
            raise AssertionError("streaming graph must not call step")

    graph = _StreamingGraph()
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        turn_producer=graph,  # type: ignore[arg-type]
        config=RuntimeConfig(approval_mode="yolo", execution_engine="deterministic"),
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
    persisted = runtime._repositories.sessions.load_session(workspace=tmp_path, session_id="race-stream")
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
    def produce(
        self,
        request: TurnRequest,
        tool_results: tuple[object, ...],
        *,
        session: SessionState,
    ) -> ToolTurn | FinalTurn:
        _ = session
        if not tool_results and "pre-seal steer" not in request.prompt:
            return ToolTurn(
                calls=(
                    ToolCall(
                        tool_name="write",
                        arguments={"path": "alpha.txt", "content": "1"},
                    ),
                )
            )
        return FinalTurn(output="done")


def _waiting_approval_request_id(response: RuntimeResponse) -> str:
    approval_events = [event for event in response.events if event.event_type == "runtime.approval_requested"]
    assert approval_events
    request_id = approval_events[-1].payload.get("request_id")
    assert isinstance(request_id, str)
    return request_id


def test_steer_landing_after_approval_resolution_is_rejected(tmp_path: Path) -> None:
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        turn_producer=_ApprovalThenDoneGraph(),
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


def test_steer_queued_while_run_active_is_accepted(tmp_path: Path) -> None:
    class _ImmediateDoneGraph:
        def produce(
            self,
            request: TurnRequest,
            tool_results: tuple[object, ...],
            *,
            session: TurnSession,
        ) -> ToolTurn | FinalTurn:
            _ = request, tool_results, session
            return FinalTurn(output="done")

    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        turn_producer=_ImmediateDoneGraph(),  # type: ignore[arg-type]
        config=RuntimeConfig(approval_mode="yolo", execution_engine="deterministic"),
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


def test_same_run_followup_executes_each_real_read_once_in_input_order(tmp_path: Path) -> None:
    (tmp_path / "first.txt").write_text("first real file", encoding="utf-8")
    (tmp_path / "second.txt").write_text("second real file", encoding="utf-8")

    class TrackedRead:
        definition = ReadTool.definition

        def __init__(self) -> None:
            self.paths: list[object] = []
            self.delegate = ReadTool()

        def invoke(self, call: ToolCall, *, context: ToolContext) -> ToolResult:
            self.paths.append(call.arguments["path"])
            return self.delegate.invoke(call, context=context)

    tool = TrackedRead()
    owner = SqliteSessionStore(database_path=tmp_path / "follow-up.sqlite3")
    with VoidCodeRuntime(
        workspace=tmp_path,
        repositories=repositories_for_test_store(owner),
        tool_registry=ToolRegistry.from_tools((tool,)),
        config=RuntimeConfig(approval_mode="yolo", execution_engine="deterministic"),
    ) as runtime:
        for chunk in runtime.run_stream(RuntimeRequest(prompt="read first.txt", session_id="follow-up-active")):
            if chunk.event is not None and chunk.event.event_type == "runtime.tool_completed" and tool.paths == ["first.txt"]:
                runtime.queue_follow_up("follow-up-active", "read second.txt")
        completed = runtime.session_result(session_id="follow-up-active")
        assert completed.session.status == "completed"
        assert completed.output == "second real file"
        assert tool.paths == ["first.txt", "second.txt"]
        results = [event for event in completed.transcript if event.event_type == "runtime.tool_completed"]
        assert [cast(dict[str, object], event.payload["arguments"])["path"] for event in results] == ["first.txt", "second.txt"]


def test_steer_rejected_on_interrupted_session_without_active_run(tmp_path: Path) -> None:
    store = SqliteSessionStore()
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        repositories=repositories_for_test_store(store),
        turn_producer=_SuccessGraph(),
        config=RuntimeConfig(approval_mode="yolo", execution_engine="deterministic"),
    )
    response = runtime.run(RuntimeRequest(prompt="interrupted probe", session_id="interrupted-steer"))
    save_checkpoint(
        store,
        workspace=tmp_path,
        session_id="interrupted-steer",
        prompt="interrupted probe",
        session_metadata=response.session.metadata,
        tool_results=(),
        last_event_sequence=len(response.events),
        create_if_missing=False,
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
    store = SqliteSessionStore()
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        repositories=repositories_for_test_store(store),
        turn_producer=_SuccessGraph(),  # type: ignore[arg-type]
        config=RuntimeConfig(approval_mode="yolo", execution_engine="deterministic"),
    )
    parent = runtime.run(RuntimeRequest(prompt="leader", session_id="leader-session"))
    assert parent.session.status == "completed"

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
    # Re-running terminal reconciliation/backfill must not duplicate the
    # parent completion event (the task id is the durable dedupe identity).
    supervisor.backfill_parent_background_task_event(task=finalized)
    parent_after_backfill = store.load_session(workspace=tmp_path, session_id="leader-session")
    completion_events = [
        event
        for event in parent_after_backfill.events
        if event.event_type == RUNTIME_BACKGROUND_TASK_COMPLETED and event.payload.get("task_id") == "task-race-3"
    ]
    assert len(completion_events) == 1
    assert completion_events[0].payload["parent_session_id"] == "leader-session"
    assert completion_events[0].payload["child_session_id"] == "child-race-3"
    late_progress = store.append_session_event(
        workspace=tmp_path,
        session_id="leader-session",
        event_type=RUNTIME_BACKGROUND_TASK_PROGRESS,
        source="runtime",
        payload={
            "task_id": "task-race-3",
            "parent_session_id": "leader-session",
            "child_session_id": "child-race-3",
            "status": "running",
            "progress": {"ordinal": 1, "type": "progress", "result": "late"},
            "progress_event_sequence": 1,
        },
        dedupe_key="background-task-progress:task-race-3:1",
    )
    assert late_progress is not None
    assert late_progress.event_type == RUNTIME_BACKGROUND_TASK_PROGRESS

    with pytest.raises(SessionSealedError):
        store.append_session_events(
            workspace=tmp_path,
            session_id="leader-session",
            events=(
                (
                    "runtime.tool_completed",
                    "tool",
                    {
                        "tool": "write",
                        "status": "ok",
                        "content": "late",
                        "reported_call": report_payload(ReportedCall("write-call", "write", {}, ToolSuccess("write", output=TextOutput("late")))),
                    },
                    None,
                ),
            ),
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


def test_finalize_is_idempotent_and_backfill_repairs_missing_parent_event(tmp_path: Path) -> None:
    store = SqliteSessionStore()
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        repositories=repositories_for_test_store(store),
        turn_producer=_SuccessGraph(),  # type: ignore[arg-type]
        config=RuntimeConfig(approval_mode="yolo", execution_engine="deterministic"),
    )
    _ = runtime.run(RuntimeRequest(prompt="leader", session_id="leader-session"))
    _seed_child_session_and_task(
        store,
        workspace=tmp_path,
        task_id="task-idempotent",
        parent_session_id="leader-session",
        child_session_id="child-idempotent",
    )
    supervisor = runtime._background_task_supervisor
    task = store.load_background_task(workspace=tmp_path, task_id="task-idempotent")
    child_response = supervisor.load_background_task_child_response(task=task)
    assert child_response is not None

    # Simulate a worker that durably terminalized the task before notification
    # append; reconciliation/backfill must repair the parent event.
    terminal = store.mark_background_task_terminal(workspace=tmp_path, task_id="task-idempotent", status="completed")
    supervisor.backfill_parent_background_task_event(task=terminal)
    supervisor.finalize_background_task_from_session_response(session_response=child_response)
    supervisor.backfill_parent_background_task_event(task=terminal)

    parent = store.load_session(workspace=tmp_path, session_id="leader-session")
    events = [
        event
        for event in parent.events
        if event.event_type == RUNTIME_BACKGROUND_TASK_COMPLETED and event.payload.get("task_id") == "task-idempotent"
    ]
    assert len(events) == 1
    assert store.load_session_status(workspace=tmp_path, session_id="child-idempotent") == "completed"
    assert store.load_background_task(workspace=tmp_path, task_id="task-idempotent").status == "completed"


def test_cancel_wins_completion_race_without_mutating_child_truth(tmp_path: Path) -> None:
    store = SqliteSessionStore()
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        repositories=repositories_for_test_store(store),
        turn_producer=_SuccessGraph(),  # type: ignore[arg-type]
        config=RuntimeConfig(approval_mode="yolo", execution_engine="deterministic"),
    )
    _ = runtime.run(RuntimeRequest(prompt="leader", session_id="leader-session"))
    _seed_child_session_and_task(
        store,
        workspace=tmp_path,
        task_id="task-cancel-race",
        parent_session_id="leader-session",
        child_session_id="child-cancel-race",
    )
    task = store.load_background_task(workspace=tmp_path, task_id="task-cancel-race")
    child_response = runtime._background_task_supervisor.load_background_task_child_response(task=task)
    assert child_response is not None
    requested = store.request_background_task_cancel(workspace=tmp_path, task_id="task-cancel-race")
    assert requested.status == "running"

    runtime._background_task_supervisor.finalize_background_task_from_session_response(session_response=child_response)

    assert store.load_background_task(workspace=tmp_path, task_id="task-cancel-race").status == "cancelled"
    # Cancellation changes task truth only; transcript evidence still seals the
    # child session as completed and cannot be rolled back by the race winner.
    assert store.load_session_status(workspace=tmp_path, session_id="child-cancel-race") == "completed"


def test_unknown_parent_drops_delivery_but_preserves_child_and_task_truth(tmp_path: Path) -> None:
    store = SqliteSessionStore()
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        repositories=repositories_for_test_store(store),
        turn_producer=_SuccessGraph(),  # type: ignore[arg-type]
        config=RuntimeConfig(approval_mode="yolo", execution_engine="deterministic"),
    )
    _seed_child_session_and_task(
        store,
        workspace=tmp_path,
        task_id="task-unknown-parent",
        parent_session_id="missing-parent",
        child_session_id="child-unknown-parent",
    )
    task = store.load_background_task(workspace=tmp_path, task_id="task-unknown-parent")
    child_response = runtime._background_task_supervisor.load_background_task_child_response(task=task)
    assert child_response is not None
    runtime._background_task_supervisor.finalize_background_task_from_session_response(session_response=child_response)

    assert store.load_background_task(workspace=tmp_path, task_id="task-unknown-parent").status == "completed"
    assert store.load_session_status(workspace=tmp_path, session_id="child-unknown-parent") == "completed"
    with pytest.raises(ValueError, match="unknown session"):
        store.load_session(workspace=tmp_path, session_id="missing-parent")


# ---------------------------------------------------------------------------
# Shutdown drain

# ---------------------------------------------------------------------------
# Shutdown drain
# ---------------------------------------------------------------------------


def test_runtime_shutdown_drains_background_worker_results_before_teardown(
    tmp_path: Path,
) -> None:
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        turn_producer=_SuccessGraph(),  # type: ignore[arg-type]
        config=RuntimeConfig(approval_mode="yolo", execution_engine="deterministic"),
    )
    _ = runtime.run(RuntimeRequest(prompt="leader", session_id="leader-session"))

    started = runtime.start_background_task(_delegated_request("drain child"))

    # Shutdown joins the worker; the worker's finalization (task terminal row +
    # parent notification) is durable by the time shutdown returns.
    runtime.shutdown_background_tasks(timeout_seconds=5.0)

    task = runtime._repositories.tasks.load_background_task(workspace=tmp_path, task_id=started.task.id)
    assert is_background_task_terminal(task.status)
    assert runtime._background_task_supervisor.threads == {}
    leader = runtime._repositories.sessions.load_session(workspace=tmp_path, session_id="leader-session")
    assert any(event.event_type == RUNTIME_BACKGROUND_TASK_COMPLETED for event in leader.events)


def _background_lifecycle_runtime(workspace: Path) -> tuple[VoidCodeRuntime, SqliteSessionStore]:
    store = SqliteSessionStore()
    runtime = VoidCodeRuntime(
        workspace=workspace,
        repositories=repositories_for_test_store(store),
        turn_producer=_SuccessGraph(),  # type: ignore[arg-type]
        config=RuntimeConfig(
            approval_mode="yolo",
            execution_engine="deterministic",
            background_task=RuntimeBackgroundTaskConfig(delegated_reminders_enabled=False),
        ),
    )
    return runtime, store


def test_fresh_and_restarted_reads_backfill_terminal_and_waiting_parent_events_once(
    tmp_path: Path,
) -> None:
    runtime, store = _background_lifecycle_runtime(tmp_path)
    parent_response = runtime.run(RuntimeRequest(prompt="leader", session_id="leader-session"))
    capability_snapshot = cast(dict[str, object], parent_response.session.metadata["agent_capability_snapshot"])
    _seed_child_session_and_task(
        store,
        workspace=tmp_path,
        task_id="task-terminal-read",
        parent_session_id="leader-session",
        child_session_id="child-terminal-read",
        capability_snapshot=capability_snapshot,
    )
    _seed_waiting_child_and_task(
        store,
        workspace=tmp_path,
        task_id="task-approval-read",
        parent_session_id="leader-session",
        child_session_id="child-approval-read",
        wait_kind="approval",
        capability_snapshot=capability_snapshot,
    )
    fresh_runtime, _ = _background_lifecycle_runtime(tmp_path)
    first_summaries = fresh_runtime.list_background_tasks()
    assert {summary.task.id: summary.status for summary in first_summaries} == {
        "task-terminal-read": "completed",
        "task-approval-read": "running",
    }
    terminal_child = fresh_runtime.session_result(session_id="child-terminal-read")
    waiting_child = fresh_runtime.session_result(session_id="child-approval-read")
    assert terminal_child.session.status == "completed"
    assert waiting_child.session.status == "waiting"

    parent_after_first_reads = fresh_runtime.session_result(session_id="leader-session")
    first_events = tuple(
        event
        for event in parent_after_first_reads.transcript
        if event.event_type in (RUNTIME_BACKGROUND_TASK_COMPLETED, RUNTIME_BACKGROUND_TASK_WAITING_APPROVAL)
    )
    assert {event.event_type for event in first_events} == {
        RUNTIME_BACKGROUND_TASK_COMPLETED,
        RUNTIME_BACKGROUND_TASK_WAITING_APPROVAL,
    }
    assert {event.payload["task_id"] for event in first_events} == {
        "task-terminal-read",
        "task-approval-read",
    }
    assert all(event.payload["parent_session_id"] == "leader-session" for event in first_events)
    assert all(event.payload["child_session_id"] in {"child-terminal-read", "child-approval-read"} for event in first_events)
    assert [event.sequence for event in parent_after_first_reads.transcript] == sorted(
        event.sequence for event in parent_after_first_reads.transcript
    )
    first_sequences = tuple(event.sequence for event in parent_after_first_reads.transcript)

    # Public reads may reconcile repeatedly, but append-only parent truth must
    # not gain a second event for either task's durable dedupe key.
    _ = fresh_runtime.list_background_tasks()
    _ = fresh_runtime.session_result(session_id="child-terminal-read")
    _ = fresh_runtime.session_result(session_id="child-approval-read")
    repeated_parent = fresh_runtime.session_result(session_id="leader-session")
    assert tuple(event.sequence for event in repeated_parent.transcript) == first_sequences
    assert sum(event.event_type == RUNTIME_BACKGROUND_TASK_COMPLETED for event in repeated_parent.transcript) == 1
    assert sum(event.event_type == RUNTIME_BACKGROUND_TASK_WAITING_APPROVAL for event in repeated_parent.transcript) == 1

    restarted_runtime, _ = _background_lifecycle_runtime(tmp_path)
    _ = restarted_runtime.list_background_tasks()
    _ = restarted_runtime.session_result(session_id="child-terminal-read")
    _ = restarted_runtime.session_result(session_id="child-approval-read")
    restarted_parent = restarted_runtime.session_result(session_id="leader-session")
    assert tuple(event.sequence for event in restarted_parent.transcript) == first_sequences
    assert sum(event.event_type == RUNTIME_BACKGROUND_TASK_COMPLETED for event in restarted_parent.transcript) == 1
    assert sum(event.event_type == RUNTIME_BACKGROUND_TASK_WAITING_APPROVAL for event in restarted_parent.transcript) == 1


@pytest.mark.parametrize("wait_kind", ["approval", "question"])
def test_cancel_waiting_background_child_clears_pending_state_before_task_terminal_truth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wait_kind: str,
) -> None:
    runtime, store = _background_lifecycle_runtime(tmp_path)
    parent_response = runtime.run(RuntimeRequest(prompt="leader", session_id="leader-session"))
    capability_snapshot = cast(dict[str, object], parent_response.session.metadata["agent_capability_snapshot"])
    task_id = f"task-cancel-{wait_kind}"
    child_session_id = f"child-cancel-{wait_kind}"
    _seed_waiting_child_and_task(
        store,
        workspace=tmp_path,
        task_id=task_id,
        parent_session_id="leader-session",
        child_session_id=child_session_id,
        wait_kind=wait_kind,
        capability_snapshot=capability_snapshot,
    )

    ordering: list[str] = []
    original_clear_approval = store.clear_pending_approval
    original_clear_question = store.clear_pending_question
    original_mark_terminal = store.mark_background_task_terminal

    def record_clear_approval(*, workspace: Path, session_id: str) -> None:
        ordering.append("clear_pending_approval")
        original_clear_approval(workspace=workspace, session_id=session_id)

    def record_clear_question(*, workspace: Path, session_id: str) -> None:
        ordering.append("clear_pending_question")
        original_clear_question(workspace=workspace, session_id=session_id)

    def record_mark_terminal(
        *,
        workspace: Path,
        task_id: str,
        status: str,
        error: str | None = None,
    ) -> BackgroundTaskState:
        ordering.append("mark_background_task_terminal")
        return original_mark_terminal(workspace=workspace, task_id=task_id, status=status, error=error)

    monkeypatch.setattr(store, "clear_pending_approval", record_clear_approval)
    monkeypatch.setattr(store, "clear_pending_question", record_clear_question)
    monkeypatch.setattr(store, "mark_background_task_terminal", record_mark_terminal)

    cancelled = runtime.cancel_background_task(task_id)
    assert ordering == [
        "clear_pending_approval",
        "clear_pending_question",
        "mark_background_task_terminal",
    ]
    assert cancelled.status == "cancelled"
    assert cancelled.error == "cancelled by parent while child session was waiting"
    assert store.load_pending_approval(workspace=tmp_path, session_id=child_session_id) is None
    assert store.load_pending_question(workspace=tmp_path, session_id=child_session_id) is None

    task = store.load_background_task(workspace=tmp_path, task_id=task_id)
    child = runtime.session_result(session_id=child_session_id)
    assert task.status == "cancelled"
    assert task.cancellation_cause == "cancelled by parent while child session was waiting"
    assert child.session.status == "failed"
    assert any(event.event_type == "runtime.failed" for event in child.transcript)
    parent_events = [
        event
        for event in runtime.session_result(session_id="leader-session").transcript
        if event.event_type == "runtime.background_task_cancelled" and event.payload.get("task_id") == task_id
    ]
    assert len(parent_events) == 1
    assert parent_events[0].payload["status"] == "cancelled"
    assert parent_events[0].payload["parent_session_id"] == "leader-session"
    assert parent_events[0].payload["child_session_id"] == child_session_id
