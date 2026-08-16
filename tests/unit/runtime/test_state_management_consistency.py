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
    def __init__(self, *, output: str, is_finished: bool) -> None:
        self.output = output
        self.is_finished = is_finished
        self.tool_call = None


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
