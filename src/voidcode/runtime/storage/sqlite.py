from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from time import sleep, time
from typing import Final, Literal, NoReturn, Protocol, final, runtime_checkable

from ..background.models import (
    BackgroundTaskState,
    BackgroundTaskStatus,
    DelegatedReminderStopCondition,
    StoredBackgroundTaskSummary,
)
from ..contracts import (
    RuntimeRequest,
    RuntimeResponse,
    RuntimeSessionResult,
    RuntimeSessionRevertMarker,
)
from ..effectiveness import ToolEffectivenessReport
from ..events import (
    EventEnvelope,
    EventSource,
)
from ..execution_ownership import EXECUTION_OWNERSHIP
from ..paths import sessions_db_path
from ..permission import PendingApproval
from ..question import PendingQuestion
from ..session import (
    SessionEntrySummary,
    SessionStatus,
    StoredSessionForestEntry,
    StoredSessionLineageEntry,
    StoredSessionSummary,
)
from .background_processes import _BackgroundProcessStorageMixin
from .background_tasks import _BackgroundTaskStorageMixin
from .diagnostics import _DiagnosticsStorageMixin
from .effectiveness import _EffectivenessStorageMixin
from .fork import _ForkStorageMixin
from .resume import _ResumeStorageMixin
from .revert import _RevertStorageMixin
from .rows import (
    IndexInfoRow,
    IndexListRow,
    SqliteMasterNameRow,
    StorageSequenceValueRow,
    TableInfoRow,
    decode_row,
    fetch_row,
    fetch_rows,
)
from .sessions import SessionEventsAfter, _SessionStorageMixin

# The storage schema is a cutover, not a migration: the in-session tree added
# ``session_events.parent_sequence`` and ``sessions.leaf_sequence``, so the
# version was reset to 1 and any database written before the change is now an
# incompatible generation (see ``_raise_schema_mismatch``).
SCHEMA_VERSION: Final[int] = 1


@runtime_checkable
class SessionStore(Protocol):
    def save_run(
        self,
        *,
        workspace: Path,
        request: RuntimeRequest,
        response: RuntimeResponse,
        clear_pending_approval: bool = True,
        seal_terminal_status: bool = True,
    ) -> None: ...

    def append_session_events(
        self,
        *,
        workspace: Path,
        session_id: str,
        events: tuple[tuple[str, EventSource, dict[str, object], str | None], ...],
        interrupted_checkpoint: dict[str, object] | None = None,
    ) -> tuple[EventEnvelope, ...]: ...

    def save_interrupted_checkpoint(
        self,
        *,
        workspace: Path,
        session_id: str,
        prompt: str,
        session_metadata: dict[str, object],
        tool_results: tuple[dict[str, object], ...],
        last_event_sequence: int,
        output: str | None = None,
        create_if_missing: bool = True,
        turn: int = 1,
        parent_session_id: str | None = None,
    ) -> None: ...

    def list_sessions(self, *, workspace: Path) -> tuple[StoredSessionSummary, ...]: ...

    def has_session(self, *, workspace: Path, session_id: str) -> bool: ...

    def load_session(self, *, workspace: Path, session_id: str) -> RuntimeResponse: ...

    def update_session_metadata(self, *, workspace: Path, session_id: str, metadata: dict[str, object]) -> None: ...

    def load_session_result(self, *, workspace: Path, session_id: str) -> RuntimeSessionResult: ...

    def revert_session(self, *, workspace: Path, session_id: str, sequence: int) -> RuntimeSessionRevertMarker: ...

    def undo_session(self, *, workspace: Path, session_id: str) -> RuntimeSessionRevertMarker: ...

    def unrevert_session(self, *, workspace: Path, session_id: str) -> RuntimeSessionRevertMarker | None: ...

    def rename_session(self, *, workspace: Path, session_id: str, title: str) -> None: ...

    def fork_session(
        self,
        *,
        workspace: Path,
        session_id: str,
        at_sequence: int | None = None,
    ) -> StoredSessionSummary: ...

    def session_lineage(self, *, workspace: Path, session_id: str | None = None) -> tuple[StoredSessionLineageEntry, ...]: ...

    def session_forest(self, *, workspace: Path) -> tuple[StoredSessionForestEntry, ...]: ...

    def checkout_session(self, *, workspace: Path, session_id: str, sequence: int) -> int: ...

    def session_path(self, *, workspace: Path, session_id: str, sequence: int | None = None) -> tuple[EventEnvelope, ...]: ...

    def session_entries(self, *, workspace: Path, session_id: str) -> tuple[SessionEntrySummary, ...]: ...

    def save_pending_approval(
        self,
        *,
        workspace: Path,
        request: RuntimeRequest,
        response: RuntimeResponse,
        pending_approval: PendingApproval,
    ) -> None: ...

    def load_pending_approval(self, *, workspace: Path, session_id: str) -> PendingApproval | None: ...

    def claim_pending_approval(self, *, workspace: Path, session_id: str, request_id: str) -> bool: ...

    def reconcile_resolved_approval(self, *, workspace: Path, session_id: str, request_id: str) -> bool: ...

    def clear_pending_approval(self, *, workspace: Path, session_id: str) -> None: ...

    def save_pending_question(
        self,
        *,
        workspace: Path,
        request: RuntimeRequest,
        response: RuntimeResponse,
        pending_question: PendingQuestion,
    ) -> None: ...

    def load_pending_question(self, *, workspace: Path, session_id: str) -> PendingQuestion | None: ...

    def clear_pending_question(self, *, workspace: Path, session_id: str) -> None: ...

    def load_resume_checkpoint(self, *, workspace: Path, session_id: str) -> dict[str, object] | None: ...
    def register_background_process(
        self,
        *,
        workspace: Path,
        process_id: str,
        owner_session_id: str | None,
        command: str,
        cwd: str,
        pid: int,
        process_group_id: int | None,
        process_identity: str | None,
        stdout_path: str,
        stderr_path: str,
    ) -> None: ...

    def load_background_process(self, *, workspace: Path, process_id: str) -> dict[str, object] | None: ...

    def list_background_processes(self, *, workspace: Path) -> tuple[dict[str, object], ...]: ...

    def mark_background_process_exit(
        self,
        *,
        workspace: Path,
        process_id: str,
        status: str,
        exit_code: int | None,
    ) -> None: ...
    def create_background_task(
        self,
        *,
        workspace: Path,
        task: BackgroundTaskState,
    ) -> None: ...

    def load_background_task(self, *, workspace: Path, task_id: str) -> BackgroundTaskState: ...

    def list_background_tasks(self, *, workspace: Path) -> tuple[StoredBackgroundTaskSummary, ...]: ...

    def list_queued_background_tasks(self, *, workspace: Path) -> tuple[StoredBackgroundTaskSummary, ...]: ...

    def list_running_background_tasks(self, *, workspace: Path) -> tuple[StoredBackgroundTaskSummary, ...]: ...

    def list_background_tasks_by_parent_session(self, *, workspace: Path, parent_session_id: str) -> tuple[StoredBackgroundTaskSummary, ...]: ...
    def list_background_tasks_by_parallel_group(
        self,
        *,
        workspace: Path,
        parallel_group_id: str,
        parent_session_id: str | None = None,
    ) -> tuple[StoredBackgroundTaskSummary, ...]: ...

    def load_background_task_by_child_session(self, *, workspace: Path, child_session_id: str) -> BackgroundTaskState | None: ...

    def mark_background_task_running(
        self,
        *,
        workspace: Path,
        task_id: str,
        session_id: str,
    ) -> BackgroundTaskState: ...

    def mark_background_task_terminal(
        self,
        *,
        workspace: Path,
        task_id: str,
        status: BackgroundTaskStatus,
        error: str | None = None,
    ) -> BackgroundTaskState: ...

    def mark_background_task_idle(
        self,
        *,
        workspace: Path,
        task_id: str,
    ) -> BackgroundTaskState: ...

    def mark_background_task_steered(
        self,
        *,
        workspace: Path,
        task_id: str,
        steer_prompt: str,
    ) -> BackgroundTaskState: ...

    def request_background_task_cancel(
        self,
        *,
        workspace: Path,
        task_id: str,
    ) -> BackgroundTaskState: ...

    def record_background_task_idle_reminder_eligible(
        self,
        *,
        workspace: Path,
        task_id: str,
        child_session_id: str,
        idle_episode_id: str,
        idle_detected_at_unix_ms: int,
    ) -> BackgroundTaskState: ...

    def mark_background_task_idle_reminder_sent(
        self,
        *,
        workspace: Path,
        task_id: str,
        idle_episode_id: str,
        reminder_sent_at_unix_ms: int,
    ) -> BackgroundTaskState: ...

    def stop_background_task_idle_reminder(
        self,
        *,
        workspace: Path,
        task_id: str,
        stop_condition: DelegatedReminderStopCondition,
    ) -> BackgroundTaskState: ...

    def persist_background_task_schema_validation(
        self,
        *,
        workspace: Path,
        task_id: str,
        structured_output_json: str | None,
        schema_validation_json: str,
    ) -> None: ...

    def fail_incomplete_background_tasks(
        self,
        *,
        workspace: Path,
        message: str,
        include_queued: bool = True,
    ) -> tuple[BackgroundTaskState, ...]: ...

    def storage_diagnostics(self, *, workspace: Path) -> dict[str, object]: ...

    def tool_effectiveness_report(self, *, workspace: Path) -> ToolEffectivenessReport: ...

    def prune_runtime_storage(
        self,
        *,
        workspace: Path,
        keep_sessions: int | None = None,
        keep_background_tasks: int | None = None,
        older_than: int | None = None,
    ) -> dict[str, int]: ...

    def reset_runtime_storage(self, *, workspace: Path) -> dict[str, object]: ...

    def truncate_session_events_after(self, *, workspace: Path, session_id: str, sequence: int) -> None: ...

    def load_session_status(self, *, workspace: Path, session_id: str) -> SessionStatus: ...

    def read_session_events_after(
        self,
        *,
        workspace: Path,
        session_id: str,
        after_sequence: int,
    ) -> SessionEventsAfter: ...


@runtime_checkable
class SessionEventAppender(Protocol):
    def append_session_event(
        self,
        *,
        workspace: Path,
        session_id: str,
        event_type: str,
        source: EventSource,
        payload: dict[str, object],
        dedupe_key: str | None = None,
    ) -> EventEnvelope | None: ...


@dataclass(frozen=True, slots=True)
class _SQLitePolicy:
    busy_timeout_ms: int = 5_000
    configure_retry_interval_seconds: float = 0.05
    synchronous: str = "NORMAL"
    wal_autocheckpoint_pages: int = 1_000


@dataclass(frozen=True, slots=True)
class _DatabaseBootstrap:
    """A verified database file: identity, schema version, and schema cookie."""

    identity: tuple[int, int]
    schema_version: int
    schema_cookie: int

    # Verified files skip _ensure_schema while sentinel reads still match.


_BOOTSTRAP_LOCK = Lock()
_BOOTSTRAPPED_DATABASES: dict[str, _DatabaseBootstrap] = {}


@final
class SqliteSessionStore(
    _BackgroundProcessStorageMixin,
    _BackgroundTaskStorageMixin,
    _SessionStorageMixin,
    _ResumeStorageMixin,
    _RevertStorageMixin,
    _ForkStorageMixin,
    _EffectivenessStorageMixin,
    _DiagnosticsStorageMixin,
):
    _database_path: Path
    _SCHEMA_VERSION = SCHEMA_VERSION
    _RESUME_CHECKPOINT_KINDS = frozenset({"approval_wait", "question_wait", "provider_failure_retryable", "terminal", "interrupted"})
    _SEQUENCE_SCOPES = ("sessions", "background_tasks", "auxiliary")
    _sqlite_policy = _SQLitePolicy()

    _DEFAULT_MAX_SESSIONS_PER_WORKSPACE: int = 50
    _DEFAULT_MAX_SESSION_AGE_DAYS: int = 30

    _CANONICAL_SCHEMA: dict[str, tuple[tuple[str, str, int, str | None, int], ...]] = {
        "sessions": (
            ("session_id", "TEXT", 1, None, 2),
            ("parent_session_id", "TEXT", 0, None, 0),
            ("workspace_id", "TEXT", 1, None, 1),
            ("status", "TEXT", 1, None, 0),
            ("turn", "INTEGER", 1, None, 0),
            ("prompt", "TEXT", 1, None, 0),
            ("output", "TEXT", 0, None, 0),
            ("metadata_json", "TEXT", 1, None, 0),
            ("pending_approval_json", "TEXT", 0, None, 0),
            ("pending_question_json", "TEXT", 0, None, 0),
            ("resume_checkpoint_json", "TEXT", 0, None, 0),
            ("created_at", "INTEGER", 1, None, 0),
            ("updated_at", "INTEGER", 1, None, 0),
            ("last_event_sequence", "INTEGER", 1, None, 0),
            ("leaf_sequence", "INTEGER", 0, None, 0),
            ("created_at_unix_ms", "INTEGER", 0, None, 0),
            # Order here must match the fresh CREATE in ``_ensure_schema``:
            # ``_assert_canonical_table_shape`` compares ``PRAGMA table_info``
            # column order, so a reordered DDL fails closed.
            ("title", "TEXT", 0, None, 0),
            ("forked_from_session_id", "TEXT", 0, None, 0),
            ("forked_at_sequence", "INTEGER", 0, None, 0),
        ),
        "session_events": (
            ("workspace_id", "TEXT", 1, None, 1),
            ("session_id", "TEXT", 1, None, 2),
            ("sequence", "INTEGER", 1, None, 3),
            ("parent_sequence", "INTEGER", 0, None, 0),
            ("event_type", "TEXT", 1, None, 0),
            ("source", "TEXT", 1, None, 0),
            ("payload_json", "TEXT", 1, None, 0),
        ),
        "background_tasks": (
            ("task_id", "TEXT", 1, None, 2),
            ("workspace_id", "TEXT", 1, None, 1),
            ("status", "TEXT", 1, None, 0),
            ("prompt", "TEXT", 1, None, 0),
            ("request_session_id", "TEXT", 0, None, 0),
            ("request_parent_session_id", "TEXT", 0, None, 0),
            ("request_metadata_json", "TEXT", 1, None, 0),
            ("requested_child_session_id", "TEXT", 0, None, 0),
            ("routing_mode", "TEXT", 0, None, 0),
            ("routing_subagent_type", "TEXT", 0, None, 0),
            ("routing_description", "TEXT", 0, None, 0),
            ("routing_command", "TEXT", 0, None, 0),
            ("approval_request_id", "TEXT", 0, None, 0),
            ("question_request_id", "TEXT", 0, None, 0),
            ("cancellation_cause", "TEXT", 0, None, 0),
            ("result_available", "INTEGER", 1, "0", 0),
            ("delegated_reminder_json", "TEXT", 0, None, 0),
            ("allocate_session_id", "INTEGER", 1, None, 0),
            ("session_id", "TEXT", 0, None, 0),
            ("error", "TEXT", 0, None, 0),
            ("cancel_requested_at", "INTEGER", 0, None, 0),
            ("created_at", "INTEGER", 1, None, 0),
            ("updated_at", "INTEGER", 1, None, 0),
            ("started_at", "INTEGER", 0, None, 0),
            ("finished_at", "INTEGER", 0, None, 0),
            ("created_at_unix_ms", "INTEGER", 0, None, 0),
            ("started_at_unix_ms", "INTEGER", 0, None, 0),
            ("finished_at_unix_ms", "INTEGER", 0, None, 0),
            ("keep_alive", "INTEGER", 1, "0", 0),
            ("steer_prompt", "TEXT", 0, None, 0),
            ("output_schema_json", "TEXT", 0, None, 0),
            ("schema_mode", "TEXT", 1, "'permissive'", 0),
            ("structured_output_json", "TEXT", 0, None, 0),
            ("schema_validation_json", "TEXT", 0, None, 0),
        ),
        "background_processes": (
            ("process_id", "TEXT", 1, None, 2),
            ("workspace_id", "TEXT", 1, None, 1),
            ("owner_session_id", "TEXT", 0, None, 0),
            ("command", "TEXT", 1, None, 0),
            ("cwd", "TEXT", 1, None, 0),
            ("pid", "INTEGER", 1, None, 0),
            ("process_group_id", "INTEGER", 0, None, 0),
            ("process_identity", "TEXT", 0, None, 0),
            ("stdout_path", "TEXT", 1, None, 0),
            ("stderr_path", "TEXT", 1, None, 0),
            ("status", "TEXT", 1, None, 0),
            ("exit_code", "INTEGER", 0, None, 0),
            ("reconciliation_reason", "TEXT", 0, None, 0),
            ("created_at", "INTEGER", 1, None, 0),
            ("updated_at", "INTEGER", 1, None, 0),
        ),
        "session_event_deliveries": (
            ("workspace_id", "TEXT", 1, None, 1),
            ("session_id", "TEXT", 1, None, 2),
            ("dedupe_key", "TEXT", 1, None, 3),
            ("delivered_at", "INTEGER", 1, None, 0),
            ("event_sequence", "INTEGER", 1, None, 0),
        ),
        "storage_sequences": (
            ("scope", "TEXT", 0, None, 1),
            ("value", "INTEGER", 1, None, 0),
        ),
    }
    _CANONICAL_UNIQUE_INDEXES: dict[str, frozenset[tuple[str, ...]]] = {
        "sessions": frozenset(),
        "session_events": frozenset(),
        "background_tasks": frozenset(),
        "session_event_deliveries": frozenset(),
        "storage_sequences": frozenset(),
    }

    def __init__(self, *, database_path: Path | None = None) -> None:
        # Resolve the canonical path exactly once, here. A store that outlives
        # the context that constructed it (a background worker outliving its
        # test, an embedded reconfiguration) must keep writing the database it
        # was built for: re-reading ``$VOIDCODE_DB_PATH``/XDG on every operation
        # would silently retarget it when the environment changes underneath it.
        self._database_path = database_path if database_path is not None else sessions_db_path()

    def _resolve_database_path(self) -> Path:
        return self._database_path

    @contextmanager
    def _connect(self, workspace: Path) -> Iterator[sqlite3.Connection]:
        _ = workspace
        database_path = self._resolve_database_path()
        database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(database_path, timeout=self._sqlite_policy.busy_timeout_ms / 1_000, isolation_level=None)
        try:
            connection.row_factory = sqlite3.Row
            self._configure_connection(connection=connection)
            self._ensure_schema_once(connection=connection, database_path=database_path)
        except RuntimeError:
            connection.close()
            raise
        try:
            yield connection
        finally:
            connection.close()

    def _configure_connection(self, *, connection: sqlite3.Connection) -> None:
        deadline = time() + (self._sqlite_policy.busy_timeout_ms / 1_000)
        while True:
            try:
                _ = connection.execute(f"PRAGMA busy_timeout = {self._sqlite_policy.busy_timeout_ms}")
                _ = connection.execute("PRAGMA journal_mode = WAL")
                _ = connection.execute(f"PRAGMA synchronous = {self._sqlite_policy.synchronous}")
                _ = connection.execute("PRAGMA foreign_keys = ON")
                _ = connection.execute(f"PRAGMA wal_autocheckpoint = {self._sqlite_policy.wal_autocheckpoint_pages}")
                _ = connection.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
                return
            except sqlite3.OperationalError as exc:
                if "database is locked" not in str(exc).lower() or time() >= deadline:
                    raise
                sleep(self._sqlite_policy.configure_retry_interval_seconds)

    @contextmanager
    def _write_connect(self, workspace: Path) -> Iterator[sqlite3.Connection]:
        # Execution ownership gate: this is the single gateway every storage
        # mutation passes through, so a revoked execution cannot commit here
        # regardless of which caller reaches it. See
        # ``runtime/execution_ownership.py`` and the "Execution ownership"
        # invariant in docs/contracts/background-task-delegation.md.
        EXECUTION_OWNERSHIP.assert_writes_allowed(operation="session_store_write")
        with self._connect(workspace) as connection:
            _ = connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except Exception:
                connection.rollback()
                raise

    def _ensure_schema_once(self, *, connection: sqlite3.Connection, database_path: Path) -> None:
        """Verify or bootstrap the canonical schema, at most once per file.

        Skips ``_ensure_schema`` only when this connection's sentinel reads
        (user_version, schema cookie, sequence rows, file identity) prove the
        file is still the one this process verified. Any mismatch falls back
        to ``_ensure_schema``, which fails fast on drift it cannot migrate.
        """
        if self._schema_bootstrap_is_valid(connection=connection, database_path=database_path):
            return
        self._ensure_schema(connection=connection, database_path=database_path)
        self._remember_schema_bootstrap(connection=connection, database_path=database_path)

    def _schema_bootstrap_is_valid(self, *, connection: sqlite3.Connection, database_path: Path) -> bool:
        identity = self._database_file_identity(database_path)
        if identity is None:
            return False
        if not self._schema_version_matches(connection=connection):
            return False
        with _BOOTSTRAP_LOCK:
            verified = _BOOTSTRAPPED_DATABASES.get(str(database_path))
        if verified is None or verified.identity != identity:
            return False
        if self._schema_cookie(connection=connection) != verified.schema_cookie:
            return False
        return self._storage_sequences_present(connection=connection)

    @classmethod
    def _remember_schema_bootstrap(cls, *, connection: sqlite3.Connection, database_path: Path) -> None:
        """Record a verified file; cookie is read post-bootstrap."""
        identity = cls._database_file_identity(database_path)
        if identity is None:
            return
        with _BOOTSTRAP_LOCK:
            _BOOTSTRAPPED_DATABASES[str(database_path)] = _DatabaseBootstrap(
                identity=identity,
                schema_version=cls._SCHEMA_VERSION,
                schema_cookie=cls._schema_cookie(connection=connection),
            )

    def _schema_version_matches(self, *, connection: sqlite3.Connection) -> bool:
        return self._schema_version(connection=connection) == self._SCHEMA_VERSION

    @staticmethod
    def _database_file_identity(database_path: Path) -> tuple[int, int] | None:
        try:
            stat = database_path.stat()
        except OSError:
            return None
        return (stat.st_dev, stat.st_ino)

    @staticmethod
    def _schema_version(*, connection: sqlite3.Connection) -> int:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])

    @staticmethod
    def _schema_cookie(*, connection: sqlite3.Connection) -> int:
        return int(connection.execute("PRAGMA schema_version").fetchone()[0])

    @classmethod
    def _storage_sequences_present(cls, *, connection: sqlite3.Connection) -> bool:
        placeholders = ", ".join("?" for _ in cls._SEQUENCE_SCOPES)
        row = connection.execute(
            f"SELECT count(*) FROM storage_sequences WHERE scope IN ({placeholders})",
            cls._SEQUENCE_SCOPES,
        ).fetchone()
        return int(row[0]) == len(cls._SEQUENCE_SCOPES)

    @classmethod
    def _assert_supported_version(cls, *, connection: sqlite3.Connection, database_path: Path) -> None:
        """Fail closed before touching the file when ``user_version`` is not this build's.

        Version 0 is a fresh file and defers to the CREATE path below; the
        current version is already verified. Anything else is a different
        generation of the file — there are no migrations, so the version is an
        exact match, not a floor.
        """
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version in (0, cls._SCHEMA_VERSION):
            return
        cls._raise_schema_mismatch(
            database_path=database_path,
            detail=f"schema version mismatch: expected {cls._SCHEMA_VERSION} got {version}",
            reason="too-new" if version > cls._SCHEMA_VERSION else "needs-upgrade",
        )

    def _ensure_schema(self, *, connection: sqlite3.Connection, database_path: Path) -> None:
        self._assert_supported_version(connection=connection, database_path=database_path)
        _ = connection.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
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
                leaf_sequence INTEGER,
                created_at_unix_ms INTEGER,
                title TEXT,
                forked_from_session_id TEXT,
                forked_at_sequence INTEGER,
                PRIMARY KEY (workspace_id, session_id)
            )
            """
        )
        _ = connection.execute(
            """
            CREATE TABLE IF NOT EXISTS session_events (
                workspace_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                parent_sequence INTEGER,
                event_type TEXT NOT NULL,
                source TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY (workspace_id, session_id, sequence)
            )
            """
        )
        _ = connection.execute(
            """
            CREATE TABLE IF NOT EXISTS background_tasks (
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
                delegated_reminder_json TEXT,
                allocate_session_id INTEGER NOT NULL,
                session_id TEXT,
                error TEXT,
                cancel_requested_at INTEGER,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                started_at INTEGER,
                finished_at INTEGER,
                created_at_unix_ms INTEGER,
                started_at_unix_ms INTEGER,
                finished_at_unix_ms INTEGER,
                keep_alive INTEGER NOT NULL DEFAULT 0,
                steer_prompt TEXT,
                output_schema_json TEXT,
                schema_mode TEXT NOT NULL DEFAULT 'permissive',
                structured_output_json TEXT,
                schema_validation_json TEXT,
                PRIMARY KEY (workspace_id, task_id)
            )
            """
        )
        _ = connection.execute(
            """
            CREATE TABLE IF NOT EXISTS background_processes (
                process_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                owner_session_id TEXT,
                command TEXT NOT NULL,
                cwd TEXT NOT NULL,
                pid INTEGER NOT NULL,
                process_group_id INTEGER,
                process_identity TEXT,
                stdout_path TEXT NOT NULL,
                stderr_path TEXT NOT NULL,
                status TEXT NOT NULL,
                exit_code INTEGER,
                reconciliation_reason TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (workspace_id, process_id)
            )
            """
        )
        _ = connection.execute(
            """
            CREATE TABLE IF NOT EXISTS session_event_deliveries (
                workspace_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                dedupe_key TEXT NOT NULL,
                delivered_at INTEGER NOT NULL,
                event_sequence INTEGER NOT NULL,
                PRIMARY KEY (workspace_id, session_id, dedupe_key)
            )
            """
        )
        _ = connection.execute(
            """
            CREATE TABLE IF NOT EXISTS storage_sequences (
                scope TEXT PRIMARY KEY,
                value INTEGER NOT NULL
            )
            """
        )
        self._assert_canonical_schema(connection=connection, database_path=database_path)
        self._ensure_workspace_indexes(connection=connection)
        self._ensure_storage_sequences(connection=connection)
        self._assert_schema_version(connection=connection, database_path=database_path)
        connection.commit()

    @staticmethod
    def _ensure_workspace_indexes(*, connection: sqlite3.Connection) -> None:
        _ = connection.execute("CREATE INDEX IF NOT EXISTS sessions_workspace_idx ON sessions(workspace_id, status, updated_at DESC)")
        _ = connection.execute("CREATE INDEX IF NOT EXISTS background_tasks_workspace_idx ON background_tasks(workspace_id, status, updated_at DESC)")

    @staticmethod
    def _ensure_storage_sequences(*, connection: sqlite3.Connection) -> None:
        """Insert missing sequence rows and lift lagging floors (INSERT OR IGNORE race)."""
        for scope in SqliteSessionStore._SEQUENCE_SCOPES:
            _ = connection.execute(
                "INSERT OR IGNORE INTO storage_sequences (scope, value) VALUES (?, 0)",
                (scope,),
            )
        SqliteSessionStore._bump_sequence_floor(
            connection=connection,
            scope="sessions",
            floor=SqliteSessionStore._max_existing_timestamp(
                connection=connection,
                table="sessions",
                columns=("updated_at",),
            ),
        )
        SqliteSessionStore._bump_sequence_floor(
            connection=connection,
            scope="background_tasks",
            floor=SqliteSessionStore._max_existing_timestamp(
                connection=connection,
                table="background_tasks",
                columns=(
                    "created_at",
                    "updated_at",
                    "started_at",
                    "finished_at",
                    "cancel_requested_at",
                ),
            ),
        )
        SqliteSessionStore._bump_sequence_floor(
            connection=connection,
            scope="auxiliary",
            floor=max(
                SqliteSessionStore._max_existing_timestamp(
                    connection=connection,
                    table="sessions",
                    columns=("created_at",),
                ),
                SqliteSessionStore._max_existing_timestamp(
                    connection=connection,
                    table="session_event_deliveries",
                    columns=("delivered_at",),
                ),
            ),
        )

    @staticmethod
    def _max_existing_timestamp(*, connection: sqlite3.Connection, table: str, columns: tuple[str, ...]) -> int:
        maxima = [int(connection.execute(f"SELECT COALESCE(MAX({column}), 0) FROM {table}").fetchone()[0]) for column in columns]
        return max(maxima, default=0)

    @staticmethod
    def _bump_sequence_floor(*, connection: sqlite3.Connection, scope: str, floor: int) -> None:
        # Equivalent to ``value = MAX(value, floor)`` but writes only when the
        # floor actually moves, so a re-verified database is never rewritten.
        _ = connection.execute(
            "UPDATE storage_sequences SET value = ? WHERE scope = ? AND value < ?",
            (floor, scope, floor),
        )

    @classmethod
    def _assert_schema_version(cls, *, connection: sqlite3.Connection, database_path: Path) -> None:
        """Stamp ``PRAGMA user_version`` after schema validation."""
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version == 0:
            _ = connection.execute(f"PRAGMA user_version = {cls._SCHEMA_VERSION}")
            return
        if version != cls._SCHEMA_VERSION:
            reason: Literal["needs-upgrade", "too-new"] = "too-new" if version > cls._SCHEMA_VERSION else "needs-upgrade"
            cls._raise_schema_mismatch(
                database_path=database_path,
                detail=f"schema version mismatch: expected {cls._SCHEMA_VERSION} got {version}",
                reason=reason,
            )

    @classmethod
    def _assert_canonical_schema(cls, *, connection: sqlite3.Connection, database_path: Path) -> None:
        existing_tables = {
            decode_row(row, SqliteMasterNameRow)["name"] for row in fetch_rows(connection, "SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        missing_tables = sorted(set(cls._CANONICAL_SCHEMA) - existing_tables)
        if missing_tables:
            cls._raise_schema_mismatch(
                database_path=database_path,
                detail=f"missing tables: {', '.join(missing_tables)}",
                reason="corrupt",
            )
        for table_name, expected_columns in cls._CANONICAL_SCHEMA.items():
            cls._assert_canonical_table_shape(
                connection=connection,
                database_path=database_path,
                table_name=table_name,
                expected_columns=expected_columns,
            )
        for table_name, expected_indexes in cls._CANONICAL_UNIQUE_INDEXES.items():
            cls._assert_canonical_unique_indexes(
                connection=connection,
                database_path=database_path,
                table_name=table_name,
                expected_indexes=expected_indexes,
            )

    @classmethod
    def _assert_canonical_table_shape(
        cls,
        *,
        connection: sqlite3.Connection,
        database_path: Path,
        table_name: str,
        expected_columns: tuple[tuple[str, str, int, str | None, int], ...],
    ) -> None:
        actual_columns = cls._table_columns(connection=connection, table_name=table_name)
        expected_column_names = {column[0] for column in expected_columns}
        actual_column_names = {column[0] for column in actual_columns}
        missing_columns = sorted(expected_column_names - actual_column_names)
        if missing_columns:
            cls._raise_schema_mismatch(
                database_path=database_path,
                detail=f"table '{table_name}' missing columns: {', '.join(missing_columns)}",
                reason="corrupt",
            )
        unexpected_columns = sorted(actual_column_names - expected_column_names)
        if unexpected_columns:
            cls._raise_schema_mismatch(
                database_path=database_path,
                detail=(f"table '{table_name}' has unexpected columns: {', '.join(unexpected_columns)}"),
                reason="corrupt",
            )
        if actual_columns != expected_columns:
            cls._raise_schema_mismatch(
                database_path=database_path,
                detail=f"table '{table_name}' shape does not match canonical runtime schema",
                reason="corrupt",
            )

    @classmethod
    def _assert_canonical_unique_indexes(
        cls,
        *,
        connection: sqlite3.Connection,
        database_path: Path,
        table_name: str,
        expected_indexes: frozenset[tuple[str, ...]],
    ) -> None:
        actual_indexes = cls._table_unique_indexes(connection=connection, table_name=table_name)
        if actual_indexes == expected_indexes:
            return
        expected = ", ".join("(" + ", ".join(index) + ")" for index in sorted(expected_indexes))
        actual = ", ".join("(" + ", ".join(index) + ")" for index in sorted(actual_indexes))
        cls._raise_schema_mismatch(
            database_path=database_path,
            detail=(f"table '{table_name}' unique indexes do not match canonical runtime schema: expected [{expected}] got [{actual}]"),
            reason="corrupt",
        )

    @staticmethod
    def _table_columns(*, connection: sqlite3.Connection, table_name: str) -> tuple[tuple[str, str, int, str | None, int], ...]:
        return tuple(
            (column["name"], column["type"], column["notnull"], column["dflt_value"], column["pk"])
            for column in (decode_row(row, TableInfoRow) for row in fetch_rows(connection, f"PRAGMA table_info({table_name})"))
        )

    @staticmethod
    def _table_unique_indexes(*, connection: sqlite3.Connection, table_name: str) -> frozenset[tuple[str, ...]]:
        return frozenset(
            tuple(decode_row(column_row, IndexInfoRow)["name"] for column_row in fetch_rows(connection, f"PRAGMA index_info({index['name']})"))
            for index in (decode_row(row, IndexListRow) for row in fetch_rows(connection, f"PRAGMA index_list({table_name})"))
            if index["unique"] == 1 and index["origin"] == "u"
        )

    @staticmethod
    def _raise_schema_mismatch(
        *,
        database_path: Path,
        detail: str,
        reason: Literal["needs-upgrade", "too-new", "corrupt"],
    ) -> NoReturn:
        if reason == "needs-upgrade":
            raise RuntimeError(
                "sqlite runtime schema upgrade required: "
                f"{detail}. This build ships no schema migrations - the storage schema changed "
                "shape in place, so an older-generation database cannot be opened by it. "
                "There is no upgrade in place: point VOIDCODE_DB_PATH (or the configured database "
                "path) at a new database file. Only as a last resort, if the database is expendable, "
                "`uv run voidcode storage reset` clears local state (this discards sessions)."
            )
        if reason == "too-new":
            raise RuntimeError(
                "sqlite runtime schema is too new: "
                f"{detail}. This database was most likely written by an older voidcode whose storage "
                "schema changed shape without a migration, so the version it stamped is not "
                f"compatible with this build (expected {SqliteSessionStore._SCHEMA_VERSION}). "
                "There is nothing to upgrade and resetting cannot close the gap: start from a new "
                "database path (or remove the old database file and its -wal/-shm siblings) and "
                f"accept that its sessions are not readable. Database: '{database_path}'."
            )
        raise RuntimeError(
            "sqlite runtime schema mismatch: "
            f"{detail}. backup '{database_path}' plus matching -wal/-shm files "
            "to a safe location before storage reset. "
            "Reset the runtime database with "
            f"`uv run voidcode storage reset` or remove '{database_path}' "
            "plus matching -wal/-shm files."
        )

    @staticmethod
    def _parse_session_status(value: str) -> SessionStatus:
        if value == "idle":
            return "idle"
        if value == "running":
            return "running"
        if value == "waiting":
            return "waiting"
        if value == "completed":
            return "completed"
        if value == "failed":
            return "failed"
        if value == "interrupted":
            return "interrupted"
        raise ValueError(f"invalid session status: {value}")

    @staticmethod
    def _parse_event_source(value: str) -> EventSource:
        if value == "runtime":
            return "runtime"
        if value == "graph":
            return "graph"
        if value == "tool":
            return "tool"
        raise ValueError(f"invalid event source: {value}")

    @staticmethod
    def _parse_background_task_status(value: str) -> BackgroundTaskStatus:
        if value == "queued":
            return "queued"
        if value == "running":
            return "running"
        if value == "idle":
            return "idle"
        if value == "completed":
            return "completed"
        if value == "failed":
            return "failed"
        if value == "cancelled":
            return "cancelled"
        if value == "interrupted":
            return "interrupted"
        raise ValueError(f"invalid background task status: {value}")

    @staticmethod
    def _session_last_event_sequence(events: tuple[EventEnvelope, ...]) -> int:
        return events[-1].sequence if events else 0

    @staticmethod
    def _current_unix_ms() -> int:
        return int(time() * 1000)

    def _next_auxiliary_timestamp(self, *, connection: sqlite3.Connection) -> int:
        return self._next_sequence_value(connection=connection, scope="auxiliary")

    @staticmethod
    def _next_sequence_value(*, connection: sqlite3.Connection, scope: str) -> int:
        row = fetch_row(
            connection,
            """
                UPDATE storage_sequences
                SET value = value + 1
                WHERE scope = ?
                RETURNING value
                """,
            (scope,),
        )
        if row is None:
            raise RuntimeError(f"runtime storage sequence is missing: {scope}")
        return decode_row(row, StorageSequenceValueRow)["value"]

    def _next_timestamp(self, *, connection: sqlite3.Connection) -> int:
        return self._next_sequence_value(connection=connection, scope="sessions")
