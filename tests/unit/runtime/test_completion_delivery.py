from __future__ import annotations

from pathlib import Path

import pytest

from voidcode.runtime.active_session import ACTIVE_SESSION_REGISTRY
from voidcode.runtime.background.models import BackgroundTaskRef, BackgroundTaskRequestSnapshot, BackgroundTaskState
from voidcode.runtime.background.supervisor import RuntimeBackgroundTaskSupervisor
from voidcode.runtime.contracts import BackgroundTaskResult, RuntimeRequest, RuntimeResponse
from voidcode.runtime.interaction_queue import drain_runtime_messages, enqueue_runtime_message
from voidcode.runtime.service import VoidCodeRuntime
from voidcode.runtime.session import SessionRef, SessionState


def _runtime_with_session(tmp_path: Path, session_id: str, status: str) -> VoidCodeRuntime:
    runtime = VoidCodeRuntime(workspace=tmp_path)
    runtime._session_store.save_run(
        workspace=tmp_path,
        request=RuntimeRequest(prompt="parent", session_id=session_id),
        response=RuntimeResponse(
            session=SessionState(
                session=SessionRef(id=session_id),
                status=status,  # type: ignore[arg-type]
                turn=1,
                metadata={},
            ),
            events=(),
            output="parent output",
        ),
    )
    return runtime


class _QueueSurface:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str, str | None]] = []

    def queue_completion_interaction(self, session_id: str, content: str, *, dedupe_key: str) -> None:
        if any(item[2] == dedupe_key for item in self.messages):
            return
        self.messages.append((session_id, content, dedupe_key))

    def queue_steering(self, session_id: str, content: str, *, dedupe_key: str | None = None) -> None:
        if dedupe_key is not None and any(item[2] == dedupe_key for item in self.messages):
            return
        self.messages.append((session_id, content, dedupe_key))


def _supervisor(tmp_path: Path, surface: _QueueSurface) -> RuntimeBackgroundTaskSupervisor:
    supervisor = object.__new__(RuntimeBackgroundTaskSupervisor)
    supervisor._workspace = tmp_path
    supervisor._surface = surface
    return supervisor


def _task(status: str = "completed") -> BackgroundTaskState:
    return BackgroundTaskState(
        task=BackgroundTaskRef(id="task-delivery"),
        status=status,  # type: ignore[arg-type]
        request=BackgroundTaskRequestSnapshot(
            prompt="child prompt",
            session_id="child-session",
            parent_session_id="parent-session",
        ),
        session_id="child-session",
    )


def _result(status: str = "completed", summary: str | None = "child summary") -> BackgroundTaskResult:
    return BackgroundTaskResult(
        task_id="task-delivery",
        parent_session_id="parent-session",
        child_session_id="child-session",
        status=status,  # type: ignore[arg-type]
        summary_output=summary,
        error=("child failed" if status == "failed" else None),
        result_available=status == "completed",
    )


def test_active_parent_completion_is_queued_once_and_consumed_once(tmp_path: Path) -> None:
    surface = _QueueSurface()
    supervisor = _supervisor(tmp_path, surface)
    ACTIVE_SESSION_REGISTRY.register(workspace=tmp_path, session_id="parent-session", run_id="run-1", metadata={})
    try:
        supervisor._queue_active_parent_completion_interaction(task=_task(), result=_result())
        supervisor._queue_active_parent_completion_interaction(task=_task(), result=_result())
    finally:
        ACTIVE_SESSION_REGISTRY.unregister(workspace=tmp_path, session_id="parent-session")

    assert len(surface.messages) == 1
    _, content, dedupe_key = surface.messages[0]
    assert dedupe_key == "background-task-completion:task-delivery:completed"
    metadata = {"pending_messages": [{"id": "m1", "kind": "steering", "content": content, "dedupe_key": dedupe_key}]}
    remaining, delivered = drain_runtime_messages(metadata, kind="steering")
    assert len(delivered) == 1
    assert "child transcript" not in delivered[0].content
    assert remaining == {}


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_failure_and_cancellation_completion_are_deliverable(tmp_path: Path, status: str) -> None:
    surface = _QueueSurface()
    supervisor = _supervisor(tmp_path, surface)
    ACTIVE_SESSION_REGISTRY.register(workspace=tmp_path, session_id="parent-session", run_id="run-1", metadata={})
    try:
        supervisor._queue_active_parent_completion_interaction(task=_task(status), result=_result(status, None))
    finally:
        ACTIVE_SESSION_REGISTRY.unregister(workspace=tmp_path, session_id="parent-session")

    assert len(surface.messages) == 1
    assert f"status={status}" in surface.messages[0][1]
    assert "child failed" in surface.messages[0][1] if status == "failed" else "No summary was provided." in surface.messages[0][1]


def test_inactive_parent_does_not_get_reactivated_or_queued(tmp_path: Path) -> None:
    surface = _QueueSurface()
    supervisor = _supervisor(tmp_path, surface)
    supervisor._queue_active_parent_completion_interaction(task=_task(), result=_result())
    assert len(surface.messages) == 1
    assert surface.messages[0][0] == "parent-session"
    assert not ACTIVE_SESSION_REGISTRY.contains(workspace=tmp_path, session_id="parent-session")


def test_completion_summary_is_bounded_and_contains_no_child_transcript(tmp_path: Path) -> None:
    surface = _QueueSurface()
    supervisor = _supervisor(tmp_path, surface)
    ACTIVE_SESSION_REGISTRY.register(workspace=tmp_path, session_id="parent-session", run_id="run-1", metadata={})
    try:
        supervisor._queue_active_parent_completion_interaction(task=_task(), result=_result(summary="x" * 5000))
    finally:
        ACTIVE_SESSION_REGISTRY.unregister(workspace=tmp_path, session_id="parent-session")

    content = surface.messages[0][1]
    summary = content.split("Summary: ", 1)[1].split(" Use background_task", 1)[0]
    assert len(summary) == 1000
    assert summary.endswith("...")
    assert "child prompt" not in content
    assert "transcript" not in content


def test_interaction_queue_dedupe_survives_restart_projection() -> None:
    metadata: dict[str, object] = {}
    metadata = enqueue_runtime_message(
        metadata,
        content="completion",
        kind="steering",
        dedupe_key="background-task-completion:task-delivery:completed",
    )
    metadata = enqueue_runtime_message(
        metadata,
        content="completion",
        kind="steering",
        dedupe_key="background-task-completion:task-delivery:completed",
    )
    assert len(metadata["pending_messages"]) == 1  # type: ignore[arg-type]
    _, first = drain_runtime_messages(metadata, kind="steering")
    _, second = drain_runtime_messages(metadata, kind="steering")
    assert len(first) == len(second) == 1


def test_public_completion_queue_drains_and_remembers_delivery_cursor(tmp_path: Path) -> None:
    runtime = _runtime_with_session(tmp_path, "active-parent", "running")
    key = "background-task-completion:task-public:completed"
    runtime.queue_completion_interaction("active-parent", "child completed", dedupe_key=key)
    assert runtime.drain_queued_messages("active-parent", kind="steering") == ("child completed",)

    # The prepare-turn drain remembers the durable key, so repeated backfill is a no-op.
    runtime.queue_completion_interaction("active-parent", "child completed", dedupe_key=key)
    stored = runtime._session_store.load_session(workspace=tmp_path, session_id="active-parent")
    assert stored.session.metadata["runtime_interaction_delivery_cursor"] == [key]
    assert "pending_messages" not in stored.session.metadata


@pytest.mark.parametrize("status", ["running", "completed"])
def test_public_completion_queue_supports_active_inactive_and_sealed_parents(tmp_path: Path, status: str) -> None:
    session_id = f"parent-{status}"
    runtime = _runtime_with_session(tmp_path, session_id, status)
    key = f"background-task-completion:task-{status}:completed"
    runtime.queue_completion_interaction(session_id, "completion", dedupe_key=key)
    assert runtime.drain_queued_messages(session_id, kind="steering") == ("completion",)


def test_public_completion_queue_rejects_unknown_parent(tmp_path: Path) -> None:
    runtime = VoidCodeRuntime(workspace=tmp_path)
    with pytest.raises(Exception, match="session"):
        runtime.queue_completion_interaction("missing-parent", "completion", dedupe_key="background-task-completion:missing:completed")
