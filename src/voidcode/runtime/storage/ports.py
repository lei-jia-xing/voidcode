from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..background.models import (
    BackgroundTaskState,
    BackgroundTaskStatus,
    DelegatedReminderStopCondition,
    StoredBackgroundTaskSummary,
)
from ..background.process import BackgroundProcessPersistence
from ..composition import CompositionRef, FrozenComposition
from ..contracts import RuntimeRequest, RuntimeResponse, RuntimeSessionResult
from ..effectiveness import ToolEffectivenessReport
from ..events import EventEnvelope, EventSource
from ..interaction_queue import QueuedMessageKind, QueuedRuntimeMessage
from ..permission import PendingApproval
from ..question import PendingQuestion
from ..session import (
    SessionEntrySummary,
    SessionStatus,
    StoredSessionForestEntry,
    StoredSessionLineageEntry,
    StoredSessionSummary,
)
from .sessions import SessionEventPage, SessionEventsAfter


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


class SessionEventRepository(SessionEventAppender, Protocol):
    def append_session_events(
        self,
        *,
        workspace: Path,
        session_id: str,
        events: tuple[tuple[str, EventSource, dict[str, object], str | None], ...],
        interrupted_checkpoint: dict[str, object] | None = None,
    ) -> tuple[EventEnvelope, ...]: ...

    def session_path(self, *, workspace: Path, session_id: str, sequence: int | None = None) -> tuple[EventEnvelope, ...]: ...

    def newest_sequence_before(self, *, workspace: Path, session_id: str, sequence: int) -> int | None: ...

    def read_session_events_after(
        self,
        *,
        workspace: Path,
        session_id: str,
        after_sequence: int,
    ) -> SessionEventsAfter: ...

    def read_session_event_page(
        self,
        *,
        workspace: Path,
        session_id: str,
        after_sequence: int,
        limit: int,
        leaf_sequence: int | None = None,
    ) -> SessionEventPage: ...


class SessionRepository(Protocol):
    def list_sessions(self, *, workspace: Path) -> tuple[StoredSessionSummary, ...]: ...

    def has_session(self, *, workspace: Path, session_id: str) -> bool: ...

    def load_session(self, *, workspace: Path, session_id: str) -> RuntimeResponse: ...

    def export_session_bundle_rows(
        self,
        *,
        workspace: Path,
        session_ids: tuple[str, ...],
        task_ids: tuple[str, ...],
    ) -> dict[str, object]: ...

    def import_session_bundle_rows(
        self,
        *,
        workspace: Path,
        sessions: tuple[dict[str, object], ...],
        events: tuple[dict[str, object], ...],
        tasks: tuple[dict[str, object], ...],
        deliveries: tuple[dict[str, object], ...],
    ) -> None: ...

    def update_session_metadata(self, *, workspace: Path, session_id: str, metadata: dict[str, object]) -> None: ...

    def enqueue_session_message(
        self,
        *,
        workspace: Path,
        session_id: str,
        content: str,
        kind: QueuedMessageKind,
        dedupe_key: str | None = None,
    ) -> tuple[dict[str, object], ...]: ...

    def drain_session_messages(
        self,
        *,
        workspace: Path,
        session_id: str,
        kind: QueuedMessageKind,
        remember_dedupe: bool = False,
    ) -> tuple[QueuedRuntimeMessage, ...]: ...

    def load_session_result(self, *, workspace: Path, session_id: str) -> RuntimeSessionResult: ...

    def load_session_status(self, *, workspace: Path, session_id: str) -> SessionStatus: ...

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

    def session_entries(self, *, workspace: Path, session_id: str) -> tuple[SessionEntrySummary, ...]: ...


class SessionRunWriter(Protocol):
    def save_run(
        self,
        *,
        workspace: Path,
        request: RuntimeRequest,
        response: RuntimeResponse,
        clear_pending_approval: bool = True,
        seal_terminal_status: bool = True,
    ) -> None: ...


class SessionRecoveryRepository(Protocol):
    def save_interrupted_checkpoint(
        self,
        *,
        workspace: Path,
        session_id: str,
        prompt: str,
        session_metadata: dict[str, object],
        tool_results: tuple[dict[str, object], ...],
        last_event_sequence: int,
        composition_ref: CompositionRef,
        composition: FrozenComposition | None = None,
        output: str | None = None,
        create_if_missing: bool = True,
        turn: int = 1,
        parent_session_id: str | None = None,
    ) -> None: ...
    def load_resume_checkpoint(self, *, workspace: Path, session_id: str) -> dict[str, object] | None: ...

    def load_execution_composition(self, *, ref: CompositionRef) -> FrozenComposition: ...

    def restore_leaf_after_interrupted_resume(self, *, workspace: Path, session_id: str, sequence: int) -> None: ...

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


class BackgroundTaskRepository(Protocol):
    def create_background_task(
        self,
        *,
        workspace: Path,
        task: BackgroundTaskState,
        composition_ref: CompositionRef,
        composition: FrozenComposition | None = None,
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

    def mark_background_task_idle(self, *, workspace: Path, task_id: str) -> BackgroundTaskState: ...

    def mark_background_task_steered(
        self,
        *,
        workspace: Path,
        task_id: str,
        steer_prompt: str,
    ) -> BackgroundTaskState: ...

    def request_background_task_cancel(self, *, workspace: Path, task_id: str) -> BackgroundTaskState: ...

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


class RuntimeStorageMaintenance(Protocol):
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


@dataclass(frozen=True, slots=True)
class RuntimeRepositories:
    events: SessionEventRepository
    sessions: SessionRepository
    run_writer: SessionRunWriter
    recovery: SessionRecoveryRepository
    tasks: BackgroundTaskRepository
    maintenance: RuntimeStorageMaintenance
    process_persistence: BackgroundProcessPersistence
