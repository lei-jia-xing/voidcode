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
from typing import Literal, Self, cast

from ...sdk.json_values import json_wire_object, own_json_object
from ...sdk.services import PluginBudget

SCHEMA_MAGIC = "voidcode-kernel-v1"
SCHEMA_VERSION = 2


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


@dataclass(frozen=True, slots=True)
class ActionConfirmationRecord:
    request_id: str
    session_id: str
    owner: str
    command_name: str
    arguments: Mapping[str, object]
    revision_id: str
    expected_state_version: int | None
    ui_type: str
    ui_data: Mapping[str, object]
    status: str
    created_at: int
    decided_at: int | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "arguments", own_json_object(self.arguments))
        object.__setattr__(self, "ui_data", own_json_object(self.ui_data))


@dataclass(frozen=True, slots=True)
class ContinuationRecord:
    continuation_id: str
    owner: str
    session_id: str
    source: str
    run_id: str
    revision_id: str
    idempotency_key: str | None
    status: str
    payload: Mapping[str, object]
    created_at: int
    updated_at: int
    root_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", own_json_object(self.payload))


@dataclass(frozen=True, slots=True)
class StagedStateUpdate:
    owner: str
    scope: str
    scope_key: str
    payload: Mapping[str, object]
    expected_version: int
    schema_version: int = 1
    revision_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", own_json_object(self.payload))


@dataclass(frozen=True, slots=True)
class StagedContinuationDraft:
    continuation_id: str
    owner: str
    source: str
    payload: Mapping[str, object]
    idempotency_key: str | None = None
    ordinal: int = 0
    root_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", own_json_object(self.payload))


@dataclass(frozen=True, slots=True)
class StagedConfirmation:
    """A pending action confirmation created atomically with the event that announces it."""

    request_id: str
    owner: str
    command_name: str
    arguments: Mapping[str, object]
    ui_type: str
    ui_data: Mapping[str, object]
    expected_state_version: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "arguments", own_json_object(self.arguments))
        object.__setattr__(self, "ui_data", own_json_object(self.ui_data))


@dataclass(frozen=True, slots=True)
class StagedCancellation:
    owner: str
    continuation_id: str | None = None
    ordinal: int = 0


@dataclass(frozen=True, slots=True)
class StagedBudgetUpdate:
    owner: str
    action: Literal["set", "clear"]
    budget: PluginBudget | None = None


@dataclass(frozen=True, slots=True)
class ObserverReceiptRecord:
    event_sequence: int
    owner: str


@dataclass(frozen=True, slots=True)
class PluginStateRow:
    """One durable owner state row plus its core-owned version metadata."""

    owner: str
    scope: str
    scope_key: str
    payload: Mapping[str, object]
    version: int
    schema_version: int
    revision_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", own_json_object(self.payload))


def revision_identity(payload: Mapping[str, object]) -> str:
    """Content address of a capability revision payload, computed without storing it."""
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


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
        cursor = probe.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {r[0] for r in cursor.fetchall()}
        required = {
            "kernel_identity",
            "revisions",
            "sessions",
            "session_events",
            "checkpoints",
            "plugin_state",
            "action_confirmations",
            "continuations",
            "session_budgets",
            "observer_receipts",
            "root_budgets",
            "workspace_state",
        }
        if not required.issubset(tables):
            raise ForeignDatabaseError(f"unsupported database file or missing tables in {path}: missing {required - tables}")
        cursor = probe.execute("PRAGMA table_info(plugin_state)")
        plugin_state_cols = {r[1] for r in cursor.fetchall()}
        required_plugin_state_cols = {
            "scope",
            "scope_key",
            "owner",
            "version",
            "schema_version",
            "revision_id",
            "payload_json",
            "updated_at",
        }
        if not required_plugin_state_cols.issubset(plugin_state_cols):
            raise ForeignDatabaseError(f"unsupported plugin_state schema in {path}: missing {required_plugin_state_cols - plugin_state_cols}")

        cursor = probe.execute("PRAGMA table_info(continuations)")
        continuation_cols = {r[1] for r in cursor.fetchall()}
        required_continuation_cols = {
            "continuation_id",
            "owner",
            "session_id",
            "source",
            "run_id",
            "revision_id",
            "idempotency_key",
            "root_id",
            "status",
            "payload_json",
            "created_at",
            "updated_at",
        }
        if not required_continuation_cols.issubset(continuation_cols):
            raise ForeignDatabaseError(f"unsupported continuations schema in {path}: missing {required_continuation_cols - continuation_cols}")

        cursor = probe.execute("PRAGMA table_info(root_budgets)")
        root_cols = {r[1] for r in cursor.fetchall()}
        required_root_cols = {"root_id", "budget", "reserved", "created_at", "updated_at"}
        if not required_root_cols.issubset(root_cols):
            raise ForeignDatabaseError(f"unsupported root_budgets schema in {path}: missing {required_root_cols - root_cols}")

        cursor = probe.execute("PRAGMA table_info(session_budgets)")
        budget_cols = {r[1] for r in cursor.fetchall()}
        required_budget_cols = {
            "session_id",
            "owner",
            "since_sequence",
            "max_input_tokens",
            "max_output_tokens",
            "max_total_tokens",
            "max_cost_usd",
            "max_elapsed_seconds",
            "created_at",
            "updated_at",
        }
        if not required_budget_cols.issubset(budget_cols):
            raise ForeignDatabaseError(f"unsupported session_budgets schema in {path}: missing {required_budget_cols - budget_cols}")

        cursor = probe.execute("PRAGMA table_info(observer_receipts)")
        receipt_cols = {r[1] for r in cursor.fetchall()}
        required_receipt_cols = {"session_id", "event_sequence", "owner", "created_at"}
        if not required_receipt_cols.issubset(receipt_cols) or "generation" in receipt_cols:
            raise ForeignDatabaseError(f"unsupported observer_receipts schema in {path}: expected per-(event, owner) receipts")

        cursor = probe.execute("PRAGMA table_info(workspace_state)")
        workspace_cols = {r[1] for r in cursor.fetchall()}
        required_workspace_cols = {"workspace", "active_revision_id", "updated_at"}
        if not required_workspace_cols.issubset(workspace_cols):
            raise ForeignDatabaseError(f"unsupported workspace_state schema in {path}: missing {required_workspace_cols - workspace_cols}")
    except sqlite3.Error as exc:
        raise ForeignDatabaseError(f"unsupported database file or missing identity: {path}") from exc
    finally:
        probe.close()


def inspect_existing_workspace(database: Path, workspace: str) -> tuple[str | None, Mapping[str, object] | None]:
    """Read one workspace's adopted revision without creating or changing anything.

    Returns (None, None) when there is no database (or no adopted revision for this workspace),
    (revision_id, payload) when there is, and raises for a file that is not a kernel database.
    Never writes, never sets pragmas. One state directory may hold several workspaces.
    """
    if not database.exists():
        return (None, None)
    _verify_existing_file(database)
    uri = f"{database.resolve().as_uri()}?mode=ro"
    probe = sqlite3.connect(uri, uri=True)
    try:
        cursor = probe.execute("SELECT active_revision_id FROM workspace_state WHERE workspace = ?", (workspace,))
        row = cursor.fetchone()
        if row is None:
            return (None, None)
        revision_id = str(row[0])
        payload_row = probe.execute("SELECT payload_json FROM revisions WHERE revision_id = ?", (revision_id,)).fetchone()
        if payload_row is None:
            raise ForeignDatabaseError(f"database {database} points at missing revision {revision_id}")
        payload = json.loads(str(payload_row[0]))
        if not isinstance(payload, dict):
            raise ForeignDatabaseError(f"database {database} stores a malformed revision payload")
        return (revision_id, json_wire_object(payload))
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
        """Create a new session pinned to the workspace's recorded revision.

        The insert takes the revision from the workspace pointer in the SAME statement and
        requires it to still be the caller's expected revision, so a session created while a
        capability activation commits can never be pinned to the generation that just moved.
        """
        now = int(time.time() * 1000)
        with self._lock:
            self._check_open()
            cursor = self._connection.execute(
                """
                INSERT INTO sessions (
                    session_id, workspace, status, active_revision_id, run_id, turn, created_at, updated_at, last_event_sequence
                )
                SELECT ?, ?, 'idle', w.active_revision_id, NULL, 0, ?, ?, 0
                FROM workspace_state w
                WHERE w.workspace = ? AND w.active_revision_id = ?
                """,
                (session_id, workspace, now, now, workspace, revision_id),
            )
            if cursor.rowcount == 0:
                raise StoreConflict(f"workspace revision moved away from {revision_id!r}; retry against the current revision")
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

    def sessions(self, workspace: str | None = None, limit: int = 100) -> tuple[SessionRecord, ...]:
        """Fetch sessions, optionally filtered by workspace, ordered by updated_at descending."""
        with self._lock:
            self._check_open()
            capped_limit = max(1, limit)
            if workspace is not None:
                cursor = self._connection.execute(
                    """
                    SELECT session_id, workspace, status, active_revision_id, run_id, turn, last_event_sequence
                    FROM sessions
                    WHERE workspace = ?
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (workspace, capped_limit),
                )
            else:
                cursor = self._connection.execute(
                    """
                    SELECT session_id, workspace, status, active_revision_id, run_id, turn, last_event_sequence
                    FROM sessions
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (capped_limit,),
                )
            return tuple(
                SessionRecord(
                    session_id=row[0],
                    workspace=row[1],
                    status=row[2],
                    active_revision_id=row[3],
                    run_id=row[4],
                    turn=row[5],
                    last_event_sequence=row[6],
                )
                for row in cursor.fetchall()
            )

    def events(self, session_id: str, after_sequence: int = 0, limit: int | None = None) -> tuple[StoredEvent, ...]:
        """Fetch stored events for a session strictly after a given sequence number, optionally limited."""
        with self._lock:
            self._check_open()
            self.session(session_id)
            if limit is not None:
                cursor = self._connection.execute(
                    """
                    SELECT session_id, sequence, run_id, event_type, source, actor, revision_id, payload_json
                    FROM session_events
                    WHERE session_id = ? AND sequence > ?
                    ORDER BY sequence ASC
                    LIMIT ?
                    """,
                    (session_id, after_sequence, max(1, limit)),
                )
            else:
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

    def events_after(self, session_id: str, *, after_sequence: int = 0, limit: int = 100) -> tuple[StoredEvent, ...]:
        """Bounded tail read of stored events for continuous SSE streaming."""
        return self.events(session_id, after_sequence=after_sequence, limit=limit)

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
        return self.commit_staged(
            session_id,
            expected_sequence=expected_sequence,
            expected_run_id=expected_run_id,
            run_id=run_id,
            revision_id=revision_id,
            events=events,
            status=status,
            checkpoint_kind=checkpoint_kind,
            checkpoint_payload=checkpoint_payload,
            start_turn=start_turn,
        )

    def commit_staged(
        self,
        session_id: str,
        *,
        expected_sequence: int | None = None,
        expected_run_id: str | None = None,
        run_id: str,
        revision_id: str,
        events: tuple[EventDraft, ...] | Sequence[EventDraft],
        status: str,
        checkpoint_kind: str | None = None,
        checkpoint_payload: Mapping[str, object] | None = None,
        staged_state: Sequence[StagedStateUpdate] = (),
        staged_continuations: Sequence[StagedContinuationDraft] = (),
        staged_cancellations: Sequence[StagedCancellation] = (),
        staged_budgets: Sequence[StagedBudgetUpdate] = (),
        staged_confirmations: Sequence[StagedConfirmation] = (),
        observer_receipts: Sequence[ObserverReceiptRecord] = (),
        completed_continuation_id: str | None = None,
        completed_continuation_status: Literal["completed", "failed", "cancelled"] = "completed",
        root_reservation: tuple[str, int] | None = None,
        start_turn: bool = False,
        checkpoint_bindings: Sequence[tuple[str, str]] = (),
    ) -> SessionRecord:
        """Atomic CAS commit applying staged state updates, continuations, event drafts, and checkpoint.

        Each `(payload_field, event_type)` binding in `checkpoint_bindings` is
        assigned the actual appended sequence of the first event of that type in
        THIS transaction, so a checkpoint reference can never point at a guessed
        or not-yet-committed sequence.
        """
        with self._lock:
            self._check_open()
            in_transaction = False
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                in_transaction = True

                if root_reservation is not None:
                    root_id, root_limit = root_reservation
                    reserved_at = int(time.time() * 1000)
                    self._connection.execute(
                        """
                        INSERT OR IGNORE INTO root_budgets (root_id, budget, reserved, created_at, updated_at)
                        VALUES (?, ?, 0, ?, ?)
                        """,
                        (root_id, max(0, root_limit), reserved_at, reserved_at),
                    )
                    # Reserve atomically with the event that bills this action, so a
                    # crash after dispatch cannot refund the consumed root step.
                    reserved = self._connection.execute(
                        """
                        UPDATE root_budgets
                        SET reserved = reserved + 1, updated_at = ?
                        WHERE root_id = ? AND reserved < budget
                        """,
                        (reserved_at, root_id),
                    )
                    if reserved.rowcount == 0:
                        raise StoreConflict(f"root action budget exhausted for {root_id!r}")

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

                if expected_sequence is not None and curr_seq != expected_sequence:
                    raise StoreConflict(f"sequence mismatch for session {session_id!r}: expected {expected_sequence}, got {curr_seq}")
                if expected_run_id is not None and curr_run_id != expected_run_id:
                    raise StoreConflict(f"run_id mismatch for session {session_id!r}: expected {expected_run_id!r}, got {curr_run_id!r}")
                if curr_revision_id != revision_id:
                    raise StoreConflict(f"active revision mismatch for session {session_id!r}: active is {curr_revision_id!r}, got {revision_id!r}")
                now = int(time.time() * 1000)

                # 1. Apply staged state updates
                self._apply_state_updates(staged_state, default_revision_id=revision_id, now=now)

                # 2. Apply staged continuation operations in their recorded order, so
                # cancel-then-enqueue keeps the new continuation while enqueue-then-cancel
                # (including by returned id) cancels exactly the one that was enqueued.
                ops: list[tuple[int, Literal["enqueue", "cancel"], object]] = []
                ops.extend((c.ordinal, "enqueue", c) for c in staged_continuations)
                ops.extend((c.ordinal, "cancel", c) for c in staged_cancellations)
                for _ord, kind, payload in sorted(ops, key=lambda item: item[0]):
                    if kind == "enqueue":
                        cont = cast(StagedContinuationDraft, payload)
                        cont_payload_json = canonical_json(cont.payload)
                        if cont.idempotency_key is not None:
                            c_chk = self._connection.execute(
                                """
                                SELECT continuation_id FROM continuations
                                WHERE session_id = ? AND owner = ? AND idempotency_key = ?
                                """,
                                (session_id, cont.owner, cont.idempotency_key),
                            )
                            if c_chk.fetchone() is not None:
                                continue
                        try:
                            self._connection.execute(
                                """
                                INSERT INTO continuations (
                                    continuation_id, owner, session_id, source, run_id, revision_id,
                                    idempotency_key, root_id, status, payload_json, created_at, updated_at
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                                """,
                                (
                                    cont.continuation_id,
                                    cont.owner,
                                    session_id,
                                    cont.source,
                                    run_id,
                                    revision_id,
                                    cont.idempotency_key,
                                    cont.root_id,
                                    cont_payload_json,
                                    now,
                                    now,
                                ),
                            )
                        except sqlite3.IntegrityError as exc:
                            raise StoreConflict(f"continuation {cont.continuation_id!r} already exists") from exc
                    else:
                        canc = cast(StagedCancellation, payload)
                        if canc.continuation_id is not None:
                            self._connection.execute(
                                """
                                UPDATE continuations
                                SET status = 'cancelled', updated_at = ?
                                WHERE session_id = ? AND owner = ? AND continuation_id = ?
                                  AND status IN ('pending', 'claimed')
                                """,
                                (now, session_id, canc.owner, canc.continuation_id),
                            )
                        else:
                            self._connection.execute(
                                """
                                UPDATE continuations
                                SET status = 'cancelled', updated_at = ?
                                WHERE session_id = ? AND owner = ? AND status IN ('pending', 'claimed')
                                """,
                                (now, session_id, canc.owner),
                            )

                # 2c. Apply staged budgets
                for bu in staged_budgets:
                    if bu.action == "clear":
                        self._connection.execute(
                            "DELETE FROM session_budgets WHERE session_id = ? AND owner = ?",
                            (session_id, bu.owner),
                        )
                    elif bu.action == "set" and bu.budget is not None:
                        self._connection.execute(
                            """
                            INSERT INTO session_budgets (
                                session_id, owner, since_sequence, max_input_tokens, max_output_tokens, max_total_tokens,
                                max_cost_usd, max_elapsed_seconds, created_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT (session_id, owner) DO UPDATE SET
                                since_sequence = excluded.since_sequence,
                                max_input_tokens = excluded.max_input_tokens,
                                max_output_tokens = excluded.max_output_tokens,
                                max_total_tokens = excluded.max_total_tokens,
                                max_cost_usd = excluded.max_cost_usd,
                                max_elapsed_seconds = excluded.max_elapsed_seconds,
                                updated_at = excluded.updated_at
                            """,
                            (
                                session_id,
                                bu.owner,
                                bu.budget.since_sequence,
                                bu.budget.max_input_tokens,
                                bu.budget.max_output_tokens,
                                bu.budget.max_total_tokens,
                                bu.budget.max_cost_usd,
                                bu.budget.max_elapsed_seconds,
                                now,
                                now,
                            ),
                        )

                # 2c2. Create pending action confirmations with the event that announces them.
                for confirmation in staged_confirmations:
                    try:
                        self._connection.execute(
                            """
                            INSERT INTO action_confirmations (
                                request_id, session_id, owner, command_name, arguments_json, revision_id,
                                expected_state_version, ui_type, ui_data_json, status, created_at, decided_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, NULL)
                            """,
                            (
                                confirmation.request_id,
                                session_id,
                                confirmation.owner,
                                confirmation.command_name,
                                canonical_json(confirmation.arguments),
                                revision_id,
                                confirmation.expected_state_version,
                                confirmation.ui_type,
                                canonical_json(confirmation.ui_data),
                                now,
                            ),
                        )
                    except sqlite3.IntegrityError as exc:
                        raise StoreConflict(f"confirmation {confirmation.request_id!r} already exists") from exc

                # 2d. Apply observer receipts
                for r in observer_receipts:
                    self._connection.execute(
                        """
                        INSERT OR IGNORE INTO observer_receipts (session_id, event_sequence, owner, created_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (session_id, r.event_sequence, r.owner, now),
                    )

                # 2e. Update completed continuation
                if completed_continuation_id is not None:
                    self._connection.execute(
                        """
                        UPDATE continuations
                        SET status = ?, updated_at = ?
                        WHERE continuation_id = ?
                        """,
                        (completed_continuation_status, now, completed_continuation_id),
                    )

                # 3. Append events
                current_seq = curr_seq
                first_sequence_by_type: dict[str, int] = {}
                for draft in events:
                    current_seq += 1
                    first_sequence_by_type.setdefault(draft.event_type, current_seq)
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

                # 4. Update session
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

                if checkpoint_kind is not None and checkpoint_payload is not None:
                    bound_payload = dict(checkpoint_payload)
                    for bound_field, bound_event_type in checkpoint_bindings:
                        bound_sequence = first_sequence_by_type.get(bound_event_type)
                        if bound_sequence is None:
                            raise StoreConflict(f"checkpoint binding {bound_field!r} has no {bound_event_type!r} event in this commit")
                        bound_payload[bound_field] = bound_sequence
                    checkpoint_json = canonical_json(bound_payload)
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

    def _apply_state_updates(
        self,
        state_updates: Sequence[StagedStateUpdate],
        *,
        default_revision_id: str,
        now: int,
    ) -> None:
        """Apply owner state copies with their optimistic-concurrency obligation, inside an open transaction."""
        seen: set[tuple[str, str, str]] = set()
        for st in state_updates:
            key = (st.scope, st.scope_key, st.owner)
            if key in seen:
                raise StoreConflict(f"duplicate state update for {st.owner!r} in {st.scope}:{st.scope_key} in one commit")
            seen.add(key)
            payload_json = canonical_json(st.payload)
            actual_schema_ver = max(1, st.schema_version)
            actual_rev_id = st.revision_id or default_revision_id
            if st.expected_version == 0:
                try:
                    self._connection.execute(
                        """
                        INSERT INTO plugin_state (scope, scope_key, owner, version, schema_version, revision_id, payload_json, updated_at)
                        VALUES (?, ?, ?, 1, ?, ?, ?, ?)
                        """,
                        (st.scope, st.scope_key, st.owner, actual_schema_ver, actual_rev_id, payload_json, now),
                    )
                except sqlite3.IntegrityError as exc:
                    raise StoreConflict(f"state already exists for owner {st.owner!r} in {st.scope}:{st.scope_key}") from exc
            else:
                c = self._connection.execute(
                    """
                    UPDATE plugin_state
                    SET version = version + 1, schema_version = ?, revision_id = ?, payload_json = ?, updated_at = ?
                    WHERE scope = ? AND scope_key = ? AND owner = ? AND version = ?
                    """,
                    (actual_schema_ver, actual_rev_id, payload_json, now, st.scope, st.scope_key, st.owner, st.expected_version),
                )
                if c.rowcount == 0:
                    raise StoreConflict(f"state CAS conflict for {st.owner!r} in {st.scope}:{st.scope_key}: expected version {st.expected_version}")

    def commit_activation(
        self,
        *,
        workspace: str,
        session_id: str,
        expected_sequence: int,
        expected_run_id: str | None,
        expected_revision_id: str,
        target_revision_id: str,
        target_payload: Mapping[str, object],
        run_id: str,
        status: str,
        events: Sequence[EventDraft],
        checkpoint_kind: Literal["ready", "approval_wait", "terminal", "interrupted"] | None,
        checkpoint_payload: Mapping[str, object] | None,
        state_updates: Sequence[StagedStateUpdate] = (),
        cancelled_owners: Sequence[str] = (),
        cleared_budget_owners: Sequence[str] = (),
    ) -> SessionRecord:
        """One CAS transaction that switches the workspace to a new capability revision.

        Covers, atomically: the immutable revision row, every migrated/deactivated
        owner state copy, the workspace pointer, EVERY session ref in the workspace
        (including the acting running session), retargeted or cancelled queued work,
        cleared budgets, the acting session's events/checkpoint, and therefore the
        durable activation request consumption that those events record. Blockers
        are re-verified inside the transaction, so a racing writer cannot slip a
        second generation in.
        """
        target_text = canonical_json(target_payload)
        computed_target = hashlib.sha256(target_text.encode("utf-8")).hexdigest()
        if computed_target != target_revision_id:
            raise ValueError("target revision payload does not match its declared identity")
        with self._lock:
            self._check_open()
            in_transaction = False
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                in_transaction = True
                now = int(time.time() * 1000)

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
                _, row_workspace, _, curr_revision_id, curr_run_id, curr_turn, curr_seq = row
                if row_workspace != workspace:
                    raise StoreConflict(f"session {session_id!r} belongs to workspace {row_workspace!r}")
                if curr_seq != expected_sequence:
                    raise StoreConflict(f"sequence mismatch for session {session_id!r}: expected {expected_sequence}, got {curr_seq}")
                if expected_run_id is not None and curr_run_id != expected_run_id:
                    raise StoreConflict(f"run_id mismatch for session {session_id!r}: expected {expected_run_id!r}, got {curr_run_id!r}")
                if curr_revision_id != expected_revision_id:
                    raise StoreConflict(
                        f"active revision mismatch for session {session_id!r}: active is {curr_revision_id!r}, got {expected_revision_id!r}"
                    )
                if target_revision_id == expected_revision_id:
                    raise StoreConflict("the workspace already runs the target capability revision")

                blockers = self._activation_blockers_locked(workspace, session_id, exclude_claimed_in_session=True)
                if blockers:
                    raise StoreConflict(f"activation blocked: {'; '.join(blockers)}")

                self._connection.execute(
                    """
                    INSERT INTO revisions (revision_id, created_at, payload_json)
                    VALUES (?, ?, ?)
                    ON CONFLICT (revision_id) DO NOTHING
                    """,
                    (target_revision_id, now, target_text),
                )
                self._connection.execute(
                    """
                    INSERT INTO workspace_state (workspace, active_revision_id, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT (workspace) DO UPDATE SET
                        active_revision_id = excluded.active_revision_id,
                        updated_at = excluded.updated_at
                    """,
                    (workspace, target_revision_id, now),
                )
                self._connection.execute(
                    """
                    UPDATE sessions
                    SET active_revision_id = ?, updated_at = ?
                    WHERE workspace = ?
                    """,
                    (target_revision_id, now, workspace),
                )

                self._apply_state_updates(state_updates, default_revision_id=target_revision_id, now=now)

                for owner in cancelled_owners:
                    self._connection.execute(
                        """
                        UPDATE continuations
                        SET status = 'cancelled', updated_at = ?
                        WHERE status IN ('pending', 'claimed') AND owner = ?
                          AND session_id IN (SELECT session_id FROM sessions WHERE workspace = ?)
                        """,
                        (now, owner, workspace),
                    )
                    self._connection.execute(
                        """
                        DELETE FROM session_budgets
                        WHERE owner = ?
                          AND session_id IN (SELECT session_id FROM sessions WHERE workspace = ?)
                        """,
                        (owner, workspace),
                    )
                cleared = set(cancelled_owners) | set(cleared_budget_owners)
                for owner in cleared:
                    self._connection.execute(
                        """
                        DELETE FROM session_budgets
                        WHERE owner = ?
                          AND session_id IN (SELECT session_id FROM sessions WHERE workspace = ?)
                        """,
                        (owner, workspace),
                    )
                # Queued work that stays enabled keeps its provenance and shared root
                # budget, but is rebound to the revision that will execute it.
                self._connection.execute(
                    """
                    UPDATE continuations
                    SET revision_id = ?, updated_at = ?
                    WHERE session_id = ? AND status IN ('pending', 'claimed')
                    """,
                    (target_revision_id, now, session_id),
                )

                current_seq = curr_seq
                first_sequence_by_type: dict[str, int] = {}
                for draft in events:
                    current_seq += 1
                    first_sequence_by_type.setdefault(draft.event_type, current_seq)
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
                            target_revision_id,
                            canonical_json(draft.payload),
                        ),
                    )

                self._connection.execute(
                    """
                    UPDATE sessions
                    SET status = ?, run_id = ?, updated_at = ?, last_event_sequence = ?
                    WHERE session_id = ?
                    """,
                    (status, run_id, now, current_seq, session_id),
                )

                if checkpoint_kind is not None and checkpoint_payload is not None:
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
                        (session_id, checkpoint_kind, target_revision_id, run_id, current_seq, canonical_json(checkpoint_payload), now),
                    )

                self._connection.execute("COMMIT")
                in_transaction = False
                return SessionRecord(
                    session_id=session_id,
                    workspace=workspace,
                    status=status,
                    active_revision_id=target_revision_id,
                    run_id=run_id,
                    turn=curr_turn,
                    last_event_sequence=current_seq,
                )
            except Exception:
                if in_transaction:
                    self._connection.execute("ROLLBACK")
                raise

    def workspace_current(self, workspace: str) -> str | None:
        """The capability revision the workspace is currently running, or None before first adoption."""
        with self._lock:
            self._check_open()
            cursor = self._connection.execute(
                "SELECT active_revision_id FROM workspace_state WHERE workspace = ?",
                (workspace,),
            )
            row = cursor.fetchone()
            return None if row is None else str(row[0])

    def record_workspace_revision(self, workspace: str, revision_id: str) -> None:
        """Adopt a revision for a workspace without ever overwriting an existing pointer."""
        with self._lock:
            self._check_open()
            self._connection.execute(
                """
                INSERT INTO workspace_state (workspace, active_revision_id, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT (workspace) DO NOTHING
                """,
                (workspace, revision_id, int(time.time() * 1000)),
            )

    def plugin_state_rows(self, scope: str, scope_key: str) -> tuple[PluginStateRow, ...]:
        """Every owner state row in one scope, with its core-owned version metadata."""
        with self._lock:
            self._check_open()
            cursor = self._connection.execute(
                """
                SELECT owner, scope, scope_key, payload_json, version, schema_version, revision_id
                FROM plugin_state
                WHERE scope = ? AND scope_key = ?
                ORDER BY owner ASC
                """,
                (scope, scope_key),
            )
            return tuple(
                PluginStateRow(
                    owner=str(row[0]),
                    scope=str(row[1]),
                    scope_key=str(row[2]),
                    payload=own_json_object(json.loads(row[3])),
                    version=int(row[4]),
                    schema_version=int(row[5]),
                    revision_id=str(row[6]),
                )
                for row in cursor.fetchall()
            )

    def _activation_blockers_locked(self, workspace: str, acting_session_id: str, *, exclude_claimed_in_session: bool) -> list[str]:
        """Blockers that must stop a capability switch; callers hold the connection lock."""
        blockers: list[str] = []
        cursor = self._connection.execute(
            """
            SELECT session_id, status
            FROM sessions
            WHERE workspace = ? AND session_id <> ? AND status IN ('running', 'waiting')
            ORDER BY session_id ASC
            """,
            (workspace, acting_session_id),
        )
        blockers.extend(f"session {row[0]} is {row[1]}" for row in cursor.fetchall())
        cursor = self._connection.execute(
            """
            SELECT session_id, request_id
            FROM action_confirmations
            WHERE status = 'pending' AND session_id <> ?
              AND session_id IN (SELECT session_id FROM sessions WHERE workspace = ?)
            ORDER BY request_id ASC
            """,
            (acting_session_id, workspace),
        )
        blockers.extend(f"session {row[0]} awaits the decision for confirmation {row[1]}" for row in cursor.fetchall())
        cursor = self._connection.execute(
            """
            SELECT session_id, continuation_id, owner, status
            FROM continuations
            WHERE status IN ('pending', 'claimed')
              AND session_id IN (SELECT session_id FROM sessions WHERE workspace = ?)
            ORDER BY continuation_id ASC
            """,
            (workspace,),
        )
        for row in cursor.fetchall():
            if row[0] == acting_session_id:
                # The acting session may keep its own queued work; it is retargeted
                # by the activation transaction instead of being rejected.
                continue
            blockers.append(f"session {row[0]} has a {row[3]} continuation {row[1]} owned by {row[2]}")
        if exclude_claimed_in_session:
            cursor = self._connection.execute(
                """
                SELECT continuation_id, owner
                FROM continuations
                WHERE session_id = ? AND status = 'claimed'
                ORDER BY continuation_id ASC
                """,
                (acting_session_id,),
            )
            claimed = cursor.fetchall()
            if len(claimed) > 1:
                blockers.append(f"session {acting_session_id} has multiple claimed continuations")
        return blockers

    def activation_blockers(self, workspace: str, acting_session_id: str) -> tuple[str, ...]:
        """Read-only view of everything that must be idle before a capability switch."""
        with self._lock:
            self._check_open()
            return tuple(self._activation_blockers_locked(workspace, acting_session_id, exclude_claimed_in_session=False))

    def plugin_state(self, owner: str, scope: str, scope_key: str) -> tuple[Mapping[str, object] | None, int]:
        """Fetch current plugin state and version for the given owner, scope, and scope_key."""
        with self._lock:
            self._check_open()
            cursor = self._connection.execute(
                """
                SELECT payload_json, version
                FROM plugin_state
                WHERE scope = ? AND scope_key = ? AND owner = ?
                """,
                (scope, scope_key, owner),
            )
            row = cursor.fetchone()
            if row is None:
                return (None, 0)
            return (own_json_object(json.loads(row[0])), row[1])

    def replace_plugin_state(
        self,
        owner: str,
        scope: str,
        scope_key: str,
        payload: Mapping[str, object],
        expected_version: int,
    ) -> int:
        """Optimistic CAS state replacement for a plugin owner."""
        with self._lock:
            self._check_open()
            now = int(time.time() * 1000)
            payload_json = canonical_json(payload)
            if expected_version == 0:
                try:
                    self._connection.execute(
                        """
                        INSERT INTO plugin_state (scope, scope_key, owner, version, payload_json, updated_at)
                        VALUES (?, ?, ?, 1, ?, ?)
                        """,
                        (scope, scope_key, owner, payload_json, now),
                    )
                    return 1
                except sqlite3.IntegrityError as exc:
                    raise StoreConflict(f"state already exists for owner {owner!r} in {scope}:{scope_key}") from exc
            else:
                cursor = self._connection.execute(
                    """
                    UPDATE plugin_state
                    SET version = version + 1, payload_json = ?, updated_at = ?
                    WHERE scope = ? AND scope_key = ? AND owner = ? AND version = ?
                    """,
                    (payload_json, now, scope, scope_key, owner, expected_version),
                )
                if cursor.rowcount == 0:
                    raise StoreConflict(
                        f"optimistic concurrency check failed for {owner!r} in {scope}:{scope_key}: expected version {expected_version}"
                    )
                return expected_version + 1

    def confirmation(self, request_id: str) -> ActionConfirmationRecord | None:
        """Fetch an action confirmation record by request ID."""
        with self._lock:
            self._check_open()
            cursor = self._connection.execute(
                """
                SELECT request_id, session_id, owner, command_name, arguments_json, revision_id,
                       expected_state_version, ui_type, ui_data_json, status, created_at, decided_at
                FROM action_confirmations
                WHERE request_id = ?
                """,
                (request_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return ActionConfirmationRecord(
                request_id=row[0],
                session_id=row[1],
                owner=row[2],
                command_name=row[3],
                arguments=json.loads(row[4]),
                revision_id=row[5],
                expected_state_version=row[6],
                ui_type=row[7],
                ui_data=json.loads(row[8]),
                status=row[9],
                created_at=row[10],
                decided_at=row[11],
            )

    def decide_confirmation(self, request_id: str, decision: str) -> None:
        """Transition a pending confirmation record to allowed, denied, or stale."""
        with self._lock:
            self._check_open()
            now = int(time.time() * 1000)
            cursor = self._connection.execute(
                """
                UPDATE action_confirmations
                SET status = ?, decided_at = ?
                WHERE request_id = ? AND status = 'pending'
                """,
                (decision, now, request_id),
            )
            if cursor.rowcount == 0:
                raise StoreConflict(f"confirmation {request_id!r} is not in pending state or does not exist")

    def enqueue_continuation(
        self,
        continuation_id: str,
        owner: str,
        session_id: str,
        source: str,
        run_id: str,
        revision_id: str,
        payload: Mapping[str, object],
        *,
        idempotency_key: str | None = None,
        root_id: str | None = None,
        root_budget: int | None = None,
    ) -> str:
        """Directly enqueue a continuation in pending state, optionally opening its root budget."""
        with self._lock:
            self._check_open()
            now = int(time.time() * 1000)
            if idempotency_key is not None:
                cursor = self._connection.execute(
                    """
                    SELECT continuation_id FROM continuations
                    WHERE session_id = ? AND owner = ? AND idempotency_key = ?
                    """,
                    (session_id, owner, idempotency_key),
                )
                row = cursor.fetchone()
                if row is not None:
                    return row[0]
            if root_id is None and root_budget is not None:
                root_id = continuation_id
            if root_budget is not None and root_id is not None:
                self._connection.execute(
                    """
                    INSERT OR IGNORE INTO root_budgets (root_id, budget, reserved, created_at, updated_at)
                    VALUES (?, ?, 0, ?, ?)
                    """,
                    (root_id, max(0, root_budget), now, now),
                )
            try:
                self._connection.execute(
                    """
                    INSERT INTO continuations (
                        continuation_id, owner, session_id, source, run_id, revision_id,
                        idempotency_key, root_id, status, payload_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                    """,
                    (
                        continuation_id,
                        owner,
                        session_id,
                        source,
                        run_id,
                        revision_id,
                        idempotency_key,
                        root_id,
                        canonical_json(payload),
                        now,
                        now,
                    ),
                )
                return continuation_id
            except sqlite3.IntegrityError as exc:
                raise StoreConflict(f"continuation {continuation_id!r} already exists") from exc

    def ensure_root_budget(self, root_id: str, budget: int) -> None:
        """Open a root action budget; an existing root keeps its original limit."""
        with self._lock:
            self._check_open()
            now = int(time.time() * 1000)
            self._connection.execute(
                """
                INSERT OR IGNORE INTO root_budgets (root_id, budget, reserved, created_at, updated_at)
                VALUES (?, ?, 0, ?, ?)
                """,
                (root_id, max(0, budget), now, now),
            )

    def root_budget_state(self, root_id: str) -> tuple[int, int] | None:
        """Return (budget, reserved) for a root action chain, or None when unknown."""
        with self._lock:
            self._check_open()
            cursor = self._connection.execute(
                "SELECT budget, reserved FROM root_budgets WHERE root_id = ?",
                (root_id,),
            )
            row = cursor.fetchone()
            return None if row is None else (int(row[0]), int(row[1]))

    def claim_continuations(
        self,
        session_id: str,
        limit: int,
        run_id: str,
    ) -> tuple[ContinuationRecord, ...]:
        """Atomically claim up to `limit` pending continuations for a session under run_id."""
        with self._lock:
            self._check_open()
            now = int(time.time() * 1000)
            capped_limit = max(1, limit)
            in_transaction = False
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                in_transaction = True
                cursor = self._connection.execute(
                    """
                    SELECT continuation_id, owner, session_id, source, run_id, revision_id,
                           idempotency_key, root_id, status, payload_json, created_at, updated_at
                    FROM continuations
                    WHERE session_id = ? AND status = 'pending'
                    ORDER BY created_at ASC
                    LIMIT ?
                    """,
                    (session_id, capped_limit),
                )
                rows = cursor.fetchall()
                records: list[ContinuationRecord] = []
                for row in rows:
                    cid = row[0]
                    self._connection.execute(
                        """
                        UPDATE continuations
                        SET status = 'claimed', run_id = ?, updated_at = ?
                        WHERE continuation_id = ?
                        """,
                        (run_id, now, cid),
                    )
                    records.append(
                        ContinuationRecord(
                            continuation_id=row[0],
                            owner=row[1],
                            session_id=row[2],
                            source=row[3],
                            run_id=run_id,
                            revision_id=row[5],
                            idempotency_key=row[6],
                            status="claimed",
                            payload=own_json_object(json.loads(row[9])),
                            created_at=row[10],
                            updated_at=now,
                            root_id=row[7],
                        )
                    )
                self._connection.execute("COMMIT")
                in_transaction = False
                return tuple(records)
            except Exception:
                if in_transaction:
                    self._connection.execute("ROLLBACK")
                raise

    def continuation_id_for_idempotency(self, session_id: str, owner: str, idempotency_key: str) -> str | None:
        """Return the durable continuation id already recorded for an idempotency key."""
        with self._lock:
            self._check_open()
            cursor = self._connection.execute(
                """
                SELECT continuation_id FROM continuations
                WHERE session_id = ? AND owner = ? AND idempotency_key = ?
                """,
                (session_id, owner, idempotency_key),
            )
            row = cursor.fetchone()
            return None if row is None else str(row[0])

    def continuation_status(self, continuation_id: str) -> str | None:
        """Return the durable status of one continuation row, or None when absent."""
        with self._lock:
            self._check_open()
            cursor = self._connection.execute(
                "SELECT status FROM continuations WHERE continuation_id = ?",
                (continuation_id,),
            )
            row = cursor.fetchone()
            return None if row is None else str(row[0])

    def continuation_owner(self, continuation_id: str) -> str | None:
        """Return the owning plugin of one continuation row, or None when absent."""
        with self._lock:
            self._check_open()
            cursor = self._connection.execute(
                "SELECT owner FROM continuations WHERE continuation_id = ?",
                (continuation_id,),
            )
            row = cursor.fetchone()
            return None if row is None else str(row[0])

    def pending_continuations(
        self,
        session_id: str,
        limit: int = 1,
    ) -> tuple[ContinuationRecord, ...]:
        """Fetch pending continuations in order of creation."""
        with self._lock:
            self._check_open()
            cursor = self._connection.execute(
                """
                SELECT continuation_id, owner, session_id, source, run_id, revision_id,
                       idempotency_key, root_id, status, payload_json, created_at, updated_at
                FROM continuations
                WHERE session_id = ? AND status = 'pending'
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (session_id, max(1, limit)),
            )
            rows = cursor.fetchall()
            return tuple(
                ContinuationRecord(
                    continuation_id=row[0],
                    owner=row[1],
                    session_id=row[2],
                    source=row[3],
                    run_id=row[4],
                    revision_id=row[5],
                    idempotency_key=row[6],
                    status=row[8],
                    payload=own_json_object(json.loads(row[9])),
                    created_at=row[10],
                    updated_at=row[11],
                    root_id=row[7],
                )
                for row in rows
            )

    def claimed_continuations(
        self,
        session_id: str,
        limit: int = 100,
    ) -> tuple[ContinuationRecord, ...]:
        """Fetch previously claimed continuations for recovery in order of creation."""
        with self._lock:
            self._check_open()
            cursor = self._connection.execute(
                """
                SELECT continuation_id, owner, session_id, source, run_id, revision_id,
                       idempotency_key, root_id, status, payload_json, created_at, updated_at
                FROM continuations
                WHERE session_id = ? AND status = 'claimed'
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (session_id, max(1, limit)),
            )
            rows = cursor.fetchall()
            return tuple(
                ContinuationRecord(
                    continuation_id=row[0],
                    owner=row[1],
                    session_id=row[2],
                    source=row[3],
                    run_id=row[4],
                    revision_id=row[5],
                    idempotency_key=row[6],
                    status=row[8],
                    payload=own_json_object(json.loads(row[9])),
                    created_at=row[10],
                    updated_at=row[11],
                    root_id=row[7],
                )
                for row in rows
            )

    def complete_continuation(self, continuation_id: str, status: Literal["completed", "failed", "cancelled"] = "completed") -> None:
        """Mark a claimed continuation as completed, failed, or cancelled."""
        with self._lock:
            self._check_open()
            now = int(time.time() * 1000)
            cursor = self._connection.execute(
                """
                UPDATE continuations
                SET status = ?, updated_at = ?
                WHERE continuation_id = ?
                """,
                (status, now, continuation_id),
            )
            if cursor.rowcount == 0:
                raise StoreConflict(f"continuation {continuation_id!r} does not exist")

    def claim_and_start_continuation(
        self,
        session_id: str,
        run_id: str,
        logical_turn: str,
        *,
        revision_id: str,
        chain_step: int = 0,
        action_budget_remaining: int = 32,
    ) -> tuple[ContinuationRecord, SessionRecord]:
        """Atomically claim ONE pending continuation, emit continuation_started, advance session, and commit ready CP."""
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
                _, workspace, status, curr_revision_id, curr_run_id, curr_turn, curr_seq = row
                if curr_revision_id != revision_id:
                    raise StoreConflict(f"active revision mismatch for session {session_id!r}: active is {curr_revision_id!r}, got {revision_id!r}")

                now = int(time.time() * 1000)

                c_cursor = self._connection.execute(
                    """
                    SELECT continuation_id, owner, session_id, source, run_id, revision_id,
                           idempotency_key, root_id, status, payload_json, created_at, updated_at
                    FROM continuations
                    WHERE session_id = ? AND status = 'pending'
                    ORDER BY created_at ASC
                    LIMIT 1
                    """,
                    (session_id,),
                )
                cont_row = c_cursor.fetchone()
                if cont_row is None:
                    c_cursor2 = self._connection.execute(
                        """
                        SELECT continuation_id, owner, session_id, source, run_id, revision_id,
                               idempotency_key, root_id, status, payload_json, created_at, updated_at
                        FROM continuations
                        WHERE session_id = ? AND status = 'claimed'
                        ORDER BY created_at ASC
                        LIMIT 1
                        """,
                        (session_id,),
                    )
                    claimed_row = c_cursor2.fetchone()
                    if claimed_row is None:
                        raise StoreConflict(f"no pending or claimed continuations for session {session_id!r}")
                    cont_row = claimed_row
                    claimed_record = ContinuationRecord(
                        continuation_id=cont_row[0],
                        owner=cont_row[1],
                        session_id=cont_row[2],
                        source=cont_row[3],
                        run_id=cont_row[4],
                        revision_id=cont_row[5],
                        idempotency_key=cont_row[6],
                        status="claimed",
                        payload=own_json_object(json.loads(cont_row[9])),
                        created_at=cont_row[10],
                        updated_at=cont_row[11],
                        root_id=cont_row[7],
                    )
                    self._connection.execute("COMMIT")
                    in_transaction = False
                    return claimed_record, SessionRecord(
                        session_id=session_id,
                        workspace=workspace,
                        status=status,
                        active_revision_id=curr_revision_id,
                        run_id=curr_run_id,
                        turn=curr_turn,
                        last_event_sequence=curr_seq,
                    )

                cid = cont_row[0]
                self._connection.execute(
                    """
                    UPDATE continuations
                    SET status = 'claimed', run_id = ?, updated_at = ?
                    WHERE continuation_id = ?
                    """,
                    (run_id, now, cid),
                )

                cont_record = ContinuationRecord(
                    continuation_id=cont_row[0],
                    owner=cont_row[1],
                    session_id=cont_row[2],
                    source=cont_row[3],
                    run_id=run_id,
                    revision_id=revision_id,
                    idempotency_key=cont_row[6],
                    status="claimed",
                    payload=own_json_object(json.loads(cont_row[9])),
                    created_at=cont_row[10],
                    updated_at=now,
                    root_id=cont_row[7],
                )

                new_seq = curr_seq + 1
                disp_payload = canonical_json({"continuation_id": cont_record.continuation_id, "payload": cont_record.payload})
                self._connection.execute(
                    """
                    INSERT INTO session_events (
                        session_id, sequence, run_id, event_type, source, actor, revision_id, payload_json
                    ) VALUES (?, ?, ?, 'kernel.continuation_started', ?, 'plugin', ?, ?)
                    """,
                    (session_id, new_seq, run_id, f"plugin.{cont_record.owner}", revision_id, disp_payload),
                )

                new_turn = curr_turn + 1
                self._connection.execute(
                    """
                    UPDATE sessions
                    SET status = 'running',
                        run_id = ?,
                        turn = ?,
                        updated_at = ?,
                        last_event_sequence = ?
                    WHERE session_id = ?
                    """,
                    (run_id, new_turn, now, new_seq, session_id),
                )

                checkpoint_payload = {
                    "step": 0,
                    "continuation_id": cont_record.continuation_id,
                    "root_id": cont_record.root_id or cont_record.continuation_id,
                    "logical_turn": logical_turn,
                    "chain_step": chain_step,
                    "action_budget_remaining": action_budget_remaining,
                }
                checkpoint_json = canonical_json(checkpoint_payload)
                self._connection.execute(
                    """
                    INSERT INTO checkpoints (
                        session_id, kind, revision_id, run_id, last_event_sequence, payload_json, updated_at
                    ) VALUES (?, 'ready', ?, ?, ?, ?, ?)
                    ON CONFLICT (session_id) DO UPDATE SET
                        kind = excluded.kind,
                        revision_id = excluded.revision_id,
                        run_id = excluded.run_id,
                        last_event_sequence = excluded.last_event_sequence,
                        payload_json = excluded.payload_json,
                        updated_at = excluded.updated_at
                    """,
                    (session_id, revision_id, run_id, new_seq, checkpoint_json, now),
                )

                self._connection.execute("COMMIT")
                in_transaction = False
                return cont_record, SessionRecord(
                    session_id=session_id,
                    workspace=workspace,
                    status="running",
                    active_revision_id=revision_id,
                    run_id=run_id,
                    turn=new_turn,
                    last_event_sequence=new_seq,
                )
            except Exception:
                if in_transaction:
                    self._connection.execute("ROLLBACK")
                raise

    def session_budgets(self, session_id: str) -> tuple[PluginBudget, ...]:
        """Fetch all active budget constraints for a session."""
        with self._lock:
            self._check_open()
            cursor = self._connection.execute(
                """
                SELECT owner, since_sequence, max_input_tokens, max_output_tokens, max_total_tokens, max_cost_usd, max_elapsed_seconds
                FROM session_budgets
                WHERE session_id = ?
                """,
                (session_id,),
            )
            rows = cursor.fetchall()
            return tuple(
                PluginBudget(
                    owner=r[0],
                    since_sequence=r[1],
                    max_input_tokens=r[2],
                    max_output_tokens=r[3],
                    max_total_tokens=r[4],
                    max_cost_usd=r[5],
                    max_elapsed_seconds=r[6],
                )
                for r in rows
            )

    def owner_budget(self, session_id: str, owner: str) -> PluginBudget | None:
        """Fetch active budget constraint for the bound owner in a session."""
        with self._lock:
            self._check_open()
            cursor = self._connection.execute(
                """
                SELECT owner, since_sequence, max_input_tokens, max_output_tokens, max_total_tokens, max_cost_usd, max_elapsed_seconds
                FROM session_budgets
                WHERE session_id = ? AND owner = ?
                """,
                (session_id, owner),
            )
            r = cursor.fetchone()
            if r is None:
                return None
            return PluginBudget(
                owner=r[0],
                since_sequence=r[1],
                max_input_tokens=r[2],
                max_output_tokens=r[3],
                max_total_tokens=r[4],
                max_cost_usd=r[5],
                max_elapsed_seconds=r[6],
            )

    def has_observer_receipt(self, session_id: str, event_sequence: int, owner: str) -> bool:
        """Whether this (source fact event, owner) pair was already delivered exactly once."""
        with self._lock:
            self._check_open()
            cursor = self._connection.execute(
                """
                SELECT 1 FROM observer_receipts
                WHERE session_id = ? AND event_sequence = ? AND owner = ?
                """,
                (session_id, event_sequence, owner),
            )
            return cursor.fetchone() is not None

    def cancel_pending_continuations(self, session_id: str, owner: str, *, continuation_id: str | None = None) -> int:
        """Directly cancel pending continuations owned by owner in session."""
        with self._lock:
            self._check_open()
            now = int(time.time() * 1000)
            if continuation_id is not None:
                cursor = self._connection.execute(
                    """
                    UPDATE continuations
                    SET status = 'cancelled', updated_at = ?
                    WHERE session_id = ? AND owner = ? AND continuation_id = ? AND status = 'pending'
                    """,
                    (now, session_id, owner, continuation_id),
                )
            else:
                cursor = self._connection.execute(
                    """
                    UPDATE continuations
                    SET status = 'cancelled', updated_at = ?
                    WHERE session_id = ? AND owner = ? AND status = 'pending'
                    """,
                    (now, session_id, owner),
                )
            return cursor.rowcount
