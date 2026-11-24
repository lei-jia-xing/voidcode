"""SQLite storage facade for the VoidCode core, composing sessions, state, and continuations."""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Self

from .continuations import _ContinuationsMixin
from .records import (
    APPLICATION_ID,
    BACKGROUND_ACTION_SOURCE,
    DATABASE_FILENAME,
    MAX_CHILD_LABEL_BYTES,
    MAX_COMMITTED_EVENT_BYTES,
    MAX_CONTINUATION_PAYLOAD_BYTES,
    MAX_WAKEUP_PAYLOAD_BYTES,
    SCHEMA_VERSION,
    ActionConfirmationRecord,
    AskAnswerConflict,
    Checkpoint,
    CheckpointKind,
    ConfirmationStatus,
    ContinuationRecord,
    ContinuationStatus,
    EventDraft,
    FencedSubtree,
    ForeignDatabaseError,
    InvalidRevision,
    ObserverReceiptRecord,
    PendingFollowupRecord,
    PluginStateRow,
    RootBudgetExhausted,
    SequenceConflict,
    SessionRecord,
    SessionStatus,
    StagedBudgetUpdate,
    StagedCancellation,
    StagedConfirmation,
    StagedContinuationDraft,
    StagedStateUpdate,
    StagedWakeup,
    StagedWakeupCancel,
    StaleRevision,
    StorageError,
    StoreConflict,
    StoredEvent,
    StoredValueError,
    UnknownConfirmation,
    UnknownContinuation,
    UnknownSession,
    UnknownWorkspace,
    WaitingAskPark,
    WakeupRecord,
    WakeupSkip,
    WindowProjectionRecord,
    _check_identity,
    _check_regular_nonempty,
    _load_schema_sql,
    _read_file_identity,
    canonical_json,
    inspect_existing_workspace,
    open_read_only,
)
from .sessions import _SessionsMixin
from .state import _StateMixin

__all__ = [
    "APPLICATION_ID",
    "ActionConfirmationRecord",
    "AskAnswerConflict",
    "BACKGROUND_ACTION_SOURCE",
    "Checkpoint",
    "CheckpointKind",
    "ConfirmationStatus",
    "ContinuationRecord",
    "ContinuationStatus",
    "DATABASE_FILENAME",
    "EventDraft",
    "FencedSubtree",
    "ForeignDatabaseError",
    "InvalidRevision",
    "MAX_CHILD_LABEL_BYTES",
    "MAX_COMMITTED_EVENT_BYTES",
    "MAX_CONTINUATION_PAYLOAD_BYTES",
    "MAX_WAKEUP_PAYLOAD_BYTES",
    "ObserverReceiptRecord",
    "PendingFollowupRecord",
    "PluginStateRow",
    "RootBudgetExhausted",
    "SCHEMA_VERSION",
    "SequenceConflict",
    "SessionRecord",
    "SessionStatus",
    "StagedBudgetUpdate",
    "StagedCancellation",
    "StagedConfirmation",
    "StagedContinuationDraft",
    "StagedStateUpdate",
    "StagedWakeup",
    "StagedWakeupCancel",
    "StaleRevision",
    "StorageError",
    "StoreConflict",
    "StoredEvent",
    "StoredValueError",
    "UnknownConfirmation",
    "UnknownContinuation",
    "UnknownSession",
    "UnknownWorkspace",
    "WaitingAskPark",
    "WakeupRecord",
    "WakeupSkip",
    "WindowProjectionRecord",
    "canonical_json",
    "inspect_existing_workspace",
    "open_read_only",
]


class SqliteStore(_SessionsMixin, _StateMixin, _ContinuationsMixin):
    """Concrete SQLite store for core sessions, revisions, events, and checkpoints."""

    _path: Path

    def __init__(self, path: Path | str) -> None:
        self._lock = threading.RLock()
        #: Optional in-process cancellation fence consulted inside store
        #: transactions (set by DelegationManager). The guard takes a
        #: caller-captured ancestor chain plus an optional child id, reads only
        #: in-memory fence state under its own lock, and never calls back into
        #: the store, so lock order stays lifecycle -> store and never inverts.
        self._cancel_fence_guard: Callable[[frozenset[str], str | None], str | None] | None = None
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
                _check_regular_nonempty(resolved_path)
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
            if not is_new:
                assert resolved_path is not None
                app_id, user_version = _read_file_identity(self._connection, resolved_path)
                _check_identity(app_id, user_version, resolved_path)
            self._connection.execute("PRAGMA busy_timeout = 5000")
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA foreign_keys = ON")
            if is_new:
                self._connection.executescript(_load_schema_sql())
                self._connection.execute("PRAGMA foreign_keys = ON")
                self._connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
                self._connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
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
