from __future__ import annotations

import sqlite3
from importlib.resources import files

import pytest

from voidcode.kernel.storage.store import SCHEMA_VERSION


def test_historical_facts_cannot_be_rewritten_or_orphaned() -> None:
    with sqlite3.connect(":memory:") as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(files("voidcode.kernel.storage").joinpath("schema.sql").read_text(encoding="utf-8"))
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
