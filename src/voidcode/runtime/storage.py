from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from time import sleep, time
from typing import Protocol, cast, final, runtime_checkable

from .contracts import (
    RuntimeNotification,
    RuntimeNotificationKind,
    RuntimeNotificationStatus,
    RuntimeRequest,
    RuntimeResponse,
    RuntimeSessionResult,
    RuntimeSessionRevertMarker,
    UnknownSessionError,
)
from .effectiveness import ToolEffectivenessEvent, ToolEffectivenessReport, project_tool_effectiveness
from .events import (
    DELEGATED_BACKGROUND_TASK_EVENT_TYPES,
    RUNTIME_ACP_CONNECTED,
    RUNTIME_ACP_DELEGATED_LIFECYCLE,
    RUNTIME_ACP_DISCONNECTED,
    RUNTIME_ACP_FAILED,
    RUNTIME_APPROVAL_REQUESTED,
    RUNTIME_MCP_SERVER_ACQUIRED,
    RUNTIME_MCP_SERVER_FAILED,
    RUNTIME_MCP_SERVER_IDLE_CLEANED,
    RUNTIME_MCP_SERVER_RELEASED,
    RUNTIME_MCP_SERVER_REUSED,
    RUNTIME_MCP_SERVER_STARTED,
    RUNTIME_MCP_SERVER_STOPPED,
    RUNTIME_QUESTION_REQUESTED,
    EventEnvelope,
    EventSource,
)
from .memory import MemoryKind, MemoryRecord, MemorySearchResult, MemoryStatus
from .paths import DB_PATH_ENV, sessions_db_path
from .permission import OperationClass, PathScope, PendingApproval, PermissionDecision
from .question import PendingQuestion, PendingQuestionOption, PendingQuestionPrompt
from .session import (
    SessionRef,
    SessionState,
    SessionStatus,
    StoredSessionSummary,
    normalize_persisted_session_metadata,
    session_metadata_for_persistence,
)
from .task import (
    BackgroundTaskRef,
    BackgroundTaskRequestSnapshot,
    BackgroundTaskState,
    BackgroundTaskStatus,
    DelegatedReminderState,
    DelegatedReminderStopCondition,
    StoredBackgroundTaskSummary,
    is_background_task_terminal,
    is_background_task_transition_allowed,
    validate_background_task_id,
)
from .todos import runtime_todos_from_state_payload, todo_state_payload


class SessionSealedError(Exception):
    """Raised when appending a non-lifecycle event to a terminal session.

    Once a session reaches ``completed`` or ``failed`` its event stream is
    sealed: only lifecycle events (ACP/MCP release, background-task finalize)
    may still be appended. Anything else would silently resurrect a terminal
    session's ordering/sequence and is rejected instead.

    This is the storage-level half of the runtime's single authoritative
    terminal-seal guard. Both event append paths (``append_session_event`` and
    ``append_session_events``) enforce it via
    ``_assert_terminal_session_events_allowed``, so a late event (provider
    delta, tool result, background-task completion, steer/follow-up replay)
    can never mutate a sealed session's truth regardless of which append path
    it enters through. The runtime-level half is
    ``VoidCodeRuntime._sealed_session_status``, which extends the seal to
    ``interrupted`` rows with no active run and gates the interaction queue.
    """


# Event types still allowed to append once a session is terminal. Each entry
# names the source that emits it after/around terminal status so the list stays
# auditable as call sites evolve:
#
# * ACP lifecycle — service._append_parent_acp_delegated_lifecycle_event
#   (RUNTIME_ACP_DELEGATED_LIFECYCLE) plus envelopes_for_acp_events
#   (events.py / event_envelopes.py: RUNTIME_ACP_CONNECTED/DISCONNECTED/FAILED).
# * MCP lifecycle — service._release_mcp_session_events →
#   envelopes_for_mcp_events (events.py / event_envelopes.py): the
#   runtime.mcp_server_* release/stop/idle-clean/failure events.
# * Delegated background-task lifecycle — background_tasks.py
#   append_session_event call sites (event_type_by_status →
#   completed/failed/cancelled; group_completed; waiting_approval;
#   idle_reminder; delegated_result_available), enumerated by
#   DELEGATED_BACKGROUND_TASK_EVENT_TYPES.
_TERMINAL_ALLOWED_EVENT_TYPES: frozenset[str] = frozenset(
    {
        RUNTIME_ACP_CONNECTED,
        RUNTIME_ACP_DISCONNECTED,
        RUNTIME_ACP_FAILED,
        RUNTIME_ACP_DELEGATED_LIFECYCLE,
        RUNTIME_MCP_SERVER_STARTED,
        RUNTIME_MCP_SERVER_REUSED,
        RUNTIME_MCP_SERVER_ACQUIRED,
        RUNTIME_MCP_SERVER_RELEASED,
        RUNTIME_MCP_SERVER_STOPPED,
        RUNTIME_MCP_SERVER_IDLE_CLEANED,
        RUNTIME_MCP_SERVER_FAILED,
        *DELEGATED_BACKGROUND_TASK_EVENT_TYPES,
    }
)


def _assert_terminal_session_events_allowed(
    *,
    session_id: str,
    status: str,
    events: Sequence[tuple[str, EventSource, dict[str, object], str | None]],
) -> None:
    """Single authoritative storage-level seal check for session event appends.

    ``append_session_event`` and ``append_session_events`` are the only writers
    of ``session_events`` rows; both MUST route through this check so a sealed
    terminal session (``completed``/``failed``) rejects every late non-lifecycle
    event regardless of entry path. ``interrupted`` rows are NOT sealed here:
    that row status is the live in-flight state of a running session; the
    runtime-level guard (``VoidCodeRuntime._sealed_session_status``) extends
    sealing to ``interrupted`` rows once no run is active.
    """
    if status not in {"completed", "failed"}:
        return
    for event_type, _source, _payload, _dedupe_key in events:
        if event_type not in _TERMINAL_ALLOWED_EVENT_TYPES:
            raise SessionSealedError(f"session {session_id!r} is {status}: refusing non-lifecycle event {event_type!r}")


def _pending_path_scope(value: object) -> PathScope | None:
    if value == "workspace":
        return "workspace"
    if value == "external":
        return "external"
    return None


def _pending_operation_class(value: object) -> OperationClass | None:
    if value == "read":
        return "read"
    if value == "write":
        return "write"
    if value == "execute":
        return "execute"
    return None


def _pending_permission_decision(value: object) -> PermissionDecision:
    if value == "allow":
        return "allow"
    if value == "deny":
        return "deny"
    if value == "ask":
        return "ask"
    raise ValueError(f"invalid permission policy mode: {value}")


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
class SqliteSessionStore:
    _database_path: Path | None
    _SCHEMA_VERSION = 11
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
    def _todo_state_from_metadata(metadata: dict[str, object]) -> dict[str, object] | None:
        raw_runtime_state = metadata.get("runtime_state")
        if not isinstance(raw_runtime_state, dict):
            return None
        runtime_state = cast(dict[str, object], raw_runtime_state)
        raw_todo_state = runtime_state.get("todos")
        if not isinstance(raw_todo_state, dict):
            return None
        todo_state = cast(dict[str, object], raw_todo_state)
        revision = todo_state.get("revision")
        return todo_state_payload(
            runtime_todos_from_state_payload(todo_state.get("todos")),
            revision=revision if isinstance(revision, int) and revision >= 0 else 0,
        )

    def _replace_session_todos(
        self,
        *,
        connection: sqlite3.Connection,
        workspace: Path,
        session_id: str,
        metadata: dict[str, object],
    ) -> None:
        todo_state = self._todo_state_from_metadata(metadata)
        _ = connection.execute(
            "DELETE FROM session_todos WHERE workspace_id = ? AND session_id = ?",
            (str(workspace), session_id),
        )
        if todo_state is None:
            return
        todos = runtime_todos_from_state_payload(todo_state.get("todos"))
        _ = connection.executemany(
            """
            INSERT INTO session_todos (
                workspace_id, session_id, position, content, status, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    str(workspace),
                    session_id,
                    todo["position"],
                    todo["content"],
                    todo["status"],
                    todo["updated_at"],
                )
                for todo in todos
            ],
        )

    def _todo_state_from_rows(
        self,
        *,
        connection: sqlite3.Connection,
        workspace: Path,
        session_id: str,
    ) -> dict[str, object] | None:
        rows = cast(
            list[sqlite3.Row],
            connection.execute(
                """
                SELECT position, content, status, updated_at
                FROM session_todos
                WHERE workspace_id = ? AND session_id = ?
                ORDER BY position ASC
                """,
                (str(workspace), session_id),
            ).fetchall(),
        )
        if not rows:
            return None
        todos = runtime_todos_from_state_payload(
            [
                {
                    "position": cast(int, row["position"]),
                    "content": cast(str, row["content"]),
                    "status": cast(str, row["status"]),
                    "updated_at": cast(int, row["updated_at"]),
                }
                for row in rows
            ]
        )
        revision = max((todo["updated_at"] for todo in todos), default=0)
        return todo_state_payload(todos, revision=revision)

    @classmethod
    def _metadata_with_todo_state(
        cls,
        metadata: dict[str, object],
        todo_state: dict[str, object] | None,
    ) -> dict[str, object]:
        if todo_state is None:
            return metadata
        raw_runtime_state = metadata.get("runtime_state")
        runtime_state = dict(cast(dict[str, object], raw_runtime_state)) if isinstance(raw_runtime_state, dict) else {}
        runtime_state["todos"] = todo_state
        return {**metadata, "runtime_state": runtime_state}

    @staticmethod
    def _todo_state_from_events(events: tuple[EventEnvelope, ...]) -> dict[str, object] | None:
        for event in reversed(events):
            if event.event_type != "runtime.todo_updated":
                continue
            todos = runtime_todos_from_state_payload(event.payload.get("todos"))
            revision = event.payload.get("revision")
            return todo_state_payload(
                todos,
                revision=revision if isinstance(revision, int) and revision >= 0 else 0,
            )
        return None

    def _write_session_snapshot(
        self,
        *,
        connection: sqlite3.Connection,
        workspace: Path,
        request: RuntimeRequest,
        response: RuntimeResponse,
        pending_approval_json: str | None,
        pending_question_json: str | None,
        resume_checkpoint: dict[str, object],
        seal_terminal_status: bool = True,
    ) -> int:
        """Persist session row metadata and todo state.

        Boundary contract (storage is append-only truth, context_window is the
        sole read-time projection layer):

        - The event log is NOT written here. Events are appended incrementally
          by the run loop via ``append_session_events``; this method only seals
          the terminal session-row snapshot (status, output, metadata, turn,
          prompt, updated_at, todos, resume checkpoint).
        - ``last_event_sequence`` is never regressed: it is set to the maximum
          of the row's existing value (maintained by incremental appends) and
          the highest sequence in ``response.events``.
        - Metadata is bounded for safety via ``session_metadata_for_persistence``
          (secret scrubbing, length limits) — that is a safety bound, not
          context compaction. Context projection lives in ``context_window.py``.
        - When ``seal_terminal_status`` is False the row is written as
          ``interrupted`` instead of the terminal status: a newer run on the
          same session is still active, so the terminal seal must not clobber
          it (the incremental event log means overlapping runs can coexist).
        """
        session_id = response.session.session.id
        events = response.events
        persisted_metadata = session_metadata_for_persistence(
            response.session.metadata,
            events=events,
        )
        created_at = self._read_created_at(
            connection=connection,
            workspace=workspace,
            session_id=session_id,
        )
        created_at_unix_ms = self._read_created_at_unix_ms(
            connection=connection,
            workspace=workspace,
            session_id=session_id,
        )
        if created_at_unix_ms is None:
            created_at_unix_ms = int(time() * 1000)
        updated_at = self._next_timestamp(connection=connection)
        # The row watermark (maintained by every incremental
        # ``append_session_events``) IS the persisted truth. Clamp the sealed
        # value to the actual ``session_events`` max so a response whose
        # trailing events were only locally sequenced (resume paths resequence
        # client-only events like MCP/hook/release chunks that are never
        # appended) can never inflate the watermark beyond the durable event
        # log — a phantom sequence would break replay, resume truncation, and
        # notification reference-integrity.
        last_event_sequence = max(
            self._read_last_event_sequence(
                connection=connection,
                workspace=workspace,
                session_id=session_id,
            ),
            self._max_persisted_event_sequence(
                connection=connection,
                workspace=workspace,
                session_id=session_id,
            ),
        )
        status = response.session.status if seal_terminal_status else "interrupted"
        _ = connection.execute(
            """
            INSERT OR REPLACE INTO sessions (
                session_id, parent_session_id, workspace_id, status, turn, prompt, output,
                metadata_json, pending_approval_json, pending_question_json,
                resume_checkpoint_json, created_at, updated_at,
                last_event_sequence, created_at_unix_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                response.session.session.parent_id,
                str(workspace),
                status,
                response.session.turn,
                request.prompt,
                response.output,
                json.dumps(persisted_metadata, sort_keys=True),
                pending_approval_json,
                pending_question_json,
                json.dumps(resume_checkpoint, sort_keys=True),
                created_at,
                updated_at,
                last_event_sequence,
                created_at_unix_ms,
            ),
        )
        self._replace_session_todos(
            connection=connection,
            workspace=workspace,
            session_id=session_id,
            metadata=persisted_metadata,
        )
        return updated_at

    @staticmethod
    def _checkpoint_skill_snapshot(
        metadata: dict[str, object],
    ) -> tuple[object | None, object | None, dict[str, object]]:
        snapshot_payload = metadata.get("skill_snapshot")
        snapshot = cast(dict[str, object], snapshot_payload) if isinstance(snapshot_payload, dict) else {}
        binding_payload = snapshot.get("binding_snapshot")
        binding_snapshot = cast(dict[str, object], binding_payload) if isinstance(binding_payload, dict) else {}
        return snapshot.get("snapshot_hash"), snapshot.get("snapshot_version"), binding_snapshot

    @classmethod
    def _resume_checkpoint_base(
        cls,
        *,
        request: RuntimeRequest,
        response: RuntimeResponse,
        kind: str,
        last_event_sequence: int | None = None,
    ) -> dict[str, object]:
        snapshot_hash, snapshot_version, binding_snapshot = cls._checkpoint_skill_snapshot(response.session.metadata)
        return {
            "version": 1,
            "kind": kind,
            "prompt": request.prompt,
            "session_status": response.session.status,
            "session_metadata": session_metadata_for_persistence(
                response.session.metadata,
                events=response.events,
            ),
            "skill_snapshot_hash": snapshot_hash,
            "skill_snapshot_version": snapshot_version,
            "skill_binding_snapshot": binding_snapshot,
            "tool_results": cls._tool_results_from_events(response.events),
            "last_event_sequence": (cls._session_last_event_sequence(response.events) if last_event_sequence is None else last_event_sequence),
            "output": response.output,
        }

    @staticmethod
    def _decode_json_object_payload(
        payload: str,
        *,
        malformed_message: str,
        non_object_message: str,
    ) -> dict[str, object]:
        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError(malformed_message) from exc
        if not isinstance(decoded, dict):
            raise ValueError(non_object_message)
        return cast(dict[str, object], decoded)

    @classmethod
    def _decode_resume_checkpoint_payload(cls, payload: str) -> dict[str, object]:
        checkpoint = cls._decode_json_object_payload(
            payload,
            malformed_message="persisted resume checkpoint JSON is malformed",
            non_object_message="persisted resume checkpoint payload must decode to an object",
        )
        kind = checkpoint.get("kind")
        if not isinstance(kind, str) or kind not in cls._RESUME_CHECKPOINT_KINDS:
            raise ValueError(f"persisted resume checkpoint kind is invalid: {kind!r}")
        return checkpoint

    @staticmethod
    def _background_task_runtime_state_defaults() -> dict[str, object]:
        return {
            "approval_request_id": None,
            "question_request_id": None,
            "cancellation_cause": None,
            "result_available": 0,
        }

    @classmethod
    def _request_id_from_pending_payload(cls, payload: str | None) -> str | None:
        if payload is None:
            return None
        parsed = json.loads(payload)
        if not isinstance(parsed, dict):
            return None
        request_id = cast(dict[str, object], parsed).get("request_id")
        return request_id if isinstance(request_id, str) else None

    @classmethod
    def _background_task_runtime_state_from_session_row(cls, row: sqlite3.Row | None) -> dict[str, object]:
        if row is None:
            return cls._background_task_runtime_state_defaults()
        status = cast(str, row["status"])
        return {
            "approval_request_id": cls._request_id_from_pending_payload(cast(str | None, row["pending_approval_json"])),
            "question_request_id": cls._request_id_from_pending_payload(cast(str | None, row["pending_question_json"])),
            "cancellation_cause": None,
            "result_available": 1 if status in {"waiting", "completed", "failed"} else 0,
        }

    @classmethod
    def _background_task_summary_from_row(cls, row: sqlite3.Row) -> StoredBackgroundTaskSummary:
        return StoredBackgroundTaskSummary(
            task=BackgroundTaskRef(id=cast(str, row["task_id"])),
            status=cls._parse_background_task_status(cast(str, row["status"])),
            prompt=cast(str, row["prompt"]),
            session_id=cast(str | None, row["session_id"]),
            error=cast(str | None, row["error"]),
            created_at=cast(int, row["created_at"]),
            updated_at=cast(int, row["updated_at"]),
            created_at_unix_ms=cast(int | None, row["created_at_unix_ms"]),
            keep_alive=bool(cast(int, row["keep_alive"])),
            steer_prompt=cast(str | None, row["steer_prompt"]),
        )

    @staticmethod
    def _background_task_durable_payload(row: sqlite3.Row) -> dict[str, object]:
        durable_payload: dict[str, object] = {
            "task_id": cast(str, row["task_id"]),
            "parent_session_id": cast(str | None, row["request_parent_session_id"]),
            "status": cast(str, row["status"]),
            "result_available": bool(cast(int, row["result_available"])),
        }
        reminder_state = SqliteSessionStore._delegated_reminder_state_from_payload(cast(str | None, row["delegated_reminder_json"]))
        if reminder_state is not None:
            durable_payload["delegated_reminder"] = SqliteSessionStore._delegated_reminder_state_payload(reminder_state)
        optional_fields: tuple[tuple[str, str], ...] = (
            ("requested_child_session_id", "requested_child_session_id"),
            ("child_session_id", "session_id"),
            ("approval_request_id", "approval_request_id"),
            ("question_request_id", "question_request_id"),
            ("routing_mode", "routing_mode"),
            ("routing_subagent_type", "routing_subagent_type"),
            ("routing_description", "routing_description"),
            ("routing_command", "routing_command"),
            ("cancellation_cause", "cancellation_cause"),
        )
        for payload_key, row_key in optional_fields:
            value = row[row_key]
            if value is not None:
                durable_payload[payload_key] = cast(object, value)
        return durable_payload

    @staticmethod
    def _delegated_reminder_state_payload(state: DelegatedReminderState) -> dict[str, object]:
        payload: dict[str, object] = {
            "task_id": state.task_id,
            "eligible": state.eligible,
            "already_sent_for_idle_episode": state.already_sent_for_idle_episode,
        }
        optional_fields: tuple[tuple[str, object | None], ...] = (
            ("parent_session_id", state.parent_session_id),
            ("child_session_id", state.child_session_id),
            ("idle_episode_id", state.idle_episode_id),
            ("idle_detected_at_unix_ms", state.idle_detected_at_unix_ms),
            ("reminder_sent_at_unix_ms", state.reminder_sent_at_unix_ms),
            ("stopped_at_unix_ms", state.stopped_at_unix_ms),
            ("stop_condition", state.stop_condition),
        )
        for key, value in optional_fields:
            if value is not None:
                payload[key] = value
        return payload

    @staticmethod
    def _parse_delegated_reminder_stop_condition(
        value: object,
    ) -> DelegatedReminderStopCondition | None:
        if value is None:
            return None
        if value in {
            "result_read",
            "explicit_retry",
            "cancellation",
            "terminal_status",
            "already_sent_for_idle_episode",
        }:
            return cast(DelegatedReminderStopCondition, value)
        raise ValueError(f"invalid delegated reminder stop condition: {value!r}")

    @classmethod
    def _delegated_reminder_state_from_payload(cls, payload: str | None) -> DelegatedReminderState | None:
        if payload is None:
            return None
        decoded = cls._decode_json_object_payload(
            payload,
            malformed_message="persisted delegated reminder JSON is malformed",
            non_object_message="persisted delegated reminder payload must decode to an object",
        )
        task_id = decoded.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("persisted delegated reminder task_id must be a non-empty string")
        stop_condition = cls._parse_delegated_reminder_stop_condition(decoded.get("stop_condition"))
        return DelegatedReminderState(
            task_id=task_id,
            parent_session_id=cls._optional_string(decoded.get("parent_session_id")),
            child_session_id=cls._optional_string(decoded.get("child_session_id")),
            idle_episode_id=cls._optional_string(decoded.get("idle_episode_id")),
            idle_detected_at_unix_ms=cls._optional_int(decoded.get("idle_detected_at_unix_ms")),
            reminder_sent_at_unix_ms=cls._optional_int(decoded.get("reminder_sent_at_unix_ms")),
            stopped_at_unix_ms=cls._optional_int(decoded.get("stopped_at_unix_ms")),
            stop_condition=stop_condition,
        )

    @staticmethod
    def _optional_string(value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value:
            raise ValueError("persisted delegated reminder string fields must be non-empty strings")
        return value

    @staticmethod
    def _optional_int(value: object) -> int | None:
        if value is None:
            return None
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError("persisted delegated reminder timestamp fields must be integers")
        return value

    @staticmethod
    def _pending_question_payload(pending_question: PendingQuestion) -> dict[str, object]:
        return {
            "request_id": pending_question.request_id,
            "tool_name": pending_question.tool_name,
            "arguments": pending_question.arguments,
            "prompts": [
                {
                    "question": prompt.question,
                    "header": prompt.header,
                    "multiple": prompt.multiple,
                    "options": [{"label": option.label, "description": option.description} for option in prompt.options],
                }
                for prompt in pending_question.prompts
            ],
        }

    def save_run(
        self,
        *,
        workspace: Path,
        request: RuntimeRequest,
        response: RuntimeResponse,
        clear_pending_approval: bool = True,
        seal_terminal_status: bool = True,
    ) -> None:
        """Seal the terminal session-row state for a completed run.

        Boundary: this is a terminal seal-writer — it writes the ``sessions``
        row snapshot (status, output, metadata, turn, prompt, updated_at,
        todos) and the terminal ``resume_checkpoint_json``, but it does NOT
        write ``session_events`` rows. The event log is persisted incrementally
        by the run loop via ``append_session_events``; this method only seals
        the terminal state and never regresses ``last_event_sequence``.
        Context assembly lives in ``context_window.py``.

        ``seal_terminal_status=False`` writes the row as ``interrupted`` instead
        of the terminal status, so an older-finishing run cannot re-seal a
        session that a newer run is still actively appending to.
        """
        session_id = response.session.session.id
        with self._write_connect(workspace) as connection:
            # The durable event-log watermark — see ``_write_session_snapshot``.
            # The resume checkpoint and the terminal notification must reference
            # this persisted truth, never a locally-resequenced response tail.
            persisted_last_sequence = self._max_persisted_event_sequence(
                connection=connection,
                workspace=workspace,
                session_id=session_id,
            )
            updated_at = self._write_session_snapshot(
                connection=connection,
                workspace=workspace,
                request=request,
                response=response,
                pending_approval_json=(
                    None
                    if clear_pending_approval
                    else self._read_pending_approval_json(
                        connection=connection,
                        workspace=workspace,
                        session_id=session_id,
                    )
                ),
                pending_question_json=None,
                resume_checkpoint=self._run_resume_checkpoint(
                    request=request,
                    response=response,
                    last_event_sequence=persisted_last_sequence,
                ),
                seal_terminal_status=seal_terminal_status,
            )
            self._sync_background_task_durable_state(
                connection=connection,
                workspace=workspace,
                request=request,
                response=response,
            )
            self._sync_notifications(
                connection=connection,
                workspace=workspace,
                request=request,
                response=response,
                pending_approval=None,
                notification_run_id=updated_at,
                last_event_sequence=persisted_last_sequence,
            )
            connection.commit()

    def list_sessions(self, *, workspace: Path) -> tuple[StoredSessionSummary, ...]:
        self._auto_prune_sessions_for_list(workspace=workspace)
        with self._connect(workspace) as connection:
            rows = cast(
                list[sqlite3.Row],
                connection.execute(
                    """
                SELECT session_id, parent_session_id, status, turn, prompt, updated_at
                FROM sessions
                WHERE workspace_id = ?
                ORDER BY updated_at DESC, session_id ASC
                """,
                    (str(workspace),),
                ).fetchall(),
            )
        return tuple(
            StoredSessionSummary(
                session=SessionRef(
                    id=cast(str, row["session_id"]),
                    parent_id=cast(str | None, row["parent_session_id"]),
                ),
                status=self._parse_session_status(cast(str, row["status"])),
                turn=cast(int, row["turn"]),
                prompt=cast(str, row["prompt"]),
                updated_at=cast(int, row["updated_at"]),
            )
            for row in rows
        )

    def _auto_prune_sessions_for_list(self, *, workspace: Path) -> None:
        with self._write_connect(workspace) as connection:
            self._auto_prune_sessions(connection=connection, workspace=workspace)
            connection.commit()

    @staticmethod
    def _validate_memory_content(content: str) -> str:
        if not content.strip():
            raise ValueError("memory content must not be empty")
        return content

    @classmethod
    def _validate_memory_kind(cls, kind: str) -> MemoryKind:
        if kind not in cls._MEMORY_KINDS:
            raise ValueError(f"invalid memory kind: {kind}")
        return kind

    @staticmethod
    def _validate_memory_tags(tags: tuple[str, ...]) -> tuple[str, ...]:
        for tag in tags:
            if not tag.strip():
                raise ValueError("memory tags must not be empty")
        if len(set(tags)) != len(tags):
            raise ValueError("memory tags must be unique")
        return tags

    @classmethod
    def _memory_record_from_row(cls, row: sqlite3.Row) -> MemoryRecord:
        tags_payload = cast(str, row["tags_json"])
        try:
            decoded_tags = json.loads(tags_payload)
        except json.JSONDecodeError as exc:
            raise ValueError("persisted memory tags JSON is malformed") from exc
        if not isinstance(decoded_tags, list) or not all(isinstance(tag, str) for tag in decoded_tags):
            raise ValueError("persisted memory tags payload must decode to a string list")
        scope = cast(str, row["scope"])
        if scope != "workspace":
            raise ValueError(f"invalid memory scope: {scope}")
        return MemoryRecord(
            id=cast(str, row["memory_id"]),
            workspace_id=cast(str, row["workspace_id"]),
            kind=cls._parse_memory_kind(cast(str, row["kind"])),
            content=cast(str, row["content"]),
            tags=tuple(decoded_tags),
            status=cls._parse_memory_status(cast(str, row["status"])),
            scope="workspace",
            created_at=cast(int, row["created_at"]),
            updated_at=cast(int, row["updated_at"]),
            deleted_at=cast(int | None, row["deleted_at"]),
            source_session_id=cast(str | None, row["source_session_id"]),
        )

    def add_memory(
        self,
        *,
        workspace: Path,
        content: str,
        kind: MemoryKind = "project",
        tags: tuple[str, ...] = (),
        source_session_id: str | None = None,
    ) -> MemoryRecord:
        validated_content = self._validate_memory_content(content)
        validated_kind = self._validate_memory_kind(kind)
        validated_tags = self._validate_memory_tags(tags)
        with self._write_connect(workspace) as connection:
            timestamp = self._next_memory_timestamp(connection=connection)
            memory_id = f"mem_{timestamp}"
            _ = connection.execute(
                """
                INSERT INTO memories (
                    memory_id, workspace_id, kind, content, tags_json, scope, status,
                    source_session_id, created_at, updated_at, deleted_at
                ) VALUES (?, ?, ?, ?, ?, 'workspace', 'active', ?, ?, ?, NULL)
                """,
                (
                    memory_id,
                    str(workspace),
                    validated_kind,
                    validated_content,
                    json.dumps(list(validated_tags), sort_keys=True),
                    source_session_id,
                    timestamp,
                    timestamp,
                ),
            )
            connection.commit()
        record = self.get_memory(workspace=workspace, memory_id=memory_id)
        if record is None:
            raise RuntimeError(f"memory was not persisted: {memory_id}")
        return record

    def list_memories(self, *, workspace: Path, include_deleted: bool = False) -> tuple[MemoryRecord, ...]:
        status_clause = "" if include_deleted else "AND status = 'active'"
        with self._connect(workspace) as connection:
            rows = cast(
                list[sqlite3.Row],
                connection.execute(
                    f"""
                    SELECT memory_id, workspace_id, kind, content, tags_json, scope, status,
                           source_session_id, created_at, updated_at, deleted_at
                    FROM memories
                    WHERE workspace_id = ? {status_clause}
                    ORDER BY updated_at DESC, memory_id ASC
                    """,
                    (str(workspace),),
                ).fetchall(),
            )
        return tuple(self._memory_record_from_row(row) for row in rows)

    @staticmethod
    def _memory_search_terms(query: str) -> tuple[str, ...]:
        terms: list[str] = []
        seen: set[str] = set()
        for raw_term in query.casefold().split():
            term = raw_term.strip()
            if term and term not in seen:
                terms.append(term)
                seen.add(term)
        return tuple(terms)

    @staticmethod
    def _score_memory(record: MemoryRecord, terms: tuple[str, ...]) -> tuple[int, tuple[str, ...]]:
        haystacks = (record.content.casefold(), *(tag.casefold() for tag in record.tags))
        matched_terms = tuple(term for term in terms if any(term in haystack for haystack in haystacks))
        score = sum(haystack.count(term) for term in terms for haystack in haystacks)
        return score, matched_terms

    def search_memories(self, *, workspace: Path, query: str) -> tuple[MemorySearchResult, ...]:
        terms = self._memory_search_terms(query)
        if not terms:
            return ()
        results: list[MemorySearchResult] = []
        for record in self.list_memories(workspace=workspace):
            score, matched_terms = self._score_memory(record, terms)
            if score == 0:
                continue
            results.append(MemorySearchResult(record=record, score=score, matched_terms=matched_terms))
        return tuple(
            sorted(
                results,
                key=lambda result: (-result.score, -result.record.updated_at, result.record.id),
            )
        )

    def get_memory(self, *, workspace: Path, memory_id: str) -> MemoryRecord | None:
        with self._connect(workspace) as connection:
            row = cast(
                sqlite3.Row | None,
                connection.execute(
                    """
                    SELECT memory_id, workspace_id, kind, content, tags_json, scope, status,
                           source_session_id, created_at, updated_at, deleted_at
                    FROM memories
                    WHERE workspace_id = ? AND memory_id = ? AND status = 'active'
                    """,
                    (str(workspace), memory_id),
                ).fetchone(),
            )
        return None if row is None else self._memory_record_from_row(row)

    def delete_memory(self, *, workspace: Path, memory_id: str) -> MemoryRecord:
        with self._write_connect(workspace) as connection:
            existing = self._memory_row(
                connection=connection,
                workspace=workspace,
                memory_id=memory_id,
                include_deleted=False,
            )
            if existing is None:
                raise ValueError(f"unknown memory: {memory_id}")
            timestamp = self._next_memory_timestamp(connection=connection)
            _ = connection.execute(
                """
                UPDATE memories
                SET status = 'deleted', updated_at = ?, deleted_at = ?
                WHERE workspace_id = ? AND memory_id = ? AND status = 'active'
                """,
                (timestamp, timestamp, str(workspace), memory_id),
            )
            deleted = self._memory_row(
                connection=connection,
                workspace=workspace,
                memory_id=memory_id,
                include_deleted=True,
            )
            connection.commit()
        if deleted is None:
            raise RuntimeError(f"memory was not tombstoned: {memory_id}")
        return self._memory_record_from_row(deleted)

    @staticmethod
    def _memory_row(
        *,
        connection: sqlite3.Connection,
        workspace: Path,
        memory_id: str,
        include_deleted: bool,
    ) -> sqlite3.Row | None:
        status_clause = "" if include_deleted else "AND status = 'active'"
        return cast(
            sqlite3.Row | None,
            connection.execute(
                f"""
                SELECT memory_id, workspace_id, kind, content, tags_json, scope, status,
                       source_session_id, created_at, updated_at, deleted_at
                FROM memories
                WHERE workspace_id = ? AND memory_id = ? {status_clause}
                """,
                (str(workspace), memory_id),
            ).fetchone(),
        )

    def save_pending_approval(
        self,
        *,
        workspace: Path,
        request: RuntimeRequest,
        response: RuntimeResponse,
        pending_approval: PendingApproval,
    ) -> None:
        with self._write_connect(workspace) as connection:
            persisted_last_sequence = self._max_persisted_event_sequence(
                connection=connection,
                workspace=workspace,
                session_id=response.session.session.id,
            )
            updated_at = self._write_session_snapshot(
                connection=connection,
                workspace=workspace,
                request=request,
                response=response,
                pending_approval_json=json.dumps(asdict(pending_approval), sort_keys=True),
                pending_question_json=None,
                resume_checkpoint=self._approval_wait_resume_checkpoint(
                    request=request,
                    response=response,
                    pending_approval=pending_approval,
                    last_event_sequence=persisted_last_sequence,
                ),
            )
            self._sync_background_task_durable_state(
                connection=connection,
                workspace=workspace,
                request=request,
                response=response,
                approval_request_id=pending_approval.request_id,
            )
            self._sync_notifications(
                connection=connection,
                workspace=workspace,
                request=request,
                response=response,
                pending_approval=pending_approval,
                notification_run_id=updated_at,
                last_event_sequence=persisted_last_sequence,
            )
            connection.commit()

    def load_pending_approval(self, *, workspace: Path, session_id: str) -> PendingApproval | None:
        with self._connect(workspace) as connection:
            row = cast(
                sqlite3.Row | None,
                connection.execute(
                    """
                    SELECT pending_approval_json
                    FROM sessions
                    WHERE workspace_id = ? AND session_id = ?
                    """,
                    (str(workspace), session_id),
                ).fetchone(),
            )
        if row is None:
            raise UnknownSessionError(f"unknown session: {session_id}")
        payload = cast(str | None, row["pending_approval_json"])
        if payload is None:
            return None
        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"persisted pending approval for session {session_id!r} is corrupt: "
                f"{exc}. Run `voidcode sessions debug {session_id}` to inspect, "
                "or `voidcode storage reset` to recover."
            ) from exc
        if not isinstance(decoded, dict):
            raise RuntimeError(f"persisted pending approval for session {session_id!r} is corrupt: payload must decode to an object.")
        data = cast(dict[str, object], decoded)
        required_fields = frozenset(field.name for field in fields(PendingApproval))
        missing_fields = sorted(required_fields - data.keys())
        if missing_fields:
            raise RuntimeError(
                f"persisted pending approval for session {session_id!r} is missing "
                f"required fields {missing_fields!r}; run `voidcode storage reset` to recover."
            )
        request_id = data["request_id"]
        tool_name = data["tool_name"]
        if not isinstance(request_id, str) or not isinstance(tool_name, str):
            raise RuntimeError(
                f"persisted pending approval for session {session_id!r} has invalid "
                "request_id/tool_name types; run `voidcode storage reset` to recover."
            )
        arguments = data["arguments"]
        target_summary = data["target_summary"]
        reason = data["reason"]
        if not isinstance(arguments, dict):
            raise RuntimeError("persisted pending approval arguments must be an object")
        if not isinstance(target_summary, str) or not isinstance(reason, str):
            raise RuntimeError("persisted pending approval summary and reason must be strings")
        raw_policy_mode = data["policy_mode"]
        try:
            policy_mode = _pending_permission_decision(raw_policy_mode)
        except ValueError as exc:
            raise RuntimeError(f"persisted pending approval for session {session_id!r} has invalid policy_mode {raw_policy_mode!r}: {exc}") from exc
        request_event_sequence = data["request_event_sequence"]
        if request_event_sequence is not None and (not isinstance(request_event_sequence, int) or isinstance(request_event_sequence, bool)):
            raise RuntimeError("persisted pending approval request_event_sequence must be an integer or null")
        nullable_string_fields = (
            "owner_session_id",
            "owner_parent_session_id",
            "delegated_task_id",
            "canonical_path",
            "matched_rule",
            "policy_surface",
        )
        for field_name in nullable_string_fields:
            value = data[field_name]
            if value is not None and not isinstance(value, str):
                raise RuntimeError(f"persisted pending approval {field_name} must be a string or null")
        path_scope = _pending_path_scope(data["path_scope"])
        if data["path_scope"] is not None and path_scope is None:
            raise RuntimeError("persisted pending approval path_scope is invalid")
        operation_class = _pending_operation_class(data["operation_class"])
        if data["operation_class"] is not None and operation_class is None:
            raise RuntimeError("persisted pending approval operation_class is invalid")
        return PendingApproval(
            request_id=request_id,
            tool_name=tool_name,
            arguments=cast(dict[str, object], arguments),
            target_summary=target_summary,
            reason=reason,
            policy_mode=policy_mode,
            request_event_sequence=request_event_sequence,
            owner_session_id=(data["owner_session_id"] if isinstance(data["owner_session_id"], str) else None),
            owner_parent_session_id=(data["owner_parent_session_id"] if isinstance(data["owner_parent_session_id"], str) else None),
            delegated_task_id=(data["delegated_task_id"] if isinstance(data["delegated_task_id"], str) else None),
            path_scope=path_scope,
            operation_class=operation_class,
            canonical_path=(data["canonical_path"] if isinstance(data["canonical_path"], str) else None),
            matched_rule=(data["matched_rule"] if isinstance(data["matched_rule"], str) else None),
            policy_surface=(data["policy_surface"] if isinstance(data["policy_surface"], str) else None),
        )

    def clear_pending_approval(self, *, workspace: Path, session_id: str) -> None:
        with self._write_connect(workspace) as connection:
            _ = connection.execute(
                "UPDATE sessions SET pending_approval_json = NULL WHERE workspace_id = ? AND session_id = ?",  # noqa: E501
                (str(workspace), session_id),
            )
            connection.commit()

    def save_pending_question(
        self,
        *,
        workspace: Path,
        request: RuntimeRequest,
        response: RuntimeResponse,
        pending_question: PendingQuestion,
    ) -> None:
        with self._write_connect(workspace) as connection:
            persisted_last_sequence = self._max_persisted_event_sequence(
                connection=connection,
                workspace=workspace,
                session_id=response.session.session.id,
            )
            updated_at = self._write_session_snapshot(
                connection=connection,
                workspace=workspace,
                request=request,
                response=response,
                pending_approval_json=None,
                pending_question_json=json.dumps(self._pending_question_payload(pending_question), sort_keys=True),
                resume_checkpoint=self._question_wait_resume_checkpoint(
                    request=request,
                    response=response,
                    pending_question=pending_question,
                    last_event_sequence=persisted_last_sequence,
                ),
            )
            self._sync_background_task_durable_state(
                connection=connection,
                workspace=workspace,
                request=request,
                response=response,
                question_request_id=pending_question.request_id,
            )
            self._sync_notifications(
                connection=connection,
                workspace=workspace,
                request=request,
                response=response,
                pending_approval=None,
                pending_question=pending_question,
                notification_run_id=updated_at,
                last_event_sequence=persisted_last_sequence,
            )
            connection.commit()

    def load_pending_question(self, *, workspace: Path, session_id: str) -> PendingQuestion | None:
        with self._connect(workspace) as connection:
            row = cast(
                sqlite3.Row | None,
                connection.execute(
                    """
                    SELECT pending_question_json
                    FROM sessions
                    WHERE workspace_id = ? AND session_id = ?
                    """,
                    (str(workspace), session_id),
                ).fetchone(),
            )
        if row is None:
            raise UnknownSessionError(f"unknown session: {session_id}")
        payload = cast(str | None, row["pending_question_json"])
        if payload is None:
            return None
        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"persisted pending question for session {session_id!r} is corrupt: "
                f"{exc}. Run `voidcode sessions debug {session_id}` to inspect, "
                "or `voidcode storage reset` to recover."
            ) from exc
        if not isinstance(decoded, dict):
            raise RuntimeError(f"persisted pending question for session {session_id!r} is corrupt: payload must decode to an object.")
        data = cast(dict[str, object], decoded)
        required_fields = {"request_id", "tool_name", "arguments", "prompts"}
        missing_fields = sorted(required_fields - data.keys())
        if missing_fields:
            raise RuntimeError(
                f"persisted pending question for session {session_id!r} is missing "
                f"required fields {missing_fields!r}; run `voidcode storage reset` to recover."
            )
        request_id = data["request_id"]
        if not isinstance(request_id, str):
            raise RuntimeError(
                f"persisted pending question for session {session_id!r} has invalid request_id type; run `voidcode storage reset` to recover."
            )
        raw_prompts = data["prompts"]
        if not isinstance(raw_prompts, list):
            raise RuntimeError(f"persisted pending question for session {session_id!r} has invalid prompts payload (must be a list).")
        prompts: list[PendingQuestionPrompt] = []
        for prompt_index, raw_prompt in enumerate(cast(list[object], raw_prompts)):
            if not isinstance(raw_prompt, dict):
                raise RuntimeError(f"persisted pending question for session {session_id!r} has invalid prompts[{prompt_index}] (must be an object).")
            prompt_payload = cast(dict[str, object], raw_prompt)
            if not {"question", "header", "multiple", "options"} <= prompt_payload.keys():
                raise RuntimeError(f"persisted pending question for session {session_id!r} has incomplete prompts[{prompt_index}] payload.")
            raw_options = prompt_payload["options"]
            if not isinstance(raw_options, list):
                raise RuntimeError(
                    f"persisted pending question for session {session_id!r} has invalid prompts[{prompt_index}].options (must be a list)."
                )
            options_list: list[PendingQuestionOption] = []
            for option_index, raw_option in enumerate(cast(list[object], raw_options)):
                if not isinstance(raw_option, dict):
                    raise RuntimeError(
                        f"persisted pending question for session {session_id!r} has "
                        f"invalid prompts[{prompt_index}].options[{option_index}] "
                        "(must be an object)."
                    )
                option_payload = cast(dict[str, object], raw_option)
                if set(option_payload) != {"label", "description"}:
                    raise RuntimeError(
                        f"persisted pending question for session {session_id!r} has "
                        f"incomplete prompts[{prompt_index}].options[{option_index}] payload."
                    )
                option_label = option_payload["label"]
                option_description = option_payload["description"]
                if not isinstance(option_label, str) or not isinstance(option_description, str):
                    raise RuntimeError(
                        f"persisted pending question for session {session_id!r} has invalid prompts[{prompt_index}].options[{option_index}] strings."
                    )
                options_list.append(
                    PendingQuestionOption(
                        label=option_label,
                        description=option_description,
                    )
                )
            prompt_question = prompt_payload["question"]
            prompt_header = prompt_payload["header"]
            if not isinstance(prompt_question, str) or not isinstance(prompt_header, str):
                raise RuntimeError(
                    f"persisted pending question for session {session_id!r} has invalid prompts[{prompt_index}].question/header (must be strings)."
                )
            raw_multiple = prompt_payload["multiple"]
            if not isinstance(raw_multiple, bool):
                raise RuntimeError(
                    f"persisted pending question for session {session_id!r} has invalid prompts[{prompt_index}].multiple (must be a boolean)."
                )
            prompts.append(
                PendingQuestionPrompt(
                    question=prompt_question,
                    header=prompt_header,
                    options=tuple(options_list),
                    multiple=raw_multiple,
                )
            )
        tool_name_value = data["tool_name"]
        if not isinstance(tool_name_value, str):
            raise RuntimeError("persisted pending question tool_name must be a string")
        arguments_value = data["arguments"]
        if not isinstance(arguments_value, dict):
            raise RuntimeError("persisted pending question arguments must be an object")
        return PendingQuestion(
            request_id=request_id,
            tool_name=tool_name_value,
            arguments=cast(dict[str, object], arguments_value),
            prompts=tuple(prompts),
        )

    def clear_pending_question(self, *, workspace: Path, session_id: str) -> None:
        with self._write_connect(workspace) as connection:
            _ = connection.execute(
                ("UPDATE sessions SET pending_question_json = NULL WHERE workspace_id = ? AND session_id = ?"),
                (str(workspace), session_id),
            )
            connection.commit()

    def load_resume_checkpoint(self, *, workspace: Path, session_id: str) -> dict[str, object] | None:
        with self._connect(workspace) as connection:
            row = cast(
                sqlite3.Row | None,
                connection.execute(
                    """
                    SELECT resume_checkpoint_json
                    FROM sessions
                    WHERE workspace_id = ? AND session_id = ?
                    """,
                    (str(workspace), session_id),
                ).fetchone(),
            )
        if row is None:
            raise UnknownSessionError(f"unknown session: {session_id}")
        payload = cast(str | None, row["resume_checkpoint_json"])
        if payload is None:
            return None
        return self._decode_resume_checkpoint_payload(payload)

    def append_session_event(
        self,
        *,
        workspace: Path,
        session_id: str,
        event_type: str,
        source: EventSource,
        payload: dict[str, object],
        dedupe_key: str | None = None,
    ) -> EventEnvelope | None:
        """Append a single event to the session_events table — append-only, never modifies.

        Boundary: no compaction, no merging, no truncation. Events are append-only
        truth. Context projection (what the model sees) is handled exclusively by
        ``context_window.py``. This method and ``append_session_events`` are the
        only writers of ``session_events`` rows.
        """
        with self._write_connect(workspace) as connection:
            payload = self._enriched_background_task_event_payload(
                connection=connection,
                workspace=workspace,
                event_type=event_type,
                payload=payload,
            )
            # Verify the session exists before mutating any session state. We hold a
            # write lock from BEGIN IMMEDIATE, so this read is consistent with the
            # subsequent UPDATE.
            existing_row = cast(
                sqlite3.Row | None,
                connection.execute(
                    """
                    SELECT status
                    FROM sessions
                    WHERE workspace_id = ? AND session_id = ?
                    """,
                    (str(workspace), session_id),
                ).fetchone(),
            )
            if existing_row is None:
                raise UnknownSessionError(f"unknown session: {session_id}")
            # Same authoritative seal as the batch path: a sealed terminal
            # session rejects every late non-lifecycle event, no matter which
            # append entry point delivers it.
            _assert_terminal_session_events_allowed(
                session_id=session_id,
                status=cast(str, existing_row["status"]),
                events=((event_type, source, payload, dedupe_key),),
            )
            # Claim the dedupe slot before touching the session row. Losing the
            # race means this is a duplicate delivery, and duplicate deliveries
            # must not perturb session ordering or sequence counters.
            if dedupe_key is not None:
                delivered_at = self._next_auxiliary_timestamp(connection=connection)
                inserted_delivery = connection.execute(
                    """
                    INSERT OR IGNORE INTO session_event_deliveries (
                        workspace_id, session_id, dedupe_key, delivered_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (str(workspace), session_id, dedupe_key, delivered_at),
                )
                if inserted_delivery.rowcount == 0:
                    connection.commit()
                    return None
            updated_at = self._next_timestamp(connection=connection)
            sequence_row = cast(
                sqlite3.Row | None,
                connection.execute(
                    """
                    UPDATE sessions
                    SET updated_at = ?, last_event_sequence = last_event_sequence + 1
                    WHERE workspace_id = ? AND session_id = ?
                    RETURNING last_event_sequence
                    """,
                    (updated_at, str(workspace), session_id),
                ).fetchone(),
            )
            if sequence_row is None:
                # Session disappeared mid-transaction; should not happen under
                # BEGIN IMMEDIATE but kept defensively.
                raise UnknownSessionError(f"unknown session: {session_id}")
            sequence = cast(int, sequence_row["last_event_sequence"])
            event = EventEnvelope(
                session_id=session_id,
                sequence=sequence,
                event_type=event_type,
                source=source,
                payload=payload,
            )
            _ = connection.execute(
                """
                INSERT INTO session_events (
                    workspace_id, session_id, sequence, event_type, source, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(workspace),
                    event.session_id,
                    event.sequence,
                    event.event_type,
                    event.source,
                    json.dumps(event.payload, sort_keys=True),
                ),
            )
            connection.commit()
            return event

    def append_session_events(
        self,
        *,
        workspace: Path,
        session_id: str,
        events: tuple[tuple[str, EventSource, dict[str, object], str | None], ...],
        interrupted_checkpoint: dict[str, object] | None = None,
    ) -> tuple[EventEnvelope, ...]:
        """Append a batch of session events in one transaction — append-only.

        Mirrors ``append_session_event`` per event (dedupe slot via
        ``session_event_deliveries``, DB-assigned sequence via the
        ``last_event_sequence`` bump) but holds a single ``BEGIN IMMEDIATE``
        transaction so the whole batch is atomic. Terminal sessions reject
        non-lifecycle events via ``SessionSealedError``; an optional interrupted
        checkpoint is upserted in the same transaction.
        """
        with self._write_connect(workspace) as connection:
            status_row = cast(
                sqlite3.Row | None,
                connection.execute(
                    "SELECT status FROM sessions WHERE workspace_id = ? AND session_id = ?",
                    (str(workspace), session_id),
                ).fetchone(),
            )
            if status_row is None:
                raise UnknownSessionError(f"unknown session: {session_id}")
            status = cast(str, status_row["status"])
            _assert_terminal_session_events_allowed(
                session_id=session_id,
                status=status,
                events=events,
            )
            assigned: list[EventEnvelope] = []
            for event_type, source, payload, dedupe_key in events:
                payload = self._enriched_background_task_event_payload(
                    connection=connection,
                    workspace=workspace,
                    event_type=event_type,
                    payload=payload,
                )
                if dedupe_key is not None:
                    delivered_at = self._next_auxiliary_timestamp(connection=connection)
                    inserted_delivery = connection.execute(
                        """
                        INSERT OR IGNORE INTO session_event_deliveries (
                            workspace_id, session_id, dedupe_key, delivered_at
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (str(workspace), session_id, dedupe_key, delivered_at),
                    )
                    if inserted_delivery.rowcount == 0:
                        continue
                updated_at = self._next_timestamp(connection=connection)
                sequence_row = cast(
                    sqlite3.Row | None,
                    connection.execute(
                        """
                        UPDATE sessions
                        SET updated_at = ?, last_event_sequence = last_event_sequence + 1
                        WHERE workspace_id = ? AND session_id = ?
                        RETURNING last_event_sequence
                        """,
                        (updated_at, str(workspace), session_id),
                    ).fetchone(),
                )
                if sequence_row is None:
                    raise UnknownSessionError(f"unknown session: {session_id}")
                sequence = cast(int, sequence_row["last_event_sequence"])
                event = EventEnvelope(
                    session_id=session_id,
                    sequence=sequence,
                    event_type=event_type,
                    source=source,
                    payload=payload,
                )
                _ = connection.execute(
                    """
                    INSERT INTO session_events (
                        workspace_id, session_id, sequence, event_type, source, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(workspace),
                        event.session_id,
                        event.sequence,
                        event.event_type,
                        event.source,
                        json.dumps(event.payload, sort_keys=True),
                    ),
                )
                assigned.append(event)
            if interrupted_checkpoint is not None:
                checkpoint_updated_at = self._next_timestamp(connection=connection)
                _ = connection.execute(
                    """
                    UPDATE sessions
                    SET status = 'interrupted', resume_checkpoint_json = ?, updated_at = ?
                    WHERE workspace_id = ? AND session_id = ?
                    """,
                    (
                        json.dumps(interrupted_checkpoint, sort_keys=True),
                        checkpoint_updated_at,
                        str(workspace),
                        session_id,
                    ),
                )
            connection.commit()
            return tuple(assigned)

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
    ) -> None:
        """Persist a lightweight ``interrupted`` resume checkpoint to the sessions row.

        This is a cheap checkpoint of an in-flight run for resume-after-interrupt.
        Unlike ``save_run`` it does NOT write the ``session_events`` table, does
        NOT replace ``session_todos``, and does NOT write ``output`` /
        ``pending_approval_json`` / ``pending_question_json`` (``output`` is only
        preserved, never overwritten with NULL on the update path). It writes
        exactly one ``sessions`` row — creating it on first call when
        ``create_if_missing`` is set (mandatory: ``append_session_events`` raises
        ``UnknownSessionError`` when the row is absent, so the row must exist
        before the first event append).

        ``parent_session_id`` is persisted on both the insert and update paths so
        a child session's first (un-sealed) row already carries its parent — the
        child must reference its parent even when the run ends before a terminal
        seal (``_write_session_snapshot`` is the only other writer of
        ``parent_session_id``, and it only runs at seal time).

        ``tool_results`` must be the serialized ``ToolResult`` form produced by
        ``_tool_results_from_events`` and accepted by
        ``tool_results_from_checkpoint`` in ``resume.py``: a tuple of dicts, each
        carrying the identity keys ``tool_name`` (str), ``status`` (``"ok"`` |
        ``"error"``), ``data`` (dict), ``content`` (str | None), ``error``
        (str | None), plus — only when errored — the optional ``error_kind``,
        ``error_summary``, ``error_details`` (dict) and ``retry_guidance`` (str).
        Callers hold the durable events at this boundary and may derive these
        via ``_tool_results_from_events``.
        """
        checkpoint = self._interrupted_resume_checkpoint(
            prompt=prompt,
            session_metadata=session_metadata,
            tool_results=tool_results,
            last_event_sequence=last_event_sequence,
            output=output,
        )
        persisted_metadata = session_metadata_for_persistence(session_metadata)
        checkpoint_json = json.dumps(checkpoint, sort_keys=True)
        metadata_json = json.dumps(persisted_metadata, sort_keys=True)
        with self._write_connect(workspace) as connection:
            existing = cast(
                sqlite3.Row | None,
                connection.execute(
                    "SELECT 1 FROM sessions WHERE workspace_id = ? AND session_id = ?",
                    (str(workspace), session_id),
                ).fetchone(),
            )
            if existing is None:
                if not create_if_missing:
                    raise UnknownSessionError(f"unknown session: {session_id}")
                created_at = self._read_created_at(
                    connection=connection,
                    workspace=workspace,
                    session_id=session_id,
                )
                updated_at = self._next_timestamp(connection=connection)
                _ = connection.execute(
                    """
                    INSERT INTO sessions (
                        session_id, parent_session_id, workspace_id, status, turn, prompt, output,
                        metadata_json, pending_approval_json, pending_question_json,
                        resume_checkpoint_json, created_at, updated_at,
                        last_event_sequence, created_at_unix_ms
                    ) VALUES (?, ?, ?, 'interrupted', ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        parent_session_id,
                        str(workspace),
                        turn,
                        prompt,
                        output,
                        metadata_json,
                        checkpoint_json,
                        created_at,
                        updated_at,
                        last_event_sequence,
                        int(time() * 1000),
                    ),
                )
            else:
                _ = connection.execute(
                    """
                    UPDATE sessions
                    SET status = 'interrupted',
                        resume_checkpoint_json = ?,
                        metadata_json = ?,
                        prompt = ?,
                        output = COALESCE(?, output),
                        parent_session_id = COALESCE(?, parent_session_id),
                        updated_at = ?,
                        last_event_sequence = MAX(last_event_sequence, ?)
                    WHERE workspace_id = ? AND session_id = ?
                    """,
                    (
                        checkpoint_json,
                        metadata_json,
                        prompt,
                        output,
                        parent_session_id,
                        self._next_timestamp(connection=connection),
                        last_event_sequence,
                        str(workspace),
                        session_id,
                    ),
                )
            connection.commit()

    def truncate_session_events_after(self, *, workspace: Path, session_id: str, sequence: int) -> None:
        """Delete orphaned-tail ``session_events`` rows past a sequence for resume.

        Scoped to a single (workspace, session) pair in one transaction. Rows with
        ``sequence > sequence`` are removed so a resumed run can re-append a clean
        tail after the checkpoint without leaving stale trailing events. Rows at
        or below ``sequence`` are untouched. The session's ``last_event_sequence``
        watermark is reset to the surviving max so subsequent appends continue
        contiguously instead of skipping the freed sequence range.
        """
        with self._write_connect(workspace) as connection:
            _ = connection.execute(
                "DELETE FROM session_events WHERE workspace_id = ? AND session_id = ? AND sequence > ?",
                (str(workspace), session_id, sequence),
            )
            _ = connection.execute(
                """
                UPDATE sessions
                SET last_event_sequence = (
                    SELECT COALESCE(MAX(sequence), 0)
                    FROM session_events
                    WHERE workspace_id = ? AND session_id = ?
                )
                WHERE workspace_id = ? AND session_id = ?
                """,
                (str(workspace), session_id, str(workspace), session_id),
            )
            connection.commit()

    def _sync_background_task_durable_state(
        self,
        *,
        connection: sqlite3.Connection,
        workspace: Path,
        request: RuntimeRequest,
        response: RuntimeResponse,
        approval_request_id: str | None = None,
        question_request_id: str | None = None,
    ) -> None:
        background_task_id = response.session.metadata.get("background_task_id")
        if not isinstance(background_task_id, str) or (response.session.metadata.get("background_run") is not True):
            return
        routing = request.subagent_routing
        result_available = response.session.status in {"waiting", "completed", "failed"}
        inferred_approval_request_id = approval_request_id
        inferred_question_request_id = question_request_id
        if inferred_approval_request_id is None or inferred_question_request_id is None:
            for event in reversed(response.events):
                request_id = event.payload.get("request_id")
                if not isinstance(request_id, str):
                    continue
                if event.event_type == RUNTIME_APPROVAL_REQUESTED and inferred_approval_request_id is None:
                    inferred_approval_request_id = request_id
                if event.event_type == RUNTIME_QUESTION_REQUESTED and inferred_question_request_id is None:
                    inferred_question_request_id = request_id
        updated_at = self._next_background_task_timestamp(connection=connection)
        _ = connection.execute(
            """
            UPDATE background_tasks
            SET requested_child_session_id = COALESCE(requested_child_session_id, ?),
                routing_mode = COALESCE(routing_mode, ?),
                routing_subagent_type = COALESCE(routing_subagent_type, ?),
                routing_description = COALESCE(routing_description, ?),
                routing_command = COALESCE(routing_command, ?),
                approval_request_id = COALESCE(?, approval_request_id),
                question_request_id = COALESCE(?, question_request_id),
                result_available = ?,
                session_id = COALESCE(session_id, ?),
                updated_at = ?
            WHERE workspace_id = ? AND task_id = ?
            """,
            (
                request.session_id,
                routing.mode if routing is not None else None,
                routing.subagent_type if routing is not None else None,
                routing.description if routing is not None else None,
                routing.command if routing is not None else None,
                inferred_approval_request_id,
                inferred_question_request_id,
                1 if result_available else 0,
                response.session.session.id,
                updated_at,
                str(workspace),
                background_task_id,
            ),
        )

    def _stop_delegated_reminder_state(
        self,
        *,
        existing_payload: str | None,
        stop_condition: DelegatedReminderStopCondition,
        stopped_at_unix_ms: int,
    ) -> str | None:
        reminder_state = self._delegated_reminder_state_from_payload(existing_payload)
        if reminder_state is None:
            return None
        if reminder_state.stop_condition is not None and stop_condition == "already_sent_for_idle_episode":
            return existing_payload
        stopped_state = DelegatedReminderState(
            task_id=reminder_state.task_id,
            parent_session_id=reminder_state.parent_session_id,
            child_session_id=reminder_state.child_session_id,
            idle_episode_id=reminder_state.idle_episode_id,
            idle_detected_at_unix_ms=reminder_state.idle_detected_at_unix_ms,
            reminder_sent_at_unix_ms=reminder_state.reminder_sent_at_unix_ms,
            stopped_at_unix_ms=stopped_at_unix_ms,
            stop_condition=stop_condition,
        )
        return json.dumps(
            self._delegated_reminder_state_payload(stopped_state),
            sort_keys=True,
        )

    def _enriched_background_task_event_payload(
        self,
        *,
        connection: sqlite3.Connection,
        workspace: Path,
        event_type: str,
        payload: dict[str, object],
    ) -> dict[str, object]:
        if event_type not in DELEGATED_BACKGROUND_TASK_EVENT_TYPES:
            return payload
        task_id = payload.get("task_id")
        if not isinstance(task_id, str):
            return payload
        try:
            row = self._background_task_runtime_row(
                connection=connection,
                workspace=workspace,
                task_id=task_id,
            )
        except ValueError:
            return payload
        return {**payload, **self._background_task_durable_payload(row)}

    def _read_pending_approval_json(self, *, connection: sqlite3.Connection, workspace: Path, session_id: str) -> str | None:
        row = cast(
            sqlite3.Row | None,
            connection.execute(
                ("SELECT pending_approval_json FROM sessions WHERE workspace_id = ? AND session_id = ?"),
                (str(workspace), session_id),
            ).fetchone(),
        )
        if row is None:
            return None
        return cast(str | None, row["pending_approval_json"])

    @staticmethod
    def _approval_wait_resume_checkpoint(
        *,
        request: RuntimeRequest,
        response: RuntimeResponse,
        pending_approval: PendingApproval,
        last_event_sequence: int | None = None,
    ) -> dict[str, object]:
        return {
            **SqliteSessionStore._resume_checkpoint_base(
                request=request,
                response=response,
                kind="approval_wait",
                last_event_sequence=last_event_sequence,
            ),
            "pending_approval_request_id": pending_approval.request_id,
            "pending_approval_tool_name": pending_approval.tool_name,
            "pending_approval_arguments": pending_approval.arguments,
            "pending_approval_request_event_sequence": pending_approval.request_event_sequence,
            "pending_approval_owner_session_id": pending_approval.owner_session_id,
            "pending_approval_owner_parent_session_id": pending_approval.owner_parent_session_id,
            "pending_approval_delegated_task_id": pending_approval.delegated_task_id,
        }

    @staticmethod
    def _question_wait_resume_checkpoint(
        *,
        request: RuntimeRequest,
        response: RuntimeResponse,
        pending_question: PendingQuestion,
        last_event_sequence: int | None = None,
    ) -> dict[str, object]:
        return {
            **SqliteSessionStore._resume_checkpoint_base(
                request=request,
                response=response,
                kind="question_wait",
                last_event_sequence=last_event_sequence,
            ),
            "pending_question_request_id": pending_question.request_id,
            "pending_question_tool_name": pending_question.tool_name,
            "pending_question_prompts": [
                {
                    "header": prompt.header,
                    "question": prompt.question,
                    "multiple": prompt.multiple,
                    "options": [
                        {
                            "label": option.label,
                            "description": option.description,
                        }
                        for option in prompt.options
                    ],
                }
                for prompt in pending_question.prompts
            ],
        }

    @staticmethod
    def _provider_failure_retryable_resume_checkpoint(
        *, request: RuntimeRequest, response: RuntimeResponse, failure_event: EventEnvelope, last_event_sequence: int | None = None
    ) -> dict[str, object]:
        payload = failure_event.payload
        last_tool: dict[str, object] = next(
            (
                event.payload
                for event in reversed(response.events)
                if event.event_type == "runtime.tool_completed" and event.payload.get("status") != "error"
            ),
            cast(dict[str, object], {}),
        )
        return {
            **SqliteSessionStore._resume_checkpoint_base(
                request=request,
                response=response,
                kind="provider_failure_retryable",
                last_event_sequence=last_event_sequence,
            ),
            "provider_error_kind": payload.get("provider_error_kind"),
            "provider": payload.get("provider"),
            "model": payload.get("model"),
            "fallback_exhausted": payload.get("fallback_exhausted"),
            "provider_error_details": payload.get("provider_error_details"),
            "failure_event_sequence": failure_event.sequence,
            "last_successful_tool": last_tool.get("tool"),
            "last_successful_tool_call_id": last_tool.get("tool_call_id"),
        }

    @staticmethod
    def _terminal_resume_checkpoint(
        *,
        request: RuntimeRequest,
        response: RuntimeResponse,
        last_event_sequence: int | None = None,
    ) -> dict[str, object]:
        return SqliteSessionStore._resume_checkpoint_base(
            request=request,
            response=response,
            kind="terminal",
            last_event_sequence=last_event_sequence,
        )

    @staticmethod
    def _run_resume_checkpoint(
        *,
        request: RuntimeRequest,
        response: RuntimeResponse,
        last_event_sequence: int | None = None,
    ) -> dict[str, object]:
        if response.session.status != "failed":
            return SqliteSessionStore._terminal_resume_checkpoint(
                request=request,
                response=response,
                last_event_sequence=last_event_sequence,
            )
        failure_event = next(
            (event for event in reversed(response.events) if event.event_type == "runtime.failed"),
            None,
        )
        if failure_event is None:
            return SqliteSessionStore._terminal_resume_checkpoint(
                request=request,
                response=response,
                last_event_sequence=last_event_sequence,
            )
        if failure_event.payload.get("provider_error_kind") != "transient_failure":
            return SqliteSessionStore._terminal_resume_checkpoint(
                request=request,
                response=response,
                last_event_sequence=last_event_sequence,
            )
        if not any(event.event_type == "runtime.tool_completed" and event.payload.get("status") != "error" for event in response.events):
            return SqliteSessionStore._terminal_resume_checkpoint(
                request=request,
                response=response,
                last_event_sequence=last_event_sequence,
            )
        return SqliteSessionStore._provider_failure_retryable_resume_checkpoint(
            request=request,
            response=response,
            failure_event=failure_event,
            last_event_sequence=last_event_sequence,
        )

    @classmethod
    def _interrupted_resume_checkpoint(
        cls,
        *,
        prompt: str,
        session_metadata: dict[str, object],
        tool_results: tuple[dict[str, object], ...],
        last_event_sequence: int,
        output: str | None,
    ) -> dict[str, object]:
        snapshot_hash, snapshot_version, binding_snapshot = cls._checkpoint_skill_snapshot(session_metadata)
        return {
            "version": 1,
            "kind": "interrupted",
            "prompt": prompt,
            "session_status": "interrupted",
            "session_metadata": session_metadata_for_persistence(session_metadata),
            "skill_snapshot_hash": snapshot_hash,
            "skill_snapshot_version": snapshot_version,
            "skill_binding_snapshot": binding_snapshot,
            "tool_results": list(tool_results),
            "last_event_sequence": last_event_sequence,
            "output": output,
        }

    @staticmethod
    def _tool_results_from_events(events: tuple[EventEnvelope, ...]) -> list[dict[str, object]]:
        tool_results: list[dict[str, object]] = []
        for event in events:
            if event.event_type != "runtime.tool_completed":
                continue
            payload = event.payload
            raw_status = payload.get("status")
            is_err = raw_status == "error"
            if raw_status not in {"ok", "error"}:
                is_err = payload.get("error") is not None
            raw_content = payload.get("content")
            raw_error = payload.get("error")
            tool_result: dict[str, object] = {
                "tool_name": str(payload.get("tool", "unknown")),
                "content": str(raw_content) if raw_content is not None and not is_err else None,
                "status": "error" if is_err else "ok",
                "data": payload,
                "error": str(raw_error) if raw_error is not None and is_err else None,
            }
            if is_err:
                if payload.get("error_kind") is not None:
                    tool_result["error_kind"] = str(payload.get("error_kind"))
                if payload.get("error_summary") is not None:
                    tool_result["error_summary"] = str(payload.get("error_summary"))
                if isinstance(payload.get("error_details"), dict):
                    tool_result["error_details"] = cast(dict[str, object], payload.get("error_details"))
                if payload.get("retry_guidance") is not None:
                    tool_result["retry_guidance"] = str(payload.get("retry_guidance"))
            tool_results.append(tool_result)
        return tool_results

    def tool_effectiveness_report(self, *, workspace: Path) -> ToolEffectivenessReport:
        """Project aggregate tool quality metrics from append-only session truth."""

        with self._connect(workspace) as connection:
            session_rows = cast(
                list[sqlite3.Row],
                connection.execute(
                    """
                    SELECT session_id, metadata_json
                    FROM sessions
                    WHERE workspace_id = ?
                    ORDER BY session_id ASC
                    """,
                    (str(workspace),),
                ).fetchall(),
            )
            event_rows = cast(
                list[sqlite3.Row],
                connection.execute(
                    """
                    SELECT session_id, sequence, event_type, source, payload_json
                    FROM session_events
                    WHERE workspace_id = ? AND event_type IN (
                        'runtime.tool_completed',
                        'runtime.request_received',
                        'runtime.context_compacted',
                        'runtime.approval_requested'
                    )
                    ORDER BY session_id ASC, sequence ASC
                    """,
                    (str(workspace),),
                ).fetchall(),
            )

        session_ids = tuple(cast(str, row["session_id"]) for row in session_rows)
        session_metadata = {
            cast(str, row["session_id"]): cast(dict[str, object], json.loads(cast(str, row["metadata_json"]))) for row in session_rows
        }
        events = tuple(
            ToolEffectivenessEvent(
                session_id=cast(str, row["session_id"]),
                event=EventEnvelope(
                    session_id=cast(str, row["session_id"]),
                    sequence=cast(int, row["sequence"]),
                    event_type=cast(str, row["event_type"]),
                    source=self._parse_event_source(cast(str, row["source"])),
                    payload=cast(dict[str, object], json.loads(cast(str, row["payload_json"]))),
                ),
            )
            for row in event_rows
        )
        return project_tool_effectiveness(
            workspace_id=str(workspace),
            session_ids=session_ids,
            session_metadata=session_metadata,
            events=events,
        )

    def has_session(self, *, workspace: Path, session_id: str) -> bool:
        with self._connect(workspace) as connection:
            row = cast(
                sqlite3.Row | None,
                connection.execute(
                    """
                    SELECT 1
                    FROM sessions
                    WHERE workspace_id = ? AND session_id = ?
                    """,
                    (str(workspace), session_id),
                ).fetchone(),
            )
        return row is not None

    def read_recent_tool_results(self, *, workspace: Path, session_id: str) -> str | None:
        with self._connect(workspace) as connection:
            row = cast(
                sqlite3.Row | None,
                connection.execute(
                    """
                    SELECT recent_tool_results_json
                    FROM sessions
                    WHERE workspace_id = ? AND session_id = ?
                    """,
                    (str(workspace), session_id),
                ).fetchone(),
            )
        if row is None:
            return None
        return cast(str | None, row["recent_tool_results_json"])

    def load_session(self, *, workspace: Path, session_id: str) -> RuntimeResponse:
        """Return ALL persisted events for a session, unfiltered except for revert markers.

        Boundary: storage returns every event — no compaction, no truncation, no
        context-window projection. The caller (or context_window.py) decides what
        subset to present to the model.
        """
        return self._load_session_response(
            workspace=workspace,
            session_id=session_id,
            filter_reverted=True,
        )

    def load_session_status(self, *, workspace: Path, session_id: str) -> SessionStatus:
        """Return the persisted row status for a session.

        Lightweight read used by the runtime's terminal-seal guard
        (``VoidCodeRuntime._sealed_session_status``): the guard must inspect the
        durable status without materializing the full event log.
        """
        with self._connect(workspace) as connection:
            row = cast(
                sqlite3.Row | None,
                connection.execute(
                    "SELECT status FROM sessions WHERE workspace_id = ? AND session_id = ?",
                    (str(workspace), session_id),
                ).fetchone(),
            )
        if row is None:
            raise UnknownSessionError(f"unknown session: {session_id}")
        return self._parse_session_status(cast(str, row["status"]))

    def update_session_metadata(self, *, workspace: Path, session_id: str, metadata: dict[str, object]) -> None:
        """Persist bounded runtime metadata without fabricating a new response."""
        persisted = session_metadata_for_persistence(metadata)
        with self._write_connect(workspace) as connection:
            updated = connection.execute(
                "UPDATE sessions SET metadata_json = ?, updated_at = ? WHERE workspace_id = ? AND session_id = ?",
                (json.dumps(persisted, sort_keys=True), self._next_timestamp(connection=connection), str(workspace), session_id),
            ).rowcount
            if updated != 1:
                raise UnknownSessionError(f"unknown session: {session_id}")
            connection.commit()

    def _load_session_response(
        self,
        *,
        workspace: Path,
        session_id: str,
        filter_reverted: bool,
    ) -> RuntimeResponse:
        """Load a session with ALL events from durable storage.

        Boundary: returns every stored event row unfiltered — no compaction,
        no truncation, no context-driven dropping. The only filter applied is
        the revert marker (when ``filter_reverted=True``), which is a user
        intent, not a storage-level compaction.
        """
        with self._connect(workspace) as connection:
            session_row = cast(
                sqlite3.Row | None,
                connection.execute(
                    """
                SELECT session_id, parent_session_id, status, turn, output, metadata_json
                FROM sessions
                WHERE workspace_id = ? AND session_id = ?
                """,
                    (str(workspace), session_id),
                ).fetchone(),
            )
            if session_row is None:
                raise UnknownSessionError(f"unknown session: {session_id}")
            event_rows = cast(
                list[sqlite3.Row],
                connection.execute(
                    """
                SELECT sequence, event_type, source, payload_json
                FROM session_events
                WHERE workspace_id = ? AND session_id = ?
                ORDER BY sequence ASC
                """,
                    (str(workspace), session_id),
                ).fetchall(),
            )
            stored_todo_state = self._todo_state_from_rows(
                connection=connection,
                workspace=workspace,
                session_id=session_id,
            )
        metadata = self._metadata_with_todo_state(
            normalize_persisted_session_metadata(cast(dict[str, object], json.loads(cast(str, session_row["metadata_json"])))),
            stored_todo_state,
        )
        session = SessionState(
            session=SessionRef(
                id=cast(str, session_row["session_id"]),
                parent_id=cast(str | None, session_row["parent_session_id"]),
            ),
            status=self._parse_session_status(cast(str, session_row["status"])),
            turn=cast(int, session_row["turn"]),
            metadata=metadata,
        )
        events = tuple(
            EventEnvelope(
                session_id=session_id,
                sequence=cast(int, row["sequence"]),
                event_type=cast(str, row["event_type"]),
                source=self._parse_event_source(cast(str, row["source"])),
                payload=cast(dict[str, object], json.loads(cast(str, row["payload_json"]))),
            )
            for row in event_rows
        )
        marker = self._revert_marker_from_metadata(session.metadata)
        output = cast(str | None, session_row["output"])
        if filter_reverted and marker is not None and marker.active:
            events = tuple(event for event in events if event.sequence < marker.sequence)
            session = SessionState(
                session=session.session,
                status=session.status,
                turn=session.turn,
                metadata=self._active_revert_metadata(session.metadata, events=events),
            )
            output = None
        return RuntimeResponse(session=session, events=events, output=output)

    def load_session_result(self, *, workspace: Path, session_id: str) -> RuntimeSessionResult:
        response = self._load_session_response(
            workspace=workspace,
            session_id=session_id,
            filter_reverted=False,
        )
        with self._connect(workspace) as connection:
            row = cast(
                sqlite3.Row | None,
                connection.execute(
                    """
                    SELECT prompt
                    FROM sessions
                    WHERE workspace_id = ? AND session_id = ?
                    """,
                    (str(workspace), session_id),
                ).fetchone(),
            )
        if row is None:
            raise UnknownSessionError(f"unknown session: {session_id}")
        prompt = cast(str, row["prompt"])
        summary, error = self._result_summary(response=response, prompt=prompt)
        return RuntimeSessionResult(
            session=response.session,
            prompt=prompt,
            status=response.session.status,
            summary=summary,
            output=response.output,
            error=error,
            transcript=response.events,
            last_event_sequence=response.events[-1].sequence if response.events else 0,
            revert_marker=self._revert_marker_from_metadata(response.session.metadata),
        )

    @staticmethod
    def _revert_marker_from_metadata(
        metadata: dict[str, object],
    ) -> RuntimeSessionRevertMarker | None:
        raw_marker = metadata.get("conversation_revert")
        if not isinstance(raw_marker, dict):
            return None
        marker_payload = cast(dict[object, object], raw_marker)
        raw_sequence = marker_payload.get("sequence")
        if not isinstance(raw_sequence, int) or isinstance(raw_sequence, bool) or raw_sequence < 1:
            return None
        raw_active = marker_payload.get("active", True)
        active = raw_active if isinstance(raw_active, bool) else True
        return RuntimeSessionRevertMarker(sequence=raw_sequence, active=active)

    @staticmethod
    def _metadata_with_revert_marker(
        metadata: dict[str, object],
        marker: RuntimeSessionRevertMarker | None,
    ) -> dict[str, object]:
        next_metadata = dict(metadata)
        if marker is None:
            next_metadata.pop("conversation_revert", None)
            return next_metadata
        next_metadata["conversation_revert"] = {
            "sequence": marker.sequence,
            "active": marker.active,
        }
        return next_metadata

    def _active_revert_metadata(
        self,
        metadata: dict[str, object],
        *,
        events: tuple[EventEnvelope, ...],
    ) -> dict[str, object]:
        next_metadata = dict(metadata)
        raw_runtime_state = next_metadata.get("runtime_state")
        if not isinstance(raw_runtime_state, dict):
            return next_metadata
        runtime_state = dict(cast(dict[str, object], raw_runtime_state))
        runtime_state.pop("context_projection", None)
        todo_state = self._todo_state_from_events(events)
        if todo_state is None:
            runtime_state.pop("todos", None)
        else:
            runtime_state["todos"] = todo_state
        next_metadata["runtime_state"] = runtime_state
        return next_metadata

    def _session_metadata_and_events(
        self,
        *,
        connection: sqlite3.Connection,
        workspace: Path,
        session_id: str,
    ) -> tuple[dict[str, object], tuple[EventEnvelope, ...]]:
        session_row = cast(
            sqlite3.Row | None,
            connection.execute(
                """
                SELECT metadata_json
                FROM sessions
                WHERE workspace_id = ? AND session_id = ?
                """,
                (str(workspace), session_id),
            ).fetchone(),
        )
        if session_row is None:
            raise UnknownSessionError(f"unknown session: {session_id}")
        event_rows = cast(
            list[sqlite3.Row],
            connection.execute(
                """
                SELECT sequence, event_type, source, payload_json
                FROM session_events
                WHERE workspace_id = ? AND session_id = ?
                ORDER BY sequence ASC
                """,
                (str(workspace), session_id),
            ).fetchall(),
        )
        events = tuple(
            EventEnvelope(
                session_id=session_id,
                sequence=cast(int, row["sequence"]),
                event_type=cast(str, row["event_type"]),
                source=self._parse_event_source(cast(str, row["source"])),
                payload=cast(dict[str, object], json.loads(cast(str, row["payload_json"]))),
            )
            for row in event_rows
        )
        return (
            normalize_persisted_session_metadata(cast(dict[str, object], json.loads(cast(str, session_row["metadata_json"])))),
            events,
        )

    def _write_revert_marker(
        self,
        *,
        connection: sqlite3.Connection,
        workspace: Path,
        session_id: str,
        marker: RuntimeSessionRevertMarker | None,
    ) -> None:
        metadata, _events = self._session_metadata_and_events(
            connection=connection,
            workspace=workspace,
            session_id=session_id,
        )
        updated_at = self._next_timestamp(connection=connection)
        _ = connection.execute(
            """
            UPDATE sessions
            SET metadata_json = ?, updated_at = ?
            WHERE workspace_id = ? AND session_id = ?
            """,
            (
                json.dumps(self._metadata_with_revert_marker(metadata, marker), sort_keys=True),
                updated_at,
                str(workspace),
                session_id,
            ),
        )

    def revert_session(self, *, workspace: Path, session_id: str, sequence: int) -> RuntimeSessionRevertMarker:
        if sequence < 1:
            raise ValueError("revert sequence must be a positive integer")
        with self._write_connect(workspace) as connection:
            _metadata, events = self._session_metadata_and_events(
                connection=connection,
                workspace=workspace,
                session_id=session_id,
            )
            if not any(event.sequence == sequence for event in events):
                raise ValueError(f"session {session_id} has no event sequence {sequence}")
            marker = RuntimeSessionRevertMarker(sequence=sequence, active=True)
            self._write_revert_marker(
                connection=connection,
                workspace=workspace,
                session_id=session_id,
                marker=marker,
            )
            connection.commit()
            return marker

    def undo_session(self, *, workspace: Path, session_id: str) -> RuntimeSessionRevertMarker:
        with self._write_connect(workspace) as connection:
            metadata, events = self._session_metadata_and_events(
                connection=connection,
                workspace=workspace,
                session_id=session_id,
            )
            active_marker = self._revert_marker_from_metadata(metadata)
            active_cutoff = active_marker.sequence if active_marker is not None else None
            candidate = next(
                (
                    event
                    for event in reversed(events)
                    if event.event_type == "runtime.request_received" and (active_cutoff is None or event.sequence < active_cutoff)
                ),
                None,
            )
            if candidate is None:
                raise ValueError(f"session {session_id} has no user turn to undo")
            marker = RuntimeSessionRevertMarker(sequence=candidate.sequence, active=True)
            self._write_revert_marker(
                connection=connection,
                workspace=workspace,
                session_id=session_id,
                marker=marker,
            )
            connection.commit()
            return marker

    def unrevert_session(self, *, workspace: Path, session_id: str) -> RuntimeSessionRevertMarker | None:
        with self._write_connect(workspace) as connection:
            metadata, _events = self._session_metadata_and_events(
                connection=connection,
                workspace=workspace,
                session_id=session_id,
            )
            marker = self._revert_marker_from_metadata(metadata)
            if marker is None:
                connection.commit()
                return None
            self._write_revert_marker(
                connection=connection,
                workspace=workspace,
                session_id=session_id,
                marker=None,
            )
            connection.commit()
            return marker

    def list_notifications(self, *, workspace: Path) -> tuple[RuntimeNotification, ...]:
        with self._connect(workspace) as connection:
            rows = cast(
                list[sqlite3.Row],
                connection.execute(
                    """
                    SELECT notifications.notification_id, notifications.session_id,
                           notifications.kind, notifications.status, notifications.summary,
                           notifications.payload_json, notifications.event_sequence,
                           notifications.created_at, notifications.acknowledged_at,
                           sessions.parent_session_id
                    FROM session_notifications AS notifications
                    LEFT JOIN sessions ON sessions.session_id = notifications.session_id
                                      AND sessions.workspace_id = notifications.workspace_id
                    WHERE notifications.workspace_id = ?
                    ORDER BY notifications.created_at DESC, notifications.notification_id DESC
                    """,
                    (str(workspace),),
                ).fetchall(),
            )
        return tuple(self._notification_from_row(row) for row in rows)

    def create_background_task(
        self,
        *,
        workspace: Path,
        task: BackgroundTaskState,
    ) -> None:
        task_id = validate_background_task_id(task.task.id)
        routing = task.routing_identity
        with self._write_connect(workspace) as connection:
            linked_session_id = task.session_id or task.request.session_id
            initial_runtime_state = self._linked_session_background_task_runtime_state(
                connection=connection,
                workspace=workspace,
                session_id=linked_session_id,
            )
            timestamp = self._next_background_task_timestamp(connection=connection)
            wall_clock_ms = self._current_unix_ms()
            _ = connection.execute(
                """
                INSERT INTO background_tasks (
                    task_id, workspace_id, status, prompt, request_session_id,
                    request_parent_session_id, request_metadata_json, requested_child_session_id,
                    routing_mode, routing_subagent_type,
                    routing_description, routing_command, approval_request_id,
                    question_request_id, cancellation_cause, result_available,
                    delegated_reminder_json, allocate_session_id, session_id,
                    error, cancel_requested_at,
                    created_at, updated_at, started_at, finished_at,
                    created_at_unix_ms, started_at_unix_ms, finished_at_unix_ms, keep_alive
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    task_id,
                    str(workspace),
                    task.status,
                    task.request.prompt,
                    task.request.session_id,
                    task.request.parent_session_id,
                    json.dumps(task.request.metadata, sort_keys=True),
                    task.request.session_id,
                    routing.mode if routing is not None else None,
                    routing.subagent_type if routing is not None else None,
                    routing.description if routing is not None else None,
                    routing.command if routing is not None else None,
                    initial_runtime_state["approval_request_id"],
                    initial_runtime_state["question_request_id"],
                    initial_runtime_state["cancellation_cause"],
                    initial_runtime_state["result_available"],
                    None,
                    1 if task.request.allocate_session_id else 0,
                    task.session_id,
                    task.error,
                    task.cancel_requested_at,
                    timestamp,
                    timestamp,
                    task.started_at,
                    task.finished_at,
                    wall_clock_ms,
                    task.started_at_unix_ms,
                    task.finished_at_unix_ms,
                    1 if task.request.metadata.get("keep_alive") is True else 0,
                ),
            )
            connection.commit()

    def load_background_task(self, *, workspace: Path, task_id: str) -> BackgroundTaskState:
        task_id = validate_background_task_id(task_id)
        with self._connect(workspace) as connection:
            row = cast(
                sqlite3.Row | None,
                connection.execute(
                    """
                    SELECT * FROM background_tasks
                    WHERE workspace_id = ? AND task_id = ?
                    """,
                    (str(workspace), task_id),
                ).fetchone(),
            )
        if row is None:
            raise ValueError(f"unknown background task: {task_id}")
        return self._background_task_state_from_row(row)

    def list_background_tasks(self, *, workspace: Path) -> tuple[StoredBackgroundTaskSummary, ...]:
        with self._connect(workspace) as connection:
            rows = cast(
                list[sqlite3.Row],
                connection.execute(
                    """
                    SELECT task_id, status, prompt, session_id, error, created_at, updated_at
                           , created_at_unix_ms, keep_alive, steer_prompt
                    FROM background_tasks
                    WHERE workspace_id = ?
                    ORDER BY updated_at DESC, task_id ASC
                    """,
                    (str(workspace),),
                ).fetchall(),
            )
        return tuple(self._background_task_summary_from_row(row) for row in rows)

    def list_queued_background_tasks(self, *, workspace: Path) -> tuple[StoredBackgroundTaskSummary, ...]:
        with self._connect(workspace) as connection:
            rows = cast(
                list[sqlite3.Row],
                connection.execute(
                    """
                    SELECT task_id, status, prompt, session_id, error, created_at, updated_at
                           , created_at_unix_ms, keep_alive, steer_prompt
                    FROM background_tasks
                    WHERE workspace_id = ? AND status = 'queued'
                    ORDER BY created_at ASC, task_id ASC
                    """,
                    (str(workspace),),
                ).fetchall(),
            )
        return tuple(self._background_task_summary_from_row(row) for row in rows)

    def list_running_background_tasks(self, *, workspace: Path) -> tuple[StoredBackgroundTaskSummary, ...]:
        """Status-indexed running-task summaries for worker-liveness reconciliation.

        Bounded by the task concurrency limit; unlike ``list_background_tasks``
        this never scans terminal history, so hot read paths (task loads,
        observability) can check worker liveness without a full-history pass.
        """
        with self._connect(workspace) as connection:
            rows = cast(
                list[sqlite3.Row],
                connection.execute(
                    """
                    SELECT task_id, status, prompt, session_id, error, created_at, updated_at
                           , created_at_unix_ms, keep_alive, steer_prompt
                    FROM background_tasks
                    WHERE workspace_id = ? AND status = 'running'
                    ORDER BY updated_at ASC, task_id ASC
                    """,
                    (str(workspace),),
                ).fetchall(),
            )
        return tuple(self._background_task_summary_from_row(row) for row in rows)

    def list_background_tasks_by_parent_session(self, *, workspace: Path, parent_session_id: str) -> tuple[StoredBackgroundTaskSummary, ...]:
        with self._connect(workspace) as connection:
            rows = cast(
                list[sqlite3.Row],
                connection.execute(
                    """
                    SELECT task_id, status, prompt, session_id, error, created_at, updated_at
                           , created_at_unix_ms, keep_alive, steer_prompt
                    FROM background_tasks
                    WHERE workspace_id = ? AND request_parent_session_id = ?
                    ORDER BY updated_at DESC, task_id ASC
                    """,
                    (str(workspace), parent_session_id),
                ).fetchall(),
            )
        return tuple(self._background_task_summary_from_row(row) for row in rows)

    def load_background_task_by_child_session(
        self,
        *,
        workspace: Path,
        child_session_id: str,
    ) -> BackgroundTaskState | None:
        with self._connect(workspace) as connection:
            row = cast(
                sqlite3.Row | None,
                connection.execute(
                    """
                    SELECT *
                    FROM background_tasks
                    WHERE workspace_id = ? AND session_id = ?
                    ORDER BY updated_at DESC, task_id DESC
                    LIMIT 1
                    """,
                    (str(workspace), child_session_id),
                ).fetchone(),
            )
        if row is None:
            return None
        return self._background_task_state_from_row(row)

    def mark_background_task_running(
        self,
        *,
        workspace: Path,
        task_id: str,
        session_id: str,
    ) -> BackgroundTaskState:
        task_id = validate_background_task_id(task_id)
        with self._write_connect(workspace) as connection:
            current = self._background_task_runtime_row(
                connection=connection,
                workspace=workspace,
                task_id=task_id,
            )
            current_status = self._parse_background_task_status(cast(str, current["status"]))
            if not is_background_task_transition_allowed(
                current_status=current_status,
                next_status="running",
            ):
                connection.commit()
                return self._background_task_state_from_row(current)
            updated_at = self._next_background_task_timestamp(connection=connection)
            started_at_unix_ms = self._current_unix_ms()
            _ = connection.execute(
                """
                UPDATE background_tasks
                SET status = ?, session_id = ?, started_at = COALESCE(started_at, ?),
                    started_at_unix_ms = COALESCE(started_at_unix_ms, ?), updated_at = ?
                WHERE workspace_id = ? AND task_id = ? AND status = 'queued'
                """,
                (
                    "running",
                    session_id,
                    updated_at,
                    started_at_unix_ms,
                    updated_at,
                    str(workspace),
                    task_id,
                ),
            )
            updated_row = self._background_task_runtime_row(
                connection=connection,
                workspace=workspace,
                task_id=task_id,
            )
            connection.commit()
        return self._background_task_state_from_row(updated_row)

    def mark_background_task_terminal(
        self,
        *,
        workspace: Path,
        task_id: str,
        status: BackgroundTaskStatus,
        error: str | None = None,
    ) -> BackgroundTaskState:
        if status not in ("completed", "failed", "cancelled", "interrupted"):
            raise ValueError("background task terminal status must be completed, failed, cancelled, or interrupted")
        task_id = validate_background_task_id(task_id)
        with self._write_connect(workspace) as connection:
            current = self._background_task_runtime_row(
                connection=connection,
                workspace=workspace,
                task_id=task_id,
            )
            current_status = self._parse_background_task_status(cast(str, current["status"]))
            if not is_background_task_transition_allowed(
                current_status=current_status,
                next_status=status,
            ):
                connection.commit()
                return self._background_task_state_from_row(current)
            cancellation_cause = cast(str | None, current["cancellation_cause"])
            if status == "cancelled" and error is not None:
                cancellation_cause = error
            result_available = 1 if status in ("completed", "failed", "interrupted") else 0
            updated_at = self._next_background_task_timestamp(connection=connection)
            finished_at_unix_ms = self._current_unix_ms()
            delegated_reminder_json = self._stop_delegated_reminder_state(
                existing_payload=cast(str | None, current["delegated_reminder_json"]),
                stop_condition="terminal_status",
                stopped_at_unix_ms=finished_at_unix_ms,
            )
            _ = connection.execute(
                """
                UPDATE background_tasks
                SET status = ?, error = ?, finished_at = ?, updated_at = ?,
                    cancellation_cause = ?, result_available = ?, finished_at_unix_ms = ?,
                    delegated_reminder_json = ?
                WHERE workspace_id = ? AND task_id = ?
                """,
                (
                    status,
                    error,
                    updated_at,
                    updated_at,
                    cancellation_cause,
                    result_available,
                    finished_at_unix_ms,
                    delegated_reminder_json,
                    str(workspace),
                    task_id,
                ),
            )
            updated_row = self._background_task_runtime_row(
                connection=connection,
                workspace=workspace,
                task_id=task_id,
            )
            connection.commit()
        return self._background_task_state_from_row(updated_row)

    def mark_background_task_idle(
        self,
        *,
        workspace: Path,
        task_id: str,
    ) -> BackgroundTaskState:
        task_id = validate_background_task_id(task_id)
        with self._write_connect(workspace) as connection:
            current = self._background_task_runtime_row(
                connection=connection,
                workspace=workspace,
                task_id=task_id,
            )
            current_status = self._parse_background_task_status(cast(str, current["status"]))
            if not is_background_task_transition_allowed(
                current_status=current_status,
                next_status="idle",
            ):
                connection.commit()
                return self._background_task_state_from_row(current)
            updated_at = self._next_background_task_timestamp(connection=connection)
            _ = connection.execute(
                """
                UPDATE background_tasks
                SET status = ?, steer_prompt = NULL, updated_at = ?
                WHERE workspace_id = ? AND task_id = ? AND status = 'running'
                """,
                (
                    "idle",
                    updated_at,
                    str(workspace),
                    task_id,
                ),
            )
            updated_row = self._background_task_runtime_row(
                connection=connection,
                workspace=workspace,
                task_id=task_id,
            )
            connection.commit()
        return self._background_task_state_from_row(updated_row)

    def mark_background_task_steered(
        self,
        *,
        workspace: Path,
        task_id: str,
        steer_prompt: str,
    ) -> BackgroundTaskState:
        task_id = validate_background_task_id(task_id)
        with self._write_connect(workspace) as connection:
            current = self._background_task_runtime_row(
                connection=connection,
                workspace=workspace,
                task_id=task_id,
            )
            current_status = self._parse_background_task_status(cast(str, current["status"]))
            if not is_background_task_transition_allowed(
                current_status=current_status,
                next_status="running",
            ):
                connection.commit()
                return self._background_task_state_from_row(current)
            updated_at = self._next_background_task_timestamp(connection=connection)
            _ = connection.execute(
                """
                UPDATE background_tasks
                SET status = ?, steer_prompt = ?, updated_at = ?
                WHERE workspace_id = ? AND task_id = ? AND status IN ('idle', 'interrupted')
                """,
                (
                    "running",
                    steer_prompt,
                    updated_at,
                    str(workspace),
                    task_id,
                ),
            )
            updated_row = self._background_task_runtime_row(
                connection=connection,
                workspace=workspace,
                task_id=task_id,
            )
            connection.commit()
        return self._background_task_state_from_row(updated_row)

    def request_background_task_cancel(
        self,
        *,
        workspace: Path,
        task_id: str,
    ) -> BackgroundTaskState:
        task_id = validate_background_task_id(task_id)
        with self._write_connect(workspace) as connection:
            current = self._background_task_runtime_row(
                connection=connection,
                workspace=workspace,
                task_id=task_id,
            )
            current_status = self._parse_background_task_status(cast(str, current["status"]))
            if is_background_task_terminal(current_status):
                connection.commit()
                return self._background_task_state_from_row(current)
            if current_status == "queued":
                updated_at = self._next_background_task_timestamp(connection=connection)
                stopped_at_unix_ms = self._current_unix_ms()
                delegated_reminder_json = self._stop_delegated_reminder_state(
                    existing_payload=cast(str | None, current["delegated_reminder_json"]),
                    stop_condition="cancellation",
                    stopped_at_unix_ms=stopped_at_unix_ms,
                )
                cancelled = connection.execute(
                    """
                    UPDATE background_tasks
                    SET status = 'cancelled', error = ?, cancellation_cause = ?,
                        result_available = 0, finished_at = ?, updated_at = ?,
                        delegated_reminder_json = ?
                    WHERE workspace_id = ? AND task_id = ? AND status = 'queued'
                    """,
                    (
                        "cancelled before start",
                        "cancelled before start",
                        updated_at,
                        updated_at,
                        delegated_reminder_json,
                        str(workspace),
                        task_id,
                    ),
                ).rowcount
                if cancelled == 1:
                    updated_row = self._background_task_runtime_row(
                        connection=connection,
                        workspace=workspace,
                        task_id=task_id,
                    )
                    connection.commit()
                    return self._background_task_state_from_row(updated_row)
                current = self._background_task_runtime_row(
                    connection=connection,
                    workspace=workspace,
                    task_id=task_id,
                )
                current_status = self._parse_background_task_status(cast(str, current["status"]))
                if is_background_task_terminal(current_status):
                    connection.commit()
                    return self._background_task_state_from_row(current)
            updated_at = self._next_background_task_timestamp(connection=connection)
            delegated_reminder_json = self._stop_delegated_reminder_state(
                existing_payload=cast(str | None, current["delegated_reminder_json"]),
                stop_condition="cancellation",
                stopped_at_unix_ms=self._current_unix_ms(),
            )
            _ = connection.execute(
                """
                UPDATE background_tasks
                SET cancel_requested_at = ?, updated_at = ?, delegated_reminder_json = ?
                WHERE workspace_id = ? AND task_id = ? AND status = 'running'
                    AND cancel_requested_at IS NULL
                """,
                (updated_at, updated_at, delegated_reminder_json, str(workspace), task_id),
            )
            updated_row = self._background_task_runtime_row(
                connection=connection,
                workspace=workspace,
                task_id=task_id,
            )
            connection.commit()
        return self._background_task_state_from_row(updated_row)

    def record_background_task_idle_reminder_eligible(
        self,
        *,
        workspace: Path,
        task_id: str,
        child_session_id: str,
        idle_episode_id: str,
        idle_detected_at_unix_ms: int,
    ) -> BackgroundTaskState:
        task_id = validate_background_task_id(task_id)
        with self._write_connect(workspace) as connection:
            current = self._background_task_runtime_row(
                connection=connection,
                workspace=workspace,
                task_id=task_id,
            )
            current_status = self._parse_background_task_status(cast(str, current["status"]))
            if is_background_task_terminal(current_status):
                connection.commit()
                return self._background_task_state_from_row(current)
            existing_state = self._delegated_reminder_state_from_payload(cast(str | None, current["delegated_reminder_json"]))
            if existing_state is not None and existing_state.stop_condition in {
                "result_read",
                "explicit_retry",
                "cancellation",
                "terminal_status",
            }:
                connection.commit()
                return self._background_task_state_from_row(current)
            if existing_state is not None and existing_state.idle_episode_id == idle_episode_id:
                connection.commit()
                return self._background_task_state_from_row(current)
            reminder_state = DelegatedReminderState(
                task_id=task_id,
                parent_session_id=cast(str | None, current["request_parent_session_id"]),
                child_session_id=child_session_id,
                idle_episode_id=idle_episode_id,
                idle_detected_at_unix_ms=idle_detected_at_unix_ms,
            )
            updated_at = self._next_background_task_timestamp(connection=connection)
            _ = connection.execute(
                """
                UPDATE background_tasks
                SET delegated_reminder_json = ?, updated_at = ?
                WHERE workspace_id = ? AND task_id = ?
                """,
                (
                    json.dumps(
                        self._delegated_reminder_state_payload(reminder_state),
                        sort_keys=True,
                    ),
                    updated_at,
                    str(workspace),
                    task_id,
                ),
            )
            updated_row = self._background_task_runtime_row(
                connection=connection,
                workspace=workspace,
                task_id=task_id,
            )
            connection.commit()
        return self._background_task_state_from_row(updated_row)

    def mark_background_task_idle_reminder_sent(
        self,
        *,
        workspace: Path,
        task_id: str,
        idle_episode_id: str,
        reminder_sent_at_unix_ms: int,
    ) -> BackgroundTaskState:
        task_id = validate_background_task_id(task_id)
        with self._write_connect(workspace) as connection:
            current = self._background_task_runtime_row(
                connection=connection,
                workspace=workspace,
                task_id=task_id,
            )
            reminder_state = self._delegated_reminder_state_from_payload(cast(str | None, current["delegated_reminder_json"]))
            if reminder_state is None or reminder_state.idle_episode_id != idle_episode_id:
                connection.commit()
                return self._background_task_state_from_row(current)
            if reminder_state.stop_condition not in (None, "already_sent_for_idle_episode"):
                connection.commit()
                return self._background_task_state_from_row(current)
            sent_state = DelegatedReminderState(
                task_id=reminder_state.task_id,
                parent_session_id=reminder_state.parent_session_id,
                child_session_id=reminder_state.child_session_id,
                idle_episode_id=reminder_state.idle_episode_id,
                idle_detected_at_unix_ms=reminder_state.idle_detected_at_unix_ms,
                reminder_sent_at_unix_ms=reminder_sent_at_unix_ms,
                stopped_at_unix_ms=reminder_sent_at_unix_ms,
                stop_condition="already_sent_for_idle_episode",
            )
            updated_at = self._next_background_task_timestamp(connection=connection)
            _ = connection.execute(
                """
                UPDATE background_tasks
                SET delegated_reminder_json = ?, updated_at = ?
                WHERE workspace_id = ? AND task_id = ?
                """,
                (
                    json.dumps(self._delegated_reminder_state_payload(sent_state), sort_keys=True),
                    updated_at,
                    str(workspace),
                    task_id,
                ),
            )
            updated_row = self._background_task_runtime_row(
                connection=connection,
                workspace=workspace,
                task_id=task_id,
            )
            connection.commit()
        return self._background_task_state_from_row(updated_row)

    def stop_background_task_idle_reminder(
        self,
        *,
        workspace: Path,
        task_id: str,
        stop_condition: DelegatedReminderStopCondition,
    ) -> BackgroundTaskState:
        task_id = validate_background_task_id(task_id)
        with self._write_connect(workspace) as connection:
            current = self._background_task_runtime_row(
                connection=connection,
                workspace=workspace,
                task_id=task_id,
            )
            delegated_reminder_json = self._stop_delegated_reminder_state(
                existing_payload=cast(str | None, current["delegated_reminder_json"]),
                stop_condition=stop_condition,
                stopped_at_unix_ms=self._current_unix_ms(),
            )
            if delegated_reminder_json == cast(str | None, current["delegated_reminder_json"]):
                connection.commit()
                return self._background_task_state_from_row(current)
            updated_at = self._next_background_task_timestamp(connection=connection)
            _ = connection.execute(
                """
                UPDATE background_tasks
                SET delegated_reminder_json = ?, updated_at = ?
                WHERE workspace_id = ? AND task_id = ?
                """,
                (delegated_reminder_json, updated_at, str(workspace), task_id),
            )
            updated_row = self._background_task_runtime_row(
                connection=connection,
                workspace=workspace,
                task_id=task_id,
            )
            connection.commit()
        return self._background_task_state_from_row(updated_row)

    def fail_incomplete_background_tasks(
        self,
        *,
        workspace: Path,
        message: str,
        include_queued: bool = True,
    ) -> tuple[BackgroundTaskState, ...]:
        incomplete_status_predicate = "background_tasks.status IN ('queued', 'running')" if include_queued else "background_tasks.status = 'running'"
        with self._write_connect(workspace) as connection:
            rows = cast(
                list[sqlite3.Row],
                connection.execute(
                    f"""
                    SELECT background_tasks.task_id, background_tasks.cancel_requested_at,
                           background_tasks.delegated_reminder_json
                    FROM background_tasks
                    LEFT JOIN sessions
                      ON sessions.workspace_id = background_tasks.workspace_id
                     AND sessions.session_id = background_tasks.session_id
                    WHERE background_tasks.workspace_id = ?
                      AND {incomplete_status_predicate}
                      AND NOT (
                          background_tasks.status = 'running'
                          AND background_tasks.session_id IS NOT NULL
                          AND sessions.status = 'waiting'
                          AND (
                              sessions.pending_approval_json IS NOT NULL
                              OR sessions.pending_question_json IS NOT NULL
                          )
                      )
                    ORDER BY background_tasks.updated_at ASC, background_tasks.task_id ASC
                    """,
                    (str(workspace),),
                ).fetchall(),
            )
            if not rows:
                return ()
            reconciled_task_ids: list[str] = []
            for row in rows:
                task_id = cast(str, row["task_id"])
                cancel_requested_at = cast(int | None, row["cancel_requested_at"])
                updated_at = self._next_background_task_timestamp(connection=connection)
                if cancel_requested_at is not None:
                    delegated_reminder_json = self._stop_delegated_reminder_state(
                        existing_payload=cast(str | None, row["delegated_reminder_json"]),
                        stop_condition="cancellation",
                        stopped_at_unix_ms=self._current_unix_ms(),
                    )
                    _ = connection.execute(
                        """
                        UPDATE background_tasks
                        SET status = 'cancelled',
                            error = ?,
                            cancellation_cause = COALESCE(cancellation_cause, ?),
                            finished_at = ?,
                            updated_at = ?,
                            result_available = 0,
                            delegated_reminder_json = ?
                        WHERE workspace_id = ?
                          AND task_id = ?
                          AND status = 'running'
                          AND cancel_requested_at IS NOT NULL
                        """,
                        (
                            "cancelled by parent during delegated execution",
                            "cancelled by parent during delegated execution",
                            updated_at,
                            updated_at,
                            delegated_reminder_json,
                            str(workspace),
                            task_id,
                        ),
                    )
                else:
                    delegated_reminder_json = self._stop_delegated_reminder_state(
                        existing_payload=cast(str | None, row["delegated_reminder_json"]),
                        stop_condition="terminal_status",
                        stopped_at_unix_ms=self._current_unix_ms(),
                    )
                    _ = connection.execute(
                        """
                        UPDATE background_tasks
                        SET status = 'interrupted',
                            error = ?,
                            finished_at = ?,
                            updated_at = ?,
                            result_available = 1,
                            delegated_reminder_json = ?
                        WHERE workspace_id = ?
                          AND task_id = ?
                          AND status IN ('queued', 'running')
                          AND cancel_requested_at IS NULL
                        """,
                        (
                            message,
                            updated_at,
                            updated_at,
                            delegated_reminder_json,
                            str(workspace),
                            task_id,
                        ),
                    )
                reconciled_task_ids.append(task_id)
            connection.commit()
        return tuple(self.load_background_task(workspace=workspace, task_id=task_id) for task_id in reconciled_task_ids)

    def persist_background_task_runtime_state(
        self,
        *,
        workspace: Path,
        task_id: str,
        approval_request_id: str | None = None,
        question_request_id: str | None = None,
        result_available: bool | None = None,
        cancellation_cause: str | None = None,
    ) -> BackgroundTaskState:
        task_id = validate_background_task_id(task_id)
        with self._write_connect(workspace) as connection:
            current = self._background_task_runtime_row(
                connection=connection,
                workspace=workspace,
                task_id=task_id,
            )
            if is_background_task_terminal(self._parse_background_task_status(cast(str, current["status"]))):
                connection.commit()
                return self._background_task_state_from_row(current)
            updated_at = self._next_background_task_timestamp(connection=connection)
            _ = connection.execute(
                """
                UPDATE background_tasks
                SET approval_request_id = ?,
                    question_request_id = ?,
                    cancellation_cause = ?,
                    result_available = ?,
                    updated_at = ?
                WHERE workspace_id = ? AND task_id = ?
                """,
                (
                    approval_request_id if approval_request_id is not None else cast(str | None, current["approval_request_id"]),
                    question_request_id if question_request_id is not None else cast(str | None, current["question_request_id"]),
                    cancellation_cause if cancellation_cause is not None else cast(str | None, current["cancellation_cause"]),
                    (1 if result_available else 0 if result_available is not None else cast(int, current["result_available"])),
                    updated_at,
                    str(workspace),
                    task_id,
                ),
            )
            connection.commit()
        return self.load_background_task(workspace=workspace, task_id=task_id)

    def _background_task_state_from_row(self, row: sqlite3.Row) -> BackgroundTaskState:
        metadata = json.loads(cast(str, row["request_metadata_json"]))
        if not isinstance(metadata, dict):
            raise ValueError("background task metadata must decode to an object")
        metadata = normalize_persisted_session_metadata(cast(dict[str, object], metadata))
        return BackgroundTaskState(
            task=BackgroundTaskRef(id=cast(str, row["task_id"])),
            status=self._parse_background_task_status(cast(str, row["status"])),
            request=BackgroundTaskRequestSnapshot(
                prompt=cast(str, row["prompt"]),
                session_id=cast(str | None, row["request_session_id"]),
                parent_session_id=cast(str | None, row["request_parent_session_id"]),
                metadata=metadata,
                allocate_session_id=bool(cast(int, row["allocate_session_id"])),
            ),
            session_id=cast(str | None, row["session_id"]),
            approval_request_id=cast(str | None, row["approval_request_id"]),
            question_request_id=cast(str | None, row["question_request_id"]),
            cancellation_cause=cast(str | None, row["cancellation_cause"]),
            result_available=bool(cast(int, row["result_available"])),
            error=cast(str | None, row["error"]),
            created_at=cast(int, row["created_at"]),
            updated_at=cast(int, row["updated_at"]),
            started_at=cast(int | None, row["started_at"]),
            finished_at=cast(int | None, row["finished_at"]),
            created_at_unix_ms=cast(int | None, row["created_at_unix_ms"]),
            started_at_unix_ms=cast(int | None, row["started_at_unix_ms"]),
            finished_at_unix_ms=cast(int | None, row["finished_at_unix_ms"]),
            cancel_requested_at=cast(int | None, row["cancel_requested_at"]),
            delegated_reminder=self._delegated_reminder_state_from_payload(cast(str | None, row["delegated_reminder_json"])),
            keep_alive=bool(cast(int, row["keep_alive"])),
            steer_prompt=cast(str | None, row["steer_prompt"]),
        )

    @staticmethod
    def _current_unix_ms() -> int:
        return int(time() * 1000)

    def _background_task_runtime_row(
        self,
        *,
        connection: sqlite3.Connection,
        workspace: Path,
        task_id: str,
    ) -> sqlite3.Row:
        row = cast(
            sqlite3.Row | None,
            connection.execute(
                """
                SELECT * FROM background_tasks
                WHERE workspace_id = ? AND task_id = ?
                """,
                (str(workspace), task_id),
            ).fetchone(),
        )
        if row is None:
            raise ValueError(f"unknown background task: {task_id}")
        return row

    def _next_background_task_timestamp(self, *, connection: sqlite3.Connection) -> int:
        return self._next_sequence_value(connection=connection, scope="background_tasks")

    def _next_memory_timestamp(self, *, connection: sqlite3.Connection) -> int:
        return self._next_sequence_value(connection=connection, scope="memories")

    def _next_auxiliary_timestamp(self, *, connection: sqlite3.Connection) -> int:
        return self._next_sequence_value(connection=connection, scope="auxiliary")

    def _linked_session_background_task_runtime_state(
        self,
        *,
        connection: sqlite3.Connection,
        workspace: Path,
        session_id: str | None,
    ) -> dict[str, object]:
        if session_id is None:
            return self._background_task_runtime_state_defaults()
        row = cast(
            sqlite3.Row | None,
            connection.execute(
                """
                SELECT status, pending_approval_json, pending_question_json
                FROM sessions
                WHERE workspace_id = ? AND session_id = ?
                """,
                (str(workspace), session_id),
            ).fetchone(),
        )
        return self._background_task_runtime_state_from_session_row(row)

    def acknowledge_notification(self, *, workspace: Path, notification_id: str) -> RuntimeNotification:
        with self._write_connect(workspace) as connection:
            existing_row = cast(
                sqlite3.Row | None,
                connection.execute(
                    """
                    SELECT notification_id, session_id, kind, status, summary, payload_json,
                           event_sequence, created_at, acknowledged_at
                    FROM session_notifications
                    WHERE workspace_id = ? AND notification_id = ?
                    """,
                    (str(workspace), notification_id),
                ).fetchone(),
            )
            if existing_row is None:
                raise ValueError(f"unknown notification: {notification_id}")
            acknowledged_at = cast(int | None, existing_row["acknowledged_at"])
            if acknowledged_at is None:
                acknowledged_at = self._next_auxiliary_timestamp(connection=connection)
                _ = connection.execute(
                    """
                    UPDATE session_notifications
                    SET status = 'acknowledged', acknowledged_at = ?
                    WHERE workspace_id = ? AND notification_id = ?
                    """,
                    (acknowledged_at, str(workspace), notification_id),
                )
                connection.commit()
            row = cast(
                sqlite3.Row,
                connection.execute(
                    """
                    SELECT notifications.notification_id, notifications.session_id,
                           notifications.kind, notifications.status, notifications.summary,
                           notifications.payload_json, notifications.event_sequence,
                           notifications.created_at, notifications.acknowledged_at,
                           sessions.parent_session_id
                    FROM session_notifications AS notifications
                    LEFT JOIN sessions ON sessions.session_id = notifications.session_id
                                      AND sessions.workspace_id = notifications.workspace_id
                    WHERE notifications.workspace_id = ? AND notifications.notification_id = ?
                    """,
                    (str(workspace), notification_id),
                ).fetchone(),
            )
        return self._notification_from_row(row)

    def storage_diagnostics(self, *, workspace: Path) -> dict[str, object]:
        database_path = self._resolve_database_path()
        with self._connect(workspace) as connection:
            journal_mode = self._pragma_scalar(connection=connection, name="journal_mode")
            synchronous = self._pragma_scalar(connection=connection, name="synchronous")
            busy_timeout = self._pragma_scalar(connection=connection, name="busy_timeout")
            foreign_keys = self._pragma_scalar(connection=connection, name="foreign_keys")
            wal_autocheckpoint = self._pragma_scalar(
                connection=connection,
                name="wal_autocheckpoint",
            )
            checkpoint = self._wal_checkpoint(connection=connection, mode="PASSIVE")
            counts = self._storage_table_counts(connection=connection, workspace=workspace)
            task_status_counts = self._background_task_status_counts(
                connection=connection,
                workspace=workspace,
            )
            pending_counts = self._pending_state_counts(
                connection=connection,
                workspace=workspace,
            )
        return {
            "database_path": str(database_path),
            "database_exists": database_path.exists(),
            "sqlite_version": sqlite3.sqlite_version,
            "connection_policy": {
                "journal_mode": journal_mode,
                "synchronous": synchronous,
                "busy_timeout_ms": busy_timeout,
                "foreign_keys": foreign_keys,
                "wal_autocheckpoint_pages": wal_autocheckpoint,
            },
            "checkpoint": checkpoint,
            "file_sizes": self._database_file_sizes(database_path),
            "counts": counts,
            "background_task_status_counts": task_status_counts,
            "pending_counts": pending_counts,
        }

    def prune_runtime_storage(
        self,
        *,
        workspace: Path,
        keep_sessions: int | None = None,
        keep_background_tasks: int | None = None,
        older_than: int | None = None,
    ) -> dict[str, int]:
        if keep_sessions is not None and keep_sessions < 0:
            raise ValueError("keep_sessions must be non-negative when provided")
        if keep_background_tasks is not None and keep_background_tasks < 0:
            raise ValueError("keep_background_tasks must be non-negative when provided")
        if older_than is not None and older_than < 0:
            raise ValueError("older_than must be non-negative when provided")
        with self._write_connect(workspace) as connection:
            task_ids = self._prunable_background_task_ids(
                connection=connection,
                workspace=workspace,
                keep_background_tasks=keep_background_tasks,
                older_than=older_than,
            )
            retained_background_task_session_ids = self._retained_background_task_session_ids(
                connection=connection,
                workspace=workspace,
                pruned_task_ids=task_ids,
            )
            session_ids = self._prunable_session_ids(
                connection=connection,
                workspace=workspace,
                keep_sessions=keep_sessions,
                older_than=older_than,
                protected_session_ids=retained_background_task_session_ids,
            )
            counts = {
                "session_events": self._delete_for_ids(
                    connection=connection,
                    table="session_events",
                    column="session_id",
                    ids=session_ids,
                    workspace=workspace,
                ),
                "session_todos": self._delete_for_ids(
                    connection=connection,
                    table="session_todos",
                    column="session_id",
                    ids=session_ids,
                    workspace=workspace,
                ),
                "session_event_deliveries": self._delete_for_ids(
                    connection=connection,
                    table="session_event_deliveries",
                    column="session_id",
                    ids=session_ids,
                    workspace=workspace,
                ),
                "session_notifications": self._delete_for_ids(
                    connection=connection,
                    table="session_notifications",
                    column="session_id",
                    ids=session_ids,
                    workspace=workspace,
                ),
                "sessions": self._delete_for_ids(
                    connection=connection,
                    table="sessions",
                    column="session_id",
                    ids=session_ids,
                    workspace=workspace,
                ),
                "background_tasks": self._delete_for_ids(
                    connection=connection,
                    table="background_tasks",
                    column="task_id",
                    ids=task_ids,
                    workspace=workspace,
                ),
            }
            connection.commit()
            _ = self._wal_checkpoint(connection=connection, mode="PASSIVE")
        return counts

    def reset_runtime_storage(self, *, workspace: Path) -> dict[str, object]:
        _ = workspace
        database_path = self._resolve_database_path()
        removed: list[str] = []
        for path in (
            database_path,
            database_path.with_name(f"{database_path.name}-wal"),
            database_path.with_name(f"{database_path.name}-shm"),
        ):
            if path.exists():
                self._unlink_with_retries(path)
                removed.append(str(path))
        return {
            "database_path": str(database_path),
            "removed": removed,
            "reset": bool(removed),
        }

    @staticmethod
    def _unlink_with_retries(path: Path, *, attempts: int = 5, delay_seconds: float = 0.05) -> None:
        for attempt in range(attempts):
            try:
                path.unlink()
                return
            except PermissionError:
                if attempt == attempts - 1:
                    raise
                sleep(delay_seconds)

    @staticmethod
    def _pragma_scalar(*, connection: sqlite3.Connection, name: str) -> object:
        row = connection.execute(f"PRAGMA {name}").fetchone()
        return None if row is None else cast(object, row[0])

    @staticmethod
    def _wal_checkpoint(*, connection: sqlite3.Connection, mode: str) -> dict[str, int]:
        row = connection.execute(f"PRAGMA wal_checkpoint({mode})").fetchone()
        if row is None:
            return {"busy": 0, "log_pages": 0, "checkpointed_pages": 0}
        return {
            "busy": int(row[0]),
            "log_pages": int(row[1]),
            "checkpointed_pages": int(row[2]),
        }

    @staticmethod
    def _database_file_sizes(database_path: Path) -> dict[str, int]:
        candidates = {
            "database": database_path,
            "wal": database_path.with_name(f"{database_path.name}-wal"),
            "shm": database_path.with_name(f"{database_path.name}-shm"),
        }
        return {name: path.stat().st_size if path.exists() else 0 for name, path in candidates.items()}

    @staticmethod
    def _storage_table_counts(*, connection: sqlite3.Connection, workspace: Path) -> dict[str, int]:
        scoped_tables = ("sessions", "background_tasks", "session_notifications")
        counts = {
            table: int(
                connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE workspace_id = ?",
                    (str(workspace),),
                ).fetchone()[0]
            )
            for table in scoped_tables
        }
        session_ids = tuple(
            cast(str, row["session_id"])
            for row in cast(
                list[sqlite3.Row],
                connection.execute(
                    "SELECT session_id FROM sessions WHERE workspace_id = ?",
                    (str(workspace),),
                ).fetchall(),
            )
        )
        counts["session_events"] = SqliteSessionStore._count_for_ids(
            connection=connection,
            table="session_events",
            column="session_id",
            ids=session_ids,
            workspace=workspace,
        )
        counts["session_todos"] = SqliteSessionStore._count_for_ids(
            connection=connection,
            table="session_todos",
            column="session_id",
            ids=session_ids,
            workspace=workspace,
        )
        counts["session_event_deliveries"] = SqliteSessionStore._count_for_ids(
            connection=connection,
            table="session_event_deliveries",
            column="session_id",
            ids=session_ids,
            workspace=workspace,
        )
        return counts

    @staticmethod
    def _background_task_status_counts(*, connection: sqlite3.Connection, workspace: Path) -> dict[str, int]:
        return {
            cast(str, row["status"]): cast(int, row["count"])
            for row in cast(
                list[sqlite3.Row],
                connection.execute(
                    """
                    SELECT status, COUNT(*) AS count
                    FROM background_tasks
                    WHERE workspace_id = ?
                    GROUP BY status
                    ORDER BY status ASC
                    """,
                    (str(workspace),),
                ).fetchall(),
            )
        }

    @staticmethod
    def _pending_state_counts(*, connection: sqlite3.Connection, workspace: Path) -> dict[str, int]:
        row = connection.execute(
            """
            SELECT
                SUM(CASE WHEN pending_approval_json IS NOT NULL THEN 1 ELSE 0 END) AS approvals,
                SUM(CASE WHEN pending_question_json IS NOT NULL THEN 1 ELSE 0 END) AS questions
            FROM sessions
            WHERE workspace_id = ?
            """,
            (str(workspace),),
        ).fetchone()
        if row is None:
            return {"pending_approvals": 0, "pending_questions": 0}
        return {
            "pending_approvals": int(row[0] or 0),
            "pending_questions": int(row[1] or 0),
        }

    @staticmethod
    def _count_for_ids(
        *,
        connection: sqlite3.Connection,
        table: str,
        column: str,
        ids: tuple[str, ...],
        workspace: Path | None = None,
    ) -> int:
        if not ids:
            return 0
        placeholders = ", ".join("?" for _ in ids)
        workspace_clause = " AND workspace_id = ?" if workspace is not None else ""
        parameters: tuple[object, ...] = (*ids, str(workspace)) if workspace is not None else ids
        return int(
            connection.execute(
                (f"SELECT COUNT(*) FROM {table} WHERE {column} IN ({placeholders}){workspace_clause}"),
                parameters,
            ).fetchone()[0]
        )

    @staticmethod
    def _delete_for_ids(
        *,
        connection: sqlite3.Connection,
        table: str,
        column: str,
        ids: tuple[str, ...],
        workspace: Path | None = None,
    ) -> int:
        if not ids:
            return 0
        placeholders = ", ".join("?" for _ in ids)
        workspace_clause = " AND workspace_id = ?" if workspace is not None else ""
        parameters: tuple[object, ...] = (*ids, str(workspace)) if workspace is not None else ids
        cursor = connection.execute(
            f"DELETE FROM {table} WHERE {column} IN ({placeholders}){workspace_clause}",
            parameters,
        )
        return cursor.rowcount

    @staticmethod
    def _prunable_session_ids(
        *,
        connection: sqlite3.Connection,
        workspace: Path,
        keep_sessions: int | None,
        older_than: int | None,
        protected_session_ids: tuple[str, ...] = (),
    ) -> tuple[str, ...]:
        conditions = ["workspace_id = ?", "status IN ('completed', 'failed')"]
        parameters: list[object] = [str(workspace)]
        if older_than is not None:
            conditions.append("updated_at < ?")
            parameters.append(older_than)
        protected_clause = ""
        if protected_session_ids:
            protected_placeholders = ", ".join("?" for _ in protected_session_ids)
            protected_clause = f"AND session_id NOT IN ({protected_placeholders})"
            parameters.extend(protected_session_ids)
        keep_clause = ""
        if keep_sessions is not None:
            keep_clause = (
                "AND session_id NOT IN ("
                "SELECT session_id FROM sessions "
                "WHERE workspace_id = ? AND status IN ('completed', 'failed') "
                "ORDER BY updated_at DESC, session_id ASC LIMIT ?"
                ")"
            )
            parameters.extend([str(workspace), keep_sessions])
        rows = connection.execute(
            f"""
            SELECT session_id
            FROM sessions
            WHERE {" AND ".join(conditions)}
              {protected_clause}
              {keep_clause}
            ORDER BY updated_at ASC, session_id ASC
            """,
            tuple(parameters),
        ).fetchall()
        return tuple(cast(str, row["session_id"]) for row in cast(list[sqlite3.Row], rows))

    @staticmethod
    def _retained_background_task_session_ids(
        *,
        connection: sqlite3.Connection,
        workspace: Path,
        pruned_task_ids: tuple[str, ...],
    ) -> tuple[str, ...]:
        pruned_clause = ""
        parameters: list[object] = [str(workspace)]
        if pruned_task_ids:
            pruned_placeholders = ", ".join("?" for _ in pruned_task_ids)
            pruned_clause = f"AND task_id NOT IN ({pruned_placeholders})"
            parameters.extend(pruned_task_ids)
        rows = connection.execute(
            f"""
            SELECT DISTINCT session_id
            FROM background_tasks
            WHERE workspace_id = ?
              AND session_id IS NOT NULL
              {pruned_clause}
            """,
            tuple(parameters),
        ).fetchall()
        return tuple(cast(str, row["session_id"]) for row in cast(list[sqlite3.Row], rows))

    @staticmethod
    def _prunable_background_task_ids(
        *,
        connection: sqlite3.Connection,
        workspace: Path,
        keep_background_tasks: int | None,
        older_than: int | None,
    ) -> tuple[str, ...]:
        conditions = [
            "workspace_id = ?",
            "status IN ('completed', 'failed', 'cancelled', 'interrupted')",
        ]
        parameters: list[object] = [str(workspace)]
        if older_than is not None:
            conditions.append("updated_at < ?")
            parameters.append(older_than)
        keep_clause = ""
        if keep_background_tasks is not None:
            keep_clause = (
                "AND task_id NOT IN ("
                "SELECT task_id FROM background_tasks "
                "WHERE workspace_id = ? AND status IN "
                "('completed', 'failed', 'cancelled', 'interrupted') "
                "ORDER BY updated_at DESC, task_id ASC LIMIT ?"
                ")"
            )
            parameters.extend([str(workspace), keep_background_tasks])
        rows = connection.execute(
            f"""
            SELECT task_id
            FROM background_tasks
            WHERE {" AND ".join(conditions)}
              {keep_clause}
            ORDER BY updated_at ASC, task_id ASC
            """,
            tuple(parameters),
        ).fetchall()
        return tuple(cast(str, row["task_id"]) for row in cast(list[sqlite3.Row], rows))

    def _auto_prune_sessions(
        self,
        *,
        connection: sqlite3.Connection,
        workspace: Path,
    ) -> int:
        age_cutoff_ms = int((time() - self._DEFAULT_MAX_SESSION_AGE_DAYS * 86_400) * 1000)
        # Child sessions referenced by a background task must survive count/age
        # pruning: ``load_background_task_child_result`` / parent-terminal event
        # backfill read them long after the task finished. Mirrors the explicit
        # ``prune_runtime_storage`` protection via ``_retained_background_task_session_ids``.
        protected_task_session_ids = self._retained_background_task_session_ids(
            connection=connection,
            workspace=workspace,
            pruned_task_ids=(),
        )
        age_rows = cast(
            list[sqlite3.Row],
            connection.execute(
                """
                SELECT session_id FROM sessions
                WHERE workspace_id = ?
                  AND status IN ('completed', 'failed')
                  AND created_at_unix_ms IS NOT NULL
                  AND created_at_unix_ms < ?
                ORDER BY created_at_unix_ms ASC, session_id ASC
                """,
                (str(workspace), age_cutoff_ms),
            ).fetchall(),
        )
        age_ids = tuple(
            session_id for session_id in (cast(str, row["session_id"]) for row in age_rows) if session_id not in protected_task_session_ids
        )

        count_ids = self._prunable_session_ids(
            connection=connection,
            workspace=workspace,
            keep_sessions=self._DEFAULT_MAX_SESSIONS_PER_WORKSPACE,
            older_than=None,
            protected_session_ids=protected_task_session_ids,
        )
        # Dangling-parent terminal children: a child session whose parent row is
        # gone (pruned, or a synthetic parent that was never persisted) and that
        # no background task references can never be reached again — prune them
        # so the ``child.parent_id must exist`` linkage invariant converges
        # instead of accumulating orphans forever.
        dangling_child_ids = self._dangling_parent_terminal_session_ids(
            connection=connection,
            workspace=workspace,
            protected_session_ids=protected_task_session_ids,
        )
        pruned_ids = tuple(dict.fromkeys((*count_ids, *age_ids, *dangling_child_ids)))
        # Orphaned terminal background tasks: tasks that reached a terminal
        # status without ever allocating a child session (e.g. shutdown
        # terminalization of still-queued tasks) and whose parent session is
        # terminal or missing are pure garbage — no result, no child session, no
        # repair path. Delete them here (the existing list-triggered prune path)
        # so they cannot accumulate unbounded across shutdowns.
        orphaned_task_ids = self._orphaned_terminal_background_task_ids(
            connection=connection,
            workspace=workspace,
        )
        if not pruned_ids and not orphaned_task_ids:
            return 0

        for table in (
            "session_events",
            "session_todos",
            "session_event_deliveries",
            "session_notifications",
            "sessions",
        ):
            self._delete_for_ids(
                connection=connection,
                table=table,
                column="session_id",
                ids=pruned_ids,
                workspace=workspace,
            )
        if orphaned_task_ids:
            self._delete_for_ids(
                connection=connection,
                table="background_tasks",
                column="task_id",
                ids=orphaned_task_ids,
                workspace=workspace,
            )
        return len(pruned_ids) + len(orphaned_task_ids)

    @staticmethod
    def _dangling_parent_terminal_session_ids(
        *,
        connection: sqlite3.Connection,
        workspace: Path,
        protected_session_ids: tuple[str, ...] = (),
    ) -> tuple[str, ...]:
        """Event-less terminal children whose parent row is gone and no task references.

        Every real run persists at least one event before a terminal seal
        (request_received / the terminal failure), so a terminal session with an
        EMPTY event log is pure fabrication residue (direct store writes, stale
        bundle rows) — it has no transcript, no result, and no repair path.
        Combined with a dangling parent and no task reference, it can never be
        reached again; prune it so the ``child.parent_id must exist`` linkage
        converges instead of accumulating orphans forever.
        """
        protected_clause = ""
        parameters: list[object] = [str(workspace)]
        if protected_session_ids:
            protected_placeholders = ", ".join("?" for _ in protected_session_ids)
            protected_clause = f"AND session_id NOT IN ({protected_placeholders})"
            parameters.extend(protected_session_ids)
        rows = connection.execute(
            f"""
            SELECT session_id
            FROM sessions
            WHERE workspace_id = ?
              AND status IN ('completed', 'failed')
              AND parent_session_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM session_events event
                  WHERE event.workspace_id = sessions.workspace_id
                    AND event.session_id = sessions.session_id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM sessions parent
                  WHERE parent.workspace_id = sessions.workspace_id
                    AND parent.session_id = sessions.parent_session_id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM background_tasks task
                  WHERE task.workspace_id = sessions.workspace_id
                    AND task.session_id = sessions.session_id
              )
              {protected_clause}
            ORDER BY updated_at ASC, session_id ASC
            """,
            tuple(parameters),
        ).fetchall()
        return tuple(cast(str, row["session_id"]) for row in cast(list[sqlite3.Row], rows))

    @staticmethod
    def _orphaned_terminal_background_task_ids(
        *,
        connection: sqlite3.Connection,
        workspace: Path,
    ) -> tuple[str, ...]:
        """Terminal tasks with no child session whose parent is terminal or gone.

        These arise from shutdown terminalization of still-queued tasks and from
        restart reconciliation of running tasks whose worker died before any
        child session persisted. They have no result, no child session, and no
        repair path (``repair_interrupted_task_from_child_terminal_session``
        requires ``session_id``), so they are safe to prune; a terminal parent
        (or missing parent) guarantees no future dispatch can own them.
        """
        rows = connection.execute(
            """
            SELECT task_id
            FROM background_tasks
            WHERE workspace_id = ?
              AND status IN ('completed', 'failed', 'cancelled', 'interrupted')
              AND session_id IS NULL
              AND (
                  request_parent_session_id IS NULL
                  OR NOT EXISTS (
                      SELECT 1 FROM sessions parent
                      WHERE parent.workspace_id = background_tasks.workspace_id
                        AND parent.session_id = background_tasks.request_parent_session_id
                        AND parent.status NOT IN ('completed', 'failed')
                  )
              )
            ORDER BY updated_at ASC, task_id ASC
            """,
            (str(workspace),),
        ).fetchall()
        return tuple(cast(str, row["task_id"]) for row in cast(list[sqlite3.Row], rows))

    @staticmethod
    def _result_summary(*, response: RuntimeResponse, prompt: str) -> tuple[str, str | None]:
        if response.session.status == "completed":
            output = (response.output or "").strip()
            if output:
                return f"Completed: {output[:120]}", None
            return f"Completed session for prompt: {prompt[:80]}", None
        if response.session.status == "waiting":
            for event in reversed(response.events):
                if event.event_type == "runtime.approval_requested":
                    tool = str(event.payload.get("tool", "tool"))
                    target = str(event.payload.get("target_summary", "")).strip()
                    if target:
                        return f"Approval blocked on {tool}: {target[:100]}", None
                    return f"Approval blocked on {tool}", None
                if event.event_type == "runtime.question_requested":
                    question_count = event.payload.get("question_count")
                    if isinstance(question_count, int) and question_count > 0:
                        label = "question" if question_count == 1 else "questions"
                        return f"Question blocked on {question_count} {label}", None
                    return "Question blocked", None
            return "Approval blocked", None
        if response.session.status == "failed":
            for event in reversed(response.events):
                if event.event_type == "runtime.failed":
                    error = str(event.payload.get("error", "runtime failed"))
                    return f"Failed: {error[:120]}", error
            return "Failed", None
        return f"{response.session.status.capitalize()} session", None

    def _sync_notifications(
        self,
        *,
        connection: sqlite3.Connection,
        workspace: Path,
        request: RuntimeRequest,
        response: RuntimeResponse,
        pending_approval: PendingApproval | None,
        pending_question: PendingQuestion | None = None,
        notification_run_id: int,
        last_event_sequence: int | None = None,
    ) -> None:
        session_id = response.session.session.id
        if last_event_sequence is None:
            # Callers that do not pass the durable watermark (none in-tree) fall
            # back to the persisted event-log max so notification references can
            # never point at a phantom (locally-resequenced) sequence.
            last_event_sequence = self._max_persisted_event_sequence(
                connection=connection,
                workspace=workspace,
                session_id=session_id,
            )
        notification = self._notification_candidate(
            request=request,
            response=response,
            pending_approval=pending_approval,
            pending_question=pending_question,
            notification_run_id=notification_run_id,
            last_event_sequence=last_event_sequence,
        )
        if pending_approval is None:
            _ = connection.execute(
                """
                UPDATE session_notifications
                SET status = 'acknowledged',
                    acknowledged_at = COALESCE(acknowledged_at, ?)
                WHERE workspace_id = ?
                  AND session_id = ?
                  AND kind = 'approval_blocked'
                  AND status = 'unread'
                """,
                (
                    self._next_auxiliary_timestamp(connection=connection),
                    str(workspace),
                    session_id,
                ),
            )
        elif notification is not None:
            _ = connection.execute(
                """
                UPDATE session_notifications
                SET status = 'acknowledged',
                    acknowledged_at = COALESCE(acknowledged_at, ?)
                WHERE workspace_id = ?
                  AND session_id = ?
                  AND kind = 'approval_blocked'
                  AND status = 'unread'
                  AND notification_id != ?
                """,
                (
                    self._next_auxiliary_timestamp(connection=connection),
                    str(workspace),
                    session_id,
                    notification["notification_id"],
                ),
            )
        if pending_question is None:
            _ = connection.execute(
                """
                UPDATE session_notifications
                SET status = 'acknowledged',
                    acknowledged_at = COALESCE(acknowledged_at, ?)
                WHERE workspace_id = ?
                  AND session_id = ?
                  AND kind = 'question_blocked'
                  AND status = 'unread'
                """,
                (
                    self._next_auxiliary_timestamp(connection=connection),
                    str(workspace),
                    session_id,
                ),
            )
        elif notification is not None:
            _ = connection.execute(
                """
                UPDATE session_notifications
                SET status = 'acknowledged',
                    acknowledged_at = COALESCE(acknowledged_at, ?)
                WHERE workspace_id = ?
                  AND session_id = ?
                  AND kind = 'question_blocked'
                  AND status = 'unread'
                  AND notification_id != ?
                """,
                (
                    self._next_auxiliary_timestamp(connection=connection),
                    str(workspace),
                    session_id,
                    notification["notification_id"],
                ),
            )
        if notification is None:
            return
        _ = connection.execute(
            """
            INSERT OR IGNORE INTO session_notifications (
                notification_id, workspace_id, session_id, kind, status, summary, payload_json,
                event_sequence, dedupe_key, created_at, acknowledged_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                notification["notification_id"],
                str(workspace),
                session_id,
                notification["kind"],
                "unread",
                notification["summary"],
                json.dumps(notification["payload"], sort_keys=True),
                notification["event_sequence"],
                notification["dedupe_key"],
                self._next_auxiliary_timestamp(connection=connection),
                None,
            ),
        )

    def _notification_candidate(
        self,
        *,
        request: RuntimeRequest,
        response: RuntimeResponse,
        pending_approval: PendingApproval | None,
        pending_question: PendingQuestion | None,
        notification_run_id: int,
        last_event_sequence: int | None = None,
    ) -> dict[str, object] | None:
        if pending_approval is not None:
            return self._approval_notification_candidate(
                request=request,
                response=response,
                pending_approval=pending_approval,
                last_event_sequence=last_event_sequence,
            )
        if pending_question is not None:
            return self._question_notification_candidate(
                request=request,
                response=response,
                pending_question=pending_question,
                last_event_sequence=last_event_sequence,
            )
        return self._terminal_notification_candidate(
            request=request,
            response=response,
            notification_run_id=notification_run_id,
            last_event_sequence=last_event_sequence,
        )

    def _approval_notification_candidate(
        self,
        *,
        request: RuntimeRequest,
        response: RuntimeResponse,
        pending_approval: PendingApproval,
        last_event_sequence: int | None = None,
    ) -> dict[str, object]:
        session_id = response.session.session.id
        summary, _ = self._result_summary(response=response, prompt=request.prompt)
        event_sequence = self._session_last_event_sequence(response.events) if last_event_sequence is None else last_event_sequence
        dedupe_key = f"{session_id}:approval_blocked:{pending_approval.request_id}"
        return {
            "notification_id": dedupe_key,
            "dedupe_key": dedupe_key,
            "kind": "approval_blocked",
            "summary": summary,
            "event_sequence": event_sequence,
            "payload": {
                "request_id": pending_approval.request_id,
                "tool": pending_approval.tool_name,
                "arguments": pending_approval.arguments,
                "target_summary": pending_approval.target_summary,
                "reason": pending_approval.reason,
            },
        }

    def _question_notification_candidate(
        self,
        *,
        request: RuntimeRequest,
        response: RuntimeResponse,
        pending_question: PendingQuestion,
        last_event_sequence: int | None = None,
    ) -> dict[str, object]:
        session_id = response.session.session.id
        summary, _ = self._result_summary(response=response, prompt=request.prompt)
        event_sequence = self._session_last_event_sequence(response.events) if last_event_sequence is None else last_event_sequence
        dedupe_key = f"{session_id}:question_blocked:{pending_question.request_id}"
        return {
            "notification_id": dedupe_key,
            "dedupe_key": dedupe_key,
            "kind": "question_blocked",
            "summary": summary,
            "event_sequence": event_sequence,
            "payload": {
                "request_id": pending_question.request_id,
                "questions": [
                    {
                        "header": prompt.header,
                        "question": prompt.question,
                        "multiple": prompt.multiple,
                        "options": [{"label": option.label, "description": option.description} for option in prompt.options],
                    }
                    for prompt in pending_question.prompts
                ],
            },
        }

    def _terminal_notification_candidate(
        self,
        *,
        request: RuntimeRequest,
        response: RuntimeResponse,
        notification_run_id: int,
        last_event_sequence: int | None = None,
    ) -> dict[str, object] | None:
        session_id = response.session.session.id
        event_sequence = self._session_last_event_sequence(response.events) if last_event_sequence is None else last_event_sequence
        if response.session.status == "completed":
            summary, _ = self._result_summary(response=response, prompt=request.prompt)
            dedupe_key = f"{session_id}:completion:{notification_run_id}"
            return {
                "notification_id": dedupe_key,
                "dedupe_key": dedupe_key,
                "kind": "completion",
                "summary": summary,
                "event_sequence": event_sequence,
                "payload": {"output": response.output},
            }
        if response.session.status == "failed":
            summary, error = self._result_summary(response=response, prompt=request.prompt)
            dedupe_key = f"{session_id}:failure:{notification_run_id}"
            return {
                "notification_id": dedupe_key,
                "dedupe_key": dedupe_key,
                "kind": "failure",
                "summary": summary,
                "event_sequence": event_sequence,
                "payload": {"error": error},
            }
        return None

    @staticmethod
    def _notification_from_row(row: sqlite3.Row) -> RuntimeNotification:
        return RuntimeNotification(
            id=cast(str, row["notification_id"]),
            session=SessionRef(
                id=cast(str, row["session_id"]),
                parent_id=cast(str | None, row["parent_session_id"]),
            ),
            kind=cast(RuntimeNotificationKind, row["kind"]),
            status=cast(RuntimeNotificationStatus, row["status"]),
            summary=cast(str, row["summary"]),
            event_sequence=cast(int, row["event_sequence"]),
            created_at=cast(int, row["created_at"]),
            acknowledged_at=cast(int | None, row["acknowledged_at"]),
            payload=cast(dict[str, object], json.loads(cast(str, row["payload_json"]))),
        )

    def _read_created_at(self, *, connection: sqlite3.Connection, workspace: Path, session_id: str) -> int:
        row = cast(
            sqlite3.Row | None,
            connection.execute(
                "SELECT created_at FROM sessions WHERE workspace_id = ? AND session_id = ?",
                (str(workspace), session_id),
            ).fetchone(),
        )
        if row is not None:
            return cast(int, row["created_at"])
        return self._next_auxiliary_timestamp(connection=connection)

    def _read_created_at_unix_ms(self, *, connection: sqlite3.Connection, workspace: Path, session_id: str) -> int | None:
        row = cast(
            sqlite3.Row | None,
            connection.execute(
                "SELECT created_at_unix_ms FROM sessions WHERE workspace_id = ? AND session_id = ?",
                (str(workspace), session_id),
            ).fetchone(),
        )
        if row is not None and row["created_at_unix_ms"] is not None:
            return cast(int, row["created_at_unix_ms"])
        return None

    @staticmethod
    def _read_last_event_sequence(*, connection: sqlite3.Connection, workspace: Path, session_id: str) -> int:
        row = cast(
            sqlite3.Row | None,
            connection.execute(
                "SELECT last_event_sequence FROM sessions WHERE workspace_id = ? AND session_id = ?",
                (str(workspace), session_id),
            ).fetchone(),
        )
        if row is not None:
            return cast(int, row["last_event_sequence"])
        return 0

    @staticmethod
    def _max_persisted_event_sequence(*, connection: sqlite3.Connection, workspace: Path, session_id: str) -> int:
        row = connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) FROM session_events WHERE workspace_id = ? AND session_id = ?",
            (str(workspace), session_id),
        ).fetchone()
        if row is None:
            return 0
        return int(row[0])

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
