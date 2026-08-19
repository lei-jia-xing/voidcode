from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from voidcode.runtime.paths import sessions_db_path
from voidcode.runtime.storage import SqliteSessionStore
from voidcode.runtime.task import (
    BackgroundTaskRef,
    BackgroundTaskRequestSnapshot,
    BackgroundTaskState,
    BackgroundTaskStatus,
    is_background_task_terminal,
    is_background_task_transition_allowed,
)


def _task(
    *,
    task_id: str = "task-1",
    prompt: str = "read sample.txt",
    keep_alive: bool = False,
) -> BackgroundTaskState:
    metadata: dict[str, object] = {}
    if keep_alive:
        metadata["keep_alive"] = True
    return BackgroundTaskState(
        task=BackgroundTaskRef(id=task_id),
        request=BackgroundTaskRequestSnapshot(prompt=prompt, metadata=metadata),
        created_at=1,
        updated_at=1,
    )


@pytest.mark.parametrize(
    ("current", "next_"),
    [
        ("running", "idle"),
        ("idle", "running"),
        ("idle", "cancelled"),
        ("idle", "interrupted"),
        ("interrupted", "running"),
        ("interrupted", "idle"),
    ],
)
def test_background_task_keep_alive_transitions_allowed(
    current: BackgroundTaskStatus,
    next_: BackgroundTaskStatus,
) -> None:
    assert is_background_task_transition_allowed(current_status=current, next_status=next_)


@pytest.mark.parametrize(
    ("current", "next_"),
    [
        ("completed", "idle"),
        ("failed", "idle"),
        ("cancelled", "idle"),
        # Direct completion from idle is allowed at the matrix level (D1 keeps
        # idle -> {completed, failed} for mark_background_task_terminal
        # generality); the runtime surface reaches completion only via
        # idle -> running -> terminal.
        ("queued", "idle"),
    ],
)
def test_background_task_keep_alive_transitions_rejected(
    current: BackgroundTaskStatus,
    next_: BackgroundTaskStatus,
) -> None:
    assert not is_background_task_transition_allowed(current_status=current, next_status=next_)


def test_background_task_keep_alive_idle_is_not_terminal() -> None:
    assert is_background_task_terminal("idle") is False
    assert is_background_task_terminal("completed") is True
    assert is_background_task_terminal("failed") is True
    assert is_background_task_terminal("cancelled") is True
    assert is_background_task_terminal("interrupted") is True


def test_background_task_storage_mark_idle_from_running(tmp_path: Path) -> None:
    store = SqliteSessionStore()
    store.create_background_task(workspace=tmp_path, task=_task(task_id="task-idle"))

    running = store.mark_background_task_running(
        workspace=tmp_path,
        task_id="task-idle",
        session_id="session-idle",
    )
    idle = store.mark_background_task_idle(workspace=tmp_path, task_id="task-idle")

    assert running.status == "running"
    assert idle.status == "idle"
    assert idle.session_id == "session-idle"
    assert idle.steer_prompt is None
    assert idle.result_available is False


def test_background_task_storage_mark_idle_from_queued_is_noop(tmp_path: Path) -> None:
    store = SqliteSessionStore()
    store.create_background_task(workspace=tmp_path, task=_task(task_id="task-queued"))

    idle = store.mark_background_task_idle(workspace=tmp_path, task_id="task-queued")

    assert idle.status == "queued"


def test_background_task_storage_mark_idle_from_completed_is_noop(tmp_path: Path) -> None:
    store = SqliteSessionStore()
    store.create_background_task(workspace=tmp_path, task=_task(task_id="task-done"))
    _ = store.mark_background_task_running(
        workspace=tmp_path,
        task_id="task-done",
        session_id="session-done",
    )
    _ = store.mark_background_task_terminal(
        workspace=tmp_path,
        task_id="task-done",
        status="completed",
    )

    idle = store.mark_background_task_idle(workspace=tmp_path, task_id="task-done")

    assert idle.status == "completed"


def test_background_task_storage_mark_steered_from_idle_writes_prompt(tmp_path: Path) -> None:
    store = SqliteSessionStore()
    store.create_background_task(workspace=tmp_path, task=_task(task_id="task-steer"))
    _ = store.mark_background_task_running(
        workspace=tmp_path,
        task_id="task-steer",
        session_id="session-steer",
    )
    _ = store.mark_background_task_idle(workspace=tmp_path, task_id="task-steer")

    steered = store.mark_background_task_steered(
        workspace=tmp_path,
        task_id="task-steer",
        steer_prompt="continue: check sample.txt",
    )

    assert steered.status == "running"
    assert steered.steer_prompt == "continue: check sample.txt"
    assert steered.session_id == "session-steer"


def test_background_task_storage_mark_idle_clears_prior_steer_prompt(tmp_path: Path) -> None:
    store = SqliteSessionStore()
    store.create_background_task(workspace=tmp_path, task=_task(task_id="task-roundtrip"))
    _ = store.mark_background_task_running(
        workspace=tmp_path,
        task_id="task-roundtrip",
        session_id="session-roundtrip",
    )
    _ = store.mark_background_task_idle(workspace=tmp_path, task_id="task-roundtrip")
    _ = store.mark_background_task_steered(
        workspace=tmp_path,
        task_id="task-roundtrip",
        steer_prompt="first steer",
    )

    second_idle = store.mark_background_task_idle(workspace=tmp_path, task_id="task-roundtrip")

    assert second_idle.status == "idle"
    assert second_idle.steer_prompt is None


def test_background_task_storage_mark_steered_from_interrupted(tmp_path: Path) -> None:
    store = SqliteSessionStore()
    store.create_background_task(workspace=tmp_path, task=_task(task_id="task-resume"))
    _ = store.mark_background_task_running(
        workspace=tmp_path,
        task_id="task-resume",
        session_id="session-resume",
    )
    _ = store.mark_background_task_terminal(
        workspace=tmp_path,
        task_id="task-resume",
        status="interrupted",
    )

    steered = store.mark_background_task_steered(
        workspace=tmp_path,
        task_id="task-resume",
        steer_prompt="resume after restart",
    )

    assert steered.status == "running"
    assert steered.steer_prompt == "resume after restart"


def test_background_task_storage_mark_steered_from_queued_is_noop(tmp_path: Path) -> None:
    store = SqliteSessionStore()
    store.create_background_task(workspace=tmp_path, task=_task(task_id="task-queued-steer"))

    steered = store.mark_background_task_steered(
        workspace=tmp_path,
        task_id="task-queued-steer",
        steer_prompt="should not apply",
    )

    assert steered.status == "queued"
    assert steered.steer_prompt is None


def test_background_task_storage_mark_steered_from_completed_is_noop(tmp_path: Path) -> None:
    store = SqliteSessionStore()
    store.create_background_task(workspace=tmp_path, task=_task(task_id="task-done-steer"))
    _ = store.mark_background_task_running(
        workspace=tmp_path,
        task_id="task-done-steer",
        session_id="session-done-steer",
    )
    _ = store.mark_background_task_terminal(
        workspace=tmp_path,
        task_id="task-done-steer",
        status="completed",
    )

    steered = store.mark_background_task_steered(
        workspace=tmp_path,
        task_id="task-done-steer",
        steer_prompt="should not apply",
    )

    assert steered.status == "completed"
    assert steered.steer_prompt is None


def test_background_task_storage_create_persists_keep_alive_default_false(tmp_path: Path) -> None:
    store = SqliteSessionStore()
    store.create_background_task(workspace=tmp_path, task=_task(task_id="task-default"))

    loaded = store.load_background_task(workspace=tmp_path, task_id="task-default")

    assert loaded.keep_alive is False
    assert loaded.steer_prompt is None
    with closing(sqlite3.connect(sessions_db_path())) as connection:
        row = connection.execute(
            "SELECT keep_alive FROM background_tasks WHERE task_id = ?",
            ("task-default",),
        ).fetchone()
    assert row == (0,)


def test_background_task_storage_create_persists_keep_alive_explicit_true(tmp_path: Path) -> None:
    store = SqliteSessionStore()
    store.create_background_task(workspace=tmp_path, task=_task(task_id="task-ka", keep_alive=True))

    loaded = store.load_background_task(workspace=tmp_path, task_id="task-ka")
    listed = store.list_background_tasks(workspace=tmp_path)

    assert loaded.keep_alive is True
    assert loaded.steer_prompt is None
    assert len(listed) == 1
    assert listed[0].keep_alive is True
    assert listed[0].steer_prompt is None
    with closing(sqlite3.connect(sessions_db_path())) as connection:
        row = connection.execute(
            "SELECT keep_alive FROM background_tasks WHERE task_id = ?",
            ("task-ka",),
        ).fetchone()
    assert row == (1,)


def test_background_task_storage_summary_carries_keep_alive_and_steer_prompt(tmp_path: Path) -> None:
    store = SqliteSessionStore()
    store.create_background_task(workspace=tmp_path, task=_task(task_id="task-summary", keep_alive=True))
    _ = store.mark_background_task_running(
        workspace=tmp_path,
        task_id="task-summary",
        session_id="session-summary",
    )
    _ = store.mark_background_task_idle(workspace=tmp_path, task_id="task-summary")
    _ = store.mark_background_task_steered(
        workspace=tmp_path,
        task_id="task-summary",
        steer_prompt="next instruction",
    )

    listed = store.list_background_tasks(workspace=tmp_path)

    assert len(listed) == 1
    assert listed[0].keep_alive is True
    assert listed[0].steer_prompt == "next instruction"
    assert listed[0].status == "running"


def test_background_task_storage_migrates_v10_database_with_keep_alive_columns(tmp_path: Path) -> None:
    database_path = tmp_path / "v10.sqlite3"
    store = SqliteSessionStore(database_path=database_path)
    store.create_background_task(workspace=tmp_path, task=_task(task_id="task-legacy"))
    store.create_background_task(
        workspace=tmp_path,
        task=_task(task_id="task-keep-alive", keep_alive=True),
    )

    # Rewind the freshly-bootstrapped (v12) database to the previous released
    # schema (v10): drop every post-v10 column and stamp user_version = 10.
    with closing(sqlite3.connect(database_path)) as connection:
        _ = connection.execute("ALTER TABLE background_tasks DROP COLUMN keep_alive")
        _ = connection.execute("ALTER TABLE background_tasks DROP COLUMN steer_prompt")
        _ = connection.execute("ALTER TABLE background_tasks DROP COLUMN output_schema_json")
        _ = connection.execute("ALTER TABLE background_tasks DROP COLUMN schema_mode")
        _ = connection.execute("ALTER TABLE background_tasks DROP COLUMN structured_output_json")
        _ = connection.execute("ALTER TABLE background_tasks DROP COLUMN schema_validation_json")
        _ = connection.execute("PRAGMA user_version = 10")
        connection.commit()

    store = SqliteSessionStore(database_path=database_path)
    legacy = store.load_background_task(workspace=tmp_path, task_id="task-legacy")
    keep_alive = store.load_background_task(workspace=tmp_path, task_id="task-keep-alive")
    listed = store.list_background_tasks(workspace=tmp_path)

    with closing(sqlite3.connect(database_path)) as connection:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(background_tasks)").fetchall()]
        schema_version = connection.execute("PRAGMA user_version").fetchone()[0]
        rows = connection.execute("SELECT task_id, keep_alive, steer_prompt FROM background_tasks ORDER BY task_id ASC").fetchall()

    assert schema_version == 12
    assert "keep_alive" in columns
    assert "steer_prompt" in columns
    assert "output_schema_json" in columns
    assert "schema_mode" in columns
    # Pre-existing v10 rows are preserved and default to keep_alive = 0.
    assert rows == [
        ("task-keep-alive", 0, None),
        ("task-legacy", 0, None),
    ]
    assert legacy.status == "queued"
    assert legacy.keep_alive is False
    assert legacy.steer_prompt is None
    assert keep_alive.status == "queued"
    assert keep_alive.keep_alive is False
    assert keep_alive.steer_prompt is None
    assert len(listed) == 2
    assert all(item.keep_alive is False for item in listed)
