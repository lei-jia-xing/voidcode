from __future__ import annotations

import sqlite3
from importlib.resources import files

import pytest

from voidcode.core.storage.store import SCHEMA_VERSION


def test_historical_facts_cannot_be_rewritten_or_orphaned() -> None:
    with sqlite3.connect(":memory:") as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(files("voidcode.core.storage").joinpath("schema.sql").read_text(encoding="utf-8"))
        connection.execute(f"INSERT INTO kernel_identity VALUES (1, 'voidcode-kernel-v1', {SCHEMA_VERSION})")
        connection.execute("INSERT INTO revisions VALUES ('revision', 1, '{}')")
        connection.execute(
            "INSERT INTO sessions VALUES ('session', '/workspace', 'running', 'revision', 'run', 1, 1, 1, 1, NULL, 'session', 0, NULL)"
        )
        connection.execute("INSERT INTO session_events VALUES ('session', 1, 'run', 'tool.completed', 'tool', 'model', 'revision', '{}')")
        for statement in (
            "UPDATE session_events SET payload_json = '{\"changed\":true}'",
            "DELETE FROM session_events",
            "UPDATE revisions SET payload_json = '{\"changed\":true}'",
            "DELETE FROM revisions",
            "INSERT INTO session_events VALUES ('session', 2, 'run', 'tool.completed', 'tool', 'model', 'missing', '{}')",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(statement)
        assert connection.execute("SELECT payload_json FROM session_events").fetchall() == [("{}",)]
        assert connection.execute("SELECT payload_json FROM revisions").fetchall() == [("{}",)]


def test_v5_database_refused_and_v6_database_boots(tmp_path: pytest.TempPathFactory) -> None:
    from pathlib import Path

    from voidcode.core.storage.store import ForeignDatabaseError, SqliteStore

    db_path = Path(tmp_path) / "test_v5.db"
    with sqlite3.connect(db_path) as conn:
        schema_sql = files("voidcode.core.storage").joinpath("schema.sql").read_text(encoding="utf-8")
        conn.executescript(schema_sql.replace("schema_version = 6", "schema_version = 5"))
        conn.execute("INSERT INTO kernel_identity VALUES (1, 'voidcode-kernel-v1', 5)")
        conn.commit()

    with pytest.raises(ForeignDatabaseError, match="unsupported identity"):
        SqliteStore(db_path)

    v6_path = Path(tmp_path) / "test_v6.db"
    with SqliteStore(v6_path) as store:
        assert store.recorded_schema == 6
    assert v6_path.is_file()
