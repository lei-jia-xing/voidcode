from __future__ import annotations

import json
import shutil
import sqlite3
import threading
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from tests.runtime_composition import create_task, save_checkpoint
from tests.runtime_composition import save_run as save_composition_run
from voidcode.core.questions import PendingQuestionOption, PendingQuestionPrompt
from voidcode.runtime.background.models import (
    BackgroundTaskRef,
    BackgroundTaskRequestSnapshot,
    BackgroundTaskState,
)
from voidcode.runtime.contracts import RuntimeRequest, RuntimeResponse, UnknownSessionError
from voidcode.runtime.events import EventEnvelope
from voidcode.runtime.paths import sessions_db_path, state_home
from voidcode.runtime.permission import PLAN_MODE_DENIAL_REASON, PendingApproval
from voidcode.runtime.question import PendingQuestion
from voidcode.runtime.session import SessionRef, SessionState
from voidcode.runtime.storage import SCHEMA_VERSION, SessionSealedError, SqliteSessionStore


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
    save_composition_run(
        store,
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
    save_composition_run(store, workspace=workspace, request=request, response=response)


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


def test_store_constructed_without_database_path_pins_its_resolved_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A store resolves its database path once, at construction.

    The autouse isolation fixtures restore ``VOIDCODE_DB_PATH``/``XDG_STATE_HOME``
    when a test ends, but a background-task worker the test dispatched can still
    be running its final durable writes at that moment. A store that re-resolved
    the environment per operation would retarget the developer's real database
    there; resolving once keeps it bound to the database it was built for.
    """
    database_path = tmp_path / "pinned" / "sessions.sqlite3"
    monkeypatch.setenv("VOIDCODE_DB_PATH", str(database_path))
    store = SqliteSessionStore()
    assert store._resolve_database_path() == database_path

    # Simulate the isolation override being undone while the store is still live.
    monkeypatch.delenv("VOIDCODE_DB_PATH")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "host-state"))

    assert SqliteSessionStore()._resolve_database_path() == (tmp_path / "host-state" / "voidcode" / "sessions.sqlite3")
    assert store._resolve_database_path() == database_path

    save_composition_run(
        store,
        workspace=tmp_path,
        request=RuntimeRequest(prompt="pinned", session_id="pinned-session"),
        response=_completed_response("pinned-session"),
    )
    assert store.load_session(workspace=tmp_path, session_id="pinned-session").session.status == "completed"
    assert not (tmp_path / "host-state" / "voidcode").exists()


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
    save_checkpoint(
        store,
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
    save_composition_run(store, workspace=tmp_path, request=request, response=response)

    loaded = store.load_session(workspace=tmp_path, session_id="child-session")
    listed = store.list_sessions(workspace=tmp_path)
    result = store.load_session_result(workspace=tmp_path, session_id="child-session")

    assert loaded.session.session.parent_id == "leader-session"
    assert listed[0].session.parent_id == "leader-session"
    assert result.session.session.parent_id == "leader-session"


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
        "runtime_state": {"run_id": "policy-run"},
        "injected_env": {"CI": "1", "NPM_CONFIG_YES": "true"},
        "api_key": "sk-runtime-secret",
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
                        "reason": PLAN_MODE_DENIAL_REASON,
                    },
                },
            ),
        ),
        output=None,
    )

    save_composition_run(store, workspace=tmp_path, request=request, response=response)

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


def test_session_storage_reads_events_after_cursor_once_and_honors_active_revert(tmp_path: Path) -> None:
    """The incremental read is the replay-visible transcript after a cursor.

    A follow client already replayed the session, so later reads return each
    event after the cursor exactly once, in order, together with the row status
    — undecorated and unfiltered, because replay is path-scoped, not a marker.
    """
    store = SqliteSessionStore()
    request = RuntimeRequest(prompt="follow me", session_id="events-after-session")
    response = RuntimeResponse(
        session=SessionState(
            session=SessionRef(id="events-after-session"),
            status="completed",
            turn=1,
            metadata={},
        ),
        events=tuple(
            EventEnvelope(
                session_id="events-after-session",
                sequence=sequence,
                event_type="graph.provider_stream",
                source="graph",
                payload={"sequence": sequence},
            )
            for sequence in (1, 2, 3)
        ),
        output="done",
    )
    _run_session(store, tmp_path, request, response)

    tail = store.read_session_events_after(workspace=tmp_path, session_id="events-after-session", after_sequence=1)
    assert tail.status == "completed"
    assert [event.sequence for event in tail.events] == [2, 3]
    assert store.read_session_events_after(workspace=tmp_path, session_id="events-after-session", after_sequence=3).events == ()
    assert store.read_session_events_after(workspace=tmp_path, session_id="events-after-session", after_sequence=0).events == response.events


def test_session_storage_reads_events_after_unknown_session(tmp_path: Path) -> None:
    store = SqliteSessionStore()

    with pytest.raises(UnknownSessionError):
        store.read_session_events_after(workspace=tmp_path, session_id="missing-session", after_sequence=0)


def test_session_storage_persists_runtime_todos(
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
                        "version": 2,
                        "revision": 3,
                        "phases": [
                            {
                                "name": "Tasks",
                                "tasks": [{"content": "persist me", "status": "in_progress"}],
                            }
                        ],
                        "summary": {
                            "total": 1,
                            "pending": 0,
                            "in_progress": 1,
                            "completed": 0,
                            "abandoned": 0,
                            "blocked": 0,
                            "active": 1,
                        },
                    },
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
                    "phases": [{"name": "Tasks", "tasks": [{"content": "persist me", "status": "in_progress"}]}],
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
    phases = todos_state_payload.get("phases")
    assert isinstance(phases, list)
    tasks = cast(dict[str, object], cast(dict[str, object], phases[0])["tasks"])
    assert tasks[0]["content"] == "persist me"


def test_session_storage_newest_sequence_before_is_the_revert_target(tmp_path: Path) -> None:
    """``newest_sequence_before`` is the position a revert-to-S continues from.

    A revert to ``S`` under a tree is a checkout of the newest entry on the
    current path with ``sequence < S`` — the newest predecessor, not ``S``
    itself, and never an entry on an abandoned branch.
    """
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

    assert store.newest_sequence_before(workspace=tmp_path, session_id="multi-turn-session", sequence=3) == 2
    assert store.newest_sequence_before(workspace=tmp_path, session_id="multi-turn-session", sequence=1) is None


def test_session_storage_preserves_prior_events_when_same_session_continues(
    tmp_path: Path,
) -> None:
    store = SqliteSessionStore()
    session_id = "continued-session"
    # Simulate the incremental run loop: create a running row, then append two
    # batches of events before the terminal save_run seals the session.
    save_composition_run(
        store,
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

    save_composition_run(
        store,
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
    save_composition_run(
        store,
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

    save_composition_run(store, workspace=tmp_path, request=request, response=response)

    with closing(sqlite3.connect(database_path)) as connection:
        session_columns = [row[1] for row in connection.execute("PRAGMA table_info(sessions)").fetchall()]
        delivery_columns = [row[1] for row in connection.execute("PRAGMA table_info(session_event_deliveries)").fetchall()]
        schema_version = connection.execute("PRAGMA user_version").fetchone()[0]
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
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
        "leaf_sequence",
        "created_at_unix_ms",
        "title",
        "forked_from_session_id",
        "forked_at_sequence",
    ]
    assert delivery_columns == ["workspace_id", "session_id", "dedupe_key", "delivered_at", "event_sequence"]
    assert schema_version == SCHEMA_VERSION
    assert "session_todos" not in tables
    assert "memories" not in tables
    assert "memory_tags" not in tables
    assert "memory_recall_log" not in tables
    assert "memory_index_status" not in tables


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
    save_composition_run(store, workspace=tmp_path, request=old_request, response=old_response)
    create_task(
        store,
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
    save_composition_run(store, workspace=tmp_path, request=new_request, response=new_response)
    create_task(
        store,
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
    assert new_session_row == (41, 52)
    assert new_task_row == (71, 71)


def test_session_storage_bootstraps_schema_once_per_database_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the first connection to a database file runs the schema bootstrap.

    Regression guard: ``_connect`` used to run ``_ensure_schema`` (canonical
    shape verification, storage-sequence floors, and one ``CREATE TABLE IF NOT
    EXISTS`` per table) for every connection it opened, i.e. on every storage
    read — including every API request on the asyncio event loop. Warm
    connections must only pay the cheap sentinel reads that prove the file is
    still the verified one.
    """
    database_path = tmp_path / "bootstrap-once.sqlite3"
    store = SqliteSessionStore(database_path=database_path)
    original_ensure_schema = SqliteSessionStore._ensure_schema
    bootstrapped: list[Path] = []

    def _counting_ensure_schema(self: Any, *, connection: sqlite3.Connection, database_path: Path) -> None:
        bootstrapped.append(database_path)
        return original_ensure_schema(self, connection=connection, database_path=database_path)

    monkeypatch.setattr(SqliteSessionStore, "_ensure_schema", _counting_ensure_schema)

    store.list_sessions(workspace=tmp_path)
    assert bootstrapped == [database_path]

    store.list_sessions(workspace=tmp_path)
    create_task(
        store,
        workspace=tmp_path,
        task=BackgroundTaskState(
            task=BackgroundTaskRef(id="once-task"),
            request=BackgroundTaskRequestSnapshot(prompt="once"),
        ),
    )
    store.list_sessions(workspace=tmp_path)

    assert bootstrapped == [database_path]


def test_session_storage_reverifies_schema_after_version_change_with_warm_cache(
    tmp_path: Path,
) -> None:
    """A warm bootstrap cache must never mask a real schema mismatch.

    ``user_version`` is the connection-level authority: bumping it out of band
    (a migration by another process, or a downgrade) must re-enter the fail-fast
    path even though this process already verified the file.
    """
    database_path = tmp_path / "warm-version-mismatch.sqlite3"
    store = SqliteSessionStore(database_path=database_path)
    store.list_sessions(workspace=tmp_path)

    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute("PRAGMA user_version = 999")
        connection.commit()

    with pytest.raises(RuntimeError, match="schema version mismatch"):
        store.list_sessions(workspace=tmp_path)


def test_session_storage_reverifies_replaced_database_file_with_warm_cache(tmp_path: Path) -> None:
    """A replaced database file must not inherit the previous file's verification.

    Both paths keep the file's inode (``shutil.copyfile`` truncates in place, like
    ``cp``), so only the connection's own sentinel reads — the reported
    ``user_version``, the schema cookie, and the sequence rows — can catch the
    swap. Either way the next read must fail fast instead of trusting the cache.
    """
    database_path = tmp_path / "replaced-database.sqlite3"
    store = SqliteSessionStore(database_path=database_path)
    store.list_sessions(workspace=tmp_path)

    def _replace_with(source: Path) -> None:
        for suffix in ("-wal", "-shm"):
            Path(f"{database_path}{suffix}").unlink(missing_ok=True)
        shutil.copyfile(source, database_path)

    foreign_version = tmp_path / "foreign-version.sqlite3"
    with closing(sqlite3.connect(foreign_version)) as connection:
        connection.execute("CREATE TABLE sessions (session_id TEXT PRIMARY KEY, workspace TEXT NOT NULL)")
        connection.execute("PRAGMA user_version = 999")
        connection.commit()
    _replace_with(foreign_version)

    with pytest.raises(RuntimeError, match="schema version mismatch"):
        store.list_sessions(workspace=tmp_path)

    foreign_same_version = tmp_path / "foreign-same-version.sqlite3"
    with closing(sqlite3.connect(foreign_same_version)) as connection:
        connection.execute("CREATE TABLE sessions (session_id TEXT PRIMARY KEY, workspace TEXT NOT NULL)")
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        connection.commit()
    _replace_with(foreign_same_version)

    with pytest.raises(RuntimeError, match=r"table 'sessions' missing columns"):
        store.list_sessions(workspace=tmp_path)


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
        "session_events": 0,
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

    with pytest.raises(RuntimeError):
        store.list_sessions(workspace=tmp_path)

    with closing(sqlite3.connect(database_path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert tables == {"sessions"}


def test_session_storage_rejects_runtime_schema_version_mismatch(tmp_path: Path) -> None:
    database_path = tmp_path / "future-runtime.sqlite3"
    with closing(sqlite3.connect(database_path)) as connection:
        _ = connection.execute("PRAGMA user_version = 999")
        connection.commit()

    store = SqliteSessionStore(database_path=database_path)

    with pytest.raises(RuntimeError):
        store.list_sessions(workspace=tmp_path)


def test_session_storage_refuses_previous_schema_before_bootstrap(tmp_path: Path) -> None:
    database_path = tmp_path / "previous-runtime.sqlite3"
    with closing(sqlite3.connect(database_path)) as connection:
        _ = connection.execute("CREATE TABLE sessions (session_id TEXT PRIMARY KEY)")
        _ = connection.execute("PRAGMA user_version = 1")
        connection.commit()

    store = SqliteSessionStore(database_path=database_path)
    with pytest.raises(RuntimeError):
        store.list_sessions(workspace=tmp_path)

    with closing(sqlite3.connect(database_path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert tables == {"sessions"}


@pytest.mark.parametrize("version", [SCHEMA_VERSION - 1, SCHEMA_VERSION + 1])
def test_session_storage_refuses_version_mismatch_without_touching_database_or_sidecars(
    tmp_path: Path,
    version: int,
) -> None:
    database_path = tmp_path / f"unsupported-{version}.sqlite3"
    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute("CREATE TABLE sentinel (value TEXT NOT NULL)")
        connection.execute(f"PRAGMA user_version = {version}")
        connection.commit()
    sidecars = (Path(f"{database_path}-wal"), Path(f"{database_path}-shm"))
    before = (database_path.read_bytes(), database_path.stat().st_mtime_ns, tuple(path.exists() for path in sidecars))

    with pytest.raises(RuntimeError, match="schema version"):
        SqliteSessionStore(database_path=database_path).list_sessions(workspace=tmp_path)

    assert (database_path.read_bytes(), database_path.stat().st_mtime_ns, tuple(path.exists() for path in sidecars)) == before


def test_session_storage_rejects_non_canonical_schema_missing_runtime_columns(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "invalid-sessions.sqlite3"
    with closing(sqlite3.connect(database_path)) as connection:
        _ = connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
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


def test_session_storage_rejects_non_canonical_schema_with_wrong_existing_table_shape(tmp_path: Path) -> None:
    database_path = tmp_path / "wrong-table-shape.sqlite3"
    store = SqliteSessionStore(database_path=database_path)
    store.list_sessions(workspace=tmp_path)
    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute("ALTER TABLE session_event_deliveries DROP COLUMN event_sequence")
        connection.commit()
    with pytest.raises(RuntimeError, match="table 'session_event_deliveries' missing columns: event_sequence"):
        store.list_sessions(workspace=tmp_path)
    with closing(sqlite3.connect(database_path)) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(session_event_deliveries)")}
    assert "event_sequence" not in columns


def test_session_storage_schema_mismatch_errors_split_three_ways(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(SqliteSessionStore, "_SCHEMA_VERSION", SCHEMA_VERSION + 1)
    old_version_db = tmp_path / "needs-upgrade.sqlite3"
    with closing(sqlite3.connect(old_version_db)) as connection:
        _ = connection.execute("CREATE TABLE sessions (session_id TEXT PRIMARY KEY)")
        _ = connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        connection.commit()

    store = SqliteSessionStore(database_path=old_version_db)
    with pytest.raises(RuntimeError) as needs_upgrade_exc:
        store.list_sessions(workspace=tmp_path)
    needs_upgrade_message = str(needs_upgrade_exc.value)
    assert "sqlite runtime schema upgrade required" in needs_upgrade_message
    assert "no schema migrations" in needs_upgrade_message
    assert "point VOIDCODE_DB_PATH" in needs_upgrade_message
    assert needs_upgrade_message.lower().count("reset") == 1
    assert "last resort" in needs_upgrade_message

    too_new_db = tmp_path / "too-new.sqlite3"
    with closing(sqlite3.connect(too_new_db)) as connection:
        _ = connection.execute("PRAGMA user_version = 999")
        connection.commit()
    store = SqliteSessionStore(database_path=too_new_db)
    with pytest.raises(RuntimeError) as too_new_exc:
        store.list_sessions(workspace=tmp_path)
    too_new_message = str(too_new_exc.value)
    assert "sqlite runtime schema is too new" in too_new_message
    assert "changed shape without a migration" in too_new_message
    assert "start from a new database path" in too_new_message

    corrupt_db = tmp_path / "corrupt-shape.sqlite3"
    with closing(sqlite3.connect(corrupt_db)) as connection:
        _ = connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        _ = connection.execute("CREATE TABLE sessions (session_id TEXT PRIMARY KEY)")
        connection.commit()
    store = SqliteSessionStore(database_path=corrupt_db)
    with pytest.raises(RuntimeError) as corrupt_exc:
        store.list_sessions(workspace=tmp_path)
    corrupt_message = str(corrupt_exc.value)
    assert "sqlite runtime schema mismatch" in corrupt_message
    assert f"backup '{corrupt_db}' plus matching -wal/-shm files" in corrupt_message
    assert "`uv run voidcode storage reset`" in corrupt_message


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
                    "diagnostics": {"kind": "stale_edit"},
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


def test_session_storage_checkpoint_rejects_unmatched_request_prompt() -> None:
    build_checkpoint: Any = _private_attr(SqliteSessionStore, "_resume_checkpoint_base")
    response = RuntimeResponse(
        session=SessionState(
            session=SessionRef(id="checkpoint-mismatch"),
            status="failed",
            turn=1,
            metadata={},
        ),
        events=(
            EventEnvelope(
                session_id="checkpoint-mismatch",
                sequence=1,
                event_type="runtime.request_received",
                source="runtime",
                payload={"prompt": "older prompt"},
            ),
            EventEnvelope(
                session_id="checkpoint-mismatch",
                sequence=2,
                event_type="runtime.tool_completed",
                source="tool",
                payload={"tool": "read", "status": "ok", "content": "old"},
            ),
        ),
    )

    with pytest.raises(ValueError, match="cannot identify current turn request"):
        build_checkpoint(
            request=RuntimeRequest(prompt="current prompt", session_id="checkpoint-mismatch"),
            response=response,
            kind="provider_failure_retryable",
        )


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
    save_composition_run(store, workspace=tmp_path, request=request, response=response)

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
    save_composition_run(store, workspace=tmp_path, request=request, response=response)

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
    save_composition_run(store, workspace=tmp_path, request=request, response=response)

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
    save_composition_run(
        store,
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
    save_composition_run(
        store,
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
    save_composition_run(store, workspace=tmp_path, request=request, response=response)

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
    assert [session.session.id for session in store.list_sessions(workspace=tmp_path)] == [
        "waiting-session",
        "new-terminal",
    ]
    with pytest.raises(ValueError, match="unknown session: old-terminal"):
        _ = store.load_session_result(workspace=tmp_path, session_id="old-terminal")


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
    save_checkpoint(
        store,
        workspace=tmp_path,
        session_id="question-reopen-session",
        prompt=request.prompt,
        session_metadata=response.session.metadata,
        tool_results=(),
        last_event_sequence=0,
    )
    composition_ref = store.load_session(workspace=tmp_path, session_id="question-reopen-session").session.metadata["composition_ref"]
    response = replace(
        response,
        session=replace(response.session, metadata={**response.session.metadata, "composition_ref": composition_ref}),
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

    assert loaded_question == pending_question
    assert checkpoint is not None
    assert checkpoint["kind"] == "question_wait"
    assert checkpoint["pending_question_request_id"] == "question-reopen-1"


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
    save_checkpoint(
        store,
        workspace=tmp_path,
        session_id="approval-reopen-session",
        prompt=request.prompt,
        session_metadata=response.session.metadata,
        tool_results=(),
        last_event_sequence=0,
    )
    composition_ref = store.load_session(workspace=tmp_path, session_id="approval-reopen-session").session.metadata["composition_ref"]
    response = replace(
        response,
        session=replace(response.session, metadata={**response.session.metadata, "composition_ref": composition_ref}),
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

    assert loaded_approval == pending_approval
    assert checkpoint is not None
    assert checkpoint["kind"] == "approval_wait"
    assert checkpoint["pending_approval_request_id"] == "approval-reopen-1"


def test_session_storage_fail_incomplete_background_tasks_keeps_question_waiting_children(
    tmp_path: Path,
) -> None:
    store = SqliteSessionStore()
    task_id = "task-question"
    child_session_id = "child-question-session"
    metadata = {"background_run": True, "background_task_id": task_id}
    task = create_task(
        store,
        workspace=tmp_path,
        task=BackgroundTaskState(
            task=BackgroundTaskRef(id=task_id),
            status="running",
            request=BackgroundTaskRequestSnapshot(
                prompt="need input",
                session_id=child_session_id,
                parent_session_id="leader-session",
                metadata=metadata,
            ),
            session_id=child_session_id,
            created_at=1,
            updated_at=1,
            started_at=1,
        ),
    )
    metadata["composition_ref"] = task.request.metadata["composition_ref"]
    child_request = RuntimeRequest(
        prompt="need input",
        session_id=child_session_id,
        parent_session_id="leader-session",
        metadata=metadata,
    )
    child_response = RuntimeResponse(
        session=SessionState(
            session=SessionRef(id=child_session_id, parent_id="leader-session"),
            status="waiting",
            turn=1,
            metadata=metadata,
        ),
        events=(
            EventEnvelope(
                session_id=child_session_id,
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
    save_checkpoint(
        store,
        workspace=tmp_path,
        session_id=child_session_id,
        prompt=child_request.prompt,
        session_metadata=metadata,
        tool_results=(),
        last_event_sequence=0,
        parent_session_id="leader-session",
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
                ),
            ),
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
        save_composition_run(
            store,
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
        save_composition_run(
            store,
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
    save_composition_run(
        store,
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
    save_checkpoint(
        store,
        workspace=workspace,
        session_id=session_id,
        prompt=session_id,
        session_metadata={},
        tool_results=(),
        last_event_sequence=0,
    )
    store.append_session_events(
        workspace=workspace,
        session_id=session_id,
        events=(("graph.response_ready", "graph", {}, None),),
    )


def test_session_storage_tree_links_sequential_appends(tmp_path: Path) -> None:
    """A first event is a root and sets the leaf; the next one follows it."""
    store = SqliteSessionStore(database_path=tmp_path / "sessions.sqlite3")
    _seed_running_session(store, tmp_path, "tree-session")

    _ = store.append_session_events(
        workspace=tmp_path,
        session_id="tree-session",
        events=(("runtime.mcp_server_acquired", "runtime", {}, None),),
    )

    with closing(sqlite3.connect(tmp_path / "sessions.sqlite3")) as connection:
        rows = connection.execute(
            "SELECT sequence, parent_sequence FROM session_events WHERE session_id = ? ORDER BY sequence",
            ("tree-session",),
        ).fetchall()
        leaf = connection.execute("SELECT leaf_sequence FROM sessions WHERE session_id = ?", ("tree-session",)).fetchone()[0]

    assert rows == [(1, None), (2, 1)]
    assert leaf == 2

    # The terminal seal's ``INSERT OR REPLACE`` rewrites every sessions column,
    # so it must carry the position rather than reset it.
    save_composition_run(
        store,
        workspace=tmp_path,
        request=RuntimeRequest(prompt="tree-session", session_id="tree-session"),
        response=RuntimeResponse(
            session=SessionState(session=SessionRef(id="tree-session"), status="completed", turn=1, metadata={}),
            events=(),
            output="done",
        ),
    )
    with closing(sqlite3.connect(tmp_path / "sessions.sqlite3")) as connection:
        leaf_after_seal = connection.execute("SELECT leaf_sequence FROM sessions WHERE session_id = ?", ("tree-session",)).fetchone()[0]
    assert leaf_after_seal == 2


def test_session_storage_bulk_append_chains_the_batch(tmp_path: Path) -> None:
    """Each row in a multi-event append follows the previous one, not the seed."""
    store = SqliteSessionStore(database_path=tmp_path / "sessions.sqlite3")
    _seed_running_session(store, tmp_path, "bulk-chain-session")

    _ = store.append_session_events(
        workspace=tmp_path,
        session_id="bulk-chain-session",
        events=(
            ("runtime.mcp_server_acquired", "runtime", {"server": "a"}, None),
            ("runtime.mcp_server_stopped", "runtime", {"server": "b"}, None),
            ("runtime.acp_connected", "runtime", {}, None),
        ),
    )

    with closing(sqlite3.connect(tmp_path / "sessions.sqlite3")) as connection:
        rows = connection.execute(
            "SELECT sequence, parent_sequence FROM session_events WHERE session_id = ? ORDER BY sequence",
            ("bulk-chain-session",),
        ).fetchall()
        leaf = connection.execute("SELECT leaf_sequence FROM sessions WHERE session_id = ?", ("bulk-chain-session",)).fetchone()[0]

    assert rows == [(1, None), (2, 1), (3, 2), (4, 3)]
    assert leaf == 4


def test_session_storage_fork_copies_parents_and_leaves_leaf_at_boundary(tmp_path: Path) -> None:
    """The prefix copy keeps each parent verbatim; the fork leaf is its boundary."""
    store = SqliteSessionStore(database_path=tmp_path / "sessions.sqlite3")
    _seed_running_session(store, tmp_path, "fork-tree-source")
    _ = store.append_session_events(
        workspace=tmp_path,
        session_id="fork-tree-source",
        events=(
            ("runtime.mcp_server_acquired", "runtime", {}, None),
            ("runtime.acp_connected", "runtime", {}, None),
        ),
    )

    forked = store.fork_session(workspace=tmp_path, session_id="fork-tree-source")

    with closing(sqlite3.connect(tmp_path / "sessions.sqlite3")) as connection:
        fork_rows = connection.execute(
            "SELECT sequence, parent_sequence FROM session_events WHERE session_id = ? ORDER BY sequence",
            (forked.session.id,),
        ).fetchall()
        fork_leaf = connection.execute("SELECT leaf_sequence FROM sessions WHERE session_id = ?", (forked.session.id,)).fetchone()[0]

    # Seed row (1, NULL) plus the two chained rows; the fork ends at its boundary.
    assert fork_rows == [(1, None), (2, 1), (3, 2)]
    assert fork_leaf == forked.forked_at_sequence == 3

    prefix = store.fork_session(workspace=tmp_path, session_id="fork-tree-source", at_sequence=2)

    with closing(sqlite3.connect(tmp_path / "sessions.sqlite3")) as connection:
        prefix_rows = connection.execute(
            "SELECT sequence, parent_sequence FROM session_events WHERE session_id = ? ORDER BY sequence",
            (prefix.session.id,),
        ).fetchall()
        prefix_leaf = connection.execute("SELECT leaf_sequence FROM sessions WHERE session_id = ?", (prefix.session.id,)).fetchone()[0]

    assert prefix_rows == [(1, None), (2, 1)]
    assert prefix_leaf == prefix.forked_at_sequence == 2


def test_session_storage_bulk_append_events_assigns_contiguous_sequences(tmp_path: Path) -> None:
    store = SqliteSessionStore(database_path=tmp_path / "sessions.sqlite3")
    _seed_running_session(store, tmp_path, "bulk-session")

    envelopes = store.append_session_events(
        workspace=tmp_path,
        session_id="bulk-session",
        events=(
            ("runtime.mcp_server_acquired", "runtime", {"server": "a"}, "bulk-1"),
            ("runtime.mcp_server_stopped", "runtime", {"server": "b"}, "bulk-2"),
            ("runtime.acp_connected", "runtime", {}, "bulk-3"),
        ),
    )
    loaded = store.load_session(workspace=tmp_path, session_id="bulk-session")

    assert [envelope.sequence for envelope in envelopes] == [2, 3, 4]
    assert [envelope.event_type for envelope in envelopes] == [
        "runtime.mcp_server_acquired",
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
            ("runtime.mcp_server_acquired", "runtime", {"server": "a"}, "dup-key"),
            ("runtime.mcp_server_stopped", "runtime", {"server": "b"}, "dup-key"),
            ("runtime.acp_connected", "runtime", {}, "fresh-key"),
        ),
    )
    loaded = store.load_session(workspace=tmp_path, session_id="bulk-dedupe-session")

    assert [envelope.sequence for envelope in envelopes] == [2, 3]
    assert [envelope.event_type for envelope in envelopes] == [
        "runtime.mcp_server_acquired",
        "runtime.acp_connected",
    ]
    assert [event.sequence for event in loaded.events] == [1, 2, 3]


def test_session_storage_bulk_append_allows_lifecycle_event_on_terminal_session(tmp_path: Path) -> None:
    store = SqliteSessionStore(database_path=tmp_path / "sessions.sqlite3")
    save_composition_run(
        store,
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

    current_checkpoint = store.load_resume_checkpoint(workspace=tmp_path, session_id="interrupt-session")
    assert current_checkpoint is not None
    store.append_session_events(
        workspace=tmp_path,
        session_id="interrupt-session",
        events=(("runtime.mcp_server_stopped", "runtime", {"server": "a"}, "interrupt-1"),),
        interrupted_checkpoint={**current_checkpoint, "prompt": "interrupted task"},
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
            events=(("runtime.mcp_server_stopped", "runtime", {}, None),),
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

    save_checkpoint(
        store,
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
    assert checkpoint["session_metadata"]["mode"] == "plan"
    assert checkpoint["session_metadata"]["read_only"] is True
    assert "composition_ref" in checkpoint["session_metadata"]
    assert checkpoint["tool_results"] == list(tool_results)
    assert checkpoint["last_event_sequence"] == 4
    assert checkpoint["output"] == "partial output"


def test_session_storage_save_interrupted_checkpoint_updates_without_events_or_output_clobber(
    tmp_path: Path,
) -> None:
    store = SqliteSessionStore(database_path=tmp_path / "sessions.sqlite3")

    save_checkpoint(
        store,
        workspace=tmp_path,
        session_id="interrupt-session",
        prompt="first prompt",
        session_metadata={},
        tool_results=(),
        last_event_sequence=1,
        output="first output",
    )

    save_checkpoint(
        store,
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


def test_session_storage_restore_leaf_keeps_tail_rows_and_watermark(tmp_path: Path) -> None:
    """Restoring the leaf moves the position and deletes nothing.

    The interrupted resume repair is a position move: rows past the checkpoint
    stay in the table (a checkout may have put an abandoned branch there), and
    the watermark stays the append counter so the next append continues
    contiguously rather than recycling a retained row's sequence number.
    """
    store = SqliteSessionStore(database_path=tmp_path / "sessions.sqlite3")
    save_composition_run(
        store,
        workspace=tmp_path,
        request=RuntimeRequest(prompt="restore", session_id="restore-leaf-session"),
        response=RuntimeResponse(
            session=SessionState(
                session=SessionRef(id="restore-leaf-session"),
                status="running",
                turn=1,
                metadata={},
            ),
            events=(),
            output=None,
        ),
    )
    _ = store.append_session_events(
        workspace=tmp_path,
        session_id="restore-leaf-session",
        events=(
            ("runtime.mcp_server_acquired", "runtime", {"server": "a"}, "restore-1"),
            ("runtime.mcp_server_stopped", "runtime", {"server": "b"}, "restore-2"),
            ("runtime.acp_connected", "runtime", {}, "restore-3"),
        ),
    )

    store.restore_leaf_after_interrupted_resume(workspace=tmp_path, session_id="restore-leaf-session", sequence=2)

    loaded = store.load_session(workspace=tmp_path, session_id="restore-leaf-session")
    assert [event.sequence for event in loaded.events] == [1, 2, 3]
    assert [event.sequence for event in store.session_path(workspace=tmp_path, session_id="restore-leaf-session")] == [1, 2]

    resumed = store.append_session_events(
        workspace=tmp_path,
        session_id="restore-leaf-session",
        events=(("runtime.tool_completed", "runtime", {"tool": "read"}, None),),
    )
    assert [envelope.sequence for envelope in resumed] == [4]
    # The new row is a child of the restored leaf (2), not the retained tail (3).
    entries = {entry.sequence: entry for entry in store.session_entries(workspace=tmp_path, session_id="restore-leaf-session")}
    assert entries[4].parent_sequence == 2


def test_session_storage_list_sessions_shows_interrupted_session(tmp_path: Path) -> None:
    store = SqliteSessionStore(database_path=tmp_path / "sessions.sqlite3")

    save_checkpoint(
        store,
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


def test_session_storage_rename_round_trips_across_store_reopen(tmp_path: Path) -> None:
    """The title is a row column, so it must survive a new store on the same file.

    Also pins that a later run snapshot (``save_run``'s ``INSERT OR REPLACE``)
    carries the title forward instead of clearing it.
    """
    database_path = tmp_path / "rename.sqlite3"
    store = SqliteSessionStore(database_path=database_path)
    request = RuntimeRequest(prompt="original prompt", session_id="rename-session")
    response = RuntimeResponse(
        session=SessionState(session=SessionRef(id="rename-session"), status="completed", turn=1),
        events=(
            EventEnvelope(
                session_id="rename-session",
                sequence=1,
                event_type="graph.response_ready",
                source="graph",
                payload={"response": "done"},
            ),
        ),
        output="done",
    )
    _run_session(store, tmp_path, request, response)

    store.rename_session(workspace=tmp_path, session_id="rename-session", title="My label")

    reopened = SqliteSessionStore(database_path=database_path)
    assert reopened.list_sessions(workspace=tmp_path)[0].title == "My label"

    # A subsequent run on the same session rewrites the row; the title must not
    # be collateral damage of the snapshot upsert.
    _run_session(reopened, tmp_path, request, response)
    assert reopened.list_sessions(workspace=tmp_path)[0].title == "My label"


def test_session_storage_rename_rejects_unknown_and_foreign_sessions(tmp_path: Path) -> None:
    store = SqliteSessionStore(database_path=tmp_path / "rename-missing.sqlite3")
    other_workspace = tmp_path / "other"
    other_workspace.mkdir()
    request = RuntimeRequest(prompt="elsewhere", session_id="foreign-session")
    response = RuntimeResponse(
        session=SessionState(session=SessionRef(id="foreign-session"), status="completed", turn=1),
        events=(),
        output=None,
    )
    _run_session(store, other_workspace, request, response)

    with pytest.raises(UnknownSessionError, match="unknown session: missing-session"):
        store.rename_session(workspace=tmp_path, session_id="missing-session", title="nope")
    with pytest.raises(UnknownSessionError, match="unknown session: foreign-session"):
        store.rename_session(workspace=tmp_path, session_id="foreign-session", title="nope")
    # The foreign row keeps its NULL title: a foreign rename must not write.
    assert store.list_sessions(workspace=other_workspace)[0].title is None


def test_session_storage_rename_advances_updated_at(tmp_path: Path) -> None:
    store = SqliteSessionStore(database_path=tmp_path / "rename-touch.sqlite3")
    request = RuntimeRequest(prompt="touch me", session_id="touch-session")
    response = RuntimeResponse(
        session=SessionState(session=SessionRef(id="touch-session"), status="completed", turn=1),
        events=(),
        output=None,
    )
    _run_session(store, tmp_path, request, response)
    before = store.list_sessions(workspace=tmp_path)[0].updated_at

    store.rename_session(workspace=tmp_path, session_id="touch-session", title="touched")

    after = store.list_sessions(workspace=tmp_path)[0].updated_at
    assert after > before
