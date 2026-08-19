from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import closing
from pathlib import Path
from typing import Any, cast

import pytest

from voidcode.runtime.contracts import RuntimeRequest, RuntimeResponse, UnknownSessionError
from voidcode.runtime.events import EventEnvelope
from voidcode.runtime.paths import sessions_db_path, state_home
from voidcode.runtime.permission import PendingApproval
from voidcode.runtime.question import PendingQuestion, PendingQuestionOption, PendingQuestionPrompt
from voidcode.runtime.session import SessionRef, SessionState
from voidcode.runtime.storage import SessionSealedError, SqliteSessionStore
from voidcode.runtime.task import (
    BackgroundTaskRef,
    BackgroundTaskRequestSnapshot,
    BackgroundTaskState,
)


def _private_attr(instance: object, name: str) -> Any:
    return getattr(instance, name)


def _run_session(
    store: SqliteSessionStore,
    workspace: Path,
    request: RuntimeRequest,
    response: RuntimeResponse,
) -> None:
    """Simulate the run loop: create a running row, append the response events
    incrementally via ``append_session_events``, then seal the terminal
    snapshot via ``save_run``."""
    session = response.session.session
    store.save_run(
        workspace=workspace,
        request=request,
        response=RuntimeResponse(
            session=SessionState(
                session=SessionRef(id=session.id, parent_id=session.parent_id),
                status="running",
                turn=response.session.turn,
                metadata=response.session.metadata,
            ),
            events=(),
            output=None,
        ),
    )
    if response.events:
        store.append_session_events(
            workspace=workspace,
            session_id=session.id,
            events=tuple((event.event_type, event.source, event.payload, None) for event in response.events),
        )
    store.save_run(workspace=workspace, request=request, response=response)


def test_runtime_paths_honor_explicit_empty_env_mapping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host_db_path = tmp_path / "host" / "sessions.sqlite3"
    host_state_home = tmp_path / "host-state"
    monkeypatch.setenv("VOIDCODE_DB_PATH", str(host_db_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(host_state_home))

    assert sessions_db_path({}) == (Path.home() / ".local" / "state" / "voidcode" / "sessions.sqlite3")
    assert state_home({}) == Path.home() / ".local" / "state" / "voidcode"
    assert sessions_db_path({"XDG_STATE_HOME": str(tmp_path / "mapped-state")}) == (tmp_path / "mapped-state" / "voidcode" / "sessions.sqlite3")


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

    # Canonical seal flow: the event log is appended incrementally BEFORE the
    # terminal seal-writer ``save_run`` snapshots the row (the seal never writes
    # events itself). The row must therefore carry at least one persisted event;
    # event-less terminal rows are treated as fabrication residue by pruning.
    store.save_interrupted_checkpoint(
        workspace=tmp_path,
        session_id="child-session",
        prompt="child task",
        session_metadata={},
        tool_results=(),
        last_event_sequence=0,
        create_if_missing=True,
        parent_session_id="leader-session",
    )
    store.append_session_events(
        workspace=tmp_path,
        session_id="child-session",
        events=(("graph.response_ready", "graph", {"summary": "child done"}, None),),
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


def test_session_storage_roundtrips_plan_mode_metadata(tmp_path: Path) -> None:
    store = SqliteSessionStore()
    metadata: dict[str, object] = {
        "mode": "plan",
        "read_only": True,
    }
    request = RuntimeRequest(prompt="persist plan mode", session_id="plan-mode-session")
    response = RuntimeResponse(
        session=SessionState(
            session=SessionRef(id="plan-mode-session"),
            status="completed",
            turn=1,
            metadata=metadata,
        ),
        events=(
            EventEnvelope(
                session_id="plan-mode-session",
                sequence=1,
                event_type="graph.response_ready",
                source="graph",
            ),
        ),
        output="done",
    )

    store.save_run(workspace=tmp_path, request=request, response=response)

    loaded = store.load_session(workspace=tmp_path, session_id="plan-mode-session")

    assert loaded.session.metadata["mode"] == "plan"
    assert loaded.session.metadata["read_only"] is True


def test_session_storage_roundtrips_requested_mode_and_derived_read_only(
    tmp_path: Path,
) -> None:
    store = SqliteSessionStore()
    metadata: dict[str, object] = {
        "mode": "plan",
        "read_only": False,
    }
    request = RuntimeRequest(prompt="persist derived read only", session_id="derived-read-only-session")
    response = RuntimeResponse(
        session=SessionState(
            session=SessionRef(id="derived-read-only-session"),
            status="completed",
            turn=1,
            metadata=metadata,
        ),
        events=(
            EventEnvelope(
                session_id="derived-read-only-session",
                sequence=1,
                event_type="graph.response_ready",
                source="graph",
            ),
        ),
        output="done",
    )

    store.save_run(workspace=tmp_path, request=request, response=response)

    loaded = store.load_session(workspace=tmp_path, session_id="derived-read-only-session")

    # Plan mode implies read_only even when the explicit flag is False.
    assert loaded.session.metadata["mode"] == "plan"
    assert loaded.session.metadata["read_only"] is True


def test_session_storage_roundtrips_redacted_policy_observations(tmp_path: Path) -> None:
    store = SqliteSessionStore()
    metadata: dict[str, object] = {
        "mode": "plan",
        "read_only": False,
        "delegation": {"mode": "background", "subagent_type": "explore", "depth": 1},
        "prompt_stack": {
            "version": 1,
            "fragments": [
                {"source": "base", "preview": "safe preview"},
                {"source": "secret", "preview": "api_key=raw-secret-value"},
            ],
        },
        "runtime_state": {
            "injected_env": {"CI": "1", "NPM_CONFIG_YES": "true"},
            "api_key": "sk-runtime-secret",
        },
    }
    request = RuntimeRequest(prompt="persist policy", session_id="policy-session")
    response = RuntimeResponse(
        session=SessionState(
            session=SessionRef(id="policy-session"),
            status="failed",
            turn=1,
            metadata=metadata,
        ),
        events=(
            EventEnvelope(
                session_id="policy-session",
                sequence=1,
                event_type="runtime.failed",
                source="runtime",
                payload={
                    "kind": "runtime_tool_policy_denied",
                    "tool": "write",
                    "tool_policy": {
                        "tool": "write",
                        "mode": "plan",
                        "read_only": True,
                        "decision": "deny",
                        "reason": "read-only runtime policy denies mutating tools",
                    },
                },
            ),
        ),
        output=None,
    )

    store.save_run(workspace=tmp_path, request=request, response=response)

    loaded = store.load_session(workspace=tmp_path, session_id="policy-session")
    checkpoint = store.load_resume_checkpoint(workspace=tmp_path, session_id="policy-session")
    encoded = json.dumps(loaded.session.metadata, sort_keys=True)

    assert loaded.session.metadata["mode"] == "plan"
    assert loaded.session.metadata["read_only"] is True
    assert "runtime_policy" not in loaded.session.metadata
    policy_observations = cast(dict[str, object], loaded.session.metadata["policy_observations"])
    tool_policy_denial = cast(dict[str, object], policy_observations["tool_policy_denial"])
    assert tool_policy_denial["tool"] == "write"
    assert "raw-secret-value" not in encoded
    assert "sk-runtime-secret" not in encoded
    assert 'NPM_CONFIG_YES": "true' not in encoded
    assert checkpoint is not None
    checkpoint_metadata = cast(dict[str, object], checkpoint["session_metadata"])
    assert "runtime_policy" not in checkpoint_metadata


def test_tool_results_from_events_preserves_raw_read_content() -> None:
    raw_content = "\n".join(
        [
            "<path>sample.txt</path>",
            "<type>file</type>",
            "<content>",
            "1: alpha",
            "(End of file - total 1 lines)",
            "</content>",
        ]
    )
    event = EventEnvelope(
        session_id="session-1",
        sequence=1,
        event_type="runtime.tool_completed",
        source="tool",
        payload={
            "tool": "read",
            "status": "ok",
            "content": raw_content,
        },
    )
    tool_results_from_events = _private_attr(SqliteSessionStore, "_tool_results_from_events")

    tool_results = tool_results_from_events((event,))

    assert tool_results[0]["content"] == raw_content


def test_session_storage_revert_marker_filters_active_view_only(tmp_path: Path) -> None:
    store = SqliteSessionStore()
    request = RuntimeRequest(prompt="mistaken request", session_id="undo-session")
    response = RuntimeResponse(
        session=SessionState(
            session=SessionRef(id="undo-session"),
            status="completed",
            turn=1,
            metadata={"runtime_state": {"context_projection": {"facts": ["bad context"]}, "run_id": "r1"}},
        ),
        events=(
            EventEnvelope(
                session_id="undo-session",
                sequence=1,
                event_type="runtime.request_received",
                source="runtime",
                payload={"prompt": "mistaken request"},
            ),
            EventEnvelope(
                session_id="undo-session",
                sequence=2,
                event_type="runtime.tool_completed",
                source="runtime",
                payload={"tool": "read", "status": "ok", "content": "context"},
            ),
            EventEnvelope(
                session_id="undo-session",
                sequence=3,
                event_type="graph.response_ready",
                source="graph",
                payload={"response": "bad branch"},
            ),
        ),
        output="bad branch",
    )
    _run_session(store, tmp_path, request, response)

    marker = store.revert_session(workspace=tmp_path, session_id="undo-session", sequence=2)
    active = store.load_session(workspace=tmp_path, session_id="undo-session")
    result = store.load_session_result(workspace=tmp_path, session_id="undo-session")

    assert marker.sequence == 2
    assert [event.sequence for event in active.events] == [1]
    assert active.output is None
    assert active.session.metadata["runtime_state"] == {"run_id": "r1"}
    assert [event.sequence for event in result.transcript] == [1, 2, 3]
    assert result.output == "bad branch"
    assert result.revert_marker is not None
    assert result.revert_marker.sequence == 2

    restored = store.unrevert_session(workspace=tmp_path, session_id="undo-session")

    assert restored is not None
    assert restored.sequence == 2
    assert [event.sequence for event in store.load_session(workspace=tmp_path, session_id="undo-session").events] == [1, 2, 3]


def test_session_storage_persists_runtime_todos_and_filters_reverted_state(
    tmp_path: Path,
) -> None:
    store = SqliteSessionStore()
    request = RuntimeRequest(prompt="track todos", session_id="todo-session")
    response = RuntimeResponse(
        session=SessionState(
            session=SessionRef(id="todo-session"),
            status="completed",
            turn=1,
            metadata={
                "runtime_state": {
                    "todos": {
                        "version": 1,
                        "revision": 3,
                        "todos": [
                            {
                                "content": "persist me",
                                "status": "in_progress",
                                "position": 1,
                                "updated_at": 3,
                            }
                        ],
                        "summary": {
                            "total": 1,
                            "pending": 0,
                            "in_progress": 1,
                            "completed": 0,
                            "cancelled": 0,
                            "active": 1,
                        },
                    }
                }
            },
        ),
        events=(
            EventEnvelope(
                session_id="todo-session",
                sequence=1,
                event_type="runtime.request_received",
                source="runtime",
                payload={"prompt": "track todos"},
            ),
            EventEnvelope(
                session_id="todo-session",
                sequence=3,
                event_type="runtime.todo_updated",
                source="runtime",
                payload={
                    "session_id": "todo-session",
                    "revision": 3,
                    "todos": [
                        {
                            "content": "persist me",
                            "status": "in_progress",
                            "position": 1,
                            "updated_at": 3,
                        }
                    ],
                },
            ),
        ),
        output="done",
    )

    _run_session(store, tmp_path, request, response)

    loaded = store.load_session(workspace=tmp_path, session_id="todo-session")

    raw_runtime_state = loaded.session.metadata["runtime_state"]
    assert isinstance(raw_runtime_state, dict)
    runtime_state = cast(dict[str, object], raw_runtime_state)
    todos_state = runtime_state.get("todos")
    assert isinstance(todos_state, dict)
    todos_state_payload = cast(dict[str, object], todos_state)
    todos = todos_state_payload.get("todos")
    assert isinstance(todos, list)
    assert cast(dict[str, object], todos[0])["content"] == "persist me"

    store.revert_session(workspace=tmp_path, session_id="todo-session", sequence=2)
    reverted = store.load_session(workspace=tmp_path, session_id="todo-session")

    raw_reverted_runtime_state = reverted.session.metadata["runtime_state"]
    assert isinstance(raw_reverted_runtime_state, dict)
    assert "todos" not in raw_reverted_runtime_state


def test_session_storage_undo_uses_latest_visible_user_turn(tmp_path: Path) -> None:
    store = SqliteSessionStore()
    request = RuntimeRequest(prompt="second", session_id="multi-turn-session")
    response = RuntimeResponse(
        session=SessionState(session=SessionRef(id="multi-turn-session"), status="completed", turn=2),
        events=(
            EventEnvelope(
                session_id="multi-turn-session",
                sequence=1,
                event_type="runtime.request_received",
                source="runtime",
                payload={"prompt": "first"},
            ),
            EventEnvelope(
                session_id="multi-turn-session",
                sequence=2,
                event_type="graph.response_ready",
                source="graph",
            ),
            EventEnvelope(
                session_id="multi-turn-session",
                sequence=3,
                event_type="runtime.request_received",
                source="runtime",
                payload={"prompt": "second"},
            ),
            EventEnvelope(
                session_id="multi-turn-session",
                sequence=4,
                event_type="graph.response_ready",
                source="graph",
            ),
        ),
        output="second output",
    )
    _run_session(store, tmp_path, request, response)

    marker = store.undo_session(workspace=tmp_path, session_id="multi-turn-session")

    assert marker.sequence == 3
    assert [event.sequence for event in store.load_session(workspace=tmp_path, session_id="multi-turn-session").events] == [1, 2]


def test_session_storage_preserves_prior_events_when_same_session_continues(
    tmp_path: Path,
) -> None:
    store = SqliteSessionStore()
    session_id = "continued-session"
    # Simulate the incremental run loop: create a running row, then append two
    # batches of events before the terminal save_run seals the session.
    store.save_run(
        workspace=tmp_path,
        request=RuntimeRequest(prompt="first", session_id=session_id),
        response=RuntimeResponse(
            session=SessionState(session=SessionRef(id=session_id), status="running", turn=1, metadata={}),
            events=(),
            output=None,
        ),
    )
    store.append_session_events(
        workspace=tmp_path,
        session_id=session_id,
        events=(
            ("runtime.request_received", "runtime", {"prompt": "first"}, None),
            ("graph.response_ready", "graph", {"output": "first output"}, None),
        ),
    )
    store.append_session_events(
        workspace=tmp_path,
        session_id=session_id,
        events=(
            ("runtime.request_received", "runtime", {"prompt": "second"}, None),
            ("graph.response_ready", "graph", {"output": "second output"}, None),
        ),
    )
    with store._connect(tmp_path) as connection:
        event_count_before_seal = connection.execute(
            "SELECT COUNT(*) FROM session_events WHERE workspace_id = ? AND session_id = ?",
            (str(tmp_path), session_id),
        ).fetchone()[0]

    store.save_run(
        workspace=tmp_path,
        request=RuntimeRequest(prompt="second", session_id=session_id),
        response=RuntimeResponse(
            session=SessionState(session=SessionRef(id=session_id), status="completed", turn=1, metadata={}),
            events=(),
            output="second output",
        ),
    )

    with store._connect(tmp_path) as connection:
        event_count_after_seal = connection.execute(
            "SELECT COUNT(*) FROM session_events WHERE workspace_id = ? AND session_id = ?",
            (str(tmp_path), session_id),
        ).fetchone()[0]

    replay = store.load_session(workspace=tmp_path, session_id=session_id)
    result = store.load_session_result(workspace=tmp_path, session_id=session_id)

    assert event_count_before_seal == 4
    assert event_count_after_seal == 4
    assert [event.sequence for event in replay.events] == [1, 2, 3, 4]
    request_prompts = [event.payload.get("prompt") for event in replay.events if event.event_type == "runtime.request_received"]
    assert request_prompts == [
        "first",
        "second",
    ]
    assert result.last_event_sequence == 4
    assert result.output == "second output"


def test_session_storage_save_run_writes_zero_session_events(tmp_path: Path) -> None:
    store = SqliteSessionStore(database_path=tmp_path / "sessions.sqlite3")
    store.save_run(
        workspace=tmp_path,
        request=RuntimeRequest(prompt="seal only", session_id="seal-only-session"),
        response=_completed_response("seal-only-session"),
    )

    with store._connect(tmp_path) as connection:
        event_count = connection.execute(
            "SELECT COUNT(*) FROM session_events WHERE workspace_id = ? AND session_id = ?",
            (str(tmp_path), "seal-only-session"),
        ).fetchone()[0]

    assert event_count == 0
    assert store.load_session(workspace=tmp_path, session_id="seal-only-session").events == ()


def test_session_storage_removed_private_event_writers() -> None:
    assert not hasattr(SqliteSessionStore, "_replace" + "_session_events")
    assert not hasattr(SqliteSessionStore, "_session_events_payload")


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
        session_columns = [row[1] for row in connection.execute("PRAGMA table_info(sessions)").fetchall()]
        todo_columns = [row[1] for row in connection.execute("PRAGMA table_info(session_todos)").fetchall()]
        delivery_columns = [row[1] for row in connection.execute("PRAGMA table_info(session_event_deliveries)").fetchall()]
        schema_version = connection.execute("PRAGMA user_version").fetchone()[0]
        notification_indexes = connection.execute("PRAGMA index_list(session_notifications)").fetchall()

    assert session_columns == [
        "session_id",
        "parent_session_id",
        "workspace_id",
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
        "created_at_unix_ms",
    ]
    assert todo_columns == [
        "workspace_id",
        "session_id",
        "position",
        "content",
        "status",
        "updated_at",
    ]
    assert delivery_columns == ["workspace_id", "session_id", "dedupe_key", "delivered_at"]
    assert schema_version == 12
    assert any(row[2] == 1 and row[3] == "u" for row in notification_indexes)


def test_session_storage_fresh_database_background_tasks_carry_v12_schema_columns(tmp_path: Path) -> None:
    database_path = tmp_path / "fresh-schema-v12.sqlite3"
    store = SqliteSessionStore(database_path=database_path)
    store.create_background_task(
        workspace=tmp_path,
        task=BackgroundTaskState(
            task=BackgroundTaskRef(id="task-v12"),
            request=BackgroundTaskRequestSnapshot(prompt="probe"),
        ),
    )

    with closing(sqlite3.connect(database_path)) as connection:
        task_columns = [row[1] for row in connection.execute("PRAGMA table_info(background_tasks)").fetchall()]
        schema_version = connection.execute("PRAGMA user_version").fetchone()[0]

    assert schema_version == 12
    for column in ("output_schema_json", "schema_mode", "structured_output_json", "schema_validation_json"):
        assert column in task_columns


def test_session_storage_parses_interrupted_session_status(tmp_path: Path) -> None:
    database_path = tmp_path / "interrupted-status.sqlite3"
    store = SqliteSessionStore(database_path=database_path)

    assert store._parse_session_status("interrupted") == "interrupted"
    with pytest.raises(ValueError):
        store._parse_session_status("not-a-status")


def test_session_storage_bootstraps_sequences_from_existing_timestamps(tmp_path: Path) -> None:
    database_path = tmp_path / "sequence-bootstrap.sqlite3"
    store = SqliteSessionStore(database_path=database_path)
    old_request = RuntimeRequest(prompt="old", session_id="old-session")
    old_response = RuntimeResponse(
        session=SessionState(session=SessionRef(id="old-session"), status="completed", turn=1),
        events=(
            EventEnvelope(
                session_id="old-session",
                sequence=1,
                event_type="graph.response_ready",
                source="graph",
            ),
        ),
        output="old output",
    )
    store.save_run(workspace=tmp_path, request=old_request, response=old_response)
    store.create_background_task(
        workspace=tmp_path,
        task=BackgroundTaskState(
            task=BackgroundTaskRef(id="old-task"),
            request=BackgroundTaskRequestSnapshot(prompt="old task"),
        ),
    )
    with closing(sqlite3.connect(database_path)) as connection:
        _ = connection.execute(
            "UPDATE sessions SET created_at = 40, updated_at = 50 WHERE session_id = ?",
            ("old-session",),
        )
        _ = connection.execute(
            "UPDATE background_tasks SET created_at = 60, updated_at = 70 WHERE task_id = ?",
            ("old-task",),
        )
        _ = connection.execute("DELETE FROM storage_sequences")
        connection.commit()

    new_request = RuntimeRequest(prompt="new", session_id="new-session")
    new_response = RuntimeResponse(
        session=SessionState(session=SessionRef(id="new-session"), status="completed", turn=1),
        events=(
            EventEnvelope(
                session_id="new-session",
                sequence=1,
                event_type="graph.response_ready",
                source="graph",
            ),
        ),
        output="new output",
    )
    store.save_run(workspace=tmp_path, request=new_request, response=new_response)
    store.create_background_task(
        workspace=tmp_path,
        task=BackgroundTaskState(
            task=BackgroundTaskRef(id="new-task"),
            request=BackgroundTaskRequestSnapshot(prompt="new task"),
        ),
    )

    with closing(sqlite3.connect(database_path)) as connection:
        new_session_row = connection.execute(
            "SELECT created_at, updated_at FROM sessions WHERE session_id = ?",
            ("new-session",),
        ).fetchone()
        new_task_row = connection.execute(
            "SELECT created_at, updated_at FROM background_tasks WHERE task_id = ?",
            ("new-task",),
        ).fetchone()

    assert [session.session.id for session in store.list_sessions(workspace=tmp_path)] == [
        "new-session",
        "old-session",
    ]
    assert new_session_row == (41, 51)
    assert new_task_row == (71, 71)


def test_session_storage_configures_sqlite_operability_pragmas(tmp_path: Path) -> None:
    database_path = tmp_path / "operability.sqlite3"
    store = SqliteSessionStore(database_path=database_path)

    store.list_sessions(workspace=tmp_path)

    diagnostics = store.storage_diagnostics(workspace=tmp_path)

    assert diagnostics["database_path"] == str(database_path)
    assert diagnostics["database_exists"] is True
    assert diagnostics["connection_policy"] == {
        "journal_mode": "wal",
        "synchronous": 1,
        "busy_timeout_ms": 5000,
        "foreign_keys": 1,
        "wal_autocheckpoint_pages": 1000,
    }
    assert diagnostics["counts"] == {
        "sessions": 0,
        "background_tasks": 0,
        "session_notifications": 0,
        "session_events": 0,
        "session_todos": 0,
        "session_event_deliveries": 0,
    }


def test_session_storage_rejects_existing_unversioned_runtime_schema_without_mutation(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "unversioned-runtime.sqlite3"
    with closing(sqlite3.connect(database_path)) as connection:
        _ = connection.execute("CREATE TABLE sessions (session_id TEXT PRIMARY KEY, workspace TEXT NOT NULL)")
        connection.commit()

    store = SqliteSessionStore(database_path=database_path)

    with pytest.raises(
        RuntimeError,
        match=r"table 'sessions' missing columns: .*workspace_id.*unversioned-runtime\.sqlite3",
    ):
        store.list_sessions(workspace=tmp_path)

    with closing(sqlite3.connect(database_path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0


def test_session_storage_rejects_runtime_schema_version_mismatch(tmp_path: Path) -> None:
    database_path = tmp_path / "future-runtime.sqlite3"
    with closing(sqlite3.connect(database_path)) as connection:
        _ = connection.execute("PRAGMA user_version = 999")
        connection.commit()

    store = SqliteSessionStore(database_path=database_path)

    with pytest.raises(
        RuntimeError,
        match="schema version mismatch: expected 12 got 999.*future-runtime\\.sqlite3",
    ):
        store.list_notifications(workspace=tmp_path)


def test_session_storage_rejects_non_canonical_schema_missing_runtime_columns(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "invalid-sessions.sqlite3"
    with closing(sqlite3.connect(database_path)) as connection:
        _ = connection.execute("PRAGMA user_version = 6")
        _ = connection.execute(
            """
            CREATE TABLE sessions (
                session_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                status TEXT NOT NULL,
                turn INTEGER NOT NULL,
                prompt TEXT NOT NULL,
                output TEXT,
                metadata_json TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                last_event_sequence INTEGER NOT NULL,
                PRIMARY KEY (workspace_id, session_id)
            )
            """
        )
        _ = connection.execute(
            """
            CREATE TABLE session_events (
                workspace_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                source TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY (workspace_id, session_id, sequence)
            )
            """
        )
        _ = connection.execute(
            """
            CREATE TABLE session_notifications (
                notification_id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                summary TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                event_sequence INTEGER NOT NULL,
                dedupe_key TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                acknowledged_at INTEGER,
                UNIQUE(workspace_id, dedupe_key)
            )
            """
        )
        _ = connection.execute(
            """
            CREATE TABLE background_tasks (
                task_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
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
                finished_at INTEGER,
                PRIMARY KEY (workspace_id, task_id)
            )
            """
        )
        connection.commit()

    store = SqliteSessionStore(database_path=database_path)
    with pytest.raises(
        RuntimeError,
        match=(
            r"table 'sessions' missing columns: .*"
            r"Reset the runtime database with `uv run voidcode storage reset` "
            r"or remove '.*[\\/]invalid-sessions\.sqlite3' "
            r"plus matching -wal/-shm files\."
        ),
    ):
        store.list_sessions(workspace=tmp_path)

    with closing(sqlite3.connect(database_path)) as connection:
        session_columns = {row[1] for row in connection.execute("PRAGMA table_info(sessions)").fetchall()}

    assert "parent_session_id" not in session_columns
    assert "pending_approval_json" not in session_columns


def test_session_storage_rejects_non_canonical_schema_with_wrong_existing_table_shape(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "wrong-table-shape.sqlite3"
    with closing(sqlite3.connect(database_path)) as connection:
        _ = connection.execute("PRAGMA user_version = 6")
        _ = connection.execute(
            """
            CREATE TABLE sessions (
                session_id TEXT NOT NULL,
                parent_session_id TEXT,
                workspace_id TEXT NOT NULL,
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
                last_event_sequence INTEGER NOT NULL,
                PRIMARY KEY (workspace_id, session_id)
            )
            """
        )
        _ = connection.execute(
            """
            CREATE TABLE session_events (
                workspace_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                source TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY (workspace_id, session_id, sequence)
            )
            """
        )
        _ = connection.execute(
            """
            CREATE TABLE background_tasks (
                task_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                status TEXT NOT NULL,
                prompt TEXT NOT NULL,
                request_session_id TEXT,
                request_parent_session_id TEXT,
                request_metadata_json TEXT NOT NULL,
                requested_child_session_id TEXT,
                routing_mode TEXT,
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
                finished_at INTEGER,
                PRIMARY KEY (workspace_id, task_id)
            )
            """
        )
        _ = connection.execute(
            """
            CREATE TABLE session_notifications (
                notification_id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                summary TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                event_sequence INTEGER NOT NULL,
                dedupe_key TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                acknowledged_at INTEGER,
                UNIQUE(workspace_id, dedupe_key)
            )
            """
        )
        _ = connection.execute(
            """
            CREATE TABLE session_event_deliveries (
                workspace_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                delivered_at INTEGER NOT NULL,
                PRIMARY KEY (workspace_id, session_id)
            )
            """
        )
        connection.commit()

    store = SqliteSessionStore(database_path=database_path)

    with pytest.raises(
        RuntimeError,
        match=(
            r"table 'background_tasks' missing columns: created_at_unix_ms, "
            r"delegated_reminder_json, finished_at_unix_ms, keep_alive, "
            r"output_schema_json, schema_mode, schema_validation_json, "
            r"started_at_unix_ms, steer_prompt, structured_output_json.*"
            r"Reset the runtime database with `uv run voidcode storage reset` "
            r"or remove '.*[\\/]wrong-table-shape\.sqlite3' "
            r"plus matching -wal/-shm files\."
        ),
    ):
        store.list_notifications(workspace=tmp_path)

    with closing(sqlite3.connect(database_path)) as connection:
        delivery_columns = {row[1] for row in connection.execute("PRAGMA table_info(session_event_deliveries)").fetchall()}

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
                    "tool": "read",
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
            "tool_name": "read",
            "content": "alpha\n",
            "status": "ok",
            "data": {
                "tool": "read",
                "status": "ok",
                "content": "alpha\n",
                "error": None,
                "path": "sample.txt",
            },
            "error": None,
        }
    ]


def test_session_storage_projects_tool_effectiveness_from_persisted_events(tmp_path: Path) -> None:
    store = SqliteSessionStore(database_path=tmp_path / "sessions.sqlite3")
    request = RuntimeRequest(prompt="edit", session_id="effectiveness-session")
    response = RuntimeResponse(
        session=SessionState(
            session=SessionRef(id="effectiveness-session"),
            status="completed",
            turn=1,
            metadata={},
        ),
        events=(
            EventEnvelope(
                session_id="effectiveness-session",
                sequence=1,
                event_type="runtime.tool_completed",
                source="tool",
                payload={
                    "tool": "edit",
                    "status": "error",
                    "arguments": {"path": "sample.py"},
                    "error": "stale",
                    "error_kind": "stale_edit",
                },
            ),
            EventEnvelope(
                session_id="effectiveness-session",
                sequence=2,
                event_type="runtime.tool_completed",
                source="tool",
                payload={
                    "tool": "edit",
                    "status": "ok",
                    "arguments": {"path": "sample.py"},
                    "content": "updated",
                },
            ),
        ),
        output="done",
    )
    _run_session(store, tmp_path, request, response)

    report = store.tool_effectiveness_report(workspace=tmp_path)

    assert report.session_count == 1
    assert report.tool_call_count == 2
    assert report.tools[0].tool == "edit"
    assert report.tools[0].retries_after_error == 1
    assert report.tools[0].error_kinds == {"stale_edit": 1}


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
                    "tool": "write",
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
            "tool_name": "write",
            "content": None,
            "status": "ok",
            "data": {
                "tool": "write",
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

    database_path = sessions_db_path()
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

    database_path = sessions_db_path()
    with closing(sqlite3.connect(database_path)) as connection:
        _ = connection.execute(
            "UPDATE sessions SET resume_checkpoint_json = ? WHERE session_id = ?",
            (
                '{"kind":"not-real","version":1}',
                "checkpoint-invalid-kind",
            ),
        )
        connection.commit()

    with pytest.raises(ValueError, match=r"persisted resume checkpoint kind is invalid: 'not-real'"):
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
    assert first_event.sequence == 1
    assert duplicate_event is None
    assert loaded.events[-1] == first_event
    assert loaded.events[-1].sequence == 1


def test_session_storage_deduped_session_event_does_not_advance_session_order(
    tmp_path: Path,
) -> None:
    store = SqliteSessionStore()
    for session_id in ("first-session", "second-session"):
        _run_session(
            store,
            tmp_path,
            RuntimeRequest(prompt=session_id, session_id=session_id),
            RuntimeResponse(
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
            ),
        )
    first_event = store.append_session_event(
        workspace=tmp_path,
        session_id="first-session",
        event_type="runtime.background_task_waiting_approval",
        source="runtime",
        payload={"task_id": "task-123"},
        dedupe_key="background_task_waiting_approval:task-123:req-1",
    )
    assert first_event is not None
    sessions_after_first_event = store.list_sessions(workspace=tmp_path)

    duplicate_event = store.append_session_event(
        workspace=tmp_path,
        session_id="first-session",
        event_type="runtime.background_task_waiting_approval",
        source="runtime",
        payload={"task_id": "task-123"},
        dedupe_key="background_task_waiting_approval:task-123:req-1",
    )
    sessions_after_duplicate = store.list_sessions(workspace=tmp_path)

    assert duplicate_event is None
    assert [session.session.id for session in sessions_after_first_event] == [
        "first-session",
        "second-session",
    ]
    assert [session.session.id for session in sessions_after_duplicate] == [
        "first-session",
        "second-session",
    ]
    loaded = store.load_session(workspace=tmp_path, session_id="first-session")
    assert [event.sequence for event in loaded.events] == [1, 2]


def test_session_storage_reports_corrupt_pending_approval_payload(tmp_path: Path) -> None:
    database_path = tmp_path / "sessions.sqlite3"
    store = SqliteSessionStore(database_path=database_path)
    store.save_run(
        workspace=tmp_path,
        request=RuntimeRequest(prompt="approval", session_id="approval-session"),
        response=RuntimeResponse(
            session=SessionState(session=SessionRef(id="approval-session"), status="waiting"),
            events=(),
        ),
    )
    with closing(sqlite3.connect(database_path)) as connection:
        _ = connection.execute(
            "UPDATE sessions SET pending_approval_json = ? WHERE session_id = ?",
            ('{"request_id": 1, "tool_name": "write"}', "approval-session"),
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="is missing required fields"):
        _ = store.load_pending_approval(workspace=tmp_path, session_id="approval-session")


def test_session_storage_reports_corrupt_pending_question_payload(tmp_path: Path) -> None:
    database_path = tmp_path / "sessions.sqlite3"
    store = SqliteSessionStore(database_path=database_path)
    store.save_run(
        workspace=tmp_path,
        request=RuntimeRequest(prompt="question", session_id="question-session"),
        response=RuntimeResponse(
            session=SessionState(session=SessionRef(id="question-session"), status="waiting"),
            events=(),
        ),
    )
    with closing(sqlite3.connect(database_path)) as connection:
        _ = connection.execute(
            "UPDATE sessions SET pending_question_json = ? WHERE session_id = ?",
            ('{"request_id": "q1", "prompts": [{"question": 1}]}', "question-session"),
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="is missing required fields"):
        _ = store.load_pending_question(workspace=tmp_path, session_id="question-session")


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
    assert {event.sequence for event in events} == {1, 2}
    assert [event.sequence for event in loaded.events[-2:]] == [1, 2]


def test_session_storage_prunes_terminal_sessions_and_dependent_rows(tmp_path: Path) -> None:
    store = SqliteSessionStore()

    for session_id, status in (
        ("old-terminal", "completed"),
        ("new-terminal", "completed"),
        ("waiting-session", "waiting"),
    ):
        _run_session(
            store,
            tmp_path,
            RuntimeRequest(prompt=session_id, session_id=session_id),
            RuntimeResponse(
                session=SessionState(
                    session=SessionRef(id=session_id),
                    status=cast(Any, status),
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
                output="done" if status == "completed" else None,
            ),
        )

    counts = store.prune_runtime_storage(workspace=tmp_path, keep_sessions=1)

    assert counts["sessions"] == 1
    assert counts["session_events"] == 1
    assert counts["session_notifications"] == 1
    assert [session.session.id for session in store.list_sessions(workspace=tmp_path)] == [
        "waiting-session",
        "new-terminal",
    ]
    with pytest.raises(ValueError, match="unknown session: old-terminal"):
        _ = store.load_session_result(workspace=tmp_path, session_id="old-terminal")


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


def test_session_storage_persists_pending_question_across_store_reopen(
    tmp_path: Path,
) -> None:
    store = SqliteSessionStore()
    request = RuntimeRequest(prompt="need input", session_id="question-reopen-session")
    response = RuntimeResponse(
        session=SessionState(
            session=SessionRef(id="question-reopen-session"),
            status="waiting",
            turn=1,
            metadata={},
        ),
        events=(
            EventEnvelope(
                session_id="question-reopen-session",
                sequence=1,
                event_type="runtime.question_requested",
                source="runtime",
                payload={
                    "request_id": "question-reopen-1",
                    "tool": "question",
                    "question_count": 1,
                    "questions": [
                        {
                            "header": "Runtime path",
                            "question": "Which runtime path should we use?",
                            "multiple": False,
                            "options": [
                                {"label": "Reuse existing", "description": "Keep current path"},
                            ],
                        }
                    ],
                },
            ),
        ),
    )
    pending_question = PendingQuestion(
        request_id="question-reopen-1",
        tool_name="question",
        arguments={},
        prompts=(
            PendingQuestionPrompt(
                question="Which runtime path should we use?",
                header="Runtime path",
                options=(
                    PendingQuestionOption(
                        label="Reuse existing",
                        description="Keep current path",
                    ),
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

    reopened_store = SqliteSessionStore()
    loaded_question = reopened_store.load_pending_question(
        workspace=tmp_path,
        session_id="question-reopen-session",
    )
    checkpoint = reopened_store.load_resume_checkpoint(
        workspace=tmp_path,
        session_id="question-reopen-session",
    )
    notifications = reopened_store.list_notifications(workspace=tmp_path)

    assert loaded_question == pending_question
    assert checkpoint is not None
    assert checkpoint["kind"] == "question_wait"
    assert checkpoint["pending_question_request_id"] == "question-reopen-1"
    assert len(notifications) == 1
    assert notifications[0].kind == "question_blocked"
    assert notifications[0].status == "unread"
    assert notifications[0].payload["request_id"] == "question-reopen-1"


def test_session_storage_persists_pending_approval_across_store_reopen(
    tmp_path: Path,
) -> None:
    store = SqliteSessionStore()
    request = RuntimeRequest(prompt="write guarded file", session_id="approval-reopen-session")
    response = RuntimeResponse(
        session=SessionState(
            session=SessionRef(id="approval-reopen-session"),
            status="waiting",
            turn=1,
            metadata={},
        ),
        events=(
            EventEnvelope(
                session_id="approval-reopen-session",
                sequence=1,
                event_type="runtime.approval_requested",
                source="runtime",
                payload={
                    "request_id": "approval-reopen-1",
                    "tool": "write",
                    "arguments": {"path": "danger.txt"},
                    "target_summary": "write danger.txt",
                    "reason": "write requires approval",
                    "policy": {"mode": "ask"},
                },
            ),
        ),
    )
    pending_approval = PendingApproval(
        request_id="approval-reopen-1",
        tool_name="write",
        arguments={"path": "danger.txt"},
        target_summary="write danger.txt",
        reason="write requires approval",
        request_event_sequence=1,
    )
    store.save_pending_approval(
        workspace=tmp_path,
        request=request,
        response=response,
        pending_approval=pending_approval,
    )

    reopened_store = SqliteSessionStore()
    loaded_approval = reopened_store.load_pending_approval(
        workspace=tmp_path,
        session_id="approval-reopen-session",
    )
    checkpoint = reopened_store.load_resume_checkpoint(
        workspace=tmp_path,
        session_id="approval-reopen-session",
    )
    notifications = reopened_store.list_notifications(workspace=tmp_path)

    assert loaded_approval == pending_approval
    assert checkpoint is not None
    assert checkpoint["kind"] == "approval_wait"
    assert checkpoint["pending_approval_request_id"] == "approval-reopen-1"
    assert len(notifications) == 1
    assert notifications[0].kind == "approval_blocked"
    assert notifications[0].status == "unread"
    assert notifications[0].payload["request_id"] == "approval-reopen-1"


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


def _completed_response(session_id: str, output: str = "done") -> RuntimeResponse:
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
        output=output,
    )


def _create_completed_sessions(store: SqliteSessionStore, workspace: Path, count: int, prefix: str = "s") -> list[str]:
    ids: list[str] = []
    for i in range(count):
        sid = f"{prefix}-{i}"
        store.save_run(
            workspace=workspace,
            request=RuntimeRequest(prompt=sid, session_id=sid),
            response=_completed_response(sid),
        )
        ids.append(sid)
    return ids


def test_list_sessions_auto_prunes_excess_terminal_sessions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = SqliteSessionStore()
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("VOIDCODE_DB_PATH", str(db_path))

    _create_completed_sessions(store, tmp_path, count=53, prefix="excess")

    listed = store.list_sessions(workspace=tmp_path)

    assert len(listed) == 50
    for summary in listed:
        assert summary.status == "completed"


def test_list_sessions_auto_prunes_stale_terminal_sessions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = SqliteSessionStore()
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("VOIDCODE_DB_PATH", str(db_path))

    ids = _create_completed_sessions(store, tmp_path, count=3, prefix="stale")

    with store._connect(tmp_path) as conn:
        conn.execute(
            "UPDATE sessions SET created_at_unix_ms = 1 WHERE workspace_id = ? AND session_id IN (?, ?, ?)",
            (str(tmp_path), ids[0], ids[1], ids[2]),
        )
        conn.commit()

    listed = store.list_sessions(workspace=tmp_path)

    listed_ids = {summary.session.id for summary in listed}
    for sid in ids:
        assert sid not in listed_ids, f"stale session {sid} was not pruned"


def test_list_sessions_auto_prune_preserves_active_sessions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = SqliteSessionStore()
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("VOIDCODE_DB_PATH", str(db_path))

    for sid, status in (
        ("completed-1", "completed"),
        ("completed-2", "completed"),
        ("running-1", "running"),
        ("waiting-1", "waiting"),
        ("failed-1", "failed"),
    ):
        store.save_run(
            workspace=tmp_path,
            request=RuntimeRequest(prompt=sid, session_id=sid),
            response=RuntimeResponse(
                session=SessionState(
                    session=SessionRef(id=sid),
                    status=cast(Any, status),
                    turn=1,
                    metadata={},
                ),
                events=(
                    EventEnvelope(
                        session_id=sid,
                        sequence=1,
                        event_type="graph.response_ready",
                        source="graph",
                    ),
                ),
                output="done" if status in ("completed", "failed") else None,
            ),
        )

    with store._connect(tmp_path) as conn:
        conn.execute(
            "UPDATE sessions SET created_at_unix_ms = 1 WHERE workspace_id = ?",
            (str(tmp_path),),
        )
        conn.commit()

    listed = store.list_sessions(workspace=tmp_path)

    listed_ids = {summary.session.id for summary in listed}
    assert "completed-1" not in listed_ids
    assert "completed-2" not in listed_ids
    assert "failed-1" not in listed_ids
    assert "running-1" in listed_ids
    assert "waiting-1" in listed_ids


def test_list_sessions_auto_prune_cascades_to_child_tables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = SqliteSessionStore()
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("VOIDCODE_DB_PATH", str(db_path))

    sid = "cascade-session"
    store.save_run(
        workspace=tmp_path,
        request=RuntimeRequest(prompt=sid, session_id=sid),
        response=_completed_response(sid),
    )

    with store._connect(tmp_path) as conn:
        conn.execute(
            "UPDATE sessions SET created_at_unix_ms = 1 WHERE workspace_id = ? AND session_id = ?",
            (str(tmp_path), sid),
        )
        conn.commit()

    _ = store.list_sessions(workspace=tmp_path)

    with store._connect(tmp_path) as conn:
        events_count = conn.execute(
            "SELECT COUNT(*) FROM session_events WHERE workspace_id = ? AND session_id = ?",
            (str(tmp_path), sid),
        ).fetchone()[0]
        assert events_count == 0, f"session_events not cleaned up: {events_count} rows remain"

    with pytest.raises(ValueError, match=f"unknown session: {sid}"):
        _ = store.load_session_result(workspace=tmp_path, session_id=sid)


def _seed_running_session(store: SqliteSessionStore, workspace: Path, session_id: str) -> None:
    store.save_run(
        workspace=workspace,
        request=RuntimeRequest(prompt=session_id, session_id=session_id),
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
    store.append_session_events(
        workspace=workspace,
        session_id=session_id,
        events=(("graph.response_ready", "graph", {}, None),),
    )


def test_session_storage_bulk_append_events_assigns_contiguous_sequences(tmp_path: Path) -> None:
    store = SqliteSessionStore(database_path=tmp_path / "sessions.sqlite3")
    _seed_running_session(store, tmp_path, "bulk-session")

    envelopes = store.append_session_events(
        workspace=tmp_path,
        session_id="bulk-session",
        events=(
            ("runtime.mcp_server_released", "runtime", {"server": "a"}, "bulk-1"),
            ("runtime.mcp_server_stopped", "runtime", {"server": "b"}, "bulk-2"),
            ("runtime.acp_connected", "runtime", {}, "bulk-3"),
        ),
    )
    loaded = store.load_session(workspace=tmp_path, session_id="bulk-session")

    assert [envelope.sequence for envelope in envelopes] == [2, 3, 4]
    assert [envelope.event_type for envelope in envelopes] == [
        "runtime.mcp_server_released",
        "runtime.mcp_server_stopped",
        "runtime.acp_connected",
    ]
    assert [event.sequence for event in loaded.events] == [1, 2, 3, 4]
    assert loaded.events[1:] == tuple(envelopes)


def test_session_storage_bulk_append_dedupes_within_batch(tmp_path: Path) -> None:
    store = SqliteSessionStore(database_path=tmp_path / "sessions.sqlite3")
    _seed_running_session(store, tmp_path, "bulk-dedupe-session")

    envelopes = store.append_session_events(
        workspace=tmp_path,
        session_id="bulk-dedupe-session",
        events=(
            ("runtime.mcp_server_released", "runtime", {"server": "a"}, "dup-key"),
            ("runtime.mcp_server_stopped", "runtime", {"server": "b"}, "dup-key"),
            ("runtime.acp_connected", "runtime", {}, "fresh-key"),
        ),
    )
    loaded = store.load_session(workspace=tmp_path, session_id="bulk-dedupe-session")

    assert [envelope.sequence for envelope in envelopes] == [2, 3]
    assert [envelope.event_type for envelope in envelopes] == [
        "runtime.mcp_server_released",
        "runtime.acp_connected",
    ]
    assert [event.sequence for event in loaded.events] == [1, 2, 3]


def test_session_storage_bulk_append_rejects_non_lifecycle_event_on_terminal_session(tmp_path: Path) -> None:
    store = SqliteSessionStore(database_path=tmp_path / "sessions.sqlite3")
    store.save_run(
        workspace=tmp_path,
        request=RuntimeRequest(prompt="sealed", session_id="sealed-session"),
        response=_completed_response("sealed-session"),
    )

    with pytest.raises(SessionSealedError, match="sealed-session.*runtime.tool_started"):
        _ = store.append_session_events(
            workspace=tmp_path,
            session_id="sealed-session",
            events=(("runtime.tool_started", "runtime", {"tool": "write"}, None),),
        )


def test_session_storage_bulk_append_allows_lifecycle_event_on_terminal_session(tmp_path: Path) -> None:
    store = SqliteSessionStore(database_path=tmp_path / "sessions.sqlite3")
    store.save_run(
        workspace=tmp_path,
        request=RuntimeRequest(prompt="sealed", session_id="sealed-session"),
        response=_completed_response("sealed-session"),
    )

    envelopes = store.append_session_events(
        workspace=tmp_path,
        session_id="sealed-session",
        events=(("runtime.background_task_completed", "runtime", {"task_id": "task-1"}, "bt-finalize-1"),),
    )

    assert [envelope.sequence for envelope in envelopes] == [1]
    assert [envelope.event_type for envelope in envelopes] == ["runtime.background_task_completed"]


def test_session_storage_bulk_append_seal_is_atomic_on_mixed_batch(tmp_path: Path) -> None:
    store = SqliteSessionStore(database_path=tmp_path / "sessions.sqlite3")
    _run_session(
        store,
        tmp_path,
        RuntimeRequest(prompt="sealed", session_id="sealed-session"),
        _completed_response("sealed-session"),
    )

    with pytest.raises(SessionSealedError, match="sealed-session.*runtime.tool_started"):
        _ = store.append_session_events(
            workspace=tmp_path,
            session_id="sealed-session",
            events=(
                ("runtime.background_task_completed", "runtime", {"task_id": "task-1"}, "bt-finalize-1"),
                ("runtime.tool_started", "runtime", {"tool": "write"}, None),
            ),
        )

    loaded = store.load_session(workspace=tmp_path, session_id="sealed-session")
    assert [event.sequence for event in loaded.events] == [1]


def test_session_storage_bulk_append_upserts_interrupted_checkpoint(tmp_path: Path) -> None:
    store = SqliteSessionStore(database_path=tmp_path / "sessions.sqlite3")
    _seed_running_session(store, tmp_path, "interrupt-session")

    store.append_session_events(
        workspace=tmp_path,
        session_id="interrupt-session",
        events=(("runtime.mcp_server_released", "runtime", {"server": "a"}, "interrupt-1"),),
        interrupted_checkpoint={"kind": "interrupted", "version": 1, "prompt": "interrupted task"},
    )

    checkpoint = store.load_resume_checkpoint(workspace=tmp_path, session_id="interrupt-session")
    loaded = store.load_session(workspace=tmp_path, session_id="interrupt-session")

    assert checkpoint is not None
    assert checkpoint["kind"] == "interrupted"
    assert loaded.session.status == "interrupted"


def test_session_storage_bulk_append_raises_unknown_session(tmp_path: Path) -> None:
    store = SqliteSessionStore(database_path=tmp_path / "sessions.sqlite3")

    with pytest.raises(UnknownSessionError, match="unknown session: nope"):
        _ = store.append_session_events(
            workspace=tmp_path,
            session_id="nope",
            events=(("runtime.mcp_server_released", "runtime", {}, None),),
        )


def test_session_storage_save_interrupted_checkpoint_creates_row_and_roundtrips(
    tmp_path: Path,
) -> None:
    store = SqliteSessionStore(database_path=tmp_path / "sessions.sqlite3")
    tool_results: tuple[dict[str, object], ...] = (
        {
            "tool_name": "read",
            "status": "ok",
            "data": {"tool": "read", "status": "ok", "content": "alpha\n"},
            "content": "alpha\n",
            "error": None,
        },
    )

    store.save_interrupted_checkpoint(
        workspace=tmp_path,
        session_id="interrupt-session",
        prompt="interrupt me",
        session_metadata={"mode": "plan", "read_only": True},
        tool_results=tool_results,
        last_event_sequence=4,
        output="partial output",
    )

    loaded = store.load_session(workspace=tmp_path, session_id="interrupt-session")
    checkpoint = store.load_resume_checkpoint(workspace=tmp_path, session_id="interrupt-session")

    assert loaded.session.status == "interrupted"
    assert loaded.events == ()
    assert checkpoint is not None
    assert checkpoint["kind"] == "interrupted"
    assert checkpoint["session_status"] == "interrupted"
    assert checkpoint["prompt"] == "interrupt me"
    assert checkpoint["session_metadata"] == {"mode": "plan", "read_only": True}
    assert checkpoint["tool_results"] == list(tool_results)
    assert checkpoint["last_event_sequence"] == 4
    assert checkpoint["output"] == "partial output"


def test_session_storage_save_interrupted_checkpoint_updates_without_events_or_output_clobber(
    tmp_path: Path,
) -> None:
    store = SqliteSessionStore(database_path=tmp_path / "sessions.sqlite3")

    store.save_interrupted_checkpoint(
        workspace=tmp_path,
        session_id="interrupt-session",
        prompt="first prompt",
        session_metadata={},
        tool_results=(),
        last_event_sequence=1,
        output="first output",
    )

    store.save_interrupted_checkpoint(
        workspace=tmp_path,
        session_id="interrupt-session",
        prompt="second prompt",
        session_metadata={},
        tool_results=(),
        last_event_sequence=1,
        output=None,
    )

    loaded = store.load_session(workspace=tmp_path, session_id="interrupt-session")
    checkpoint = store.load_resume_checkpoint(workspace=tmp_path, session_id="interrupt-session")

    assert loaded.session.status == "interrupted"
    assert loaded.output == "first output"
    assert loaded.events == ()
    assert checkpoint is not None
    assert checkpoint["prompt"] == "second prompt"
    assert checkpoint["output"] is None


def test_session_storage_truncate_session_events_after_deletes_only_tail(tmp_path: Path) -> None:
    store = SqliteSessionStore(database_path=tmp_path / "sessions.sqlite3")
    _seed_running_session(store, tmp_path, "truncate-session")
    _ = store.append_session_events(
        workspace=tmp_path,
        session_id="truncate-session",
        events=(
            ("runtime.mcp_server_released", "runtime", {"server": "a"}, "truncate-1"),
            ("runtime.mcp_server_stopped", "runtime", {"server": "b"}, "truncate-2"),
            ("runtime.acp_connected", "runtime", {}, "truncate-3"),
        ),
    )

    store.truncate_session_events_after(workspace=tmp_path, session_id="truncate-session", sequence=2)

    loaded = store.load_session(workspace=tmp_path, session_id="truncate-session")

    assert [event.sequence for event in loaded.events] == [1, 2]


def test_session_storage_truncate_resets_last_event_sequence_watermark(tmp_path: Path) -> None:
    store = SqliteSessionStore(database_path=tmp_path / "sessions.sqlite3")
    _seed_running_session(store, tmp_path, "truncate-watermark-session")
    _ = store.append_session_events(
        workspace=tmp_path,
        session_id="truncate-watermark-session",
        events=(
            ("runtime.mcp_server_released", "runtime", {"server": "a"}, "watermark-1"),
            ("runtime.mcp_server_stopped", "runtime", {"server": "b"}, "watermark-2"),
            ("runtime.acp_connected", "runtime", {}, "watermark-3"),
        ),
    )

    store.truncate_session_events_after(workspace=tmp_path, session_id="truncate-watermark-session", sequence=2)

    loaded = store.load_session(workspace=tmp_path, session_id="truncate-watermark-session")
    assert [event.sequence for event in loaded.events] == [1, 2]

    # The next append must continue contiguously from the truncation point (3),
    # not from the stale pre-truncation watermark (5).
    resumed = store.append_session_events(
        workspace=tmp_path,
        session_id="truncate-watermark-session",
        events=(("runtime.tool_completed", "runtime", {"tool": "read"}, None),),
    )
    assert [envelope.sequence for envelope in resumed] == [3]


def test_session_storage_list_sessions_shows_interrupted_session(tmp_path: Path) -> None:
    store = SqliteSessionStore(database_path=tmp_path / "sessions.sqlite3")

    store.save_interrupted_checkpoint(
        workspace=tmp_path,
        session_id="interrupt-session",
        prompt="interrupt me",
        session_metadata={},
        tool_results=(),
        last_event_sequence=0,
    )

    listed = store.list_sessions(workspace=tmp_path)

    assert [summary.session.id for summary in listed] == ["interrupt-session"]
    assert listed[0].status == "interrupted"
