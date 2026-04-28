from __future__ import annotations

import sqlite3
import threading
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from voidcode.runtime.contracts import RuntimeRequest, RuntimeResponse
from voidcode.runtime.events import EventEnvelope
from voidcode.runtime.question import PendingQuestion, PendingQuestionOption, PendingQuestionPrompt
from voidcode.runtime.session import SessionRef, SessionState
from voidcode.runtime.storage import SqliteSessionStore
from voidcode.runtime.task import (
    BackgroundTaskRef,
    BackgroundTaskRequestSnapshot,
    BackgroundTaskState,
)


def _private_attr(instance: object, name: str) -> Any:
    return getattr(instance, name)


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


def test_session_storage_bootstraps_canonical_schema_for_fresh_database(tmp_path: Path) -> None:
    database_path = tmp_path / "fresh-sessions.sqlite3"
    store = SqliteSessionStore(database_path=database_path)
    request = RuntimeRequest(prompt="fresh bootstrap", session_id="fresh-session")
    response = RuntimeResponse(
        session=SessionState(
            session=SessionRef(id="fresh-session"),
            status="completed",
            turn=1,
            metadata={},
        ),
        events=(
            EventEnvelope(
                session_id="fresh-session",
                sequence=1,
                event_type="graph.response_ready",
                source="graph",
            ),
        ),
        output="done",
    )

    store.save_run(workspace=tmp_path, request=request, response=response)

    with closing(sqlite3.connect(database_path)) as connection:
        session_columns = [
            row[1] for row in connection.execute("PRAGMA table_info(sessions)").fetchall()
        ]
        delivery_columns = [
            row[1]
            for row in connection.execute("PRAGMA table_info(session_event_deliveries)").fetchall()
        ]
        notification_indexes = connection.execute(
            "PRAGMA index_list(session_notifications)"
        ).fetchall()

    assert session_columns == [
        "session_id",
        "parent_session_id",
        "workspace",
        "status",
        "turn",
        "prompt",
        "output",
        "metadata_json",
        "pending_approval_json",
        "pending_question_json",
        "resume_checkpoint_json",
        "created_at",
        "updated_at",
        "last_event_sequence",
    ]
    assert delivery_columns == ["workspace", "session_id", "dedupe_key", "delivered_at"]
    assert any(row[2] == 1 and row[3] == "u" for row in notification_indexes)


def test_session_storage_rejects_non_canonical_schema_missing_runtime_columns(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "invalid-sessions.sqlite3"
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
            CREATE TABLE session_notifications (
                notification_id TEXT PRIMARY KEY,
                workspace TEXT NOT NULL,
                session_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                summary TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                event_sequence INTEGER NOT NULL,
                dedupe_key TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                acknowledged_at INTEGER,
                UNIQUE(workspace, dedupe_key)
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
        connection.commit()

    store = SqliteSessionStore(database_path=database_path)
    with pytest.raises(
        RuntimeError,
        match=(
            r"table 'sessions' missing columns: .*"
            r"Remove '.*[\\/]invalid-sessions\.sqlite3' and rerun to reset local runtime storage\."
        ),
    ):
        store.list_sessions(workspace=tmp_path)

    with closing(sqlite3.connect(database_path)) as connection:
        session_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(sessions)").fetchall()
        }

    assert "parent_session_id" not in session_columns
    assert "pending_approval_json" not in session_columns


def test_session_storage_rejects_non_canonical_schema_with_wrong_existing_table_shape(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "wrong-table-shape.sqlite3"
    with closing(sqlite3.connect(database_path)) as connection:
        _ = connection.execute(
            """
            CREATE TABLE sessions (
                session_id TEXT PRIMARY KEY,
                parent_session_id TEXT,
                workspace TEXT NOT NULL,
                status TEXT NOT NULL,
                turn INTEGER NOT NULL,
                prompt TEXT NOT NULL,
                output TEXT,
                metadata_json TEXT NOT NULL,
                pending_approval_json TEXT,
                pending_question_json TEXT,
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
                request_parent_session_id TEXT,
                request_metadata_json TEXT NOT NULL,
                requested_child_session_id TEXT,
                routing_mode TEXT,
                routing_category TEXT,
                routing_subagent_type TEXT,
                routing_description TEXT,
                routing_command TEXT,
                approval_request_id TEXT,
                question_request_id TEXT,
                cancellation_cause TEXT,
                result_available INTEGER NOT NULL DEFAULT 0,
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
            CREATE TABLE session_notifications (
                notification_id TEXT PRIMARY KEY,
                workspace TEXT NOT NULL,
                session_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                summary TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                event_sequence INTEGER NOT NULL,
                dedupe_key TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                acknowledged_at INTEGER,
                UNIQUE(workspace, dedupe_key)
            )
            """
        )
        _ = connection.execute(
            """
            CREATE TABLE session_event_deliveries (
                workspace TEXT NOT NULL,
                session_id TEXT NOT NULL,
                delivered_at INTEGER NOT NULL,
                PRIMARY KEY (workspace, session_id)
            )
            """
        )
        connection.commit()

    store = SqliteSessionStore(database_path=database_path)

    with pytest.raises(
        RuntimeError,
        match=(
            r"table 'session_event_deliveries' missing columns: dedupe_key.*"
            r"Remove '.*[\\/]wrong-table-shape\.sqlite3' and rerun to reset local runtime storage\."
        ),
    ):
        store.list_notifications(workspace=tmp_path)

    with closing(sqlite3.connect(database_path)) as connection:
        delivery_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(session_event_deliveries)").fetchall()
        }

    assert "dedupe_key" not in delivery_columns


def test_tool_results_from_events_keeps_success_payloads_with_null_error() -> None:
    tool_results_from_events: Any = _private_attr(SqliteSessionStore, "_tool_results_from_events")
    tool_results = tool_results_from_events(
        (
            EventEnvelope(
                session_id="s1",
                sequence=1,
                event_type="runtime.tool_completed",
                source="runtime",
                payload={
                    "tool": "read_file",
                    "status": "ok",
                    "content": "alpha\n",
                    "error": None,
                    "path": "sample.txt",
                },
            ),
        )
    )

    assert tool_results == [
        {
            "tool_name": "read_file",
            "content": "alpha\n",
            "status": "ok",
            "data": {
                "tool": "read_file",
                "status": "ok",
                "content": "alpha\n",
                "error": None,
                "path": "sample.txt",
            },
            "error": None,
        }
    ]


def test_tool_results_from_events_preserves_successful_null_content() -> None:
    tool_results_from_events: Any = _private_attr(SqliteSessionStore, "_tool_results_from_events")
    tool_results = tool_results_from_events(
        (
            EventEnvelope(
                session_id="s1",
                sequence=1,
                event_type="runtime.tool_completed",
                source="runtime",
                payload={
                    "tool": "write_file",
                    "status": "ok",
                    "content": None,
                    "error": None,
                    "path": "beta.txt",
                },
            ),
        )
    )

    assert tool_results == [
        {
            "tool_name": "write_file",
            "content": None,
            "status": "ok",
            "data": {
                "tool": "write_file",
                "status": "ok",
                "content": None,
                "error": None,
                "path": "beta.txt",
            },
            "error": None,
        }
    ]


def test_session_storage_load_resume_checkpoint_rejects_corrupt_json(tmp_path: Path) -> None:
    store = SqliteSessionStore()
    request = RuntimeRequest(prompt="go", session_id="checkpoint-corrupt-json")
    response = RuntimeResponse(
        session=SessionState(
            session=SessionRef(id="checkpoint-corrupt-json"),
            status="waiting",
            turn=1,
            metadata={},
        ),
        events=(),
    )
    store.save_run(workspace=tmp_path, request=request, response=response)

    database_path = tmp_path / ".voidcode" / "sessions.sqlite3"
    with closing(sqlite3.connect(database_path)) as connection:
        _ = connection.execute(
            "UPDATE sessions SET resume_checkpoint_json = ? WHERE session_id = ?",
            ("{broken json", "checkpoint-corrupt-json"),
        )
        connection.commit()

    with pytest.raises(ValueError, match="persisted resume checkpoint JSON is malformed"):
        _ = store.load_resume_checkpoint(workspace=tmp_path, session_id="checkpoint-corrupt-json")


def test_session_storage_load_resume_checkpoint_rejects_invalid_kind(tmp_path: Path) -> None:
    store = SqliteSessionStore()
    request = RuntimeRequest(prompt="go", session_id="checkpoint-invalid-kind")
    response = RuntimeResponse(
        session=SessionState(
            session=SessionRef(id="checkpoint-invalid-kind"),
            status="waiting",
            turn=1,
            metadata={},
        ),
        events=(),
    )
    store.save_run(workspace=tmp_path, request=request, response=response)

    database_path = tmp_path / ".voidcode" / "sessions.sqlite3"
    with closing(sqlite3.connect(database_path)) as connection:
        _ = connection.execute(
            "UPDATE sessions SET resume_checkpoint_json = ? WHERE session_id = ?",
            (
                '{"kind":"not-real","version":1}',
                "checkpoint-invalid-kind",
            ),
        )
        connection.commit()

    with pytest.raises(
        ValueError, match=r"persisted resume checkpoint kind is invalid: 'not-real'"
    ):
        _ = store.load_resume_checkpoint(workspace=tmp_path, session_id="checkpoint-invalid-kind")


def test_session_storage_append_session_event_assigns_sequence_and_dedupes(
    tmp_path: Path,
) -> None:
    store = SqliteSessionStore()
    request = RuntimeRequest(prompt="leader task", session_id="leader-session")
    response = RuntimeResponse(
        session=SessionState(
            session=SessionRef(id="leader-session"),
            status="completed",
            turn=1,
            metadata={},
        ),
        events=(
            EventEnvelope(
                session_id="leader-session",
                sequence=1,
                event_type="graph.response_ready",
                source="graph",
            ),
        ),
        output="done",
    )
    store.save_run(workspace=tmp_path, request=request, response=response)

    first_event = store.append_session_event(
        workspace=tmp_path,
        session_id="leader-session",
        event_type="runtime.background_task_waiting_approval",
        source="runtime",
        payload={
            "task_id": "task-123",
            "parent_session_id": "leader-session",
            "child_session_id": "child-session",
            "status": "running",
            "approval_blocked": True,
        },
        dedupe_key="background_task_waiting_approval:task-123:req-1",
    )
    duplicate_event = store.append_session_event(
        workspace=tmp_path,
        session_id="leader-session",
        event_type="runtime.background_task_waiting_approval",
        source="runtime",
        payload={
            "task_id": "task-123",
            "parent_session_id": "leader-session",
            "child_session_id": "child-session",
            "status": "running",
            "approval_blocked": True,
        },
        dedupe_key="background_task_waiting_approval:task-123:req-1",
    )
    loaded = store.load_session(workspace=tmp_path, session_id="leader-session")

    assert first_event is not None
    assert first_event.sequence == 2
    assert duplicate_event is None
    assert loaded.events[-1] == first_event


def test_session_storage_append_session_event_allocates_sequences_atomically(
    tmp_path: Path,
) -> None:
    store = SqliteSessionStore()
    request = RuntimeRequest(prompt="leader task", session_id="leader-session")
    response = RuntimeResponse(
        session=SessionState(
            session=SessionRef(id="leader-session"),
            status="completed",
            turn=1,
            metadata={},
        ),
        events=(
            EventEnvelope(
                session_id="leader-session",
                sequence=1,
                event_type="graph.response_ready",
                source="graph",
            ),
        ),
        output="done",
    )
    store.save_run(workspace=tmp_path, request=request, response=response)

    events: list[EventEnvelope] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def _append_event(label: str) -> None:
        try:
            barrier.wait(timeout=5)
            event = store.append_session_event(
                workspace=tmp_path,
                session_id="leader-session",
                event_type="runtime.background_task_waiting_approval",
                source="runtime",
                payload={
                    "task_id": f"task-{label}",
                    "parent_session_id": "leader-session",
                    "child_session_id": f"child-{label}",
                    "status": "running",
                    "approval_blocked": True,
                },
                dedupe_key=f"background_task_waiting_approval:task-{label}:req-{label}",
            )
            assert event is not None
            events.append(event)
        except BaseException as exc:  # pragma: no cover - test captures unexpected failures
            errors.append(exc)

    first_thread = threading.Thread(target=_append_event, args=("a",))
    second_thread = threading.Thread(target=_append_event, args=("b",))
    first_thread.start()
    second_thread.start()
    first_thread.join(timeout=5)
    second_thread.join(timeout=5)

    loaded = store.load_session(workspace=tmp_path, session_id="leader-session")

    assert errors == []
    assert len(events) == 2
    assert {event.sequence for event in events} == {2, 3}
    assert [event.sequence for event in loaded.events[-2:]] == [2, 3]


def test_session_storage_persists_pending_question_and_question_notification(
    tmp_path: Path,
) -> None:
    store = SqliteSessionStore()
    request = RuntimeRequest(prompt="need input", session_id="question-session")
    response = RuntimeResponse(
        session=SessionState(
            session=SessionRef(id="question-session"),
            status="waiting",
            turn=1,
            metadata={},
        ),
        events=(
            EventEnvelope(
                session_id="question-session",
                sequence=1,
                event_type="runtime.question_requested",
                source="runtime",
                payload={
                    "request_id": "question-1",
                    "tool": "question",
                    "question_count": 1,
                    "questions": [
                        {
                            "header": "Runtime path",
                            "question": "Which runtime path should we use?",
                            "multiple": False,
                            "options": [
                                {"label": "Reuse existing", "description": "Keep current path"},
                                {"label": "Add new path", "description": "Create a new route"},
                            ],
                        }
                    ],
                },
            ),
        ),
    )
    pending_question = PendingQuestion(
        request_id="question-1",
        tool_name="question",
        arguments={},
        prompts=(
            PendingQuestionPrompt(
                question="Which runtime path should we use?",
                header="Runtime path",
                options=(
                    PendingQuestionOption(label="Reuse existing", description="Keep current path"),
                    PendingQuestionOption(label="Add new path", description="Create a new route"),
                ),
                multiple=False,
            ),
        ),
    )

    store.save_pending_question(
        workspace=tmp_path,
        request=request,
        response=response,
        pending_question=pending_question,
    )

    loaded_question = store.load_pending_question(workspace=tmp_path, session_id="question-session")
    notifications = store.list_notifications(workspace=tmp_path)

    assert loaded_question == pending_question
    assert len(notifications) == 1
    assert notifications[0].kind == "question_blocked"
    assert notifications[0].status == "unread"
    assert notifications[0].payload["request_id"] == "question-1"


def test_session_storage_fail_incomplete_background_tasks_keeps_question_waiting_children(
    tmp_path: Path,
) -> None:
    store = SqliteSessionStore()
    child_request = RuntimeRequest(prompt="need input", session_id="child-question-session")
    child_response = RuntimeResponse(
        session=SessionState(
            session=SessionRef(id="child-question-session", parent_id="leader-session"),
            status="waiting",
            turn=1,
            metadata={"background_run": True, "background_task_id": "task-question"},
        ),
        events=(
            EventEnvelope(
                session_id="child-question-session",
                sequence=1,
                event_type="runtime.question_requested",
                source="runtime",
                payload={
                    "request_id": "question-1",
                    "tool": "question",
                    "question_count": 1,
                    "questions": [
                        {
                            "header": "Runtime path",
                            "question": "Which runtime path should we use?",
                            "multiple": False,
                            "options": [{"label": "Reuse existing", "description": ""}],
                        }
                    ],
                },
            ),
        ),
    )
    store.save_pending_question(
        workspace=tmp_path,
        request=child_request,
        response=child_response,
        pending_question=PendingQuestion(
            request_id="question-1",
            tool_name="question",
            arguments={},
            prompts=(
                PendingQuestionPrompt(
                    question="Which runtime path should we use?",
                    header="Runtime path",
                    options=(PendingQuestionOption(label="Reuse existing"),),
                    multiple=False,
                ),
            ),
        ),
    )
    store.create_background_task(
        workspace=tmp_path,
        task=BackgroundTaskState(
            task=BackgroundTaskRef(id="task-question"),
            status="running",
            request=BackgroundTaskRequestSnapshot(
                prompt="need input",
                parent_session_id="leader-session",
            ),
            session_id="child-question-session",
            created_at=1,
            updated_at=1,
            started_at=1,
        ),
    )

    failed = store.fail_incomplete_background_tasks(
        workspace=tmp_path,
        message="background task interrupted before completion",
    )
    loaded = store.load_background_task(workspace=tmp_path, task_id="task-question")

    assert failed == ()
    assert loaded.status == "running"
