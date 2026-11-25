"""SQLite database-file identity, schema loading, and the shared connection base.

The store owns exactly one connection and one re-entrant lock. This module holds the
file-level checks that refuse foreign/empty/legacy files, the read-only inspection
helpers used outside a live store, and the `_ConnectionStore` base whose attributes the
domain mixins share so the package has one locking discipline instead of one per domain.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable, Mapping
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING

from ...sdk.json_values import json_wire_object
from ._values import APPLICATION_ID, SCHEMA_VERSION, ForeignDatabaseError, StorageError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ._values import (
        CheckpointKind,
        ContinuationStatus,
        EventDraft,
        ObserverReceiptRecord,
        SessionRecord,
        SessionStatus,
        StagedBudgetUpdate,
        StagedCancellation,
        StagedConfirmation,
        StagedContinuationDraft,
        StagedStateUpdate,
        StagedWakeup,
        StagedWakeupCancel,
    )


def _load_schema_sql() -> str:
    return files("voidcode.core.storage").joinpath("schema.sql").read_text(encoding="utf-8")


def _check_regular_nonempty(path: Path) -> None:
    """Refuse paths that are not a usable pre-existing database file."""
    if not path.is_file():
        raise ForeignDatabaseError(f"database path exists but is not a regular file: {path}")
    if path.stat().st_size == 0:
        raise ForeignDatabaseError(f"refusing pre-existing empty database file: {path}")


def _read_file_identity(connection: sqlite3.Connection, path: Path) -> tuple[int, int]:
    """Read the file-header identity of an open database without writing anything."""
    try:
        app_id = connection.execute("PRAGMA application_id").fetchone()
        user_version = connection.execute("PRAGMA user_version").fetchone()
    except sqlite3.Error as exc:
        raise ForeignDatabaseError(f"unsupported database file: {path}") from exc
    return (int(app_id[0]), int(user_version[0]))


def _check_identity(app_id: int, user_version: int, path: Path) -> None:
    """Refuse any database whose file-header identity is not this build's own."""
    if app_id != APPLICATION_ID or user_version != SCHEMA_VERSION:
        raise ForeignDatabaseError(
            f"database is not a valid voidcode database: unsupported identity in {path}: "
            f"expected ({APPLICATION_ID:#x}, {SCHEMA_VERSION}), got ({app_id:#x}, {user_version})"
        )


def open_read_only(database: Path) -> sqlite3.Connection:
    """Open one core database for inspection, with writes refused by the connection itself.

    Uses a ``mode=ro`` URI plus ``PRAGMA query_only``, after the PRAGMA identity
    check a writer runs. Foreign files raise without a single byte written.
    """
    _check_regular_nonempty(database)
    try:
        connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise ForeignDatabaseError(f"unsupported database file: {database}") from exc
    try:
        app_id, user_version = _read_file_identity(connection, database)
        _check_identity(app_id, user_version, database)
    except Exception:
        connection.close()
        raise
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def inspect_existing_workspace(database: Path, workspace: str) -> tuple[str | None, Mapping[str, object] | None, int | None]:
    """Read one workspace's adopted revision without creating or changing anything.

    Returns ``(None, None, None)`` when there is no database, ``(None, None, schema)``
    when the workspace has no adopted revision, ``(revision_id, payload, schema)`` when
    it has one, and raises for a file that is not a core database. Never writes, never sets
    write pragmas. This build opens exactly its own schema; foreign files are refused.
    """
    if not database.exists():
        return (None, None, None)
    probe = open_read_only(database)
    try:
        identity = probe.execute("PRAGMA user_version").fetchone()
        schema = int(identity[0])
        cursor = probe.execute("SELECT active_revision_id FROM workspace_state WHERE workspace = ?", (workspace,))
        row = cursor.fetchone()
        if row is None:
            return (None, None, schema)
        revision_id = str(row["active_revision_id"])
        payload_row = probe.execute("SELECT payload_json FROM revisions WHERE revision_id = ?", (revision_id,)).fetchone()
        if payload_row is None:
            raise ForeignDatabaseError(f"database {database} points at missing revision {revision_id}")
        payload = json.loads(str(payload_row["payload_json"]))
        if not isinstance(payload, dict):
            raise ForeignDatabaseError(f"database {database} stores a malformed revision payload")
        return (revision_id, json_wire_object(payload), schema)
    finally:
        probe.close()


class _ConnectionStore:
    """Connection state and lifecycle shared by every domain mixin.

    One `SqliteStore` owns exactly one connection and one re-entrant lock; the mixins
    only ever touch them through these attributes, so the storage package has one
    locking discipline instead of one per domain.
    """

    _lock: threading.RLock
    _connection: sqlite3.Connection
    _closed: bool
    #: Optional in-process cancellation fence (set by DelegationManager).
    _cancel_fence_guard: Callable[[frozenset[str], str | None], str | None] | None

    if TYPE_CHECKING:
        # Static composition contract for the few calls one domain makes into a peer
        # mixin. `SqliteStore` supplies both at runtime; declaring them here keeps the
        # split modules independently type-checkable without runtime stubs or casts.
        def session(self, session_id: str) -> SessionRecord: ...

        def commit_staged(
            self,
            session_id: str,
            *,
            expected_sequence: int | None = None,
            expected_run_id: str | None = None,
            run_id: str,
            revision_id: str,
            events: tuple[EventDraft, ...] | Sequence[EventDraft],
            status: SessionStatus,
            checkpoint_kind: CheckpointKind | None = None,
            checkpoint_payload: Mapping[str, object] | None = None,
            staged_state: Sequence[StagedStateUpdate] = (),
            staged_continuations: Sequence[StagedContinuationDraft] = (),
            staged_cancellations: Sequence[StagedCancellation] = (),
            staged_budgets: Sequence[StagedBudgetUpdate] = (),
            staged_confirmations: Sequence[StagedConfirmation] = (),
            staged_wakeups: Sequence[StagedWakeup] = (),
            staged_wakeup_cancels: Sequence[StagedWakeupCancel] = (),
            observer_receipts: Sequence[ObserverReceiptRecord] = (),
            completed_continuation_id: str | None = None,
            completed_continuation_status: ContinuationStatus = ContinuationStatus.COMPLETED,
            checkpoint_bindings: Sequence[tuple[str, str]] = (),
            root_action_id: str | None = None,
            root_action_budget: int | None = None,
            root_budget_grant: tuple[str, int, str] | None = None,
            root_reservation: tuple[str, int | None] | None = None,
            start_turn: bool = False,
            ask_answer_guard: str | None = None,
        ) -> SessionRecord: ...

    def _check_open(self) -> None:
        if self._closed:
            raise StorageError("store is closed")
