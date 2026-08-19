"""State-management consistency tests for sessions and background tasks.

Defends the fixed invariants:

1. Terminal status ↔ event canonical mapping: ``interrupted`` tasks emit
   ``runtime.background_task_interrupted`` (never ``background_task_failed``).
2. Orphaned terminal tasks (no child session, terminal/missing parent) are
   pruned by the existing list-triggered prune path instead of accumulating.
3. Event-less dangling-parent terminal children (no task reference) are pruned;
   children with events or a task reference survive.
4. The session seal watermark never exceeds the persisted event log
   (``last_event_sequence``, terminal checkpoint, and terminal notification all
   reference durable truth).
5. ``save_interrupted_checkpoint`` persists ``parent_session_id`` on both the
   insert and update paths.
6. Restart/worker-death reconciliation: a ``running`` task with no live worker
   thread is terminalized ``interrupted`` (retryable) on the next drain, while
   tasks waiting on approval/question survive.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest

from voidcode.runtime.config import RuntimeConfig, RuntimeMcpConfig
from voidcode.runtime.contracts import RuntimeRequest, RuntimeResponse
from voidcode.runtime.events import (
    RUNTIME_BACKGROUND_TASK_FAILED,
    RUNTIME_BACKGROUND_TASK_INTERRUPTED,
    EventEnvelope,
)
from voidcode.runtime.permission import PendingApproval
from voidcode.runtime.service import SessionState, VoidCodeRuntime
from voidcode.runtime.session import SessionRef
from voidcode.runtime.storage import SqliteSessionStore
from voidcode.runtime.task import (
    BackgroundTaskRef,
    BackgroundTaskRequestSnapshot,
    BackgroundTaskState,
    is_background_task_terminal,
)
from voidcode.tools.contracts import ToolCall


def _completed_response(session_id: str) -> RuntimeResponse:
    return RuntimeResponse(
        session=SessionState(
            session=SessionRef(id=session_id),
            status="completed",
            turn=1,
            metadata={},
        ),
        events=(
            EventEnvelope(
                session_id=session_id,
                sequence=1,
                event_type="graph.response_ready",
                source="graph",
            ),
        ),
        output="done",
    )


class _BlockingTaskGraph:
    """First child blocks on an event; further children finish immediately."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.prompts_seen: list[str] = []

    def step(
        self,
        request: Any,
        tool_results: tuple[object, ...],
        *,
        session: SessionState,
    ) -> Any:
        _ = tool_results
        self.prompts_seen.append(request.prompt)
        if session.session.parent_id is not None:
            if not self.started.is_set():
                self.started.set()
                assert self.release.wait(timeout=5)
            return _StubStep(output=f"{request.prompt} done", is_finished=True)
        return _StubStep(output=request.prompt, is_finished=True)


class _StubStep:
    def __init__(self, *, output: str | None = None, is_finished: bool = False, tool_call: Any | None = None) -> None:
        self.output = output
        self.is_finished = is_finished
        self.tool_call = tool_call


def _wait_for_terminal(runtime: VoidCodeRuntime, task_id: str, *, timeout: float = 5.0) -> BackgroundTaskState:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = runtime.load_background_task(task_id)
        if is_background_task_terminal(task.status):
            return task
        time.sleep(0.01)
    raise AssertionError(f"task {task_id} did not reach terminal state")


def _queued_task(task_id: str, *, parent_session_id: str | None = None) -> BackgroundTaskState:
    return BackgroundTaskState(
        task=BackgroundTaskRef(id=task_id),
        status="queued",
        request=BackgroundTaskRequestSnapshot(
            prompt=task_id,
            parent_session_id=parent_session_id,
        ),
    )


# ── 1. status ↔ event canonical mapping ────────────────────────────────────


def test_shutdown_terminalized_queued_task_emits_interrupted_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = runtime_factory(tmp_path)
    _ = runtime.run(RuntimeRequest(prompt="leader", session_id="leader-session"))
    supervisor = runtime._background_task_supervisor
    real_can_start = supervisor._can_start_task
    blocked = {"value": True}

    def _can_start_when_unblocked(identity: object) -> bool:
        if blocked["value"]:
            return False
        return real_can_start(identity)

    monkeypatch.setattr(supervisor, "_can_start_task", _can_start_when_unblocked)
    started = runtime.start_background_task(RuntimeRequest(prompt="queued orphan", parent_session_id="leader-session"))
    assert started.status == "queued"

    supervisor.shutdown(timeout_seconds=0.1)

    terminal = runtime.load_background_task(started.task.id)
    assert terminal.status == "interrupted"
    assert terminal.session_id is None

    leader = runtime._session_store.load_session(workspace=tmp_path, session_id="leader-session")
    interrupted_events = [
        event
        for event in leader.events
        if event.event_type == RUNTIME_BACKGROUND_TASK_INTERRUPTED and event.payload.get("task_id") == started.task.id
    ]
    failed_events = [
        event for event in leader.events if event.event_type == RUNTIME_BACKGROUND_TASK_FAILED and event.payload.get("task_id") == started.task.id
    ]
    # Canonical mapping: interrupted status -> runtime.background_task_interrupted.
    assert len(interrupted_events) == 1
    assert interrupted_events[0].payload["status"] == "interrupted"
    assert interrupted_events[0].payload.get("error") == "runtime shutdown requested before delegated worker execution started"
    assert failed_events == []


def runtime_factory(tmp_path: Path) -> VoidCodeRuntime:
    return VoidCodeRuntime(
        workspace=tmp_path,
        graph=_BlockingTaskGraph(),
        config=RuntimeConfig(mcp=RuntimeMcpConfig(enabled=False)),
    )


# ── 2. orphaned terminal task pruning ──────────────────────────────────────


def test_list_sessions_prunes_orphaned_terminal_tasks_with_terminal_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SqliteSessionStore()
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("VOIDCODE_DB_PATH", str(db_path))

    # A terminal parent session.
    store.save_run(
        workspace=tmp_path,
        request=RuntimeRequest(prompt="leader", session_id="leader-session"),
        response=_completed_response("leader-session"),
    )
    # Shutdown-terminalized queued tasks: terminal, no child session, terminal parent.
    for task_id in ("task-orphan-1", "task-orphan-2"):
        store.create_background_task(
            workspace=tmp_path,
            task=BackgroundTaskState(
                task=BackgroundTaskRef(id=task_id),
                status="interrupted",
                request=BackgroundTaskRequestSnapshot(prompt=task_id, parent_session_id="leader-session"),
                error="runtime shutdown requested before delegated worker execution started",
            ),
        )
    # A terminal task with a LIVE child session must survive (result linkage).
    store.create_background_task(
        workspace=tmp_path,
        task=BackgroundTaskState(
            task=BackgroundTaskRef(id="task-with-child"),
            status="completed",
            request=BackgroundTaskRequestSnapshot(prompt="child task", parent_session_id="leader-session"),
            session_id="child-session",
        ),
    )
    store.save_run(
        workspace=tmp_path,
        request=RuntimeRequest(prompt="child task", session_id="child-session", parent_session_id="leader-session"),
        response=_completed_response("child-session"),
    )
    # A terminal task under a non-terminal parent must survive.
    store.create_background_task(
        workspace=tmp_path,
        task=BackgroundTaskState(
            task=BackgroundTaskRef(id="task-live-parent"),
            status="interrupted",
            request=BackgroundTaskRequestSnapshot(prompt="x", parent_session_id="running-parent"),
        ),
    )
    store.save_run(
        workspace=tmp_path,
        request=RuntimeRequest(prompt="running", session_id="running-parent"),
        response=RuntimeResponse(
            session=SessionState(session=SessionRef(id="running-parent"), status="running", turn=1, metadata={}),
            events=(),
            output=None,
        ),
    )

    _ = store.list_sessions(workspace=tmp_path)

    remaining = {summary.task.id for summary in store.list_background_tasks(workspace=tmp_path)}
    assert "task-orphan-1" not in remaining
    assert "task-orphan-2" not in remaining
    assert "task-with-child" in remaining
    assert "task-live-parent" in remaining
    # The protected child session of the surviving task still exists.
    assert store.has_session(workspace=tmp_path, session_id="child-session")


# ── 3. dangling-parent terminal child pruning ──────────────────────────────


def test_list_sessions_prunes_eventless_dangling_child_but_keeps_real_children(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SqliteSessionStore()
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("VOIDCODE_DB_PATH", str(db_path))

    # Fabricated residue: terminal, parent row missing, NO persisted events.
    store.save_run(
        workspace=tmp_path,
        request=RuntimeRequest(prompt="ghost", session_id="ghost-child", parent_session_id="gone-parent"),
        response=RuntimeResponse(
            session=SessionState(session=SessionRef(id="ghost-child", parent_id="gone-parent"), status="failed", turn=1, metadata={}),
            events=(),
            output=None,
        ),
    )
    # Real child: terminal, parent missing, but has a persisted event log.
    store.save_interrupted_checkpoint(
        workspace=tmp_path,
        session_id="real-child",
        prompt="real",
        session_metadata={},
        tool_results=(),
        last_event_sequence=0,
        create_if_missing=True,
        parent_session_id="gone-parent",
    )
    store.append_session_events(
        workspace=tmp_path,
        session_id="real-child",
        events=(("runtime.request_received", "runtime", {"prompt": "real"}, None),),
    )
    store.save_run(
        workspace=tmp_path,
        request=RuntimeRequest(prompt="real", session_id="real-child", parent_session_id="gone-parent"),
        response=RuntimeResponse(
            session=SessionState(session=SessionRef(id="real-child", parent_id="gone-parent"), status="failed", turn=1, metadata={}),
            events=(),
            output=None,
        ),
    )

    _ = store.list_sessions(workspace=tmp_path)

    assert not store.has_session(workspace=tmp_path, session_id="ghost-child")
    assert store.has_session(workspace=tmp_path, session_id="real-child")


# ── 4. seal watermark clamped to persisted event log ───────────────────────


def test_save_run_never_inflates_last_event_sequence_beyond_persisted_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SqliteSessionStore()
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("VOIDCODE_DB_PATH", str(db_path))

    session_id = "clamp-session"
    store.save_interrupted_checkpoint(
        workspace=tmp_path,
        session_id=session_id,
        prompt="clamp",
        session_metadata={},
        tool_results=(),
        last_event_sequence=0,
        create_if_missing=True,
    )
    appended = store.append_session_events(
        workspace=tmp_path,
        session_id=session_id,
        events=(
            ("runtime.request_received", "runtime", {"prompt": "clamp"}, None),
            ("runtime.skills_loaded", "runtime", {"skills": []}, None),
            ("runtime.failed", "runtime", {"error": "boom"}, None),
        ),
    )
    # A response whose trailing event carries a LOCALLY resequenced sequence
    # (resume paths resequence client-only events) must not inflate the row.
    inflated_events = (
        appended[0],
        appended[1],
        appended[2],
        EventEnvelope(
            session_id=session_id,
            sequence=10,
            event_type="runtime.failed",
            source="runtime",
            payload={"error": "boom"},
        ),
    )
    store.save_run(
        workspace=tmp_path,
        request=RuntimeRequest(prompt="clamp", session_id=session_id),
        response=RuntimeResponse(
            session=SessionState(session=SessionRef(id=session_id), status="failed", turn=1, metadata={}),
            events=inflated_events,
            output=None,
        ),
    )

    loaded = store.load_session(workspace=tmp_path, session_id=session_id)
    checkpoint = store.load_resume_checkpoint(workspace=tmp_path, session_id=session_id)
    notifications = store.list_notifications(workspace=tmp_path)

    assert loaded.session.status == "failed"
    # The persisted event log has exactly 3 events.
    assert [event.sequence for event in loaded.events] == [1, 2, 3]
    assert checkpoint is not None
    assert checkpoint["last_event_sequence"] == 3
    assert len(notifications) == 1
    assert notifications[0].event_sequence == 3


# ── 5. save_interrupted_checkpoint persists parent ─────────────────────────


def test_save_interrupted_checkpoint_persists_parent_session_id(tmp_path: Path) -> None:
    store = SqliteSessionStore()

    # Insert path (create_if_missing): the first un-sealed row carries the parent.
    store.save_interrupted_checkpoint(
        workspace=tmp_path,
        session_id="child-new",
        prompt="child",
        session_metadata={},
        tool_results=(),
        last_event_sequence=0,
        create_if_missing=True,
        parent_session_id="parent-session",
    )
    loaded = store.load_session(workspace=tmp_path, session_id="child-new")
    assert loaded.session.session.parent_id == "parent-session"

    # Update path preserves an existing parent when the caller omits it.
    store.save_interrupted_checkpoint(
        workspace=tmp_path,
        session_id="child-new",
        prompt="child again",
        session_metadata={},
        tool_results=(),
        last_event_sequence=0,
        create_if_missing=True,
    )
    loaded = store.load_session(workspace=tmp_path, session_id="child-new")
    assert loaded.session.session.parent_id == "parent-session"


# ── 6. worker-death convergence on the drain path ──────────────────────────


def test_drain_terminalizes_running_task_without_live_worker(tmp_path: Path) -> None:
    runtime = runtime_factory(tmp_path)
    supervisor = runtime._background_task_supervisor
    store = runtime._session_store

    # A running task whose child session is waiting on a pending approval must
    # survive (approval/question waiting state is preserved across restarts).
    store.create_background_task(
        workspace=tmp_path,
        task=BackgroundTaskState(
            task=BackgroundTaskRef(id="task-waiting"),
            status="running",
            request=BackgroundTaskRequestSnapshot(prompt="waiting child"),
            session_id="child-waiting",
        ),
    )
    store.save_interrupted_checkpoint(
        workspace=tmp_path,
        session_id="child-waiting",
        prompt="waiting child",
        session_metadata={"background_task_id": "task-waiting", "background_run": True},
        tool_results=(),
        last_event_sequence=0,
        create_if_missing=True,
    )
    store.append_session_events(
        workspace=tmp_path,
        session_id="child-waiting",
        events=(
            ("runtime.request_received", "runtime", {"prompt": "waiting child"}, None),
            ("runtime.approval_requested", "runtime", {"request_id": "approval-1", "tool": "write"}, None),
        ),
    )
    store.save_pending_approval(
        workspace=tmp_path,
        request=RuntimeRequest(prompt="waiting child", session_id="child-waiting"),
        response=RuntimeResponse(
            session=SessionState(session=SessionRef(id="child-waiting"), status="waiting", turn=1, metadata={}),
            events=(),
            output=None,
        ),
        pending_approval=PendingApproval(
            request_id="approval-1",
            tool_name="write",
            arguments={"path": "child.txt"},
        ),
    )
    supervisor.reconcile_background_tasks_if_needed()
    assert runtime.load_background_task("task-waiting").status == "running"

    # In-process worker death AFTER reconcile: a running task whose worker
    # thread no longer exists must be terminalized by the next drain instead of
    # staying ``running`` forever.
    store.create_background_task(
        workspace=tmp_path,
        task=BackgroundTaskState(
            task=BackgroundTaskRef(id="task-dead-worker"),
            status="running",
            request=BackgroundTaskRequestSnapshot(prompt="dead worker"),
            session_id="child-dead",
        ),
    )
    store.save_interrupted_checkpoint(
        workspace=tmp_path,
        session_id="child-dead",
        prompt="dead worker",
        session_metadata={},
        tool_results=(),
        last_event_sequence=0,
        create_if_missing=True,
    )

    dead = runtime.load_background_task("task-dead-worker")
    waiting = runtime.load_background_task("task-waiting")

    assert dead.status == "interrupted"
    assert dead.error == "background task worker exited before a terminal update"
    assert waiting.status == "running"


# ── 7. completed children are sealed completed; interrupted rows with a
#       submit_result handoff are repaired ───────────────────────────────────


class _SubmitResultChildGraph:
    """Top-level runs finish immediately; delegated children call submit_result."""

    def step(
        self,
        request: Any,
        tool_results: tuple[object, ...],
        *,
        session: SessionState,
    ) -> Any:
        _ = request, tool_results
        if session.session.parent_id is not None:
            return _StubStep(
                output=None,
                is_finished=False,
                tool_call=ToolCall(tool_name="submit_result", arguments={"summary": request.prompt}),
            )
        return _StubStep(output=request.prompt, is_finished=True)


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


def _seed_unsealed_completed_child(
    store: SqliteSessionStore,
    *,
    workspace: Path,
    task_id: str,
    parent_session_id: str,
    child_session_id: str,
) -> tuple[EventEnvelope, ...]:
    """Seed a task + child whose ROW is ``interrupted`` but whose transcript
    proves a successful ``submit_result`` handoff (the unsealed-seal state the
    run loop can leave behind when its generator-driven seal is skipped)."""
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
    appended = store.append_session_events(
        workspace=workspace,
        session_id=child_session_id,
        events=(
            ("runtime.request_received", "runtime", {"prompt": "child probe"}, None),
            (
                "runtime.tool_completed",
                "tool",
                {
                    "tool": "submit_result",
                    "status": "ok",
                    "arguments": {"summary": "done"},
                    "handoff": {"summary": "done", "data": {"completed_work": ["completed the probe"]}},
                },
                None,
            ),
            ("graph.response_ready", "graph", {"output_preview": "done", "source": "submit_result"}, None),
        ),
    )
    store.create_background_task(
        workspace=workspace,
        task=BackgroundTaskState(
            task=BackgroundTaskRef(id=task_id),
            status="interrupted",
            request=BackgroundTaskRequestSnapshot(
                prompt="child probe",
                parent_session_id=parent_session_id,
            ),
            session_id=child_session_id,
            error="background task worker exited before a terminal update",
        ),
    )
    return appended


def test_completed_background_child_session_is_sealed_completed(tmp_path: Path) -> None:
    """A delegated child that finishes with submit_result is persisted
    ``completed`` with ``last_event_sequence`` equal to its actual event-log
    max — never left ``interrupted`` at a stale checkpoint."""
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        graph=_SubmitResultChildGraph(),  # type: ignore[arg-type]
        config=RuntimeConfig(
            approval_mode="allow",
            execution_engine="deterministic",
            mcp=RuntimeMcpConfig(enabled=False),
        ),
    )
    _ = runtime.run(RuntimeRequest(prompt="leader", session_id="leader-session"))

    started = runtime.start_background_task(_delegated_request("child probe"))
    task = _wait_for_terminal(runtime, started.task.id)
    assert task.status == "completed"
    assert task.session_id is not None

    store = runtime._session_store
    child = store.load_session(workspace=tmp_path, session_id=task.session_id)
    assert child.session.status == "completed"
    assert child.session.session.parent_id == "leader-session"
    # The seal watermark references the actual persisted event log, not a
    # stale mid-run checkpoint.
    checkpoint = store.load_resume_checkpoint(workspace=tmp_path, session_id=task.session_id)
    assert checkpoint is not None
    assert checkpoint["kind"] == "terminal"
    assert checkpoint["last_event_sequence"] == max(event.sequence for event in child.events)
    assert store.load_session_status(workspace=tmp_path, session_id=task.session_id) == "completed"


def test_interrupted_child_with_submit_result_handoff_is_repaired_completed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ``interrupted`` child whose transcript proves a successful
    ``submit_result`` handoff is repaired: the task is finalized ``completed``
    AND the unsealed session row is sealed ``completed`` (watermark = max)."""
    store = SqliteSessionStore()
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("VOIDCODE_DB_PATH", str(db_path))
    task_id = "task-repair-handoff"
    child_session_id = "child-repair-handoff"

    appended = _seed_unsealed_completed_child(
        store,
        workspace=tmp_path,
        task_id=task_id,
        parent_session_id="leader-session",
        child_session_id=child_session_id,
    )
    # The seeded state must actually mirror the bug: task interrupted AND the
    # session row still interrupted with the transcript already durable.
    assert store.load_session_status(workspace=tmp_path, session_id=child_session_id) == "interrupted"

    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        session_store=store,
        config=RuntimeConfig(mcp=RuntimeMcpConfig(enabled=False)),
    )
    supervisor = runtime._background_task_supervisor

    task = store.load_background_task(workspace=tmp_path, task_id=task_id)
    repaired = supervisor.repair_interrupted_task_from_child_terminal_session(task)

    assert repaired.status == "completed"
    assert repaired.error is None

    repaired_child = store.load_session(workspace=tmp_path, session_id=child_session_id)
    assert repaired_child.session.status == "completed"
    checkpoint = store.load_resume_checkpoint(workspace=tmp_path, session_id=child_session_id)
    assert checkpoint is not None
    assert checkpoint["kind"] == "terminal"
    assert checkpoint["last_event_sequence"] == appended[-1].sequence


def test_finalize_completed_task_repairs_unsealed_child_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finalizing a task as ``completed`` also seals the child session row
    when the run's own seal was skipped: the row is repaired to ``completed``
    at the actual event-log max instead of staying ``interrupted``."""
    store = SqliteSessionStore()
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("VOIDCODE_DB_PATH", str(db_path))
    task_id = "task-finalize-repair"
    child_session_id = "child-finalize-repair"

    appended = _seed_unsealed_completed_child(
        store,
        workspace=tmp_path,
        task_id=task_id,
        parent_session_id="leader-session",
        child_session_id=child_session_id,
    )
    # Task already finalized ``completed`` while the child row stayed
    # ``interrupted`` (the seal was skipped/downgraded on the worker path).
    store.mark_background_task_terminal(
        workspace=tmp_path,
        task_id=task_id,
        status="completed",
    )

    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        session_store=store,
        config=RuntimeConfig(mcp=RuntimeMcpConfig(enabled=False)),
    )
    supervisor = runtime._background_task_supervisor
    task = store.load_background_task(workspace=tmp_path, task_id=task_id)
    child_response = supervisor.load_background_task_child_response(task=task)
    assert child_response is not None
    assert child_response.session.status == "interrupted"

    supervisor.finalize_background_task_from_session_response(session_response=child_response)

    repaired_child = store.load_session(workspace=tmp_path, session_id=child_session_id)
    assert repaired_child.session.status == "completed"
    checkpoint = store.load_resume_checkpoint(workspace=tmp_path, session_id=child_session_id)
    assert checkpoint is not None
    assert checkpoint["kind"] == "terminal"
    assert checkpoint["last_event_sequence"] == appended[-1].sequence
    # The already-terminal task row is untouched.
    assert store.load_background_task(workspace=tmp_path, task_id=task_id).status == "completed"


def test_interrupted_child_without_handoff_stays_resumable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A genuinely resumable interrupted child (no submit_result handoff) is
    never sealed ``completed`` nor its task terminalized by the repair path."""
    store = SqliteSessionStore()
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("VOIDCODE_DB_PATH", str(db_path))
    task_id = "task-resumable"
    child_session_id = "child-resumable"

    store.save_interrupted_checkpoint(
        workspace=tmp_path,
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
    # Mid-flight transcript: a tool ran but no submit_result handoff, no
    # graph.response_ready — the run is genuinely resumable.
    store.append_session_events(
        workspace=tmp_path,
        session_id=child_session_id,
        events=(
            ("runtime.request_received", "runtime", {"prompt": "child probe"}, None),
            ("runtime.tool_completed", "tool", {"tool": "read", "status": "ok", "content": "probe"}, None),
        ),
    )
    store.create_background_task(
        workspace=tmp_path,
        task=BackgroundTaskState(
            task=BackgroundTaskRef(id=task_id),
            status="interrupted",
            request=BackgroundTaskRequestSnapshot(
                prompt="child probe",
                parent_session_id="leader-session",
            ),
            session_id=child_session_id,
            error="background task worker exited before a terminal update",
        ),
    )

    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        session_store=store,
        config=RuntimeConfig(mcp=RuntimeMcpConfig(enabled=False)),
    )
    supervisor = runtime._background_task_supervisor

    task = store.load_background_task(workspace=tmp_path, task_id=task_id)
    repaired = supervisor.repair_interrupted_task_from_child_terminal_session(task)

    assert repaired.status == "interrupted"
    assert store.load_session_status(workspace=tmp_path, session_id=child_session_id) == "interrupted"
