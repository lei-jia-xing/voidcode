from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from voidcode.runtime.contracts import RuntimeRequest, RuntimeResponse
from voidcode.runtime.events import EventEnvelope
from voidcode.runtime.session import SessionRef, SessionState
from voidcode.runtime.storage import SqliteSessionStore
from voidcode.runtime.task import (
    BackgroundTaskRef,
    BackgroundTaskRequestSnapshot,
    BackgroundTaskState,
)


def test_session_storage_persists_parent_lineage_across_read_surfaces(tmp_path: Path) -> None:
    store = SqliteSessionStore()
    request = RuntimeRequest(
        prompt="child task",
        session_id="child-session",
        parent_session_id="leader-session",
    )
    response = RuntimeResponse(
        session=SessionState(
            session=SessionRef(id="child-session", parent_id="leader-session"),
            status="completed",
            turn=1,
            metadata={},
        ),
        events=(
            EventEnvelope(
                session_id="child-session",
                sequence=1,
                event_type="graph.response_ready",
                source="graph",
            ),
        ),
        output="done",
    )

    store.save_run(workspace=tmp_path, request=request, response=response)

    loaded = store.load_session(workspace=tmp_path, session_id="child-session")
    listed = store.list_sessions(workspace=tmp_path)
    result = store.load_session_result(workspace=tmp_path, session_id="child-session")
    notifications = store.list_notifications(workspace=tmp_path)

    assert loaded.session.session.parent_id == "leader-session"
    assert listed[0].session.parent_id == "leader-session"
    assert result.session.session.parent_id == "leader-session"
    assert notifications[0].session.parent_id == "leader-session"


def test_session_storage_migrates_legacy_schema_for_parent_lineage(tmp_path: Path) -> None:
    database_path = tmp_path / "legacy-sessions.sqlite3"
    with closing(sqlite3.connect(database_path)) as connection:
        _ = connection.execute(
            """
            CREATE TABLE sessions (
                session_id TEXT PRIMARY KEY,
                workspace TEXT NOT NULL,
                status TEXT NOT NULL,
                turn INTEGER NOT NULL,
                prompt TEXT NOT NULL,
                output TEXT,
                metadata_json TEXT NOT NULL,
                pending_approval_json TEXT,
                resume_checkpoint_json TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                last_event_sequence INTEGER NOT NULL
            )
            """
        )
        _ = connection.execute(
            """
            CREATE TABLE session_events (
                session_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                source TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY (session_id, sequence)
            )
            """
        )
        _ = connection.execute(
            """
            CREATE TABLE background_tasks (
                task_id TEXT PRIMARY KEY,
                workspace TEXT NOT NULL,
                status TEXT NOT NULL,
                prompt TEXT NOT NULL,
                request_session_id TEXT,
                request_metadata_json TEXT NOT NULL,
                allocate_session_id INTEGER NOT NULL,
                session_id TEXT,
                error TEXT,
                cancel_requested_at INTEGER,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                started_at INTEGER,
                finished_at INTEGER
            )
            """
        )
        _ = connection.execute(
            """
            INSERT INTO sessions (
                session_id, workspace, status, turn, prompt, output, metadata_json,
                pending_approval_json, resume_checkpoint_json, created_at, updated_at,
                last_event_sequence
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-session",
                str(tmp_path),
                "completed",
                1,
                "legacy",
                "done",
                "{}",
                None,
                None,
                1,
                1,
                0,
            ),
        )
        _ = connection.execute("PRAGMA user_version = 4")
        connection.commit()

    store = SqliteSessionStore(database_path=database_path)
    loaded = store.load_session(workspace=tmp_path, session_id="legacy-session")
    task = BackgroundTaskState(
        task=BackgroundTaskRef(id="task-parent-lineage"),
        request=BackgroundTaskRequestSnapshot(
            prompt="child background task",
            parent_session_id="leader-session",
        ),
        created_at=1,
        updated_at=1,
    )

    store.create_background_task(workspace=tmp_path, task=task)
    loaded_task = store.load_background_task(
        workspace=tmp_path,
        task_id="task-parent-lineage",
    )

    with closing(sqlite3.connect(database_path)) as connection:
        session_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(sessions)").fetchall()
        }
        background_task_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(background_tasks)").fetchall()
        }

    assert loaded.session.session.parent_id is None
    assert "parent_session_id" in session_columns
    assert "request_parent_session_id" in background_task_columns
    assert loaded_task.request.parent_session_id == "leader-session"
