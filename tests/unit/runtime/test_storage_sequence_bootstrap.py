"""Bootstrap of the storage sequence table survives a concurrent verifier.

``_ensure_storage_sequences`` runs once per verified database file, but several
processes can verify the same file at the same time (the runtime database is
user-global and shared). Inserting the missing counter rows therefore has to be
atomic: a read-then-write pair races into
``sqlite3.IntegrityError: UNIQUE constraint failed: storage_sequences.scope``.

The interleaving cannot be forced through the SQLite driver, so the shim below
replays it deterministically: the first statement that touches
``storage_sequences`` is preceded by another writer committing the same scope
row, and a *reading* statement is additionally answered with the pre-write state
(which is exactly what a racing reader observes). A bootstrap that inserts with
``INSERT OR IGNORE`` never raises and never clobbers the foreign counter.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import cast

from voidcode.runtime.paths import sessions_db_path
from voidcode.runtime.storage.sqlite import SqliteSessionStore

SESSION_ID = "sequence-bootstrap-session"
FOREIGN_SCOPE = "sessions"
FOREIGN_VALUE = 7


class _EmptyCursor:
    """Cursor view of a reader that ran before the concurrent writer committed."""

    def fetchall(self) -> list[object]:
        return []


class _ConcurrentWriterShim:
    """Connection shim that lets another writer commit a scope row mid-bootstrap."""

    def __init__(self, *, connection: sqlite3.Connection, database_path: Path, scope: str, value: int) -> None:
        self._connection = connection
        self._database_path = database_path
        self._scope = scope
        self._value = value
        self._injected = False

    def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> object:
        if not self._injected and "storage_sequences" in sql:
            self._injected = True
            other = sqlite3.connect(self._database_path)
            try:
                _ = other.execute(
                    "INSERT OR IGNORE INTO storage_sequences (scope, value) VALUES (?, ?)",
                    (self._scope, self._value),
                )
                other.commit()
            finally:
                other.close()
            if sql.lstrip().upper().startswith("SELECT"):
                return _EmptyCursor()
        return self._connection.execute(sql, parameters)


def _sequence_rows(database_path: Path) -> dict[str, int]:
    connection = sqlite3.connect(database_path)
    try:
        rows = cast(list[tuple[str, int]], connection.execute("SELECT scope, value FROM storage_sequences").fetchall())
    finally:
        connection.close()
    return dict(rows)


def test_storage_sequence_bootstrap_survives_a_concurrent_verifier(tmp_path: Path) -> None:
    """A counter row committed by another process mid-bootstrap neither raises nor gets clobbered."""
    store = SqliteSessionStore()
    # Any store call verifies (and bootstraps) the database file; then clear the
    # counter rows so this run has rows to insert, like an unverified file.
    assert store.has_session(workspace=tmp_path, session_id=SESSION_ID) is False
    database_path = sessions_db_path()
    connection = sqlite3.connect(database_path)
    try:
        _ = connection.execute("DELETE FROM storage_sequences")
        connection.commit()
    finally:
        connection.close()

    connection = sqlite3.connect(database_path)
    try:
        shim = _ConcurrentWriterShim(
            connection=connection,
            database_path=database_path,
            scope=FOREIGN_SCOPE,
            value=FOREIGN_VALUE,
        )
        SqliteSessionStore._ensure_storage_sequences(connection=cast(sqlite3.Connection, shim))
        connection.commit()
    finally:
        connection.close()

    rows = _sequence_rows(database_path)
    assert sorted(rows) == sorted(SqliteSessionStore._SEQUENCE_SCOPES)
    assert rows[FOREIGN_SCOPE] == FOREIGN_VALUE, "bootstrap clobbered a counter another process wrote"
