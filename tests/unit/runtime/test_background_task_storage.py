from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import cast

import pytest

from voidcode.runtime.contracts import RuntimeRequest, RuntimeResponse
from voidcode.runtime.events import (
    DELEGATED_BACKGROUND_TASK_CORRELATION_FIELDS,
    DELEGATED_BACKGROUND_TASK_DURABILITY_FIELDS,
    DELEGATED_BACKGROUND_TASK_EVENT_TYPES,
    DELEGATED_BACKGROUND_TASK_ROUTING_FIELDS,
    EventEnvelope,
)
from voidcode.runtime.permission import PendingApproval
from voidcode.runtime.question import PendingQuestion, PendingQuestionOption, PendingQuestionPrompt
from voidcode.runtime.session import SessionRef, SessionState
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


def test_background_task_storage_preserves_stable_request_metadata_round_trip(
    tmp_path: Path,
) -> None:
    store = SqliteSessionStore()
    task = BackgroundTaskState(
        task=BackgroundTaskRef(id="task-metadata-roundtrip"),
        request=BackgroundTaskRequestSnapshot(
            prompt="read sample.txt",
            metadata={
                "abort_requested": False,
                "agent": {"preset": "leader", "model": "opencode/gpt-5.4"},
                "max_steps": 2,
                "provider_stream": True,
                "skills": ["alpha", "beta"],
            },
        ),
    )

    store.create_background_task(workspace=tmp_path, task=task)

    loaded = store.load_background_task(workspace=tmp_path, task_id="task-metadata-roundtrip")

    assert loaded.request.metadata == {
        "abort_requested": False,
        "agent": {"preset": "leader", "model": "opencode/gpt-5.4"},
        "max_steps": 2,
        "provider_stream": True,
        "skills": ["alpha", "beta"],
    }


def test_background_task_storage_persists_delegated_correlation_and_routing_columns(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "sessions.sqlite3"
    store = SqliteSessionStore(database_path=database_path)
    task = BackgroundTaskState(
        task=BackgroundTaskRef(id="task-routing"),
        request=BackgroundTaskRequestSnapshot(
            prompt="delegate work",
            session_id="child-requested",
            parent_session_id="leader-session",
            metadata={
                "delegation": {
                    "mode": "background",
                    "category": "quick",
                    "description": "Review quickly",
                    "command": "pytest tests/unit",
                }
            },
            allocate_session_id=True,
        ),
    )

    store.create_background_task(workspace=tmp_path, task=task)

    with closing(sqlite3.connect(database_path)) as connection:
        row = connection.execute(
            """
            SELECT requested_child_session_id, routing_mode, routing_category,
                   routing_subagent_type, routing_description, routing_command,
                   approval_request_id, question_request_id, cancellation_cause,
                   result_available
            FROM background_tasks
            WHERE task_id = ?
            """,
            ("task-routing",),
        ).fetchone()

    assert row == (
        "child-requested",
        "background",
        "quick",
        None,
        "Review quickly",
        "pytest tests/unit",
        None,
        None,
        None,
        0,
    )


def test_background_task_storage_create_assigns_store_timestamps_and_orders_by_latest_update(
    tmp_path: Path,
) -> None:
    store = SqliteSessionStore()
    first = BackgroundTaskState(
        task=BackgroundTaskRef(id="task-ts-1"),
        request=BackgroundTaskRequestSnapshot(prompt="first"),
        created_at=99,
        updated_at=42,
    )
    second = BackgroundTaskState(
        task=BackgroundTaskRef(id="task-ts-2"),
        request=BackgroundTaskRequestSnapshot(prompt="second"),
        created_at=500,
        updated_at=400,
    )

    store.create_background_task(workspace=tmp_path, task=first)
    store.create_background_task(workspace=tmp_path, task=second)

    loaded_first = store.load_background_task(workspace=tmp_path, task_id="task-ts-1")
    loaded_second = store.load_background_task(workspace=tmp_path, task_id="task-ts-2")
    listed = store.list_background_tasks(workspace=tmp_path)

    assert loaded_first.created_at == 1
    assert loaded_first.updated_at == 1
    assert loaded_second.created_at == 2
    assert loaded_second.updated_at == 2
    assert loaded_first.created_at != first.created_at
    assert loaded_second.updated_at != second.updated_at
    assert [task.task.id for task in listed] == ["task-ts-2", "task-ts-1"]


def test_background_task_storage_prunes_only_terminal_tasks(tmp_path: Path) -> None:
    store = SqliteSessionStore()
    for task_id in ("task-old", "task-new", "task-running"):
        store.create_background_task(workspace=tmp_path, task=_task(task_id=task_id))
    _ = store.mark_background_task_terminal(
        workspace=tmp_path,
        task_id="task-old",
        status="completed",
    )
    _ = store.mark_background_task_terminal(
        workspace=tmp_path,
        task_id="task-new",
        status="failed",
        error="boom",
    )
    _ = store.mark_background_task_running(
        workspace=tmp_path,
        task_id="task-running",
        session_id="session-running",
    )

    counts = store.prune_runtime_storage(workspace=tmp_path, keep_background_tasks=1)

    assert counts["background_tasks"] == 1
    assert [task.task.id for task in store.list_background_tasks(workspace=tmp_path)] == [
        "task-running",
        "task-new",
    ]
    with pytest.raises(ValueError, match="unknown background task: task-old"):
        _ = store.load_background_task(workspace=tmp_path, task_id="task-old")


def test_background_task_storage_prune_retains_sessions_referenced_by_kept_tasks(
    tmp_path: Path,
) -> None:
    store = SqliteSessionStore()
    old_request = RuntimeRequest(prompt="old child", session_id="child-old")
    old_response = RuntimeResponse(
        session=SessionState(session=SessionRef(id="child-old"), status="completed", turn=1),
        events=(
            EventEnvelope(
                session_id="child-old",
                sequence=1,
                event_type="runtime.request_received",
                source="runtime",
                payload={"prompt": "old child"},
            ),
        ),
        output="old child output",
    )
    new_request = RuntimeRequest(prompt="new child", session_id="child-new")
    new_response = RuntimeResponse(
        session=SessionState(session=SessionRef(id="child-new"), status="completed", turn=1),
        events=(
            EventEnvelope(
                session_id="child-new",
                sequence=1,
                event_type="runtime.request_received",
                source="runtime",
                payload={"prompt": "new child"},
            ),
        ),
        output="new child output",
    )
    store.save_run(workspace=tmp_path, request=old_request, response=old_response)
    store.save_run(workspace=tmp_path, request=new_request, response=new_response)
    store.create_background_task(
        workspace=tmp_path,
        task=BackgroundTaskState(
            task=BackgroundTaskRef(id="task-old"),
            status="completed",
            request=BackgroundTaskRequestSnapshot(prompt="old child"),
            session_id="child-old",
            created_at=1,
            updated_at=1,
            finished_at=1,
        ),
    )
    store.create_background_task(
        workspace=tmp_path,
        task=BackgroundTaskState(
            task=BackgroundTaskRef(id="task-new"),
            status="completed",
            request=BackgroundTaskRequestSnapshot(prompt="new child"),
            session_id="child-new",
            created_at=2,
            updated_at=2,
            finished_at=2,
        ),
    )

    counts = store.prune_runtime_storage(
        workspace=tmp_path,
        keep_sessions=1,
        keep_background_tasks=2,
    )

    assert counts["background_tasks"] == 0
    assert counts["sessions"] == 0
    assert store.load_session_result(workspace=tmp_path, session_id="child-old").output == (
        "old child output"
    )
    assert store.load_session_result(workspace=tmp_path, session_id="child-new").output == (
        "new child output"
    )


def test_background_task_storage_lists_by_parent_session_and_preserves_order(
    tmp_path: Path,
) -> None:
    store = SqliteSessionStore()
    first = BackgroundTaskState(
        task=BackgroundTaskRef(id="task-parent-a-1"),
        request=BackgroundTaskRequestSnapshot(prompt="first", parent_session_id="leader-a"),
    )
    second = BackgroundTaskState(
        task=BackgroundTaskRef(id="task-parent-b-1"),
        request=BackgroundTaskRequestSnapshot(prompt="second", parent_session_id="leader-b"),
    )
    third = BackgroundTaskState(
        task=BackgroundTaskRef(id="task-parent-a-2"),
        request=BackgroundTaskRequestSnapshot(prompt="third", parent_session_id="leader-a"),
    )
    fourth = BackgroundTaskState(
        task=BackgroundTaskRef(id="task-unparented"),
        request=BackgroundTaskRequestSnapshot(prompt="fourth"),
    )

    store.create_background_task(workspace=tmp_path, task=first)
    store.create_background_task(workspace=tmp_path, task=second)
    store.create_background_task(workspace=tmp_path, task=third)
    store.create_background_task(workspace=tmp_path, task=fourth)

    listed = store.list_background_tasks_by_parent_session(
        workspace=tmp_path,
        parent_session_id="leader-a",
    )

    assert [task.task.id for task in listed] == ["task-parent-a-2", "task-parent-a-1"]
    assert all(task.prompt in {"first", "third"} for task in listed)


def test_background_task_storage_lists_by_parent_session_returns_empty_when_no_match(
    tmp_path: Path,
) -> None:
    store = SqliteSessionStore()
    store.create_background_task(
        workspace=tmp_path,
        task=BackgroundTaskState(
            task=BackgroundTaskRef(id="task-other-parent"),
            request=BackgroundTaskRequestSnapshot(
                prompt="child background task",
                parent_session_id="leader-other",
            ),
        ),
    )

    listed = store.list_background_tasks_by_parent_session(
        workspace=tmp_path,
        parent_session_id="leader-missing",
    )

    assert listed == ()


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


def test_background_task_storage_terminal_states_persist_result_availability_and_cancellation_cause(
    tmp_path: Path,
) -> None:
    store = SqliteSessionStore()
    store.create_background_task(workspace=tmp_path, task=_task(task_id="task-terminal"))
    _ = store.mark_background_task_running(
        workspace=tmp_path,
        task_id="task-terminal",
        session_id="session-terminal",
    )

    completed = store.mark_background_task_terminal(
        workspace=tmp_path,
        task_id="task-terminal",
        status="completed",
    )
    store.create_background_task(workspace=tmp_path, task=_task(task_id="task-cancelled-terminal"))
    cancelled = store.mark_background_task_terminal(
        workspace=tmp_path,
        task_id="task-cancelled-terminal",
        status="cancelled",
        error="operator cancelled",
    )

    with closing(sqlite3.connect(tmp_path / ".voidcode" / "sessions.sqlite3")) as connection:
        rows = connection.execute(
            """
            SELECT task_id, result_available, cancellation_cause
            FROM background_tasks
            WHERE task_id IN ('task-terminal', 'task-cancelled-terminal')
            ORDER BY task_id ASC
            """
        ).fetchall()

    assert completed.status == "completed"
    assert cancelled.status == "cancelled"
    assert rows == [
        ("task-cancelled-terminal", 0, "operator cancelled"),
        ("task-terminal", 1, None),
    ]


def test_background_task_storage_cancel_semantics(tmp_path: Path) -> None:
    store = SqliteSessionStore()
    store.create_background_task(workspace=tmp_path, task=_task(task_id="task-c"))

    cancelled = store.request_background_task_cancel(workspace=tmp_path, task_id="task-c")

    assert cancelled.status == "cancelled"
    assert cancelled.error == "cancelled before start"

    with closing(sqlite3.connect(tmp_path / ".voidcode" / "sessions.sqlite3")) as connection:
        row = connection.execute(
            "SELECT cancellation_cause, result_available FROM background_tasks WHERE task_id = ?",
            ("task-c",),
        ).fetchone()

    assert row == ("cancelled before start", 0)


def test_background_task_storage_persists_approval_and_question_request_correlation_on_session_save(
    tmp_path: Path,
) -> None:
    store = SqliteSessionStore()
    store.create_background_task(
        workspace=tmp_path,
        task=BackgroundTaskState(
            task=BackgroundTaskRef(id="task-correlation"),
            status="running",
            request=BackgroundTaskRequestSnapshot(
                prompt="background child",
                session_id="child-requested",
                parent_session_id="leader-session",
                metadata={
                    "delegation": {
                        "mode": "background",
                        "subagent_type": "explore",
                    }
                },
            ),
            session_id="child-session",
            started_at=1,
        ),
    )

    approval_request = RuntimeRequest(
        prompt="background child",
        session_id="child-requested",
        parent_session_id="leader-session",
        metadata={
            "background_run": True,
            "background_task_id": "task-correlation",
            "delegation": {
                "mode": "background",
                "subagent_type": "explore",
            },
        },
    )
    approval_response = RuntimeResponse(
        session=SessionState(
            session=SessionRef(id="child-session", parent_id="leader-session"),
            status="waiting",
            turn=1,
            metadata={
                "background_run": True,
                "background_task_id": "task-correlation",
            },
        ),
        events=(
            EventEnvelope(
                session_id="child-session",
                sequence=1,
                event_type="runtime.request_received",
                source="runtime",
                payload={"prompt": "background child"},
            ),
            EventEnvelope(
                session_id="child-session",
                sequence=2,
                event_type="runtime.approval_requested",
                source="runtime",
                payload={"request_id": "approval-1", "tool": "write_file"},
            ),
        ),
    )
    store.save_pending_approval(
        workspace=tmp_path,
        request=approval_request,
        response=approval_response,
        pending_approval=PendingApproval(request_id="approval-1", tool_name="write_file"),
    )

    question_response = RuntimeResponse(
        session=SessionState(
            session=SessionRef(id="child-session", parent_id="leader-session"),
            status="waiting",
            turn=2,
            metadata={
                "background_run": True,
                "background_task_id": "task-correlation",
            },
        ),
        events=(
            EventEnvelope(
                session_id="child-session",
                sequence=1,
                event_type="runtime.request_received",
                source="runtime",
                payload={"prompt": "background child"},
            ),
            EventEnvelope(
                session_id="child-session",
                sequence=2,
                event_type="runtime.question_requested",
                source="runtime",
                payload={"request_id": "question-1", "question_count": 1},
            ),
        ),
    )
    store.save_pending_question(
        workspace=tmp_path,
        request=approval_request,
        response=question_response,
        pending_question=PendingQuestion(
            request_id="question-1",
            tool_name="question",
            arguments={},
            prompts=(
                PendingQuestionPrompt(
                    question="Proceed?",
                    header="Proceed",
                    options=(PendingQuestionOption(label="yes"),),
                    multiple=False,
                ),
            ),
        ),
    )
    terminal_response = RuntimeResponse(
        session=SessionState(
            session=SessionRef(id="child-session", parent_id="leader-session"),
            status="completed",
            turn=3,
            metadata={
                "background_run": True,
                "background_task_id": "task-correlation",
            },
        ),
        events=(
            EventEnvelope(
                session_id="child-session",
                sequence=1,
                event_type="runtime.request_received",
                source="runtime",
                payload={"prompt": "background child"},
            ),
            EventEnvelope(
                session_id="child-session",
                sequence=2,
                event_type="graph.response_ready",
                source="graph",
                payload={},
            ),
        ),
        output="done",
    )
    store.save_run(workspace=tmp_path, request=approval_request, response=terminal_response)

    with closing(sqlite3.connect(tmp_path / ".voidcode" / "sessions.sqlite3")) as connection:
        row = connection.execute(
            """
            SELECT requested_child_session_id, session_id, approval_request_id,
                   question_request_id, routing_mode, routing_subagent_type,
                   result_available
            FROM background_tasks
            WHERE task_id = ?
            """,
            ("task-correlation",),
        ).fetchone()

    assert row == (
        "child-requested",
        "child-session",
        "approval-1",
        "question-1",
        "background",
        "explore",
        1,
    )


def test_background_task_storage_round_trips_pending_approval_owner_fields(tmp_path: Path) -> None:
    store = SqliteSessionStore()
    request = RuntimeRequest(prompt="child", session_id="child-session")
    response = RuntimeResponse(
        session=SessionState(
            session=SessionRef(id="child-session", parent_id="leader-session"),
            status="waiting",
            turn=1,
            metadata={"background_task_id": "task-owner", "background_run": True},
        ),
        events=(),
    )
    pending = PendingApproval(
        request_id="approval-owner",
        tool_name="write_file",
        owner_session_id="child-session",
        owner_parent_session_id="leader-session",
        delegated_task_id="task-owner",
    )

    store.save_pending_approval(
        workspace=tmp_path,
        request=request,
        response=response,
        pending_approval=pending,
    )

    loaded = store.load_pending_approval(workspace=tmp_path, session_id="child-session")

    assert loaded == pending


def test_background_task_storage_enriches_parent_visible_delegated_event_payload_from_durable_truth(
    tmp_path: Path,
) -> None:
    store = SqliteSessionStore()
    parent_request = RuntimeRequest(prompt="leader task", session_id="leader-session")
    parent_response = RuntimeResponse(
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
    store.save_run(workspace=tmp_path, request=parent_request, response=parent_response)
    store.create_background_task(
        workspace=tmp_path,
        task=BackgroundTaskState(
            task=BackgroundTaskRef(id="task-event"),
            status="running",
            request=BackgroundTaskRequestSnapshot(
                prompt="delegated",
                session_id="child-requested",
                parent_session_id="leader-session",
                metadata={
                    "delegation": {
                        "mode": "background",
                        "category": "quick",
                        "description": "Quick delegated run",
                    }
                },
            ),
            session_id="child-session",
            started_at=1,
        ),
    )
    _ = store.persist_background_task_runtime_state(
        workspace=tmp_path,
        task_id="task-event",
        approval_request_id="approval-42",
        result_available=True,
    )

    appended = store.append_session_event(
        workspace=tmp_path,
        session_id="leader-session",
        event_type="runtime.background_task_waiting_approval",
        source="runtime",
        payload={"task_id": "task-event", "approval_blocked": True},
        dedupe_key="background_task_waiting_approval:task-event:approval-42",
    )

    assert appended is not None
    assert appended.payload == {
        "task_id": "task-event",
        "approval_blocked": True,
        "parent_session_id": "leader-session",
        "status": "running",
        "result_available": True,
        "requested_child_session_id": "child-requested",
        "child_session_id": "child-session",
        "approval_request_id": "approval-42",
        "routing_mode": "background",
        "routing_category": "quick",
        "routing_description": "Quick delegated run",
    }


def test_background_task_storage_preserves_supervisor_completed_event_payload_field_names(
    tmp_path: Path,
) -> None:
    store = SqliteSessionStore()
    parent_request = RuntimeRequest(prompt="leader task", session_id="leader-session")
    parent_response = RuntimeResponse(
        session=SessionState(
            session=SessionRef(id="leader-session"),
            status="completed",
            turn=1,
            metadata={},
        ),
        events=(),
        output="done",
    )
    store.save_run(workspace=tmp_path, request=parent_request, response=parent_response)
    store.create_background_task(
        workspace=tmp_path,
        task=BackgroundTaskState(
            task=BackgroundTaskRef(id="task-completed-event"),
            status="running",
            request=BackgroundTaskRequestSnapshot(
                prompt="delegated",
                session_id="child-requested",
                parent_session_id="leader-session",
                metadata={
                    "delegation": {
                        "mode": "background",
                        "category": "ultrabrain",
                    }
                },
            ),
            session_id="child-session",
            started_at=1,
        ),
    )
    completed = store.mark_background_task_terminal(
        workspace=tmp_path,
        task_id="task-completed-event",
        status="completed",
    )

    appended = store.append_session_event(
        workspace=tmp_path,
        session_id="leader-session",
        event_type="runtime.background_task_completed",
        source="runtime",
        payload={
            "task_id": "task-completed-event",
            "parent_session_id": "leader-session",
            "child_session_id": "child-session",
            "status": "completed",
            "result_available": True,
            "delegation": {
                "parent_session_id": "leader-session",
                "requested_child_session_id": "child-requested",
                "child_session_id": "child-session",
                "delegated_task_id": "task-completed-event",
                "approval_request_id": None,
                "question_request_id": None,
                "routing": {"mode": "background", "category": "ultrabrain"},
                "selected_preset": "advisor",
                "selected_execution_engine": "provider",
                "lifecycle_status": "completed",
                "approval_blocked": False,
                "result_available": True,
                "cancellation_cause": None,
            },
        },
    )

    assert completed.result_available is True
    assert appended is not None
    assert appended.payload["task_id"] == "task-completed-event"
    assert appended.payload["parent_session_id"] == "leader-session"
    assert appended.payload["child_session_id"] == "child-session"
    assert appended.payload["status"] == "completed"
    assert appended.payload["result_available"] is True
    assert appended.payload["routing_category"] == "ultrabrain"
    assert appended.payload["delegation"] == {
        "parent_session_id": "leader-session",
        "requested_child_session_id": "child-requested",
        "child_session_id": "child-session",
        "delegated_task_id": "task-completed-event",
        "approval_request_id": None,
        "question_request_id": None,
        "routing": {"mode": "background", "category": "ultrabrain"},
        "selected_preset": "advisor",
        "selected_execution_engine": "provider",
        "lifecycle_status": "completed",
        "approval_blocked": False,
        "result_available": True,
        "cancellation_cause": None,
    }


def test_background_task_storage_queued_cancel_race_does_not_overwrite_running_task(
    tmp_path: Path,
) -> None:
    store = SqliteSessionStore()
    store.create_background_task(workspace=tmp_path, task=_task(task_id="task-c-race"))
    running = store.mark_background_task_running(
        workspace=tmp_path,
        task_id="task-c-race",
        session_id="session-c-race",
    )
    original_load_background_task = store.load_background_task
    stale_reads_remaining = 1

    def _stale_queued_load(*, workspace: Path, task_id: str) -> BackgroundTaskState:
        nonlocal stale_reads_remaining
        if stale_reads_remaining > 0:
            stale_reads_remaining -= 1
            return BackgroundTaskState(
                task=BackgroundTaskRef(id=task_id),
                status="queued",
                request=BackgroundTaskRequestSnapshot(prompt="read sample.txt"),
                created_at=1,
                updated_at=1,
            )
        return original_load_background_task(workspace=workspace, task_id=task_id)

    store.load_background_task = _stale_queued_load

    cancelled = store.request_background_task_cancel(
        workspace=tmp_path,
        task_id="task-c-race",
    )

    assert running.status == "running"
    assert cancelled.status == "running"
    assert cancelled.cancel_requested_at is not None
    assert cancelled.finished_at is None
    assert cancelled.session_id == "session-c-race"
    assert cancelled.error is None


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


def test_background_task_storage_fail_incomplete_preserves_waiting_approval_tasks(
    tmp_path: Path,
) -> None:
    store = SqliteSessionStore()
    waiting_request = RuntimeRequest(prompt="background child", session_id="child-session")
    waiting_response = RuntimeResponse(
        session=SessionState(
            session=SessionRef(id="child-session", parent_id="leader-session"),
            status="waiting",
            turn=1,
            metadata={"background_task_id": "task-waiting", "background_run": True},
        ),
        events=(
            EventEnvelope(
                session_id="child-session",
                sequence=1,
                event_type="runtime.request_received",
                source="runtime",
                payload={"prompt": "background child"},
            ),
            EventEnvelope(
                session_id="child-session",
                sequence=2,
                event_type="runtime.approval_requested",
                source="runtime",
                payload={"request_id": "approval-1", "tool": "write_file"},
            ),
        ),
    )
    store.save_pending_approval(
        workspace=tmp_path,
        request=waiting_request,
        response=waiting_response,
        pending_approval=PendingApproval(
            request_id="approval-1",
            tool_name="write_file",
        ),
    )
    store.create_background_task(
        workspace=tmp_path,
        task=BackgroundTaskState(
            task=BackgroundTaskRef(id="task-waiting"),
            status="running",
            request=BackgroundTaskRequestSnapshot(
                prompt="background child",
                parent_session_id="leader-session",
            ),
            session_id="child-session",
            started_at=1,
            updated_at=1,
        ),
    )
    store.create_background_task(
        workspace=tmp_path,
        task=BackgroundTaskState(
            task=BackgroundTaskRef(id="task-interrupted"),
            status="running",
            request=BackgroundTaskRequestSnapshot(prompt="interrupted child"),
            session_id="interrupted-session",
            started_at=2,
            updated_at=2,
        ),
    )

    failed = store.fail_incomplete_background_tasks(
        workspace=tmp_path,
        message="background task interrupted before completion",
    )
    waiting = store.load_background_task(workspace=tmp_path, task_id="task-waiting")
    interrupted = store.load_background_task(workspace=tmp_path, task_id="task-interrupted")

    assert [task.task.id for task in failed] == ["task-interrupted"]
    assert waiting.status == "running"
    assert waiting.error is None
    assert interrupted.status == "interrupted"
    assert interrupted.error == "background task interrupted before completion"


def test_background_task_storage_running_cancel_is_idempotent_on_repeat_requests(
    tmp_path: Path,
) -> None:
    store = SqliteSessionStore()
    store.create_background_task(workspace=tmp_path, task=_task(task_id="task-d-repeat"))
    _ = store.mark_background_task_running(
        workspace=tmp_path,
        task_id="task-d-repeat",
        session_id="session-d-repeat",
    )

    first_cancel = store.request_background_task_cancel(
        workspace=tmp_path,
        task_id="task-d-repeat",
    )
    second_cancel = store.request_background_task_cancel(
        workspace=tmp_path,
        task_id="task-d-repeat",
    )

    assert first_cancel.status == "running"
    assert first_cancel.cancel_requested_at is not None
    assert second_cancel.status == "running"
    assert second_cancel.cancel_requested_at == first_cancel.cancel_requested_at
    assert second_cancel.updated_at == first_cancel.updated_at
    assert second_cancel.session_id == "session-d-repeat"
    assert second_cancel.finished_at is None


def test_background_task_storage_running_cancel_race_does_not_overwrite_terminal_state(
    tmp_path: Path,
) -> None:
    store = SqliteSessionStore()
    store.create_background_task(workspace=tmp_path, task=_task(task_id="task-d-race"))
    running = store.mark_background_task_running(
        workspace=tmp_path,
        task_id="task-d-race",
        session_id="session-d-race",
    )
    terminal = store.mark_background_task_terminal(
        workspace=tmp_path,
        task_id="task-d-race",
        status="completed",
    )
    original_load_background_task = store.load_background_task
    stale_reads_remaining = 1

    def _stale_running_load(*, workspace: Path, task_id: str) -> BackgroundTaskState:
        nonlocal stale_reads_remaining
        if stale_reads_remaining > 0:
            stale_reads_remaining -= 1
            return BackgroundTaskState(
                task=running.task,
                status="running",
                request=running.request,
                session_id=running.session_id,
                error=running.error,
                created_at=running.created_at,
                updated_at=running.updated_at,
                started_at=running.started_at,
                finished_at=running.finished_at,
                cancel_requested_at=running.cancel_requested_at,
            )
        return original_load_background_task(workspace=workspace, task_id=task_id)

    store.load_background_task = _stale_running_load

    cancelled = store.request_background_task_cancel(
        workspace=tmp_path,
        task_id="task-d-race",
    )

    assert terminal.status == "completed"
    assert cancelled.status == "completed"
    assert cancelled.cancel_requested_at is None
    assert cancelled.finished_at == terminal.finished_at
    assert cancelled.updated_at == terminal.updated_at


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
    cast(tuple[BackgroundTaskStatus, ...], ("completed", "failed", "cancelled", "interrupted")),
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


@pytest.mark.parametrize(
    ("initial_status", "next_status", "initial_error"),
    (
        ("completed", "failed", None),
        ("completed", "cancelled", None),
        ("failed", "completed", "boom"),
        ("failed", "cancelled", "boom"),
        ("cancelled", "completed", "operator cancelled"),
        ("cancelled", "failed", "operator cancelled"),
    ),
)
def test_background_task_storage_terminal_states_are_immutable_across_terminal_rewrites(
    tmp_path: Path,
    initial_status: BackgroundTaskStatus,
    next_status: BackgroundTaskStatus,
    initial_error: str | None,
) -> None:
    store = SqliteSessionStore()
    task_id = f"task-{initial_status}-immutable-{next_status}"
    store.create_background_task(workspace=tmp_path, task=_task(task_id=task_id))

    terminal = store.mark_background_task_terminal(
        workspace=tmp_path,
        task_id=task_id,
        status=initial_status,
        error=initial_error,
    )
    rewritten = store.mark_background_task_terminal(
        workspace=tmp_path,
        task_id=task_id,
        status=next_status,
        error="should be ignored",
    )

    assert rewritten == terminal
    assert rewritten.status == initial_status
    assert rewritten.error == initial_error


def test_background_task_storage_rejects_non_terminal_status_in_terminal_marker(
    tmp_path: Path,
) -> None:
    store = SqliteSessionStore()
    store.create_background_task(workspace=tmp_path, task=_task(task_id="task-illegal-terminal"))

    with pytest.raises(
        ValueError,
        match=(
            "background task terminal status must be completed, failed, cancelled, or interrupted"
        ),
    ):
        _ = store.mark_background_task_terminal(
            workspace=tmp_path,
            task_id="task-illegal-terminal",
            status=cast(BackgroundTaskStatus, "running"),
        )


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
    assert all(task.status == "interrupted" for task in reconciled)
    assert all(task.error == "background task interrupted before completion" for task in reconciled)


def test_background_task_storage_reconciliation_converts_cancel_requested_running_task_to_cancelled(
    tmp_path: Path,
) -> None:
    store = SqliteSessionStore()
    store.create_background_task(workspace=tmp_path, task=_task(task_id="task-reconcile-cancelled"))
    _ = store.mark_background_task_running(
        workspace=tmp_path,
        task_id="task-reconcile-cancelled",
        session_id="session-reconcile-cancelled",
    )
    requested_cancel = store.request_background_task_cancel(
        workspace=tmp_path,
        task_id="task-reconcile-cancelled",
    )

    reconciled = store.fail_incomplete_background_tasks(
        workspace=tmp_path,
        message="background task interrupted before completion",
    )
    cancelled = store.load_background_task(
        workspace=tmp_path,
        task_id="task-reconcile-cancelled",
    )

    assert requested_cancel.status == "running"
    assert requested_cancel.cancel_requested_at is not None
    assert [task.task.id for task in reconciled] == ["task-reconcile-cancelled"]
    assert cancelled.status == "cancelled"
    assert cancelled.error == "cancelled by parent during delegated execution"
    assert cancelled.cancellation_cause == "cancelled by parent during delegated execution"
    assert cancelled.result_available is False


def test_background_task_storage_reconciliation_preserves_approval_blocked_child_with_durable_correlation(  # noqa: E501
    tmp_path: Path,
) -> None:
    store = SqliteSessionStore()
    waiting_request = RuntimeRequest(
        prompt="background child",
        session_id="child-requested",
        parent_session_id="leader-session",
        metadata={
            "background_run": True,
            "background_task_id": "task-waiting-durable",
            "delegation": {
                "mode": "background",
                "subagent_type": "explore",
            },
        },
    )
    waiting_response = RuntimeResponse(
        session=SessionState(
            session=SessionRef(id="child-session", parent_id="leader-session"),
            status="waiting",
            turn=1,
            metadata={"background_task_id": "task-waiting-durable", "background_run": True},
        ),
        events=(
            EventEnvelope(
                session_id="child-session",
                sequence=1,
                event_type="runtime.request_received",
                source="runtime",
                payload={"prompt": "background child"},
            ),
            EventEnvelope(
                session_id="child-session",
                sequence=2,
                event_type="runtime.approval_requested",
                source="runtime",
                payload={"request_id": "approval-durable", "tool": "write_file"},
            ),
        ),
    )
    store.save_pending_approval(
        workspace=tmp_path,
        request=waiting_request,
        response=waiting_response,
        pending_approval=PendingApproval(
            request_id="approval-durable",
            tool_name="write_file",
        ),
    )
    store.create_background_task(
        workspace=tmp_path,
        task=BackgroundTaskState(
            task=BackgroundTaskRef(id="task-waiting-durable"),
            status="running",
            request=BackgroundTaskRequestSnapshot(
                prompt="background child",
                session_id="child-requested",
                parent_session_id="leader-session",
                metadata={"delegation": {"mode": "background", "subagent_type": "explore"}},
            ),
            session_id="child-session",
            started_at=1,
            updated_at=1,
        ),
    )

    failed = store.fail_incomplete_background_tasks(
        workspace=tmp_path,
        message="background task interrupted before completion",
    )

    with closing(sqlite3.connect(tmp_path / ".voidcode" / "sessions.sqlite3")) as connection:
        row = connection.execute(
            """
            SELECT status, approval_request_id, requested_child_session_id,
                   routing_mode, routing_subagent_type, result_available
            FROM background_tasks
            WHERE task_id = ?
            """,
            ("task-waiting-durable",),
        ).fetchone()

    assert failed == ()
    assert row == (
        "running",
        "approval-durable",
        "child-requested",
        "background",
        "explore",
        1,
    )


def test_runtime_events_define_delegated_background_task_durability_fields() -> None:
    assert DELEGATED_BACKGROUND_TASK_EVENT_TYPES == (
        "runtime.background_task_waiting_approval",
        "runtime.background_task_completed",
        "runtime.background_task_failed",
        "runtime.background_task_cancelled",
        "runtime.delegated_result_available",
    )
    assert DELEGATED_BACKGROUND_TASK_CORRELATION_FIELDS == (
        "task_id",
        "parent_session_id",
        "requested_child_session_id",
        "child_session_id",
        "approval_request_id",
        "question_request_id",
    )
    assert DELEGATED_BACKGROUND_TASK_ROUTING_FIELDS == (
        "routing_mode",
        "routing_category",
        "routing_subagent_type",
        "routing_description",
        "routing_command",
    )
    assert DELEGATED_BACKGROUND_TASK_DURABILITY_FIELDS == (
        *DELEGATED_BACKGROUND_TASK_CORRELATION_FIELDS,
        *DELEGATED_BACKGROUND_TASK_ROUTING_FIELDS,
        "status",
        "approval_blocked",
        "result_available",
        "cancellation_cause",
    )


def test_background_task_event_enrichment_keeps_typed_delegation_payload_transport_friendly(
    tmp_path: Path,
) -> None:
    store = SqliteSessionStore()
    store.create_background_task(
        workspace=tmp_path,
        task=BackgroundTaskState(
            task=BackgroundTaskRef(id="task-typed-event"),
            status="running",
            request=BackgroundTaskRequestSnapshot(
                prompt="delegated",
                session_id="child-requested",
                parent_session_id="leader-session",
                metadata={
                    "delegation": {
                        "mode": "background",
                        "subagent_type": "explore",
                        "description": "Inspect logs",
                    }
                },
            ),
            session_id="child-session",
            approval_request_id="approval-1",
            result_available=True,
            started_at=1,
            updated_at=1,
        ),
    )
    parent_request = RuntimeRequest(prompt="leader", session_id="leader-session")
    parent_response = RuntimeResponse(
        session=SessionState(
            session=SessionRef(id="leader-session"),
            status="completed",
            turn=1,
        ),
        events=(),
        output="done",
    )
    store.save_run(workspace=tmp_path, request=parent_request, response=parent_response)

    appended = store.append_session_event(
        workspace=tmp_path,
        session_id="leader-session",
        event_type="runtime.background_task_waiting_approval",
        source="runtime",
        payload={
            "task_id": "task-typed-event",
            "approval_blocked": True,
            "delegation": {
                "delegated_task_id": "task-typed-event",
                "parent_session_id": "leader-session",
                "child_session_id": "child-session",
                "requested_child_session_id": "child-requested",
                "routing": {
                    "mode": "background",
                    "subagent_type": "explore",
                    "description": "Inspect logs",
                },
                "lifecycle_status": "waiting_approval",
                "approval_request_id": "approval-1",
                "question_request_id": None,
                "selected_preset": None,
                "selected_execution_engine": None,
                "approval_blocked": True,
                "result_available": True,
                "cancellation_cause": None,
            },
            "message": {
                "kind": "delegated_lifecycle",
                "status": "waiting_approval",
                "summary_output": None,
                "error": None,
                "approval_blocked": True,
                "result_available": True,
            },
        },
    )

    assert appended is not None
    delegated = appended.delegated_lifecycle
    assert delegated is not None
    assert delegated.delegation.delegated_task_id == "task-typed-event"
    assert delegated.delegation.routing is not None
    assert delegated.delegation.routing.subagent_type == "explore"
    assert delegated.message.approval_blocked is True
