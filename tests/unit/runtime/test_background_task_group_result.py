"""Independent coverage for background-task group result and wait contracts."""

from __future__ import annotations

from pathlib import Path

import pytest

from voidcode.runtime.background.models import (
    BackgroundTaskRef,
    BackgroundTaskRequestSnapshot,
    BackgroundTaskState,
)
from voidcode.runtime.background.supervisor import RuntimeBackgroundTaskSupervisor
from voidcode.runtime.storage import SqliteSessionStore


def _task(
    task_id: str,
    *,
    parent: str = "leader",
    group: str = "group-1",
    size: int = 4,
    status: str = "queued",
    prompt: str | None = None,
    structured_output: dict[str, object] | None = None,
) -> BackgroundTaskState:
    return BackgroundTaskState(
        task=BackgroundTaskRef(id=task_id),
        status=status,  # type: ignore[arg-type]
        structured_output=structured_output,
        request=BackgroundTaskRequestSnapshot(
            prompt=prompt or f"child {task_id}",
            parent_session_id=parent,
            metadata={
                "delegation": {
                    "mode": "background",
                    "subagent_type": "worker",
                    "parallel_group_id": group,
                    "parallel_group_size": size,
                }
            },
        ),
    )


class _BackgroundTaskConfig:
    default_concurrency = 5
    provider_concurrency: dict[str, int] = {}
    model_concurrency: dict[str, int] = {}


class _RuntimeConfig:
    background_task = _BackgroundTaskConfig()


class _Surface:
    def runtime_config_for_request(self, request: object) -> _RuntimeConfig:
        return _RuntimeConfig()


def _supervisor(workspace: Path, store: SqliteSessionStore) -> RuntimeBackgroundTaskSupervisor:
    supervisor = object.__new__(RuntimeBackgroundTaskSupervisor)
    supervisor._workspace = workspace
    supervisor._session_store = store
    supervisor._surface = _Surface()
    supervisor.backfill_parent_background_task_event = lambda *, task: None
    supervisor.task_observability = lambda task: None
    return supervisor


def _seed_group(
    workspace: Path,
    statuses: tuple[str, ...],
    *,
    group: str = "group-1",
    parent: str = "leader",
) -> SqliteSessionStore:
    store = SqliteSessionStore(database_path=workspace / "background.sqlite3")
    for index, status in enumerate(statuses):
        task_id = f"task-{index}"
        store.create_background_task(
            workspace=workspace,
            task=_task(task_id, parent=parent, group=group, size=len(statuses), status=status),
        )
        if status != "queued":
            if status == "running":
                store.mark_background_task_running(workspace=workspace, task_id=task_id, session_id=f"child-{index}")
            else:
                store.mark_background_task_terminal(
                    workspace=workspace,
                    task_id=task_id,
                    status=status,  # type: ignore[arg-type]
                    error="failure" if status == "failed" else None,
                )
    return store


def test_group_result_reports_mixed_terminal_counts_and_complete(tmp_path: Path) -> None:
    store = _seed_group(tmp_path, ("completed", "failed", "cancelled", "interrupted"))
    result = _supervisor(tmp_path, store).load_background_task_group_result(
        parallel_group_id="group-1", parent_session_id="leader", emit_result_read_hook=False
    )

    assert result.complete is True
    assert result.timed_out is False
    assert result.expected_task_count == 4
    assert result.counts == {
        "queued": 0,
        "running": 0,
        "idle": 0,
        "completed": 1,
        "failed": 1,
        "cancelled": 1,
        "interrupted": 1,
    }


def test_group_wait_timeout_marks_running_child_timed_out(tmp_path: Path) -> None:
    store = _seed_group(tmp_path, ("completed", "running"))
    result = _supervisor(tmp_path, store).wait_for_background_task_group(
        parallel_group_id="group-1",
        parent_session_id="leader",
        timeout_seconds=0,
        emit_result_read_hook=False,
    )

    assert result.timed_out is True
    assert result.complete is False
    assert result.counts["running"] == 1


def test_group_is_loaded_from_same_persistent_sqlite_after_reconcile(tmp_path: Path) -> None:
    _seed_group(tmp_path, ("completed", "running"), group="after-restart")
    # A fresh store/supervisor models a process restart; reconcile owns the
    # interrupted in-flight row before the group is projected again.
    restarted_store = SqliteSessionStore(database_path=tmp_path / "background.sqlite3")
    reconciled = restarted_store.fail_incomplete_background_tasks(workspace=tmp_path, message="runtime restarted")
    assert [task.task.id for task in reconciled] == ["task-1"]

    result = _supervisor(tmp_path, restarted_store).load_background_task_group_result(
        parallel_group_id="after-restart", parent_session_id="leader", emit_result_read_hook=False
    )
    assert result.task_ids == ("task-0", "task-1")
    assert result.complete is True
    assert result.counts["interrupted"] == 1


def test_group_rejects_parent_mismatch_duplicate_ids_and_size_mismatch(tmp_path: Path) -> None:
    store = _seed_group(tmp_path, ("completed", "completed"), parent="owner-a")
    supervisor = _supervisor(tmp_path, store)

    with pytest.raises(ValueError, match="only its parent session"):
        supervisor.load_background_task_group_result(task_ids=("task-0", "task-1"), parent_session_id="owner-b", emit_result_read_hook=False)
    with pytest.raises(ValueError, match="duplicates"):
        supervisor.load_background_task_group_result(task_ids=("task-0", "task-0"), emit_result_read_hook=False)

    store.create_background_task(
        workspace=tmp_path,
        task=_task("task-extra", parent="owner-a", group="size-mismatch", size=3),
    )
    store.mark_background_task_terminal(workspace=tmp_path, task_id="task-extra", status="completed")
    with pytest.raises(ValueError, match="size mismatch"):
        supervisor.load_background_task_group_result(parallel_group_id="size-mismatch", parent_session_id="owner-a", emit_result_read_hook=False)


def test_task_owner_authorization_rejects_foreign_parent_without_mutation(tmp_path: Path) -> None:
    store = _seed_group(tmp_path, ("running",), parent="owner-a")
    supervisor = _supervisor(tmp_path, store)
    before = store.load_background_task(workspace=tmp_path, task_id="task-0")

    with pytest.raises(ValueError, match="only its parent session"):
        supervisor.authorize_background_task_owner("task-0", parent_session_id="owner-b")

    after = store.load_background_task(workspace=tmp_path, task_id="task-0")
    assert after == before


def test_explicit_task_ids_authorize_each_task_before_loading_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _seed_group(tmp_path, ("completed", "completed"), parent="owner-a")
    supervisor = _supervisor(tmp_path, store)
    calls: list[tuple[str, str]] = []
    original_load = store.load_background_task

    def record_load(*, workspace: Path, task_id: str) -> BackgroundTaskState:
        calls.append(("load", task_id))
        return original_load(workspace=workspace, task_id=task_id)

    def record_authorize(task_id: str, *, parent_session_id: str | None) -> None:
        calls.append(("authorize", task_id))
        assert parent_session_id == "owner-a"

    monkeypatch.setattr(store, "load_background_task", record_load)
    monkeypatch.setattr(supervisor, "authorize_background_task_owner", record_authorize)

    tasks, _group_id, _expected_count = supervisor._resolve_background_task_group(
        task_ids=("task-0", "task-1"),
        parallel_group_id=None,
        parent_session_id="owner-a",
    )

    assert [task.task.id for task in tasks] == ["task-0", "task-1"]
    assert calls == [
        ("authorize", "task-0"),
        ("authorize", "task-1"),
        ("load", "task-0"),
        ("load", "task-1"),
    ]


def test_group_result_summary_and_structured_output_are_bounded_and_not_transcript(
    tmp_path: Path,
) -> None:
    transcript_secret = "PRIVATE CHILD TRANSCRIPT"
    store = SqliteSessionStore(database_path=tmp_path / "background.sqlite3")
    task = _task(
        "task-safe",
        size=1,
        prompt=transcript_secret,
        structured_output={"answer": "structured value"},
    )
    store.create_background_task(workspace=tmp_path, task=task)
    store.mark_background_task_terminal(
        workspace=tmp_path,
        task_id="task-safe",
        status="completed",
    )

    result = _supervisor(tmp_path, store).load_background_task_group_result(task_ids=("task-safe",), emit_result_read_hook=False)
    projected = result.results[0]
    assert projected.summary_output is None or len(projected.summary_output) <= 1000
    assert projected.structured_output == {"answer": "structured value"}
    assert transcript_secret not in repr(projected)
    assert "transcript" not in repr(projected).lower()


def test_parent_status_storage_errors_are_observable_and_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SqliteSessionStore(database_path=tmp_path / "parent-status.sqlite3")
    supervisor = _supervisor(tmp_path, store)

    def fail_load_session_status(*, workspace: Path, session_id: str) -> str:
        _ = workspace, session_id
        raise RuntimeError("schema failure")

    monkeypatch.setattr(store, "load_session_status", fail_load_session_status)
    with pytest.raises(RuntimeError, match="schema failure"):
        supervisor._parent_session_is_terminal("leader")
