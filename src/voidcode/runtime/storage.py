from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from time import sleep, time
from typing import Protocol, cast, final, runtime_checkable

from .contracts import (
    RuntimeNotification,
    RuntimeRequest,
    RuntimeResponse,
    RuntimeSessionResult,
    RuntimeSessionRevertMarker,
)
from .effectiveness import ToolEffectivenessReport
from .events import (
    EventEnvelope,
    EventSource,
)
from .memory import MemoryKind, MemoryRecord, MemorySearchResult, MemoryStatus
from .paths import DB_PATH_ENV, sessions_db_path
from .permission import PendingApproval
from .question import PendingQuestion
from .session import (
    SessionStatus,
    StoredSessionSummary,
)
from .storage_background_tasks import _BackgroundTaskStorageMixin
from .storage_diagnostics import _DiagnosticsStorageMixin
from .storage_effectiveness import _EffectivenessStorageMixin
from .storage_memory import _MemoryStorageMixin
from .storage_notifications import _NotificationStorageMixin
from .storage_resume import _ResumeStorageMixin
from .storage_revert import _RevertStorageMixin
from .storage_sessions import _SessionStorageMixin
from .storage_shared import SessionSealedError as SessionSealedError
from .storage_todos import _TodoStorageMixin
from .task import (
    BackgroundTaskState,
    BackgroundTaskStatus,
    DelegatedReminderStopCondition,
    StoredBackgroundTaskSummary,
)


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

    def list_notifications(self, *, workspace: Path) -> tuple[RuntimeNotification, ...]: ...

    def acknowledge_notification(self, *, workspace: Path, notification_id: str) -> RuntimeNotification: ...

    def save_pending_approval(
        self,
        *,
        workspace: Path,
        request: RuntimeRequest,
        response: RuntimeResponse,
        pending_approval: PendingApproval,
    ) -> None: ...

    def load_pending_approval(self, *, workspace: Path, session_id: str) -> PendingApproval | None: ...

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

    def add_memory(
        self,
        *,
        workspace: Path,
        content: str,
        kind: MemoryKind = "project",
        tags: tuple[str, ...] = (),
        source_session_id: str | None = None,
    ) -> MemoryRecord: ...

    def list_memories(self, *, workspace: Path, include_deleted: bool = False) -> tuple[MemoryRecord, ...]: ...

    def search_memories(self, *, workspace: Path, query: str) -> tuple[MemorySearchResult, ...]: ...

    def get_memory(self, *, workspace: Path, memory_id: str) -> MemoryRecord | None: ...

    def delete_memory(self, *, workspace: Path, memory_id: str) -> MemoryRecord: ...

    def truncate_session_events_after(self, *, workspace: Path, session_id: str, sequence: int) -> None: ...

    def load_session_status(self, *, workspace: Path, session_id: str) -> SessionStatus: ...


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


@final
class SqliteSessionStore(
    _BackgroundTaskStorageMixin,
    _SessionStorageMixin,
    _MemoryStorageMixin,
    _ResumeStorageMixin,
    _RevertStorageMixin,
    _TodoStorageMixin,
    _NotificationStorageMixin,
    _EffectivenessStorageMixin,
    _DiagnosticsStorageMixin,
):
    _database_path: Path | None
    _SCHEMA_VERSION = 12
    _MEMORY_KINDS: frozenset[MemoryKind] = frozenset(("project", "preference", "feedback", "reference", "decision"))
    _RESUME_CHECKPOINT_KINDS = frozenset({"approval_wait", "question_wait", "provider_failure_retryable", "terminal", "interrupted"})
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
            ("created_at_unix_ms", "INTEGER", 0, None, 0),
        ),
        "session_events": (
            ("workspace_id", "TEXT", 1, None, 1),
            ("session_id", "TEXT", 1, None, 2),
            ("sequence", "INTEGER", 1, None, 3),
            ("event_type", "TEXT", 1, None, 0),
            ("source", "TEXT", 1, None, 0),
            ("payload_json", "TEXT", 1, None, 0),
        ),
        "session_todos": (
            ("workspace_id", "TEXT", 1, None, 1),
            ("session_id", "TEXT", 1, None, 2),
            ("position", "INTEGER", 1, None, 3),
            ("content", "TEXT", 1, None, 0),
            ("status", "TEXT", 1, None, 0),
            ("updated_at", "INTEGER", 1, None, 0),
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
        "memories": (
            ("memory_id", "TEXT", 1, None, 2),
            ("workspace_id", "TEXT", 1, None, 1),
            ("kind", "TEXT", 1, None, 0),
            ("content", "TEXT", 1, None, 0),
            ("tags_json", "TEXT", 1, None, 0),
            ("scope", "TEXT", 1, "'workspace'", 0),
            ("status", "TEXT", 1, "'active'", 0),
            ("source_session_id", "TEXT", 0, None, 0),
            ("created_at", "INTEGER", 1, None, 0),
            ("updated_at", "INTEGER", 1, None, 0),
            ("deleted_at", "INTEGER", 0, None, 0),
        ),
        "memory_tags": (
            ("workspace_id", "TEXT", 1, None, 1),
            ("memory_id", "TEXT", 1, None, 2),
            ("tag", "TEXT", 1, None, 3),
            ("created_at", "INTEGER", 1, None, 0),
        ),
        "memory_recall_log": (
            ("workspace_id", "TEXT", 1, None, 1),
            ("recall_id", "TEXT", 1, None, 2),
            ("session_id", "TEXT", 0, None, 0),
            ("query", "TEXT", 0, None, 0),
            ("result_count", "INTEGER", 1, "0", 0),
            ("created_at", "INTEGER", 1, None, 0),
        ),
        "memory_index_status": (
            ("workspace_id", "TEXT", 1, None, 1),
            ("index_name", "TEXT", 1, None, 2),
            ("status", "TEXT", 1, None, 0),
            ("detail_json", "TEXT", 1, "'{}'", 0),
            ("updated_at", "INTEGER", 1, None, 0),
        ),
        "session_notifications": (
            ("notification_id", "TEXT", 0, None, 1),
            ("workspace_id", "TEXT", 1, None, 0),
            ("session_id", "TEXT", 1, None, 0),
            ("kind", "TEXT", 1, None, 0),
            ("status", "TEXT", 1, None, 0),
            ("summary", "TEXT", 1, None, 0),
            ("payload_json", "TEXT", 1, None, 0),
            ("event_sequence", "INTEGER", 1, None, 0),
            ("dedupe_key", "TEXT", 1, None, 0),
            ("created_at", "INTEGER", 1, None, 0),
            ("acknowledged_at", "INTEGER", 0, None, 0),
        ),
        "session_event_deliveries": (
            ("workspace_id", "TEXT", 1, None, 1),
            ("session_id", "TEXT", 1, None, 2),
            ("dedupe_key", "TEXT", 1, None, 3),
            ("delivered_at", "INTEGER", 1, None, 0),
        ),
        "storage_sequences": (
            ("scope", "TEXT", 0, None, 1),
            ("value", "INTEGER", 1, None, 0),
        ),
    }
    _CANONICAL_UNIQUE_INDEXES: dict[str, frozenset[tuple[str, ...]]] = {
        "sessions": frozenset(),
        "session_events": frozenset(),
        "session_todos": frozenset(),
        "background_tasks": frozenset(),
        "memories": frozenset(),
        "memory_tags": frozenset(),
        "memory_recall_log": frozenset(),
        "memory_index_status": frozenset(),
        "session_notifications": frozenset({("workspace_id", "dedupe_key")}),
        "session_event_deliveries": frozenset(),
        "storage_sequences": frozenset(),
    }

    def __init__(self, *, database_path: Path | None = None) -> None:
        self._database_path = database_path

    def _resolve_database_path(self) -> Path:
        if self._database_path is not None:
            return self._database_path
        return sessions_db_path()

    @contextmanager
    def _connect(self, workspace: Path) -> Iterator[sqlite3.Connection]:
        _ = workspace
        database_path = self._resolve_database_path()
        database_path.parent.mkdir(parents=True, exist_ok=True)
        connection: sqlite3.Connection | None = None
        for attempt in range(2):
            connection = sqlite3.connect(
                database_path,
                timeout=self._sqlite_policy.busy_timeout_ms / 1_000,
                isolation_level=None,
            )
            try:
                connection.row_factory = sqlite3.Row
                self._configure_connection(connection=connection)
                self._ensure_schema(connection=connection, database_path=database_path)
                break
            except RuntimeError as exc:
                should_reset = (
                    attempt == 0 and self._database_path is None and not os.environ.get(DB_PATH_ENV) and self._is_schema_mismatch_runtime_error(exc)
                )
                if should_reset:
                    if connection is not None:
                        self._reset_storage_in_place(connection=connection)
                        connection.close()
                        connection = None
                    continue
                if connection is not None:
                    connection.close()
                    connection = None
                raise
        if connection is None:
            msg = "failed to establish sqlite runtime storage connection"
            raise RuntimeError(msg)
        try:
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _is_schema_mismatch_runtime_error(exc: RuntimeError) -> bool:
        return str(exc).startswith("sqlite runtime schema mismatch:")

    @staticmethod
    def _reset_storage_in_place(*, connection: sqlite3.Connection) -> None:
        table_rows = cast(
            list[sqlite3.Row],
            connection.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'").fetchall(),
        )
        for row in table_rows:
            table_name = cast(str, row["name"])
            _ = connection.execute(f'DROP TABLE IF EXISTS "{table_name}"')
        _ = connection.execute("PRAGMA user_version = 0")
        connection.commit()

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
        with self._connect(workspace) as connection:
            _ = connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except Exception:
                connection.rollback()
                raise

    def _ensure_schema(self, *, connection: sqlite3.Connection, database_path: Path) -> None:
        self._assert_existing_schema_version(connection=connection, database_path=database_path)
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
                created_at_unix_ms INTEGER,
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
                event_type TEXT NOT NULL,
                source TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY (workspace_id, session_id, sequence)
            )
            """
        )
        _ = connection.execute(
            """
            CREATE TABLE IF NOT EXISTS session_todos (
                workspace_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                position INTEGER NOT NULL,
                content TEXT NOT NULL,
                status TEXT NOT NULL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (workspace_id, session_id, position)
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
            CREATE TABLE IF NOT EXISTS memories (
                memory_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                content TEXT NOT NULL,
                tags_json TEXT NOT NULL,
                scope TEXT NOT NULL DEFAULT 'workspace',
                status TEXT NOT NULL DEFAULT 'active',
                source_session_id TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                deleted_at INTEGER,
                PRIMARY KEY (workspace_id, memory_id)
            )
            """
        )
        _ = connection.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_tags (
                workspace_id TEXT NOT NULL,
                memory_id TEXT NOT NULL,
                tag TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (workspace_id, memory_id, tag)
            )
            """
        )
        _ = connection.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_recall_log (
                workspace_id TEXT NOT NULL,
                recall_id TEXT NOT NULL,
                session_id TEXT,
                query TEXT,
                result_count INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (workspace_id, recall_id)
            )
            """
        )
        _ = connection.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_index_status (
                workspace_id TEXT NOT NULL,
                index_name TEXT NOT NULL,
                status TEXT NOT NULL,
                detail_json TEXT NOT NULL DEFAULT '{}',
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (workspace_id, index_name)
            )
            """
        )
        _ = connection.execute(
            """
            CREATE TABLE IF NOT EXISTS session_notifications (
                notification_id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                summary TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                event_sequence INTEGER NOT NULL,
                dedupe_key TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                acknowledged_at INTEGER,
                UNIQUE(workspace_id, dedupe_key)
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
        # Migration: add created_at_unix_ms column for v6 → v7 upgrade.
        current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if current_version == 6:
            try:
                connection.execute("ALTER TABLE sessions ADD COLUMN created_at_unix_ms INTEGER")
            except sqlite3.OperationalError:
                pass  # Column already exists (idempotent)
        # Migration: add keep-alive steering columns for v10 → v11 upgrade.
        if current_version == 10:
            try:
                connection.execute("ALTER TABLE background_tasks ADD COLUMN keep_alive INTEGER NOT NULL DEFAULT 0")
            except sqlite3.OperationalError:
                pass  # Column already exists (idempotent)
            try:
                connection.execute("ALTER TABLE background_tasks ADD COLUMN steer_prompt TEXT")
            except sqlite3.OperationalError:
                pass  # Column already exists (idempotent)
        # Migration: add output-schema columns for v11 → v12 upgrade (and for
        # the v10 → v12 upgrade path, which also ran the v10 → v11 block above).
        if current_version in (10, 11):
            try:
                connection.execute("ALTER TABLE background_tasks ADD COLUMN output_schema_json TEXT")
            except sqlite3.OperationalError:
                pass  # Column already exists (idempotent)
            try:
                connection.execute("ALTER TABLE background_tasks ADD COLUMN schema_mode TEXT NOT NULL DEFAULT 'permissive'")
            except sqlite3.OperationalError:
                pass  # Column already exists (idempotent)
            try:
                connection.execute("ALTER TABLE background_tasks ADD COLUMN structured_output_json TEXT")
            except sqlite3.OperationalError:
                pass  # Column already exists (idempotent)
            try:
                connection.execute("ALTER TABLE background_tasks ADD COLUMN schema_validation_json TEXT")
            except sqlite3.OperationalError:
                pass  # Column already exists (idempotent)
        # Validate the freshly-created/already-present schema before stamping the
        # user_version. Stamping after validation keeps schema setup atomic from the
        # caller's perspective: if any CREATE TABLE / canonical check fails, the
        # database is never marked as the canonical version.
        self._assert_canonical_schema(connection=connection, database_path=database_path)
        self._ensure_workspace_indexes(connection=connection)
        self._ensure_storage_sequences(connection=connection)
        self._assert_schema_version(connection=connection, database_path=database_path)
        connection.commit()

    @staticmethod
    def _ensure_workspace_indexes(*, connection: sqlite3.Connection) -> None:
        # Non-unique indexes that accelerate per-workspace filtering once the
        # SQLite database becomes user-global and serves multiple workspaces.
        # Additive: the strict canonical schema check ignores non-unique indexes,
        # so adding new indexes here is safe.
        _ = connection.execute("CREATE INDEX IF NOT EXISTS sessions_workspace_idx ON sessions(workspace_id, status, updated_at DESC)")
        _ = connection.execute("CREATE INDEX IF NOT EXISTS background_tasks_workspace_idx ON background_tasks(workspace_id, status, updated_at DESC)")
        _ = connection.execute("CREATE INDEX IF NOT EXISTS memories_workspace_idx ON memories(workspace_id, status, updated_at)")
        _ = connection.execute("CREATE INDEX IF NOT EXISTS memory_tags_tag_idx ON memory_tags(workspace_id, tag)")
        _ = connection.execute("CREATE INDEX IF NOT EXISTS memory_recall_log_workspace_idx ON memory_recall_log(workspace_id, created_at DESC)")
        _ = connection.execute("CREATE INDEX IF NOT EXISTS session_notifications_workspace_idx ON session_notifications(workspace_id, session_id)")

    @staticmethod
    def _ensure_storage_sequences(*, connection: sqlite3.Connection) -> None:
        _ = connection.execute("INSERT OR IGNORE INTO storage_sequences (scope, value) VALUES ('sessions', 0)")
        _ = connection.execute("INSERT OR IGNORE INTO storage_sequences (scope, value) VALUES ('background_tasks', 0)")
        _ = connection.execute("INSERT OR IGNORE INTO storage_sequences (scope, value) VALUES ('memories', 0)")
        _ = connection.execute("INSERT OR IGNORE INTO storage_sequences (scope, value) VALUES ('auxiliary', 0)")
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
            scope="memories",
            floor=SqliteSessionStore._max_existing_timestamp(
                connection=connection,
                table="memories",
                columns=("created_at", "updated_at", "deleted_at"),
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
                    table="session_notifications",
                    columns=("created_at", "acknowledged_at"),
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
        _ = connection.execute(
            "UPDATE storage_sequences SET value = MAX(value, ?) WHERE scope = ?",
            (floor, scope),
        )

    @classmethod
    def _assert_existing_schema_version(cls, *, connection: sqlite3.Connection, database_path: Path) -> None:
        """Validate persisted ``user_version`` before any schema mutation.

        Fresh databases are not stamped here. Version stamping is deferred until
        ``_ensure_schema`` finishes all ``CREATE TABLE`` statements and the
        canonical-schema check succeeds, so partial bootstrap failures cannot
        leave a database marked as the canonical version with missing tables.
        """
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version == cls._SCHEMA_VERSION:
            return
        if version == 0:
            # A fresh database can briefly expose a partially-created canonical
            # table set to concurrent connections while the bootstrapper is still
            # validating and stamping ``user_version``. Let version-0 databases
            # continue through the idempotent CREATE TABLE path; the canonical
            # The schema assertion below rejects unsupported or corrupt tables before
            # the database is stamped as current.
            return
        if version == 6:
            # Allow migration from v6 (adds created_at_unix_ms column).
            return
        if version == 10:
            # Allow migration from v10 (adds keep_alive/steer_prompt columns).
            return
        if version == 11:
            # Allow migration from v11 (adds output-schema columns).
            return
        cls._raise_schema_mismatch(
            database_path=database_path,
            detail=(f"schema version mismatch: expected {cls._SCHEMA_VERSION} got {version}"),
        )

    @classmethod
    def _assert_schema_version(cls, *, connection: sqlite3.Connection, database_path: Path) -> None:
        """Stamp ``PRAGMA user_version`` only after the schema is fully validated."""
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version == 0:
            _ = connection.execute(f"PRAGMA user_version = {cls._SCHEMA_VERSION}")
            return
        if version == 10:
            # The v10 → v11 migration (keep_alive/steer_prompt columns) ran in
            # ``_ensure_schema``; stamp the migrated database as current.
            _ = connection.execute(f"PRAGMA user_version = {cls._SCHEMA_VERSION}")
            return
        if version == 11:
            # The v11 → v12 migration (output-schema columns) ran in
            # ``_ensure_schema``; stamp the migrated database as current.
            _ = connection.execute(f"PRAGMA user_version = {cls._SCHEMA_VERSION}")
            return
        if version != cls._SCHEMA_VERSION:
            cls._raise_schema_mismatch(
                database_path=database_path,
                detail=(f"schema version mismatch: expected {cls._SCHEMA_VERSION} got {version}"),
            )

    @classmethod
    def _assert_canonical_schema(cls, *, connection: sqlite3.Connection, database_path: Path) -> None:
        existing_tables = {
            cast(str, row["name"])
            for row in cast(
                list[sqlite3.Row],
                connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall(),
            )
        }
        missing_tables = sorted(set(cls._CANONICAL_SCHEMA) - existing_tables)
        if missing_tables:
            cls._raise_schema_mismatch(
                database_path=database_path,
                detail=f"missing tables: {', '.join(missing_tables)}",
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
            )
        unexpected_columns = sorted(actual_column_names - expected_column_names)
        if unexpected_columns:
            cls._raise_schema_mismatch(
                database_path=database_path,
                detail=(f"table '{table_name}' has unexpected columns: {', '.join(unexpected_columns)}"),
            )
        if actual_columns != expected_columns:
            cls._raise_schema_mismatch(
                database_path=database_path,
                detail=f"table '{table_name}' shape does not match canonical runtime schema",
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
        )

    @staticmethod
    def _table_columns(*, connection: sqlite3.Connection, table_name: str) -> tuple[tuple[str, str, int, str | None, int], ...]:
        return tuple(
            (
                cast(str, row["name"]),
                cast(str, row["type"]),
                cast(int, row["notnull"]),
                cast(str | None, row["dflt_value"]),
                cast(int, row["pk"]),
            )
            for row in cast(
                list[sqlite3.Row],
                connection.execute(f"PRAGMA table_info({table_name})").fetchall(),
            )
        )

    @staticmethod
    def _table_unique_indexes(*, connection: sqlite3.Connection, table_name: str) -> frozenset[tuple[str, ...]]:
        return frozenset(
            tuple(
                cast(str, column_row["name"])
                for column_row in cast(
                    list[sqlite3.Row],
                    connection.execute(f"PRAGMA index_info({cast(str, index_row['name'])})").fetchall(),
                )
            )
            for index_row in cast(
                list[sqlite3.Row],
                connection.execute(f"PRAGMA index_list({table_name})").fetchall(),
            )
            if cast(int, index_row["unique"]) == 1 and cast(str, index_row["origin"]) == "u"
        )

    @staticmethod
    def _raise_schema_mismatch(*, database_path: Path, detail: str) -> None:
        raise RuntimeError(
            "sqlite runtime schema mismatch: "
            f"{detail}. Reset the runtime database with "
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

    @classmethod
    def _parse_memory_kind(cls, value: str) -> MemoryKind:
        if value in cls._MEMORY_KINDS:
            return value
        raise ValueError(f"invalid memory kind: {value}")

    @staticmethod
    def _parse_memory_status(value: str) -> MemoryStatus:
        if value == "active":
            return "active"
        if value == "deleted":
            return "deleted"
        raise ValueError(f"invalid memory status: {value}")

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
        row = cast(
            sqlite3.Row | None,
            connection.execute(
                """
                UPDATE storage_sequences
                SET value = value + 1
                WHERE scope = ?
                RETURNING value
                """,
                (scope,),
            ).fetchone(),
        )
        if row is None:
            raise RuntimeError(f"runtime storage sequence is missing: {scope}")
        return cast(int, row["value"])

    def _next_timestamp(self, *, connection: sqlite3.Connection) -> int:
        return self._next_sequence_value(connection=connection, scope="sessions")
