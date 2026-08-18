from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Protocol

from .contracts import (
    RuntimeNotification,
    RuntimeRequest,
    RuntimeResponse,
    RuntimeSessionRevertMarker,
)
from .events import (
    DELEGATED_BACKGROUND_TASK_EVENT_TYPES,
    RUNTIME_ACP_CONNECTED,
    RUNTIME_ACP_DELEGATED_LIFECYCLE,
    RUNTIME_ACP_DISCONNECTED,
    RUNTIME_ACP_FAILED,
    RUNTIME_MCP_SERVER_ACQUIRED,
    RUNTIME_MCP_SERVER_FAILED,
    RUNTIME_MCP_SERVER_IDLE_CLEANED,
    RUNTIME_MCP_SERVER_RELEASED,
    RUNTIME_MCP_SERVER_REUSED,
    RUNTIME_MCP_SERVER_STARTED,
    RUNTIME_MCP_SERVER_STOPPED,
    EventEnvelope,
    EventSource,
)
from .memory import (
    MemoryKind,
    MemoryRecord,
    MemoryStatus,
)
from .permission import (
    OperationClass,
    PathScope,
    PendingApproval,
    PermissionDecision,
)
from .question import PendingQuestion
from .session import (
    SESSION_STORAGE_SEAL_STATUSES,
    SessionStatus,
)
from .task import (
    BackgroundTaskState,
    BackgroundTaskStatus,
    DelegatedReminderState,
    DelegatedReminderStopCondition,
    StoredBackgroundTaskSummary,
)


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
    if status not in SESSION_STORAGE_SEAL_STATUSES:
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


class _StorageMixinBase(Protocol):
    _DEFAULT_MAX_SESSIONS_PER_WORKSPACE: int
    _DEFAULT_MAX_SESSION_AGE_DAYS: int
    _MEMORY_KINDS: frozenset[MemoryKind]
    _RESUME_CHECKPOINT_KINDS: frozenset[str]

    def _active_revert_metadata(self, metadata: dict[str, object], *, events: tuple[EventEnvelope, ...]) -> dict[str, object]: ...
    def _approval_notification_candidate(
        self, *, request: RuntimeRequest, response: RuntimeResponse, pending_approval: PendingApproval, last_event_sequence: int | None = None
    ) -> dict[str, object]: ...
    def _approval_wait_resume_checkpoint(
        self, *, request: RuntimeRequest, response: RuntimeResponse, pending_approval: PendingApproval, last_event_sequence: int | None = None
    ) -> dict[str, object]: ...
    def _auto_prune_sessions(self, *, connection: sqlite3.Connection, workspace: Path) -> int: ...
    def _auto_prune_sessions_for_list(self, *, workspace: Path) -> None: ...
    def _background_task_durable_payload(self, row: sqlite3.Row) -> dict[str, object]: ...
    def _background_task_runtime_row(self, *, connection: sqlite3.Connection, workspace: Path, task_id: str) -> sqlite3.Row: ...
    @staticmethod
    def _background_task_runtime_state_defaults() -> dict[str, object]: ...
    @classmethod
    def _background_task_runtime_state_from_session_row(cls, row: sqlite3.Row | None) -> dict[str, object]: ...
    def _background_task_state_from_row(self, row: sqlite3.Row) -> BackgroundTaskState: ...
    @staticmethod
    def _background_task_status_counts(*, connection: sqlite3.Connection, workspace: Path) -> dict[str, int]: ...
    @classmethod
    def _background_task_summary_from_row(cls, row: sqlite3.Row) -> StoredBackgroundTaskSummary: ...
    @staticmethod
    def _checkpoint_skill_snapshot(metadata: dict[str, object]) -> tuple[object | None, object | None, dict[str, object]]: ...
    def _connect(self, workspace: Path) -> AbstractContextManager[sqlite3.Connection]: ...
    @staticmethod
    def _current_unix_ms() -> int: ...
    @staticmethod
    def _dangling_parent_terminal_session_ids(
        *, connection: sqlite3.Connection, workspace: Path, protected_session_ids: tuple[str, ...] = ()
    ) -> tuple[str, ...]: ...
    @staticmethod
    def _database_file_sizes(database_path: Path) -> dict[str, int]: ...
    @staticmethod
    def _decode_json_object_payload(payload: str, *, malformed_message: str, non_object_message: str) -> dict[str, object]: ...
    @classmethod
    def _decode_resume_checkpoint_payload(cls, payload: str) -> dict[str, object]: ...
    @classmethod
    def _delegated_reminder_state_from_payload(cls, payload: str | None) -> DelegatedReminderState | None: ...
    @staticmethod
    def _delegated_reminder_state_payload(state: DelegatedReminderState) -> dict[str, object]: ...
    @staticmethod
    def _delete_for_ids(*, connection: sqlite3.Connection, table: str, column: str, ids: tuple[str, ...], workspace: Path | None = None) -> int: ...
    def _enriched_background_task_event_payload(
        self, *, connection: sqlite3.Connection, workspace: Path, event_type: str, payload: dict[str, object]
    ) -> dict[str, object]: ...
    @classmethod
    def _interrupted_resume_checkpoint(
        cls,
        *,
        prompt: str,
        session_metadata: dict[str, object],
        tool_results: tuple[dict[str, object], ...],
        last_event_sequence: int,
        output: str | None,
    ) -> dict[str, object]: ...
    def _linked_session_background_task_runtime_state(
        self, *, connection: sqlite3.Connection, workspace: Path, session_id: str | None
    ) -> dict[str, object]: ...
    def _load_session_response(self, *, workspace: Path, session_id: str, filter_reverted: bool) -> RuntimeResponse: ...
    @staticmethod
    def _max_persisted_event_sequence(*, connection: sqlite3.Connection, workspace: Path, session_id: str) -> int: ...
    @classmethod
    def _memory_record_from_row(cls, row: sqlite3.Row) -> MemoryRecord: ...
    @staticmethod
    def _memory_row(*, connection: sqlite3.Connection, workspace: Path, memory_id: str, include_deleted: bool) -> sqlite3.Row | None: ...
    @staticmethod
    def _memory_search_terms(query: str) -> tuple[str, ...]: ...
    @staticmethod
    def _metadata_with_revert_marker(metadata: dict[str, object], marker: RuntimeSessionRevertMarker | None) -> dict[str, object]: ...
    @classmethod
    def _metadata_with_todo_state(cls, metadata: dict[str, object], todo_state: dict[str, object] | None) -> dict[str, object]: ...
    def _next_auxiliary_timestamp(self, *, connection: sqlite3.Connection) -> int: ...
    def _next_background_task_timestamp(self, *, connection: sqlite3.Connection) -> int: ...
    def _next_memory_timestamp(self, *, connection: sqlite3.Connection) -> int: ...
    @staticmethod
    def _next_sequence_value(*, connection: sqlite3.Connection, scope: str) -> int: ...
    def _next_timestamp(self, *, connection: sqlite3.Connection) -> int: ...
    def _notification_candidate(
        self,
        *,
        request: RuntimeRequest,
        response: RuntimeResponse,
        pending_approval: PendingApproval | None,
        pending_question: PendingQuestion | None,
        notification_run_id: int,
        last_event_sequence: int | None = None,
    ) -> dict[str, object] | None: ...
    @staticmethod
    def _notification_from_row(row: sqlite3.Row) -> RuntimeNotification: ...
    @staticmethod
    def _optional_int(value: object) -> int | None: ...
    @staticmethod
    def _optional_string(value: object) -> str | None: ...
    @staticmethod
    def _orphaned_terminal_background_task_ids(*, connection: sqlite3.Connection, workspace: Path) -> tuple[str, ...]: ...
    @staticmethod
    def _parse_background_task_status(value: str) -> BackgroundTaskStatus: ...
    @staticmethod
    def _parse_delegated_reminder_stop_condition(value: object) -> DelegatedReminderStopCondition | None: ...
    @staticmethod
    def _parse_event_source(value: str) -> EventSource: ...
    @classmethod
    def _parse_memory_kind(cls, value: str) -> MemoryKind: ...
    @staticmethod
    def _parse_memory_status(value: str) -> MemoryStatus: ...
    @staticmethod
    def _parse_session_status(value: str) -> SessionStatus: ...
    @staticmethod
    def _pending_question_payload(pending_question: PendingQuestion) -> dict[str, object]: ...
    @staticmethod
    def _pending_state_counts(*, connection: sqlite3.Connection, workspace: Path) -> dict[str, int]: ...
    @staticmethod
    def _pragma_scalar(*, connection: sqlite3.Connection, name: str) -> object: ...
    @staticmethod
    def _prunable_background_task_ids(
        *, connection: sqlite3.Connection, workspace: Path, keep_background_tasks: int | None, older_than: int | None
    ) -> tuple[str, ...]: ...
    @staticmethod
    def _prunable_session_ids(
        *,
        connection: sqlite3.Connection,
        workspace: Path,
        keep_sessions: int | None,
        older_than: int | None,
        protected_session_ids: tuple[str, ...] = (),
    ) -> tuple[str, ...]: ...
    def _question_notification_candidate(
        self, *, request: RuntimeRequest, response: RuntimeResponse, pending_question: PendingQuestion, last_event_sequence: int | None = None
    ) -> dict[str, object]: ...
    def _question_wait_resume_checkpoint(
        self, *, request: RuntimeRequest, response: RuntimeResponse, pending_question: PendingQuestion, last_event_sequence: int | None = None
    ) -> dict[str, object]: ...
    def _read_created_at(self, *, connection: sqlite3.Connection, workspace: Path, session_id: str) -> int: ...
    def _read_created_at_unix_ms(self, *, connection: sqlite3.Connection, workspace: Path, session_id: str) -> int | None: ...
    @staticmethod
    def _read_last_event_sequence(*, connection: sqlite3.Connection, workspace: Path, session_id: str) -> int: ...
    def _read_pending_approval_json(self, *, connection: sqlite3.Connection, workspace: Path, session_id: str) -> str | None: ...
    def _replace_session_todos(self, *, connection: sqlite3.Connection, workspace: Path, session_id: str, metadata: dict[str, object]) -> None: ...
    @classmethod
    def _request_id_from_pending_payload(cls, payload: str | None) -> str | None: ...
    def _resolve_database_path(self) -> Path: ...
    @staticmethod
    def _result_summary(*, response: RuntimeResponse, prompt: str) -> tuple[str, str | None]: ...
    @staticmethod
    def _retained_background_task_session_ids(
        *, connection: sqlite3.Connection, workspace: Path, pruned_task_ids: tuple[str, ...]
    ) -> tuple[str, ...]: ...
    @staticmethod
    def _revert_marker_from_metadata(metadata: dict[str, object]) -> RuntimeSessionRevertMarker | None: ...
    def _run_resume_checkpoint(
        self, *, request: RuntimeRequest, response: RuntimeResponse, last_event_sequence: int | None = None
    ) -> dict[str, object]: ...
    @staticmethod
    def _score_memory(record: MemoryRecord, terms: tuple[str, ...]) -> tuple[int, tuple[str, ...]]: ...
    @staticmethod
    def _session_last_event_sequence(events: tuple[EventEnvelope, ...]) -> int: ...
    def _session_metadata_and_events(
        self, *, connection: sqlite3.Connection, workspace: Path, session_id: str
    ) -> tuple[dict[str, object], tuple[EventEnvelope, ...]]: ...
    def _stop_delegated_reminder_state(
        self, *, existing_payload: str | None, stop_condition: DelegatedReminderStopCondition, stopped_at_unix_ms: int
    ) -> str | None: ...
    def _storage_table_counts(self, *, connection: sqlite3.Connection, workspace: Path) -> dict[str, int]: ...
    def _sync_background_task_durable_state(
        self,
        *,
        connection: sqlite3.Connection,
        workspace: Path,
        request: RuntimeRequest,
        response: RuntimeResponse,
        approval_request_id: str | None = None,
        question_request_id: str | None = None,
    ) -> None: ...
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
    ) -> None: ...
    def _terminal_notification_candidate(
        self, *, request: RuntimeRequest, response: RuntimeResponse, notification_run_id: int, last_event_sequence: int | None = None
    ) -> dict[str, object] | None: ...
    @staticmethod
    def _todo_state_from_events(events: tuple[EventEnvelope, ...]) -> dict[str, object] | None: ...
    @staticmethod
    def _todo_state_from_metadata(metadata: dict[str, object]) -> dict[str, object] | None: ...
    def _todo_state_from_rows(self, *, connection: sqlite3.Connection, workspace: Path, session_id: str) -> dict[str, object] | None: ...
    @staticmethod
    def _tool_results_from_events(events: tuple[EventEnvelope, ...]) -> list[dict[str, object]]: ...
    @staticmethod
    def _unlink_with_retries(path: Path, *, attempts: int = 5, delay_seconds: float = 0.05) -> None: ...
    @staticmethod
    def _validate_memory_content(content: str) -> str: ...
    @classmethod
    def _validate_memory_kind(cls, kind: str) -> MemoryKind: ...
    @staticmethod
    def _validate_memory_tags(tags: tuple[str, ...]) -> tuple[str, ...]: ...
    @staticmethod
    def _wal_checkpoint(*, connection: sqlite3.Connection, mode: str) -> dict[str, int]: ...
    def _write_connect(self, workspace: Path) -> AbstractContextManager[sqlite3.Connection]: ...
    def _write_revert_marker(
        self, *, connection: sqlite3.Connection, workspace: Path, session_id: str, marker: RuntimeSessionRevertMarker | None
    ) -> None: ...
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
    ) -> int: ...
    def get_memory(self, *, workspace: Path, memory_id: str) -> MemoryRecord | None: ...
    def list_memories(self, *, workspace: Path, include_deleted: bool = False) -> tuple[MemoryRecord, ...]: ...
    def load_background_task(self, *, workspace: Path, task_id: str) -> BackgroundTaskState: ...
