"""SQLite storage implementation for the VoidCode kernel."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Self

from ...sdk.json_values import json_wire_object, own_json_object

SCHEMA_MAGIC = "voidcode-kernel-v1"
SCHEMA_VERSION = 1


class StorageError(RuntimeError):
    """Base error for kernel storage failures."""


class ForeignDatabaseError(StorageError):
    """Raised when an existing database is foreign, empty, legacy, or corrupt."""


class StoreConflict(StorageError):
    """Raised when an optimistic concurrency check fails or state is stale."""


@dataclass(frozen=True, slots=True)
class SessionRecord:
    session_id: str
    workspace: str
    status: str
    active_revision_id: str
    run_id: str | None
    turn: int
    last_event_sequence: int


@dataclass(frozen=True, slots=True)
class EventDraft:
    event_type: str
    source: str
    actor: str
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", own_json_object(self.payload))


@dataclass(frozen=True, slots=True)
class StoredEvent:
    session_id: str
    sequence: int
    run_id: str
    event_type: str
    source: str
    actor: str
    revision_id: str
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", own_json_object(self.payload))


@dataclass(frozen=True, slots=True)
class Checkpoint:
    kind: str
    revision_id: str
    run_id: str
    last_event_sequence: int
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", own_json_object(self.payload))


def canonical_json(value: Mapping[str, object]) -> str:
    """Produce deterministic, sorted, compact UTF-8 JSON without NaN."""
    return json.dumps(
        json_wire_object(own_json_object(value)),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _load_schema_sql() -> str:
    return files("voidcode.kernel.storage").joinpath("schema.sql").read_text(encoding="utf-8")


def _verify_existing_file(path: Path) -> None:
    """Preflight check on an existing file without altering a single byte."""
    if not path.is_file():
        raise ForeignDatabaseError(f"database path exists but is not a regular file: {path}")
    if path.stat().st_size == 0:
        raise ForeignDatabaseError(f"refusing pre-existing empty database file: {path}")

    uri = f"{path.resolve().as_uri()}?mode=ro"
    try:
        probe = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise ForeignDatabaseError(f"unsupported database file: {path}") from exc

    try:
        cursor = probe.execute("SELECT magic, schema_version FROM kernel_identity WHERE id = 1")
        row = cursor.fetchone()
        if row is None or row[0] != SCHEMA_MAGIC or row[1] != SCHEMA_VERSION:
            raise ForeignDatabaseError(f"unsupported database identity in {path}: expected ({SCHEMA_MAGIC!r}, {SCHEMA_VERSION}), got {row!r}")
    except sqlite3.Error as exc:
        raise ForeignDatabaseError(f"unsupported database file or missing identity: {path}") from exc
    finally:
        probe.close()


class SqliteStore:
    """Concrete SQLite store for kernel sessions, revisions, events, and checkpoints."""

    def __init__(self, path: Path | str) -> None:
        self._lock = threading.RLock()
        # ponytail: single store RLock serializes connection access per instance; separate reader/writer connection pools if concurrency matters
        self._closed = False
        if isinstance(path, str):
            path = Path(path)
        self._path = path

        is_memory = str(path) == ":memory:"
        is_new = False
        resolved_path: Path | None = None

        if not is_memory:
            resolved_path = path.resolve()
            resolved_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                descriptor = os.open(resolved_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                _verify_existing_file(resolved_path)
            else:
                os.close(descriptor)
                is_new = True
        else:
            is_new = True

        self._connection = sqlite3.connect(
            str(resolved_path) if resolved_path is not None else ":memory:",
            timeout=5.0,
            isolation_level=None,
            check_same_thread=False,
        )
        try:
            self._connection.execute("PRAGMA busy_timeout = 5000")
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA foreign_keys = ON")
            if is_new:
                self._connection.executescript(_load_schema_sql())
                self._connection.execute("PRAGMA foreign_keys = ON")
                self._connection.execute(
                    "INSERT INTO kernel_identity (id, magic, schema_version) VALUES (1, ?, ?)",
                    (SCHEMA_MAGIC, SCHEMA_VERSION),
                )
        except Exception:
            self._connection.close()
            self._closed = True
            if is_new and resolved_path is not None:
                resolved_path.unlink(missing_ok=True)
            raise

    @property
    def path(self) -> Path:
        return self._path

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                self._connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        self.close()

    def _check_open(self) -> None:
        if self._closed:
            raise StorageError("store is closed")

    def put_revision(self, payload: Mapping[str, object]) -> str:
        """Persist a revision payload canonically and return its SHA-256 revision ID."""
        canonical_text = canonical_json(payload)
        revision_id = hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()
        now = int(time.time() * 1000)
        with self._lock:
            self._check_open()
            self._connection.execute(
                """
                INSERT INTO revisions (revision_id, created_at, payload_json)
                VALUES (?, ?, ?)
                ON CONFLICT (revision_id) DO NOTHING
                """,
                (revision_id, now, canonical_text),
            )
        return revision_id

    def revision(self, revision_id: str) -> Mapping[str, object]:
        """Fetch the payload of a revision by ID, returning an immutable mapping snapshot."""
        with self._lock:
            self._check_open()
            cursor = self._connection.execute(
                "SELECT payload_json FROM revisions WHERE revision_id = ?",
                (revision_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise KeyError(f"unknown revision: {revision_id}")
            return own_json_object(json.loads(row[0]))

    def create_session(self, session_id: str, workspace: str, revision_id: str) -> SessionRecord:
        """Create a new session pinned to an active revision."""
        now = int(time.time() * 1000)
        with self._lock:
            self._check_open()
            self._connection.execute(
                """
                INSERT INTO sessions (
                    session_id, workspace, status, active_revision_id, run_id, turn, created_at, updated_at, last_event_sequence
                ) VALUES (?, ?, 'idle', ?, NULL, 0, ?, ?, 0)
                """,
                (session_id, workspace, revision_id, now, now),
            )
            return SessionRecord(
                session_id=session_id,
                workspace=workspace,
                status="idle",
                active_revision_id=revision_id,
                run_id=None,
                turn=0,
                last_event_sequence=0,
            )

    def session(self, session_id: str) -> SessionRecord:
        """Fetch session metadata by session ID."""
        with self._lock:
            self._check_open()
            cursor = self._connection.execute(
                """
                SELECT session_id, workspace, status, active_revision_id, run_id, turn, last_event_sequence
                FROM sessions
                WHERE session_id = ?
                """,
                (session_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise KeyError(f"unknown session: {session_id}")
            return SessionRecord(
                session_id=row[0],
                workspace=row[1],
                status=row[2],
                active_revision_id=row[3],
                run_id=row[4],
                turn=row[5],
                last_event_sequence=row[6],
            )

    def events(self, session_id: str, after_sequence: int = 0) -> tuple[StoredEvent, ...]:
        """Fetch stored events for a session strictly after a given sequence number."""
        with self._lock:
            self._check_open()
            # Verify session exists
            self.session(session_id)
            cursor = self._connection.execute(
                """
                SELECT session_id, sequence, run_id, event_type, source, actor, revision_id, payload_json
                FROM session_events
                WHERE session_id = ? AND sequence > ?
                ORDER BY sequence ASC
                """,
                (session_id, after_sequence),
            )
            return tuple(
                StoredEvent(
                    session_id=row[0],
                    sequence=row[1],
                    run_id=row[2],
                    event_type=row[3],
                    source=row[4],
                    actor=row[5],
                    revision_id=row[6],
                    payload=json.loads(row[7]),
                )
                for row in cursor.fetchall()
            )

    def checkpoint(self, session_id: str) -> Checkpoint | None:
        """Fetch the current checkpoint of a session, or None if none has been committed."""
        with self._lock:
            self._check_open()
            # Verify session exists
            self.session(session_id)
            cursor = self._connection.execute(
                """
                SELECT kind, revision_id, run_id, last_event_sequence, payload_json
                FROM checkpoints
                WHERE session_id = ?
                """,
                (session_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return Checkpoint(
                kind=row[0],
                revision_id=row[1],
                run_id=row[2],
                last_event_sequence=row[3],
                payload=json.loads(row[4]),
            )

    def commit(
        self,
        session_id: str,
        *,
        expected_sequence: int,
        expected_run_id: str | None,
        run_id: str,
        revision_id: str,
        events: tuple[EventDraft, ...] | Sequence[EventDraft],
        status: str,
        checkpoint_kind: str,
        checkpoint_payload: Mapping[str, object],
        start_turn: bool = False,
    ) -> SessionRecord:
        """Atomic CAS commit appending events, advancing session state, and upserting a checkpoint."""
        with self._lock:
            self._check_open()
            in_transaction = False
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                in_transaction = True

                cursor = self._connection.execute(
                    """
                    SELECT session_id, workspace, status, active_revision_id, run_id, turn, last_event_sequence
                    FROM sessions
                    WHERE session_id = ?
                    """,
                    (session_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    raise StoreConflict(f"session {session_id!r} does not exist")

                _, workspace, _, curr_revision_id, curr_run_id, curr_turn, curr_seq = row

                if curr_seq != expected_sequence:
                    raise StoreConflict(f"sequence mismatch for session {session_id!r}: expected {expected_sequence}, got {curr_seq}")
                if curr_run_id != expected_run_id:
                    raise StoreConflict(f"run_id mismatch for session {session_id!r}: expected {expected_run_id!r}, got {curr_run_id!r}")
                if curr_revision_id != revision_id:
                    raise StoreConflict(f"active revision mismatch for session {session_id!r}: active is {curr_revision_id!r}, got {revision_id!r}")

                current_seq = expected_sequence
                for draft in events:
                    current_seq += 1
                    payload_json = canonical_json(draft.payload)
                    self._connection.execute(
                        """
                        INSERT INTO session_events (
                            session_id, sequence, run_id, event_type, source, actor, revision_id, payload_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            session_id,
                            current_seq,
                            run_id,
                            draft.event_type,
                            draft.source,
                            draft.actor,
                            revision_id,
                            payload_json,
                        ),
                    )

                new_turn = curr_turn + 1 if start_turn else curr_turn
                now = int(time.time() * 1000)

                self._connection.execute(
                    """
                    UPDATE sessions
                    SET status = ?,
                        run_id = ?,
                        turn = ?,
                        updated_at = ?,
                        last_event_sequence = ?
                    WHERE session_id = ?
                    """,
                    (status, run_id, new_turn, now, current_seq, session_id),
                )

                checkpoint_json = canonical_json(checkpoint_payload)
                self._connection.execute(
                    """
                    INSERT INTO checkpoints (
                        session_id, kind, revision_id, run_id, last_event_sequence, payload_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (session_id) DO UPDATE SET
                        kind = excluded.kind,
                        revision_id = excluded.revision_id,
                        run_id = excluded.run_id,
                        last_event_sequence = excluded.last_event_sequence,
                        payload_json = excluded.payload_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        session_id,
                        checkpoint_kind,
                        revision_id,
                        run_id,
                        current_seq,
                        checkpoint_json,
                        now,
                    ),
                )

                self._connection.execute("COMMIT")
                in_transaction = False

                return SessionRecord(
                    session_id=session_id,
                    workspace=workspace,
                    status=status,
                    active_revision_id=revision_id,
                    run_id=run_id,
                    turn=new_turn,
                    last_event_sequence=current_seq,
                )
            except Exception:
                if in_transaction:
                    self._connection.execute("ROLLBACK")
                raise
