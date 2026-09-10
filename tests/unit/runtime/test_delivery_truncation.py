import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from voidcode.runtime.storage import SCHEMA_VERSION, SqliteSessionStore


@pytest.mark.parametrize("batched", [False, True])
def test_truncation_reclaims_only_tail_delivery_claims(tmp_path: Path, batched: bool) -> None:
    database = tmp_path / "sessions.sqlite3"
    store = SqliteSessionStore(database_path=database)
    other_workspace = tmp_path / "other"
    for workspace, session in ((tmp_path, "s"), (tmp_path, "other-session"), (other_workspace, "s")):
        store.save_interrupted_checkpoint(
            workspace=workspace, session_id=session, prompt="audit", session_metadata={}, tool_results=(), last_event_sequence=0
        )
        for key in ("prefix", "tail"):
            if batched:
                result = store.append_session_events(
                    workspace=workspace, session_id=session, events=(("runtime.background_task_completed", "runtime", {"task_id": key}, key),)
                )
                assert len(result) == 1
            else:
                assert (
                    store.append_session_event(
                        workspace=workspace,
                        session_id=session,
                        event_type="runtime.background_task_completed",
                        source="runtime",
                        payload={"task_id": key},
                        dedupe_key=key,
                    )
                    is not None
                )
    store.truncate_session_events_after(workspace=tmp_path, session_id="s", sequence=1)
    restarted = SqliteSessionStore(database_path=database)
    for workspace, session in ((tmp_path, "s"), (tmp_path, "other-session"), (other_workspace, "s")):
        result = restarted.append_session_events(
            workspace=workspace,
            session_id=session,
            events=(
                ("runtime.background_task_completed", "runtime", {"task_id": "prefix"}, "prefix"),
                ("runtime.background_task_completed", "runtime", {"task_id": "tail"}, "tail"),
            ),
        )
        assert [event.sequence for event in result] == ([2] if (workspace, session) == (tmp_path, "s") else [])
        stored = restarted.load_session(workspace=workspace, session_id=session)
        assert [event.sequence for event in stored.events] == [1, 2]
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute(
            "SELECT event_sequence FROM session_event_deliveries WHERE workspace_id = ? AND session_id = ? ORDER BY event_sequence",
            (str(tmp_path), "s"),
        ).fetchall() == [(1,), (2,)]


@pytest.mark.parametrize("version", [6, 10, 11, 12, 13, 14])
def test_old_schema_is_rejected_without_mutating_database(tmp_path: Path, version: int) -> None:
    database = tmp_path / "old.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("CREATE TABLE preserved(value TEXT)")
        connection.execute("INSERT INTO preserved VALUES ('keep')")
        connection.execute(f"PRAGMA user_version = {version}")
        connection.commit()
    with pytest.raises(RuntimeError, match=f"schema version mismatch: expected {SCHEMA_VERSION} got {version}"):
        SqliteSessionStore(database_path=database).list_sessions(workspace=tmp_path)
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == version
        assert connection.execute("SELECT * FROM preserved").fetchall() == [("keep",)]
        assert connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [("preserved",)]
