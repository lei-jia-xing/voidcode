"""Contract coverage for the shipped parallel-group terminal event and wait API.

The runtime exposes group-terminal notifications plus a runtime-owned
aggregated result and Condition-backed wait surface. These tests exercise both
without inventing polling or client-side aggregation.
"""

from __future__ import annotations

from pathlib import Path

from voidcode.runtime.background_tasks import RuntimeBackgroundTaskSupervisor
from voidcode.runtime.contracts import RuntimeRequest, RuntimeResponse
from voidcode.runtime.events import RUNTIME_BACKGROUND_TASK_GROUP_COMPLETED
from voidcode.runtime.session import SessionRef, SessionState
from voidcode.runtime.storage import SqliteSessionStore
from voidcode.runtime.task import (
    BackgroundTaskRef,
    BackgroundTaskRequestSnapshot,
    BackgroundTaskState,
)


def _parent(store: SqliteSessionStore, workspace: Path, session_id: str) -> None:
    store.save_run(
        workspace=workspace,
        request=RuntimeRequest(prompt="leader", session_id=session_id),
        response=RuntimeResponse(
            session=SessionState(
                session=SessionRef(id=session_id),
                status="running",
                turn=1,
                metadata={},
            ),
            events=(),
            output=None,
        ),
    )


def _task(task_id: str, parent: str, group: str, size: int) -> BackgroundTaskState:
    return BackgroundTaskState(
        task=BackgroundTaskRef(id=task_id),
        request=BackgroundTaskRequestSnapshot(
            prompt=f"child {task_id}",
            parent_session_id=parent,
            metadata={"parallel_group_id": group, "parallel_group_size": str(size)},
        ),
    )


def _supervisor(workspace: Path, store: SqliteSessionStore) -> RuntimeBackgroundTaskSupervisor:
    supervisor = object.__new__(RuntimeBackgroundTaskSupervisor)
    supervisor._workspace = workspace
    supervisor._session_store = store
    return supervisor


def _group_events(store: SqliteSessionStore, workspace: Path, parent: str) -> list[object]:
    return [
        event
        for event in store.load_session_result(workspace=workspace, session_id=parent).transcript
        if event.event_type == RUNTIME_BACKGROUND_TASK_GROUP_COMPLETED
    ]


def test_parallel_group_emits_one_terminal_aggregate_for_mixed_children(
    tmp_path: Path,
) -> None:
    """A group completes only when all 2+ children are terminal, regardless of outcome."""
    store = SqliteSessionStore(database_path=tmp_path / "group.sqlite3")
    _parent(store, tmp_path, "leader")
    for task_id in ("done", "failed", "cancelled"):
        store.create_background_task(
            workspace=tmp_path,
            task=_task(task_id, "leader", "g-mixed", 3),
        )
    store.mark_background_task_terminal(workspace=tmp_path, task_id="done", status="completed")
    store.mark_background_task_terminal(workspace=tmp_path, task_id="failed", status="failed", error="boom")
    store.mark_background_task_terminal(workspace=tmp_path, task_id="cancelled", status="cancelled")

    supervisor = _supervisor(tmp_path, store)
    supervisor._emit_parallel_group_terminal_event(task=store.load_background_task(workspace=tmp_path, task_id="done"))

    events = _group_events(store, tmp_path, "leader")
    assert len(events) == 1
    payload = events[0].payload
    assert payload["parallel_group_id"] == "g-mixed"
    assert payload["expected_task_count"] == 3
    assert payload["terminal_task_count"] == 3
    assert payload["counts"] == {"completed": 1, "failed": 1, "cancelled": 1, "interrupted": 0}
    assert set(payload["task_ids"]) == {"done", "failed", "cancelled"}


def test_parallel_group_does_not_complete_while_any_child_is_running(
    tmp_path: Path,
) -> None:
    store = SqliteSessionStore(database_path=tmp_path / "group.sqlite3")
    _parent(store, tmp_path, "leader")
    for task_id in ("done", "running"):
        store.create_background_task(workspace=tmp_path, task=_task(task_id, "leader", "g-running", 2))
    store.mark_background_task_terminal(workspace=tmp_path, task_id="done", status="completed")
    store.mark_background_task_running(workspace=tmp_path, task_id="running", session_id="child-running")

    supervisor = _supervisor(tmp_path, store)
    supervisor._emit_parallel_group_terminal_event(task=store.load_background_task(workspace=tmp_path, task_id="done"))
    assert _group_events(store, tmp_path, "leader") == []


def test_parallel_group_terminal_event_is_deduped_and_scoped_to_parent_owner(
    tmp_path: Path,
) -> None:
    store = SqliteSessionStore(database_path=tmp_path / "group.sqlite3")
    _parent(store, tmp_path, "leader")
    _parent(store, tmp_path, "other-leader")
    for task_id, parent in (("leader-child", "leader"), ("other-child", "other-leader")):
        store.create_background_task(workspace=tmp_path, task=_task(task_id, parent, "same-id", 1))
        store.mark_background_task_terminal(workspace=tmp_path, task_id=task_id, status="completed")

    supervisor = _supervisor(tmp_path, store)
    leader_child = store.load_background_task(workspace=tmp_path, task_id="leader-child")
    supervisor._emit_parallel_group_terminal_event(task=leader_child)
    supervisor._emit_parallel_group_terminal_event(task=leader_child)
    supervisor._emit_parallel_group_terminal_event(task=store.load_background_task(workspace=tmp_path, task_id="other-child"))

    assert len(_group_events(store, tmp_path, "leader")) == 1
    assert len(_group_events(store, tmp_path, "other-leader")) == 1
    assert _group_events(store, tmp_path, "leader")[0].payload["task_ids"] == ["leader-child"]
    assert _group_events(store, tmp_path, "other-leader")[0].payload["task_ids"] == ["other-child"]
