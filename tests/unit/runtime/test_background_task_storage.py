from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from voidcode.runtime.storage import SqliteSessionStore
from voidcode.runtime.task import (
    BackgroundTaskRef,
    BackgroundTaskRequestSnapshot,
    BackgroundTaskState,
    BackgroundTaskStatus,
    validate_background_task_id,
)


def _task(*, task_id: str = "task-1", prompt: str = "read sample.txt") -> BackgroundTaskState:
    return BackgroundTaskState(
        task=BackgroundTaskRef(id=task_id),
        request=BackgroundTaskRequestSnapshot(prompt=prompt),
        created_at=1,
        updated_at=1,
    )


def test_validate_background_task_id_rejects_empty_and_slash() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        validate_background_task_id("")

    with pytest.raises(ValueError, match="must not contain '/'"):
        validate_background_task_id("task/1")


def test_background_task_storage_create_load_and_list(tmp_path: Path) -> None:
    store = SqliteSessionStore()
    task = _task(task_id="task-a")

    store.create_background_task(workspace=tmp_path, task=task)

    loaded = store.load_background_task(workspace=tmp_path, task_id="task-a")
    listed = store.list_background_tasks(workspace=tmp_path)

    assert loaded == task
    assert len(listed) == 1
    assert listed[0].task.id == "task-a"
    assert listed[0].status == "queued"
    assert listed[0].prompt == "read sample.txt"


def test_background_task_storage_marks_running_and_terminal(tmp_path: Path) -> None:
    store = SqliteSessionStore()
    store.create_background_task(workspace=tmp_path, task=_task(task_id="task-b"))

    running = store.mark_background_task_running(
        workspace=tmp_path,
        task_id="task-b",
        session_id="session-b",
    )
    completed = store.mark_background_task_terminal(
        workspace=tmp_path,
        task_id="task-b",
        status="completed",
    )

    assert running.status == "running"
    assert running.session_id == "session-b"
    assert running.started_at is not None
    assert completed.status == "completed"
    assert completed.session_id == "session-b"
    assert completed.finished_at is not None


def test_background_task_storage_cancel_semantics(tmp_path: Path) -> None:
    store = SqliteSessionStore()
    store.create_background_task(workspace=tmp_path, task=_task(task_id="task-c"))

    cancelled = store.request_background_task_cancel(workspace=tmp_path, task_id="task-c")

    assert cancelled.status == "cancelled"
    assert cancelled.error == "cancelled before start"


def test_background_task_storage_running_cancel_records_request(tmp_path: Path) -> None:
    store = SqliteSessionStore()
    store.create_background_task(workspace=tmp_path, task=_task(task_id="task-d"))
    _ = store.mark_background_task_running(
        workspace=tmp_path,
        task_id="task-d",
        session_id="session-d",
    )

    cancelled = store.request_background_task_cancel(workspace=tmp_path, task_id="task-d")

    assert cancelled.status == "running"
    assert cancelled.cancel_requested_at is not None


def test_background_task_storage_does_not_overwrite_queued_cancelled_task_when_marking_running(
    tmp_path: Path,
) -> None:
    store = SqliteSessionStore()
    store.create_background_task(workspace=tmp_path, task=_task(task_id="task-race"))
    cancelled = store.request_background_task_cancel(workspace=tmp_path, task_id="task-race")

    running = store.mark_background_task_running(
        workspace=tmp_path,
        task_id="task-race",
        session_id="session-race",
    )

    assert cancelled.status == "cancelled"
    assert running.status == "cancelled"
    assert running.session_id is None
    assert running.started_at is None
    assert running.finished_at is not None


@pytest.mark.parametrize(
    "status",
    cast(tuple[BackgroundTaskStatus, ...], ("completed", "failed", "cancelled")),
)
def test_background_task_storage_does_not_overwrite_terminal_task_when_marking_running(
    tmp_path: Path,
    status: BackgroundTaskStatus,
) -> None:
    store = SqliteSessionStore()
    store.create_background_task(workspace=tmp_path, task=_task(task_id=f"task-{status}"))
    terminal = store.mark_background_task_terminal(
        workspace=tmp_path,
        task_id=f"task-{status}",
        status=status,
        error="terminal state",
    )

    running = store.mark_background_task_running(
        workspace=tmp_path,
        task_id=f"task-{status}",
        session_id="session-terminal",
    )

    assert terminal.status == status
    assert running == terminal
    assert running.session_id is None
    assert running.started_at is None


def test_background_task_storage_reconciles_incomplete_tasks_on_restart(tmp_path: Path) -> None:
    store = SqliteSessionStore()
    store.create_background_task(workspace=tmp_path, task=_task(task_id="task-e"))
    store.create_background_task(workspace=tmp_path, task=_task(task_id="task-f"))
    _ = store.mark_background_task_running(
        workspace=tmp_path,
        task_id="task-f",
        session_id="session-f",
    )

    reconciled = store.fail_incomplete_background_tasks(
        workspace=tmp_path,
        message="background task interrupted before completion",
    )

    assert [task.task.id for task in reconciled] == ["task-e", "task-f"]
    assert all(task.status == "failed" for task in reconciled)
    assert all(task.error == "background task interrupted before completion" for task in reconciled)
