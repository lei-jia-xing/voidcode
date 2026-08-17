from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import uuid4

from ..hook.config import RuntimeHookSurface
from ..hook.executor import LifecycleHookExecutionRequest, run_lifecycle_hooks
from ..provider.models import ResolvedProviderConfig
from .acp import append_parent_acp_delegated_lifecycle_event, publish_delegated_acp_event
from .active_session import ACTIVE_SESSION_REGISTRY
from .child_terminal import child_terminal_outcome, child_transcript_proves_completed
from .config import RuntimeConfig
from .contracts import (
    BackgroundTaskResult,
    InternalRuntimeRequestMetadata,
    RuntimeRequest,
    RuntimeRequestError,
    RuntimeRequestMetadataPayload,
    RuntimeResponse,
    RuntimeSessionResult,
    UnknownSessionError,
)
from .events import (
    RUNTIME_BACKGROUND_TASK_AWAITING_STEER,
    RUNTIME_BACKGROUND_TASK_CANCELLED,
    RUNTIME_BACKGROUND_TASK_COMPLETED,
    RUNTIME_BACKGROUND_TASK_FAILED,
    RUNTIME_BACKGROUND_TASK_GROUP_COMPLETED,
    RUNTIME_BACKGROUND_TASK_INTERRUPTED,
    RUNTIME_BACKGROUND_TASK_WAITING_APPROVAL,
    RUNTIME_FAILED,
    RUNTIME_PROVIDER_FALLBACK,
    RUNTIME_SESSION_IDLE,
    RUNTIME_TOOL_COMPLETED,
    EventEnvelope,
)
from .execution_seams import resolve_runtime_session_routing
from .hook_runtime import HOOK_RECURSION_ENV_VAR, hook_execution_policy_from_metadata
from .permission_policy import approval_request_id_from_waiting_response
from .runtime_debug import prompt_from_events
from .session import SessionState, reload_persisted_session, validate_session_workspace
from .session_metadata_helpers import waiting_reason_from_session
from .storage import SessionEventAppender, SessionSealedError, SessionStore
from .task import (
    BACKGROUND_TASK_TERMINAL_STATUSES,
    BackgroundTaskConcurrencyObservability,
    BackgroundTaskObservability,
    BackgroundTaskRef,
    BackgroundTaskRequestSnapshot,
    BackgroundTaskRetryObservability,
    BackgroundTaskState,
    BackgroundTaskStatus,
    StoredBackgroundTaskSummary,
    is_background_task_terminal,
    validate_background_task_id,
)

if TYPE_CHECKING:
    from .acp import AcpAdapter
    from .runtime_surface import RuntimeSurface

logger = logging.getLogger(__name__)

_BACKGROUND_TASK_RATE_LIMIT_RETRIES = 2
_BACKGROUND_TASK_RATE_LIMIT_BASE_BACKOFF_SECONDS = 0.05
_RUNTIME_BACKGROUND_TASK_IDLE_REMINDER = "runtime.background_task_idle_reminder"

# Per-task outcomes returned by ``_drain_background_task_queue``.
_BACKGROUND_TASK_DRAIN_DISPATCHED = "dispatched"
_BACKGROUND_TASK_DRAIN_BLOCKED_CONCURRENCY = "blocked-concurrency"
_BACKGROUND_TASK_DRAIN_BLOCKED_SHUTDOWN = "blocked-shutdown"
_BACKGROUND_TASK_DRAIN_ROUTING_FAILED = "routing-failed"

# Waiting reasons surfaced through ``BackgroundTaskObservability.waiting_reason``
# for queued tasks that the drain could not dispatch.
_QUEUED_WAITING_REASON_QUEUED = "queued"
_QUEUED_WAITING_REASON_CONCURRENCY = "concurrency_limit"
_QUEUED_WAITING_REASON_SHUTDOWN = "blocked"


@dataclass(frozen=True, slots=True)
class _BackgroundTaskConcurrencyIdentity:
    provider: str
    model: str
    limit: int
    limit_source: str

    @property
    def model_key(self) -> str:
        return f"{self.provider}/{self.model}"


@dataclass(frozen=True, slots=True)
class _BackgroundTaskConcurrencySnapshot:
    provider: str
    model: str
    limit: int
    limit_source: str
    running_provider: int
    running_model: int
    running_total: int
    queued_provider: int
    queued_model: int
    queued_total: int

    def as_payload(self) -> dict[str, object]:
        return self.as_observability().as_payload()

    def as_observability(self) -> BackgroundTaskConcurrencyObservability:
        return BackgroundTaskConcurrencyObservability(
            provider=self.provider,
            model=self.model,
            limit=self.limit,
            limit_source=self.limit_source,
            running_provider=self.running_provider,
            running_model=self.running_model,
            running_total=self.running_total,
            active_worker_slots=self.running_total,
            queued_provider=self.queued_provider,
            queued_model=self.queued_model,
            queued_total=self.queued_total,
        )


@dataclass(frozen=True, slots=True)
class _BackgroundTaskRetrySnapshot:
    retry_count: int
    max_retries: int
    backoff_seconds: float
    next_retry_at: int | None

    def as_observability(self) -> BackgroundTaskRetryObservability:
        return BackgroundTaskRetryObservability(
            retry_count=self.retry_count,
            max_retries=self.max_retries,
            backoff_seconds=self.backoff_seconds,
            next_retry_at=self.next_retry_at,
        )


@dataclass(frozen=True, slots=True)
class _BackgroundTaskObservabilityContext:
    queued_positions: dict[str, int]
    queued_provider_counts: dict[str, int]
    queued_model_counts: dict[str, int]
    queued_total: int
    running_provider_counts: dict[str, int]
    running_model_counts: dict[str, int]
    running_total: int
    retries: dict[str, _BackgroundTaskRetrySnapshot]


class RuntimeBackgroundTaskSupervisor:
    def __init__(
        self,
        surface: RuntimeSurface,
        *,
        session_store: SessionStore,
        workspace: Path,
        config: RuntimeConfig,
        acp_adapter: AcpAdapter,
    ) -> None:
        self._surface = surface
        self._session_store = session_store
        self._workspace = workspace
        self._config = config
        self._acp_adapter = acp_adapter
        self._queue_lock = threading.RLock()
        self._slot_available = threading.Condition(self._queue_lock)
        self._threads: dict[str, threading.Thread] = {}
        self._shutdown_requested = False
        self._reconciled = False
        self._provider_running_counts: dict[str, int] = {}
        self._model_running_counts: dict[str, int] = {}
        self._rate_limit_retries: dict[str, _BackgroundTaskRetrySnapshot] = {}
        # task_id -> waiting reason for queued tasks the drain could not
        # dispatch (e.g. concurrency limit, runtime shutdown). Consulted by
        # ``_waiting_reason`` so reads surface *why* a task is queued instead
        # of a generic "queued".
        self._queued_waiting_reasons: dict[str, str] = {}

    @property
    def threads(self) -> dict[str, threading.Thread]:
        return self._threads

    @threads.setter
    def threads(self, value: dict[str, threading.Thread]) -> None:
        self._threads = value

    @property
    def shutdown_requested(self) -> bool:
        return self._shutdown_requested

    @shutdown_requested.setter
    def shutdown_requested(self, value: bool) -> None:
        self._shutdown_requested = value

    @property
    def reconciled(self) -> bool:
        return self._reconciled

    @reconciled.setter
    def reconciled(self, value: bool) -> None:
        self._reconciled = value

    def shutdown(self, *, timeout_seconds: float = 2.0) -> None:
        """Drain background-task workers durably before teardown.

        Enforced ordering (see also ``VoidCodeRuntime.__exit__``):

        1. Set ``_shutdown_requested`` so the queue dispatcher stops starting
           new workers; every already-started worker still runs to completion.
        2. Join every live worker thread. Each worker finalizes its task
           durably (terminal task row, child-session truth, parent-session
           notification, lifecycle hooks) BEFORE the thread exits, so a joined
           worker's writes are committed when this loop sees it dead.
        3. If the timeout expires, terminalize (mark ``failed`` in storage)
           every worker that could not finish, so no in-flight background-task
           result is lost to teardown.

        After this returns, every dispatched task row is terminal and all
        child/background-task results are durable.
        """
        self._shutdown_requested = True
        deadline = time.monotonic() + max(timeout_seconds, 0.0)
        timed_out = False
        while not timed_out:
            with self._queue_lock:
                threads = tuple(self._threads.items())
            if not threads:
                break
            for task_id, thread in threads:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._fail_unfinished_shutdown_threads(threads)
                    timed_out = True
                    break
                thread.join(timeout=min(remaining, 0.1))
                if not thread.is_alive():
                    with self._queue_lock:
                        if self._threads.get(task_id) is thread:
                            self._threads.pop(task_id, None)
        # Terminalize still-queued (never-started) tasks so no cross-process
        # ``queued`` orphans survive a runtime teardown.
        self._terminalize_queued_tasks_for_shutdown()
        # Keep-alive tasks parked ``idle`` (awaiting steer) own no worker
        # thread, so nothing else will finalize them; mark them ``interrupted``
        # (resumable — child session and transcript stay intact) so no
        # cross-process ``idle`` orphans survive a runtime teardown.
        self._terminalize_idle_keep_alive_tasks_for_shutdown()

    def _terminalize_idle_keep_alive_tasks_for_shutdown(self) -> tuple[str, ...]:
        """Mark every idle keep-alive task ``interrupted`` durably.

        Called by ``shutdown``: an idle keep-alive task is parked awaiting
        steer with no worker thread, so it can never be dispatched again after
        teardown and must not survive as an ``idle`` orphan. ``interrupted``
        is the resumable terminal status — the child session and its
        transcript stay intact and the leader continues via the ``task`` tool
        ``session_id`` continuation or ``tasks retry`` after restart.
        """
        terminalized: list[str] = []
        for summary in self._session_store.list_background_tasks(workspace=self._workspace):
            if summary.status != "idle" or not summary.keep_alive:
                continue
            try:
                terminal_task = self._session_store.mark_background_task_terminal(
                    workspace=self._workspace,
                    task_id=summary.task.id,
                    status="interrupted",
                    error="runtime exited while keep-alive worker was awaiting steer",
                )
            except Exception as exc:
                if "unknown background task" in str(exc):
                    continue
                logger.exception(
                    "background task %s could not persist shutdown interruption state",
                    summary.task.id,
                )
                continue
            if terminal_task.status != "interrupted":
                # A concurrent steer/cancel won the transition race; the
                # winning path owns finalization, so do not emit a stale
                # shutdown lifecycle hook for it.
                continue
            with self._queue_lock:
                self._queued_waiting_reasons.pop(summary.task.id, None)
            terminalized.append(terminal_task.task.id)
            self.run_background_task_lifecycle_hook(terminal_task)
        return tuple(terminalized)

    def _terminalize_queued_tasks_for_shutdown(self) -> tuple[str, ...]:
        """Mark every still-queued task terminal (``interrupted``) durably.

        Called by ``shutdown`` and by the queue drain once the supervisor is
        shutting down: a queued task can never be dispatched again, so it must
        not survive as a ``queued`` orphan. ``interrupted`` is the resumable
        terminal status (retry is allowed), matching
        ``_mark_background_task_interrupted_before_worker``.
        """
        terminalized: list[str] = []
        for summary in self._session_store.list_background_tasks(workspace=self._workspace):
            if summary.status != "queued":
                continue
            try:
                terminal_task = self._session_store.mark_background_task_terminal(
                    workspace=self._workspace,
                    task_id=summary.task.id,
                    status="interrupted",
                    error="runtime shutdown requested before delegated worker execution started",
                )
            except Exception as exc:
                if "unknown background task" in str(exc):
                    continue
                logger.exception(
                    "background task %s could not persist shutdown interruption state",
                    summary.task.id,
                )
                continue
            with self._queue_lock:
                self._queued_waiting_reasons.pop(summary.task.id, None)
            terminalized.append(terminal_task.task.id)
            self.run_background_task_lifecycle_hook(terminal_task)
        return tuple(terminalized)

    def _fail_unfinished_shutdown_threads(self, threads: tuple[tuple[str, threading.Thread], ...]) -> None:
        for task_id, thread in threads:
            if not thread.is_alive():
                with self._queue_lock:
                    if self._threads.get(task_id) is thread:
                        self._threads.pop(task_id, None)
                continue
            try:
                task = self._session_store.load_background_task(
                    workspace=self._workspace,
                    task_id=task_id,
                )
                keep_alive = task.keep_alive
                terminal_task = self._session_store.mark_background_task_terminal(
                    workspace=self._workspace,
                    task_id=task_id,
                    status="interrupted" if keep_alive else "failed",
                    error=(
                        "runtime exited during keep-alive worker turn"
                        if keep_alive
                        else "background task stopped because parent runtime exited before completion"
                    ),
                )
                task = terminal_task
            except Exception as exc:
                if "unknown background task" in str(exc):
                    logger.debug(
                        "background task %s disappeared before shutdown finalization: %s",
                        task_id,
                        exc,
                    )
                    continue
                logger.exception(
                    "background task %s could not persist shutdown failure state",
                    task_id,
                )
                continue
            self.run_background_task_lifecycle_hook(task)

    def task_observability(
        self,
        task: BackgroundTaskState,
        *,
        context: _BackgroundTaskObservabilityContext | None = None,
    ) -> BackgroundTaskObservability:
        try:
            concurrency = self._concurrency_observability(task, context=context)
        except (RuntimeRequestError, ValueError):
            concurrency = None
        retry = self._retry_observability(task.task.id, context=context)
        return BackgroundTaskObservability(
            waiting_reason=self._waiting_reason(task=task, retry=retry),
            terminal_reason=self._terminal_reason(task),
            queue_position=self._queue_position(task, context=context),
            concurrency=concurrency,
            retry=retry,
        )

    def task_with_observability(self, task: BackgroundTaskState) -> BackgroundTaskState:
        queued_summaries = self._session_store.list_queued_background_tasks(workspace=self._workspace)
        tasks_by_id = {task.task.id: task}
        for summary in queued_summaries:
            if summary.task.id in tasks_by_id:
                continue
            tasks_by_id[summary.task.id] = self._session_store.load_background_task(
                workspace=self._workspace,
                task_id=summary.task.id,
            )
        context = self._observability_context(
            queued_summaries=queued_summaries,
            tasks_by_id=tasks_by_id,
        )
        return replace(task, observability=self.task_observability(task, context=context))

    def _load_background_task(self, task_id: str) -> BackgroundTaskState:
        """Replicate ``VoidCodeRuntime.load_background_task``'s canonical load.

        The runtime method delegates straight back to this supervisor
        (reconcile + drain + ``SessionStore.load_background_task`` +
        ``task_with_observability``); inlining keeps the surface narrow without
        changing behavior.
        """
        self.reconcile_background_tasks_if_needed()
        self.drain_queued_background_tasks()
        validate_background_task_id(task_id)
        task = self._session_store.load_background_task(workspace=self._workspace, task_id=task_id)
        return self.task_with_observability(task)

    def summary_with_observability(self, summary: StoredBackgroundTaskSummary) -> StoredBackgroundTaskSummary:
        task = self._session_store.load_background_task(
            workspace=self._workspace,
            task_id=summary.task.id,
        )
        return replace(summary, observability=self.task_observability(task))

    def summaries_with_observability(
        self,
        summaries: tuple[StoredBackgroundTaskSummary, ...],
    ) -> tuple[StoredBackgroundTaskSummary, ...]:
        if not summaries:
            return ()
        queued_summaries = self._session_store.list_queued_background_tasks(workspace=self._workspace)
        task_ids_to_load = {summary.task.id for summary in summaries}
        task_ids_to_load.update(summary.task.id for summary in queued_summaries)
        tasks_by_id = {
            task_id: self._session_store.load_background_task(
                workspace=self._workspace,
                task_id=task_id,
            )
            for task_id in task_ids_to_load
        }
        context = self._observability_context(
            queued_summaries=queued_summaries,
            tasks_by_id=tasks_by_id,
        )
        return tuple(
            replace(
                summary,
                observability=self.task_observability(
                    tasks_by_id[summary.task.id],
                    context=context,
                ),
            )
            for summary in summaries
        )

    def status_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for summary in self._session_store.list_background_tasks(workspace=self._workspace):
            counts[summary.status] = counts.get(summary.status, 0) + 1
        return counts

    def active_worker_slots(self) -> int:
        with self._queue_lock:
            return sum(self._provider_running_counts.values())

    def _retry_observability(
        self,
        task_id: str,
        *,
        context: _BackgroundTaskObservabilityContext | None = None,
    ) -> BackgroundTaskRetryObservability | None:
        if context is not None:
            retry = context.retries.get(task_id)
        else:
            with self._queue_lock:
                retry = self._rate_limit_retries.get(task_id)
        return None if retry is None else retry.as_observability()

    def _queue_position(
        self,
        task: BackgroundTaskState,
        *,
        context: _BackgroundTaskObservabilityContext | None = None,
    ) -> int | None:
        if task.status != "queued":
            return None
        if context is not None:
            return context.queued_positions.get(task.task.id)
        with self._queue_lock:
            queued = [summary.task.id for summary in self._session_store.list_queued_background_tasks(workspace=self._workspace)]
        try:
            return queued.index(task.task.id) + 1
        except ValueError:
            return None

    def _concurrency_observability(
        self,
        task: BackgroundTaskState,
        *,
        context: _BackgroundTaskObservabilityContext | None = None,
    ) -> BackgroundTaskConcurrencyObservability:
        if context is None:
            return self._concurrency_snapshot(task).as_observability()
        identity = self._concurrency_identity_for_task(task)
        return BackgroundTaskConcurrencyObservability(
            provider=identity.provider,
            model=identity.model,
            limit=identity.limit,
            limit_source=identity.limit_source,
            running_provider=context.running_provider_counts.get(identity.provider, 0),
            running_model=context.running_model_counts.get(identity.model_key, 0),
            running_total=context.running_total,
            active_worker_slots=context.running_total,
            queued_provider=context.queued_provider_counts.get(identity.provider, 0),
            queued_model=context.queued_model_counts.get(identity.model_key, 0),
            queued_total=context.queued_total,
        )

    def _observability_context(
        self,
        *,
        queued_summaries: tuple[StoredBackgroundTaskSummary, ...],
        tasks_by_id: dict[str, BackgroundTaskState],
    ) -> _BackgroundTaskObservabilityContext:
        queued_positions = {summary.task.id: index for index, summary in enumerate(queued_summaries, start=1)}
        queued_provider_counts: dict[str, int] = {}
        queued_model_counts: dict[str, int] = {}
        for summary in queued_summaries:
            task = tasks_by_id.get(summary.task.id)
            if task is None:
                continue
            try:
                identity = self._concurrency_identity_for_task(task)
            except (RuntimeRequestError, ValueError):
                continue
            queued_provider_counts[identity.provider] = queued_provider_counts.get(identity.provider, 0) + 1
            queued_model_counts[identity.model_key] = queued_model_counts.get(identity.model_key, 0) + 1
        with self._queue_lock:
            return _BackgroundTaskObservabilityContext(
                queued_positions=queued_positions,
                queued_provider_counts=queued_provider_counts,
                queued_model_counts=queued_model_counts,
                queued_total=len(queued_summaries),
                running_provider_counts=dict(self._provider_running_counts),
                running_model_counts=dict(self._model_running_counts),
                running_total=sum(self._provider_running_counts.values()),
                retries=dict(self._rate_limit_retries),
            )

    @staticmethod
    def _terminal_reason(task: BackgroundTaskState) -> str | None:
        if task.status == "completed":
            return "completed"
        if task.status == "failed":
            return task.error or "failed"
        if task.status == "cancelled":
            return task.cancellation_cause or task.error or "cancelled"
        if task.status == "interrupted":
            return task.error or "interrupted"
        return None

    def _waiting_reason(
        self,
        *,
        task: BackgroundTaskState,
        retry: BackgroundTaskRetryObservability | None,
    ) -> str:
        if task.status == "queued":
            with self._queue_lock:
                return self._queued_waiting_reasons.get(task.task.id, _QUEUED_WAITING_REASON_QUEUED)
        if task.status == "idle":
            return "awaiting_steer"
        if task.status == "running" and retry is not None:
            return "rate_limited"
        if task.status == "running" and task.cancel_requested_at is not None:
            return "cancel_requested"
        if task.status == "running" and task.approval_request_id is not None:
            return "approval_blocked"
        if task.status == "running" and task.question_request_id is not None:
            return "question_blocked"
        return task.status

    def start_background_task(self, request: RuntimeRequest) -> BackgroundTaskState:
        self.reconcile_background_tasks_if_needed()
        task_id = f"task-{uuid4().hex}"
        initial_state = BackgroundTaskState(
            task=BackgroundTaskRef(id=task_id),
            status="queued",
            request=BackgroundTaskRequestSnapshot(
                prompt=request.prompt,
                session_id=request.session_id,
                parent_session_id=request.parent_session_id,
                metadata={key: value for key, value in request.metadata.items()},
                allocate_session_id=request.allocate_session_id,
            ),
        )
        self._session_store.create_background_task(workspace=self._workspace, task=initial_state)
        registered_task = self._session_store.load_background_task(
            workspace=self._workspace,
            task_id=task_id,
        )
        self.run_background_task_lifecycle_surface(
            task=registered_task,
            surface="background_task_registered",
            session_id=registered_task.parent_session_id or registered_task.request.session_id or "runtime",
        )
        self._drain_background_task_queue()
        return self._load_background_task(task_id)

    def retry_background_task(self, task_id: str) -> BackgroundTaskState:
        self.reconcile_background_tasks_if_needed()
        validate_background_task_id(task_id)
        previous_task = self._session_store.load_background_task(
            workspace=self._workspace,
            task_id=task_id,
        )
        if previous_task.status not in ("failed", "cancelled", "interrupted"):
            raise ValueError(f"background task retry requires a failed, cancelled, or interrupted task; task {task_id} is {previous_task.status}")
        self._session_store.stop_background_task_idle_reminder(
            workspace=self._workspace,
            task_id=task_id,
            stop_condition="explicit_retry",
        )
        return self.start_background_task(
            RuntimeRequest(
                prompt=previous_task.request.prompt,
                session_id=previous_task.request.session_id,
                parent_session_id=previous_task.request.parent_session_id,
                metadata=cast(RuntimeRequestMetadataPayload, previous_task.request.metadata),
                allocate_session_id=previous_task.request.allocate_session_id,
            )
        )

    def steer_background_task(self, task_id: str, content: str) -> BackgroundTaskState:
        """Dispatch a new worker turn for a keep-alive background task.

        Validates that the task is keep-alive and parked (``idle``, or
        ``interrupted`` as a resumable breakpoint after a process restart),
        persists the steer prompt (``mark_background_task_steered`` flips the
        row ``idle|interrupted → running``), reserves a concurrency slot and
        spawns a fresh start-gated worker thread through the same dispatch
        path as the queue drain — the slot is reserved once at dispatch and
        released once in the worker's ``finally``.

        Raises ``ValueError`` when the task is not keep-alive, is not parked
        (a turn is in flight — v1 has no steer pipelining), the content is
        empty, or the provider/model concurrency limit is exhausted (the task
        stays parked and the steer may be retried).
        """
        validate_background_task_id(task_id)
        if not isinstance(content, str) or not content.strip():
            raise ValueError("background task steer requires non-empty content")
        self.reconcile_background_tasks_if_needed()
        current_task = self._session_store.load_background_task(
            workspace=self._workspace,
            task_id=task_id,
        )
        if not current_task.keep_alive:
            raise ValueError(f"background task {task_id} is not a keep-alive task and cannot be steered")
        if current_task.status not in ("idle", "interrupted"):
            raise ValueError(f"background task {task_id} can only be steered while idle or interrupted; task is {current_task.status}")
        request = RuntimeRequest(
            prompt=current_task.request.prompt,
            session_id=current_task.request.session_id,
            parent_session_id=current_task.request.parent_session_id,
            metadata=cast(RuntimeRequestMetadataPayload, current_task.request.metadata),
            allocate_session_id=current_task.request.allocate_session_id,
        )
        identity = self._concurrency_identity_for_request(request)
        with self._queue_lock:
            if not self._can_start_task(identity):
                raise ValueError(f"background task {task_id} steer blocked by the provider/model concurrency limit; retry when a worker slot frees")
            self._reserve_slot(identity)
        # Register the worker thread BEFORE flipping the row to ``running`` so
        # a concurrent drain orphan-scan never sees a ``running`` row without
        # an owner while this steer dispatch is in flight.
        worker, worker_start_gate = self._spawn_worker_thread(
            task_id=task_id,
            reserved_identity=identity,
        )
        steered_task = self._session_store.mark_background_task_steered(
            workspace=self._workspace,
            task_id=task_id,
            steer_prompt=content.strip(),
        )
        if steered_task.status != "running":
            # Raced to a terminal state (e.g. a concurrent cancel) between
            # validation and dispatch; undo the reserved slot and thread.
            with self._queue_lock:
                self._threads.pop(task_id, None)
                self._release_slot(identity)
            return self.task_with_observability(steered_task)
        try:
            worker.start()
        except RuntimeError as exc:
            with self._queue_lock:
                self._threads.pop(task_id, None)
                self._release_slot(identity)
            failed_task = self._session_store.mark_background_task_terminal(
                workspace=self._workspace,
                task_id=task_id,
                status="failed",
                error=str(exc),
            )
            self.run_background_task_lifecycle_hook(failed_task)
            return self.task_with_observability(failed_task)
        try:
            self.run_background_task_lifecycle_surface(
                task=steered_task,
                surface="background_task_started",
                session_id=(steered_task.session_id or steered_task.parent_session_id or "runtime"),
            )
        finally:
            worker_start_gate.set()
        return self.task_with_observability(steered_task)

    def _concurrency_identity_for_request(self, request: RuntimeRequest) -> _BackgroundTaskConcurrencyIdentity:
        effective_config = self._surface.runtime_config_for_request(request)
        return self._concurrency_identity_for_resolved_provider(
            effective_config.resolved_provider,
        )

    def _concurrency_identity_for_resolved_provider(self, resolved_provider: ResolvedProviderConfig) -> _BackgroundTaskConcurrencyIdentity:
        target = resolved_provider.active_target
        provider = target.selection.provider or "deterministic"
        model = target.selection.model or target.selection.raw_model or "deterministic"
        return self._concurrency_identity_for_provider_model(provider=provider, model=model)

    def _concurrency_identity_for_provider_model(
        self,
        *,
        provider: str,
        model: str,
    ) -> _BackgroundTaskConcurrencyIdentity:
        model_key = f"{provider}/{model}"
        background_task_config = self._config.background_task
        model_limit = background_task_config.model_concurrency.get(model_key)
        if model_limit is not None:
            return _BackgroundTaskConcurrencyIdentity(
                provider=provider,
                model=model,
                limit=model_limit,
                limit_source="model",
            )
        provider_limit = background_task_config.provider_concurrency.get(provider)
        if provider_limit is not None:
            return _BackgroundTaskConcurrencyIdentity(
                provider=provider,
                model=model,
                limit=provider_limit,
                limit_source="provider",
            )
        return _BackgroundTaskConcurrencyIdentity(
            provider=provider,
            model=model,
            limit=background_task_config.default_concurrency,
            limit_source="default",
        )

    def _fallback_identity_for_event(
        self,
        event: EventEnvelope,
    ) -> _BackgroundTaskConcurrencyIdentity | None:
        if event.event_type != RUNTIME_PROVIDER_FALLBACK:
            return None
        provider = event.payload.get("to_provider")
        model = event.payload.get("to_model")
        if not isinstance(provider, str) or not provider:
            return None
        if not isinstance(model, str) or not model:
            return None
        return self._concurrency_identity_for_provider_model(provider=provider, model=model)

    def _concurrency_identity_for_task(self, task: BackgroundTaskState) -> _BackgroundTaskConcurrencyIdentity:
        request = RuntimeRequest(
            prompt=task.request.prompt,
            session_id=task.request.session_id,
            parent_session_id=task.request.parent_session_id,
            metadata=cast(RuntimeRequestMetadataPayload, task.request.metadata),
            allocate_session_id=task.request.allocate_session_id,
        )
        return self._concurrency_identity_for_request(request)

    def _can_start_task(self, identity: _BackgroundTaskConcurrencyIdentity) -> bool:
        running_provider = self._provider_running_counts.get(identity.provider, 0)
        running_model = self._model_running_counts.get(identity.model_key, 0)
        if identity.limit_source == "model":
            return running_model < identity.limit
        if identity.limit_source == "provider":
            return running_provider < identity.limit
        return sum(self._provider_running_counts.values()) < identity.limit

    def _reserve_slot(self, identity: _BackgroundTaskConcurrencyIdentity) -> None:
        self._provider_running_counts[identity.provider] = self._provider_running_counts.get(identity.provider, 0) + 1
        self._model_running_counts[identity.model_key] = self._model_running_counts.get(identity.model_key, 0) + 1

    def _release_slot(self, identity: _BackgroundTaskConcurrencyIdentity) -> None:
        provider_count = max(0, self._provider_running_counts.get(identity.provider, 0) - 1)
        model_count = max(0, self._model_running_counts.get(identity.model_key, 0) - 1)
        if provider_count:
            self._provider_running_counts[identity.provider] = provider_count
        else:
            self._provider_running_counts.pop(identity.provider, None)
        if model_count:
            self._model_running_counts[identity.model_key] = model_count
        else:
            self._model_running_counts.pop(identity.model_key, None)
        self._slot_available.notify_all()

    def _task_cancel_requested(self, task_id: str) -> bool:
        task = self._session_store.load_background_task(
            workspace=self._workspace,
            task_id=task_id,
        )
        return task.status == "cancelled" or task.cancel_requested_at is not None

    def _mark_background_task_cancelled_during_retry_wait(
        self,
        *,
        task_id: str,
    ) -> None:
        terminal_task = self._session_store.mark_background_task_terminal(
            workspace=self._workspace,
            task_id=task_id,
            status="cancelled",
            error="cancelled by parent during delegated execution",
        )
        self.run_background_task_lifecycle_hook(terminal_task)

    def _wait_for_rate_limit_backoff_or_cancel(
        self,
        *,
        task_id: str,
        retry_count: int,
    ) -> bool:
        backoff_seconds = self._rate_limit_backoff_seconds(retry_count)
        deadline = time.monotonic() + backoff_seconds
        next_retry_at = int((time.time() + backoff_seconds) * 1000)
        with self._queue_lock:
            self._rate_limit_retries[task_id] = _BackgroundTaskRetrySnapshot(
                retry_count=retry_count,
                max_retries=_BACKGROUND_TASK_RATE_LIMIT_RETRIES,
                backoff_seconds=backoff_seconds,
                next_retry_at=next_retry_at,
            )
        try:
            while True:
                if self._task_cancel_requested(task_id):
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                time.sleep(min(remaining, 0.05))
        finally:
            with self._queue_lock:
                self._rate_limit_retries.pop(task_id, None)

    def _wait_for_slot_or_cancel(
        self,
        *,
        task_id: str,
        identity: _BackgroundTaskConcurrencyIdentity,
    ) -> bool:
        with self._slot_available:
            while not self._can_start_task(identity):
                if self._task_cancel_requested(task_id):
                    return True
                self._slot_available.wait(timeout=0.5)
            self._reserve_slot(identity)
            return False

    def _queued_counts_for_identity(self, identity: _BackgroundTaskConcurrencyIdentity) -> tuple[int, int, int]:
        queued_provider = 0
        queued_model = 0
        queued_total = 0
        for summary in self._session_store.list_queued_background_tasks(workspace=self._workspace):
            queued_total += 1
            task = self._session_store.load_background_task(
                workspace=self._workspace,
                task_id=summary.task.id,
            )
            task_identity = self._concurrency_identity_for_task(task)
            if task_identity.provider == identity.provider:
                queued_provider += 1
            if task_identity.model_key == identity.model_key:
                queued_model += 1
        return queued_provider, queued_model, queued_total

    def _concurrency_snapshot(self, task: BackgroundTaskState) -> _BackgroundTaskConcurrencySnapshot:
        identity = self._concurrency_identity_for_task(task)
        with self._queue_lock:
            queued_provider, queued_model, queued_total = self._queued_counts_for_identity(identity)
            return _BackgroundTaskConcurrencySnapshot(
                provider=identity.provider,
                model=identity.model,
                limit=identity.limit,
                limit_source=identity.limit_source,
                running_provider=self._provider_running_counts.get(identity.provider, 0),
                running_model=self._model_running_counts.get(identity.model_key, 0),
                running_total=sum(self._provider_running_counts.values()),
                queued_provider=queued_provider,
                queued_model=queued_model,
                queued_total=queued_total,
            )

    def _concurrency_payload_for_event(self, task: BackgroundTaskState) -> dict[str, object]:
        if self._config.background_task.default_concurrency == 5 and not (
            self._config.background_task.provider_concurrency or self._config.background_task.model_concurrency
        ):
            return {}
        try:
            return {"concurrency": self._concurrency_snapshot(task).as_payload()}
        except (RuntimeRequestError, ValueError):
            return {}

    def drain_queued_background_tasks(self) -> None:
        """Idempotent read-path re-dispatch of queued background tasks.

        Safe to call from any read/status surface (``load_background_task``,
        ``load_background_task_result``, ``list_background_tasks``, status
        snapshots, ``background_output`` polling): the underlying drain skips
        non-queued tasks and tasks that already own a worker thread, and
        consults the live concurrency counts, so a task left queued by an
        earlier drain is re-attempted instead of being stranded forever.
        """
        self._drain_background_task_queue()

    def _parent_session_is_terminal(self, parent_session_id: str | None) -> bool:
        """True when the parent session is durably terminal (completed/failed).

        An unknown parent session (row gone) is treated as terminal: the task
        is an orphan that can never be owned again. ``interrupted`` parents
        are intentionally NOT terminal — an interrupted session may be resumed.
        """
        if parent_session_id is None:
            return False
        try:
            status = self._session_store.load_session_status(
                workspace=self._workspace,
                session_id=parent_session_id,
            )
        except UnknownSessionError:
            return True
        except Exception as exc:
            logger.debug("background task parent status check failed: %s", exc)
            return False
        return status in {"completed", "failed"}

    def _drain_background_task_queue(self) -> dict[str, str]:
        """Dispatch as many queued tasks as concurrency currently allows.

        Idempotent: tasks that are no longer ``queued`` or that already own a
        worker thread are skipped, so read/status paths may call this freely.
        Returns a ``task_id -> outcome`` map for every queued task considered
        by this pass:

        * ``dispatched`` — a worker thread was started for the task
        * ``blocked-concurrency`` — the task stayed queued because the
          provider/model concurrency limit is exhausted
        * ``blocked-shutdown`` — the task was terminalized because the runtime
          is shutting down and will never dispatch again
        * ``routing-failed`` — the task was terminalized because its routing
          could not be resolved
        """
        outcomes: dict[str, str] = {}
        if self._shutdown_requested:
            for task_id in self._terminalize_queued_tasks_for_shutdown():
                outcomes[task_id] = _BACKGROUND_TASK_DRAIN_BLOCKED_SHUTDOWN
            return outcomes
        failed_tasks: list[BackgroundTaskState] = []
        started_tasks: list[
            tuple[
                BackgroundTaskState,
                threading.Thread,
                _BackgroundTaskConcurrencyIdentity,
                threading.Event,
            ]
        ] = []
        with self._queue_lock:
            if self._reconciled:
                # Deterministic convergence for tasks stuck ``running``: a
                # ``running`` row must be owned by a live worker thread. A row
                # without one means the worker exited without a terminal update
                # (crashed thread, failed finalization, or a process restart
                # already handled by reconcile) — terminalize it as
                # ``interrupted`` (retryable/repairable) instead of leaving it
                # ``running`` forever. Tasks whose child session is durably
                # ``waiting`` on approval/question are exempt: they survive
                # process restarts by design so the user can still answer.
                # Uses the status-indexed running scan (bounded by concurrency)
                # so single-task loads never scan full task history.
                for summary in self._session_store.list_running_background_tasks(workspace=self._workspace):
                    if summary.task.id in self._threads:
                        continue
                    orphan_task = self._session_store.load_background_task(
                        workspace=self._workspace,
                        task_id=summary.task.id,
                    )
                    # Keep-alive steer in flight: ``steer_background_task``
                    # registers the worker thread before flipping the row to
                    # ``running``, but a dispatch race or a failed spawn can
                    # still leave a ``running`` row carrying an unconsumed
                    # steer prompt for an instant. Treat it as in-transit
                    # instead of terminalizing a live steer (a stuck row is
                    # still caught by reconcile on the next process start).
                    if orphan_task.keep_alive and orphan_task.steer_prompt is not None:
                        continue
                    # Exemption mirroring ``fail_incomplete_background_tasks``:
                    # a running task whose child is durably waiting on a pending
                    # approval/question survives process restarts (the user must
                    # still answer). A bare ``waiting`` row without a pending
                    # payload is non-canonical and gets terminalized.
                    child_response = self.load_background_task_child_response(task=orphan_task)
                    if child_response is not None and child_response.session.status == "waiting" and orphan_task.session_id is not None:
                        store = self._session_store
                        pending_approval = store.load_pending_approval(
                            workspace=self._workspace,
                            session_id=orphan_task.session_id,
                        )
                        pending_question = store.load_pending_question(
                            workspace=self._workspace,
                            session_id=orphan_task.session_id,
                        )
                        if pending_approval is not None or pending_question is not None:
                            continue
                    terminal_orphan = self._session_store.mark_background_task_terminal(
                        workspace=self._workspace,
                        task_id=summary.task.id,
                        status="interrupted",
                        error="background task worker exited before a terminal update",
                    )
                    self._queued_waiting_reasons.pop(summary.task.id, None)
                    failed_tasks.append(terminal_orphan)
            summaries = sorted(
                self._session_store.list_queued_background_tasks(workspace=self._workspace),
                key=lambda summary: (summary.created_at, summary.task.id),
            )
            queued_ids = {summary.task.id for summary in summaries}
            for task_id in tuple(self._queued_waiting_reasons):
                if task_id not in queued_ids:
                    self._queued_waiting_reasons.pop(task_id, None)
            for summary in summaries:
                if summary.status != "queued":
                    continue
                task = self._session_store.load_background_task(
                    workspace=self._workspace,
                    task_id=summary.task.id,
                )
                if task.status != "queued" or task.task.id in self._threads:
                    continue
                request = RuntimeRequest(
                    prompt=task.request.prompt,
                    session_id=task.request.session_id,
                    parent_session_id=task.request.parent_session_id,
                    metadata=cast(RuntimeRequestMetadataPayload, task.request.metadata),
                    allocate_session_id=task.request.allocate_session_id,
                )
                try:
                    identity = self._concurrency_identity_for_task(task)
                    routing = resolve_runtime_session_routing(request)
                except (RuntimeRequestError, ValueError) as exc:
                    failed_task = self._session_store.mark_background_task_terminal(
                        workspace=self._workspace,
                        task_id=task.task.id,
                        status="failed",
                        error=str(exc),
                    )
                    failed_tasks.append(failed_task)
                    self._queued_waiting_reasons.pop(task.task.id, None)
                    outcomes[task.task.id] = _BACKGROUND_TASK_DRAIN_ROUTING_FAILED
                    continue
                if not self._can_start_task(identity):
                    self._queued_waiting_reasons[task.task.id] = _QUEUED_WAITING_REASON_CONCURRENCY
                    outcomes[task.task.id] = _BACKGROUND_TASK_DRAIN_BLOCKED_CONCURRENCY
                    continue
                self._reserve_slot(identity)
                running_task = self._session_store.mark_background_task_running(
                    workspace=self._workspace,
                    task_id=task.task.id,
                    session_id=routing.session_id,
                )
                if running_task.status != "running":
                    self._release_slot(identity)
                    self._queued_waiting_reasons.pop(task.task.id, None)
                    continue
                self._queued_waiting_reasons.pop(task.task.id, None)
                outcomes[task.task.id] = _BACKGROUND_TASK_DRAIN_DISPATCHED
                background_task_id = running_task.task.id
                worker, worker_start_gate = self._spawn_worker_thread(
                    task_id=background_task_id,
                    reserved_identity=identity,
                )
                started_tasks.append((running_task, worker, identity, worker_start_gate))
        for started_task, worker, identity, worker_start_gate in started_tasks:
            try:
                worker.start()
            except RuntimeError as exc:
                with self._queue_lock:
                    self._threads.pop(started_task.task.id, None)
                    self._release_slot(identity)
                failed_task = self._session_store.mark_background_task_terminal(
                    workspace=self._workspace,
                    task_id=started_task.task.id,
                    status="failed",
                    error=str(exc),
                )
                failed_tasks.append(failed_task)
                continue
            try:
                self.run_background_task_lifecycle_surface(
                    task=started_task,
                    surface="background_task_started",
                    session_id=(started_task.session_id or started_task.parent_session_id or "runtime"),
                )
            finally:
                worker_start_gate.set()
        for failed_task in failed_tasks:
            self.run_background_task_lifecycle_hook(failed_task)
        return outcomes

    def _spawn_worker_thread(
        self,
        *,
        task_id: str,
        reserved_identity: _BackgroundTaskConcurrencyIdentity,
    ) -> tuple[threading.Thread, threading.Event]:
        """Create (but do not start) a start-gated worker thread for ``task_id``.

        Shared by the queue drain and ``steer_background_task`` so both
        dispatch paths keep the same slot accounting: the caller MUST already
        have reserved a concurrency slot for ``reserved_identity`` at dispatch
        time, and the started thread releases it exactly once — either in the
        pre-start shutdown path (interrupt + ``_release_slot``) or in the
        worker's ``finally`` — preserving the "reserve once at dispatch,
        release once in the worker" invariant.

        The thread is registered in ``self._threads`` here, before the caller
        starts it, so a concurrent drain orphan-scan never terminalizes a
        ``running`` row that owns an in-flight dispatch.
        """
        worker_start_gate = threading.Event()

        def run_worker_after_started_hook(
            *,
            background_task_id: str = task_id,
            reserved_identity: _BackgroundTaskConcurrencyIdentity = reserved_identity,
            start_gate: threading.Event = worker_start_gate,
        ) -> None:
            try:
                start_gate.wait()
                if self._shutdown_requested:
                    try:
                        self._mark_background_task_interrupted_before_worker(task_id=background_task_id)
                    finally:
                        with self._queue_lock:
                            self._release_slot(reserved_identity)
                    return
                self.run_background_task_worker(background_task_id)
            finally:
                with self._queue_lock:
                    # Only the current worker may unregister itself. A newer
                    # steer-dispatched worker can register the same task id
                    # while this worker's finally is still running; popping
                    # blindly would delete the newer worker's registration and
                    # make the drain orphan-scan terminalize the running task
                    # as interrupted. Mirrors the shutdown-join identity guard.
                    if self._threads.get(background_task_id) is threading.current_thread():
                        self._threads.pop(background_task_id, None)

        worker = threading.Thread(
            target=run_worker_after_started_hook,
            name=f"voidcode-background-task-{task_id}",
            daemon=True,
        )
        self._threads[task_id] = worker
        return worker, worker_start_gate

    def _mark_background_task_interrupted_before_worker(self, *, task_id: str) -> None:
        try:
            terminal_task = self._session_store.mark_background_task_terminal(
                workspace=self._workspace,
                task_id=task_id,
                status="interrupted",
                error="runtime shutdown requested before delegated worker execution started",
            )
        except Exception as exc:
            if "unknown background task" in str(exc):
                logger.debug(
                    "background task %s disappeared before shutdown interruption: %s",
                    task_id,
                    exc,
                )
                return
            logger.exception(
                "background task %s could not persist shutdown interruption state",
                task_id,
            )
            return
        self.run_background_task_lifecycle_hook(terminal_task)

    def load_background_task_result(
        self,
        task_id: str,
        *,
        emit_result_read_hook: bool = True,
    ) -> BackgroundTaskResult:
        validate_background_task_id(task_id)
        task = self._session_store.load_background_task(
            workspace=self._workspace,
            task_id=task_id,
        )
        if task.status == "interrupted":
            task = self.repair_interrupted_task_from_child_terminal_session(task)
        if emit_result_read_hook:
            task = self._session_store.stop_background_task_idle_reminder(
                workspace=self._workspace,
                task_id=task.task.id,
                stop_condition="result_read",
            )
        self.backfill_parent_background_task_event(task=task)
        result = self.background_task_result(task=task)
        if emit_result_read_hook:
            self.run_background_task_lifecycle_surface(
                task=task,
                surface="background_task_result_read",
                session_id=task.parent_session_id or task.session_id or task.request.session_id or "runtime",
            )
        return result

    def repair_interrupted_task_from_child_terminal_session(self, task: BackgroundTaskState) -> BackgroundTaskState:
        """Recover a stale interrupted task when its child session already finished.

        This is intentionally not a general reconciliation path: active running tasks
        keep their task row as the runtime truth while their worker owns finalization.
        """
        if task.session_id is None:
            return task
        if task.status != "interrupted":
            return task
        child_response = self.load_background_task_child_response(task=task)
        if child_response is None:
            return task
        # A child whose ROW is still ``interrupted`` can already be terminally
        # finished: the run loop persists every event before the generator-driven
        # terminal seal, so a transcript ending in a successful ``submit_result``
        # handoff plus ``graph.response_ready`` proves completion even when the
        # seal was skipped/downgraded (worker early-exit, worker death, overlap
        # guard). ``finalize_background_task_from_session_response`` repairs the
        # unsealed session row while it terminalizes the task. Genuinely
        # resumable interrupted children (no handoff) yield None and stay put.
        if child_terminal_outcome(child_response) is not None:
            self.finalize_background_task_from_session_response(session_response=child_response)
            return self._session_store.load_background_task(
                workspace=self._workspace,
                task_id=task.task.id,
            )
        return task

    def cancel_background_task(self, task_id: str) -> BackgroundTaskState:
        validate_background_task_id(task_id)
        previous_task = self._session_store.load_background_task(
            workspace=self._workspace,
            task_id=task_id,
        )
        task = self._session_store.request_background_task_cancel(
            workspace=self._workspace,
            task_id=task_id,
        )
        if task.status == "running" and task.session_id is not None:
            child_response = self.load_background_task_child_response(task=task)
            if child_response is not None and child_response.session.status == "waiting":
                self._session_store.clear_pending_approval(
                    workspace=self._workspace,
                    session_id=task.session_id,
                )
                self._session_store.clear_pending_question(
                    workspace=self._workspace,
                    session_id=task.session_id,
                )
                cancelled_metadata = dict(child_response.session.metadata)
                cancelled_metadata["abort_requested"] = True
                cancelled_failure_payload: dict[str, object] = {
                    "error": "cancelled by parent while child session was waiting",
                    "cancelled": True,
                    "delegated_task_id": task.task.id,
                }
                cancelled_response = RuntimeResponse(
                    session=SessionState(
                        session=child_response.session.session,
                        status="failed",
                        turn=child_response.session.turn,
                        metadata=cancelled_metadata,
                    ),
                    events=child_response.events
                    + (
                        EventEnvelope(
                            session_id=task.session_id,
                            sequence=(child_response.events[-1].sequence if child_response.events else 0) + 1,
                            event_type=RUNTIME_FAILED,
                            source="runtime",
                            payload=cancelled_failure_payload,
                        ),
                    ),
                    output=child_response.output,
                )
                # ``save_run`` is a terminal seal-writer and does not write
                # events; persist the synthetic cancellation failure so a later
                # replay sees it before sealing the failed row.
                self._session_store.append_session_events(
                    workspace=self._workspace,
                    session_id=task.session_id,
                    events=((RUNTIME_FAILED, "runtime", cancelled_failure_payload, None),),
                )
                self._session_store.save_run(
                    workspace=self._workspace,
                    request=RuntimeRequest(
                        prompt=prompt_from_events(child_response.events),
                        session_id=task.session_id,
                        parent_session_id=task.parent_session_id,
                        metadata=cast(RuntimeRequestMetadataPayload, cancelled_metadata),
                    ),
                    response=cancelled_response,
                )
                task = self._session_store.mark_background_task_terminal(
                    workspace=self._workspace,
                    task_id=task_id,
                    status="cancelled",
                    error="cancelled by parent while child session was waiting",
                )
        if task.status == "idle":
            # An idle keep-alive task owns no worker thread, so nothing will
            # ever poll the cancel request; terminalize it directly.
            task = self._session_store.mark_background_task_terminal(
                workspace=self._workspace,
                task_id=task_id,
                status="cancelled",
                error="cancelled by parent while awaiting steer",
            )
        if previous_task.status != "cancelled" and task.status == "cancelled":
            self.run_background_task_lifecycle_hook(task)
        return self.task_with_observability(task)

    def load_background_task_child_response(
        self,
        *,
        task: BackgroundTaskState,
    ) -> RuntimeResponse | None:
        child_session_id = task.session_id
        if child_session_id is None:
            return None
        try:
            response = self._session_store.load_session(
                workspace=self._workspace,
                session_id=child_session_id,
            )
        except UnknownSessionError:
            return None
        validate_session_workspace(response.session, session_id=child_session_id, workspace=self._workspace)
        return response

    def load_background_task_child_result(
        self,
        *,
        task: BackgroundTaskState,
    ) -> RuntimeSessionResult | None:
        child_session_id = task.session_id
        if child_session_id is None:
            return None
        try:
            result = self._session_store.load_session_result(
                workspace=self._workspace,
                session_id=child_session_id,
            )
        except UnknownSessionError:
            return None
        validate_session_workspace(result.session, session_id=child_session_id, workspace=self._workspace)
        return result

    def background_task_result(self, *, task: BackgroundTaskState) -> BackgroundTaskResult:
        child_result = self.load_background_task_child_result(task=task)
        approval_blocked = child_result is not None and child_result.status == "waiting"
        summary_output = self._leader_safe_child_summary(
            child_result=child_result,
        )
        error = child_result.error if child_result is not None and child_result.error else task.error
        result_available = task.result_available
        if not result_available and task.status != "cancelled" and child_result is not None:
            result_available = True
        routing_error: str | None = None
        try:
            routing = task.routing_identity
        except ValueError as exc:
            routing = None
            routing_error = str(exc)
        duration_seconds = self._duration_seconds(task=task)
        tool_call_count = self._tool_call_count(child_result=child_result)
        hook_reminder = self._hook_reminder_payload(task=task, child_result=child_result)
        return BackgroundTaskResult(
            task_id=task.task.id,
            parent_session_id=task.parent_session_id,
            child_session_id=task.session_id,
            status=task.status,
            requested_child_session_id=task.request.session_id or task.session_id,
            delegated_prompt=task.request.prompt,
            routing=routing,
            approval_request_id=task.approval_request_id,
            question_request_id=task.question_request_id,
            approval_blocked=approval_blocked,
            summary_output=summary_output,
            error=error or routing_error,
            result_available=result_available,
            cancellation_cause=task.cancellation_cause,
            duration_seconds=duration_seconds,
            tool_call_count=tool_call_count,
            observability=self.task_observability(task),
            hook_reminder=hook_reminder,
        )

    @staticmethod
    def _hook_reminder_payload(
        *,
        task: BackgroundTaskState,
        child_result: RuntimeSessionResult | None,
    ) -> dict[str, object] | None:
        if child_result is None:
            return None
        delegated_events = child_result.delegated_events
        if not delegated_events and task.approval_request_id is None and task.question_request_id is None:
            return None
        latest_event = delegated_events[-1] if delegated_events else None
        reminder: dict[str, object] = {
            "active": True,
            "task_status": task.status,
            "child_status": child_result.status,
        }
        if latest_event is not None:
            reminder["lifecycle_status"] = latest_event.delegation.lifecycle_status
            reminder["approval_blocked"] = latest_event.message.approval_blocked
            reminder["result_available"] = latest_event.message.result_available
        if task.approval_request_id is not None:
            reminder["approval_request_id"] = task.approval_request_id
            reminder["message"] = "Child session is waiting on approval."
        elif task.question_request_id is not None:
            reminder["question_request_id"] = task.question_request_id
            reminder["message"] = "Child session is waiting on a question response."
        elif latest_event is not None:
            reminder["message"] = f"Delegated lifecycle status: {latest_event.delegation.lifecycle_status}."
        return reminder

    @staticmethod
    def _duration_seconds(*, task: BackgroundTaskState) -> float | None:
        started = task.started_at_unix_ms or task.created_at_unix_ms
        finished = task.finished_at_unix_ms
        if started is None or finished is None:
            return None
        return round(max(finished - started, 0) / 1000, 3)

    @staticmethod
    def _tool_call_count(*, child_result: RuntimeSessionResult | None) -> int:
        if child_result is None:
            return 0
        return sum(1 for event in child_result.transcript if event.event_type == RUNTIME_TOOL_COMPLETED)

    @staticmethod
    def _leader_safe_child_summary(
        *,
        child_result: RuntimeSessionResult | None,
    ) -> str | None:
        if child_result is None:
            return None
        handoff = RuntimeBackgroundTaskSupervisor._child_handoff(child_result)
        if handoff is not None:
            return RuntimeBackgroundTaskSupervisor._render_child_handoff(handoff)
        child_session_id = child_result.session.session.id
        if child_result.status == "completed":
            return f"Child session {child_session_id} completed without required submit_result handoff."
        if child_result.status == "waiting":
            return child_result.summary
        if child_result.status == "failed":
            return child_result.summary
        return f"Background child session {child_session_id}: {child_result.status}"

    @staticmethod
    def _child_handoff(child_result: RuntimeSessionResult) -> dict[str, object] | None:
        for event in reversed(child_result.transcript):
            if event.event_type != RUNTIME_TOOL_COMPLETED:
                continue
            if event.payload.get("tool") != "submit_result" or event.payload.get("status") != "ok":
                continue
            handoff = event.payload.get("handoff")
            if isinstance(handoff, dict):
                typed_handoff = cast(dict[str, object], handoff)
                summary = typed_handoff.get("summary")
                if isinstance(summary, str) and summary.strip():
                    return dict(typed_handoff)
        return None

    @staticmethod
    def _render_child_handoff(handoff: dict[str, object]) -> str:
        lines = [str(handoff["summary"]).strip()]
        labels = (
            ("completed_work", "Completed work"),
            ("files_touched", "Files touched"),
            ("verification", "Verification"),
            ("open_questions", "Open questions"),
            ("blockers", "Blockers"),
        )
        for key, label in labels:
            value = handoff.get(key)
            if not isinstance(value, list):
                continue
            items = [item.strip() for item in value if isinstance(item, str) and item.strip()]
            if items:
                lines.append(f"{label}: " + "; ".join(items))
        return "\n".join(lines)

    def _delegated_lifecycle_payloads(
        self,
        result: BackgroundTaskResult,
    ) -> tuple[BackgroundTaskResult, dict[str, object], dict[str, object]]:
        try:
            delegation = result.delegated_execution.as_payload()
            message = result.delegated_message.as_payload()
        except ValueError as exc:
            result = replace(result, routing=None, error=result.error or str(exc))
            delegation = result.delegated_execution.as_payload()
            message = result.delegated_message.as_payload()
        return result, delegation, message

    def emit_background_task_parent_terminal_event(self, *, task: BackgroundTaskState) -> None:
        parent_session_id = task.parent_session_id
        if parent_session_id is None or not is_background_task_terminal(task.status):
            return
        session_event_appender = self._session_store
        if not isinstance(session_event_appender, SessionEventAppender):
            logger.debug("skipping background terminal parent event for session store without append support")
            return
        result = self.background_task_result(task=task)
        event_type_by_status: dict[BackgroundTaskStatus, str] = {
            "completed": RUNTIME_BACKGROUND_TASK_COMPLETED,
            "failed": RUNTIME_BACKGROUND_TASK_FAILED,
            "cancelled": RUNTIME_BACKGROUND_TASK_CANCELLED,
            "interrupted": RUNTIME_BACKGROUND_TASK_INTERRUPTED,
        }
        event_type = event_type_by_status[task.status]
        result, delegation_payload, message_payload = self._delegated_lifecycle_payloads(result)
        payload: dict[str, object] = {
            "task_id": task.task.id,
            "parent_session_id": parent_session_id,
            "status": task.status,
            "result_available": result.result_available,
            "delegation": delegation_payload,
            "message": message_payload,
            **self._concurrency_payload_for_event(task),
        }
        if result.child_session_id is not None:
            payload["child_session_id"] = result.child_session_id
        if task.status == "completed" and result.summary_output is not None:
            payload["summary_output"] = result.summary_output
        if task.status in ("failed", "cancelled", "interrupted") and result.error is not None:
            payload["error"] = result.error
        if task.approval_request_id is not None:
            payload["approval_request_id"] = task.approval_request_id
        if task.question_request_id is not None:
            payload["question_request_id"] = task.question_request_id
        try:
            appended = session_event_appender.append_session_event(
                workspace=self._workspace,
                session_id=parent_session_id,
                event_type=event_type,
                source="runtime",
                payload=payload,
                dedupe_key=f"{event_type}:{task.task.id}",
            )
            if appended is not None:
                self.run_background_task_lifecycle_surface(
                    task=task,
                    surface="background_task_notification_enqueued",
                    session_id=parent_session_id,
                    extra_payload={
                        "notification_event_type": event_type,
                        "notification_event_sequence": appended.sequence,
                    },
                )
            self._emit_parallel_group_terminal_event(task=task)
            append_parent_acp_delegated_lifecycle_event(
                self._session_store,
                workspace=self._workspace,
                task=task,
                lifecycle_status=task.status,
                result_available=result.result_available,
                payload=payload,
            )
            publish_delegated_acp_event(
                self._acp_adapter,
                task=task,
                lifecycle_status=task.status,
                result_available=result.result_available,
                payload=payload,
            )
        except UnknownSessionError:
            logger.debug(
                "skipping background terminal event for unavailable parent session: %s",
                parent_session_id,
            )
        except SessionSealedError:
            # Terminal-seal guard: the parent session is sealed, so this
            # notification is a late event and must be dropped, never applied.
            # The child/task truth is already durable; only the parent-session
            # notification is skipped.
            logger.debug(
                "dropping background terminal event for sealed parent session: %s",
                parent_session_id,
            )

    def _emit_parallel_group_terminal_event(self, *, task: BackgroundTaskState) -> None:
        """Notify the parent once the explicitly sized parallel group is terminal."""
        parent_session_id = task.parent_session_id
        metadata = task.request.metadata
        group_id = metadata.get("parallel_group_id")
        raw_size = metadata.get("parallel_group_size")
        if not isinstance(group_id, str) or not group_id.strip() or parent_session_id is None:
            return
        if isinstance(raw_size, bool):
            return
        if isinstance(raw_size, int):
            group_size = raw_size
        elif isinstance(raw_size, str):
            try:
                group_size = int(raw_size)
            except ValueError:
                return
        else:
            return
        if group_size < 1:
            return
        summaries = self._session_store.list_background_tasks_by_parent_session(
            workspace=self._workspace,
            parent_session_id=parent_session_id,
        )
        group_tasks: list[BackgroundTaskState] = []
        for summary in summaries:
            candidate = self._session_store.load_background_task(
                workspace=self._workspace,
                task_id=summary.task.id,
            )
            if candidate.request.metadata.get("parallel_group_id") == group_id:
                group_tasks.append(candidate)
        if len(group_tasks) != group_size or not all(is_background_task_terminal(item.status) for item in group_tasks):
            return
        appender = self._session_store
        if not isinstance(appender, SessionEventAppender):
            return
        counts = {status: sum(item.status == status for item in group_tasks) for status in BACKGROUND_TASK_TERMINAL_STATUSES}
        try:
            appender.append_session_event(
                workspace=self._workspace,
                session_id=parent_session_id,
                event_type=RUNTIME_BACKGROUND_TASK_GROUP_COMPLETED,
                source="runtime",
                payload={
                    "parallel_group_id": group_id,
                    "expected_task_count": group_size,
                    "terminal_task_count": len(group_tasks),
                    "counts": counts,
                    "task_ids": [item.task.id for item in group_tasks],
                },
                dedupe_key=f"{RUNTIME_BACKGROUND_TASK_GROUP_COMPLETED}:{parent_session_id}:{group_id}",
            )
        except SessionSealedError:
            # Terminal-seal guard: sealed parent — drop the late notification.
            logger.debug(
                "dropping background group terminal event for sealed parent session: %s",
                parent_session_id,
            )

    def backfill_parent_background_task_event(self, *, task: BackgroundTaskState) -> None:
        if task.parent_session_id is None:
            return
        if is_background_task_terminal(task.status):
            self.emit_background_task_parent_terminal_event(task=task)
            return
        if task.status != "running":
            return
        child_response = self.load_background_task_child_response(task=task)
        if child_response is None or child_response.session.status != "waiting":
            return
        self.emit_background_task_idle_reminder_for_waiting_child(
            task=task,
            child_response=child_response,
        )
        self.emit_background_task_waiting_approval(
            task=task,
            child_response=child_response,
        )

    def reconcile_parent_background_task_events_for_session(
        self,
        *,
        parent_session_id: str,
    ) -> None:
        task_summaries = self._session_store.list_background_tasks_by_parent_session(
            workspace=self._workspace,
            parent_session_id=parent_session_id,
        )
        for task_summary in task_summaries:
            task = self._session_store.load_background_task(
                workspace=self._workspace,
                task_id=task_summary.task.id,
            )
            if task.status == "running" and task.session_id is not None:
                child_response = self.load_background_task_child_response(task=task)
                if child_response is not None and child_response.session.status in (
                    "waiting",
                    "completed",
                    "failed",
                ):
                    self.finalize_background_task_from_session_response(session_response=child_response)
                    continue
            self.backfill_parent_background_task_event(task=task)

    def emit_background_task_waiting_approval(
        self,
        *,
        task: BackgroundTaskState,
        child_response: RuntimeResponse,
    ) -> None:
        parent_session_id = task.parent_session_id
        child_session_id = task.session_id
        if parent_session_id is None or child_session_id is None:
            return
        approval_request_id = approval_request_id_from_waiting_response(child_response)
        dedupe_key = (
            f"background_task_waiting_approval:{task.task.id}:{approval_request_id}"
            if approval_request_id is not None
            else f"background_task_waiting_approval:{task.task.id}:{child_session_id}"
        )
        session_event_appender = self._session_store
        if not isinstance(session_event_appender, SessionEventAppender):
            logger.debug("skipping background waiting event for session store without append support")
            return
        result = self.background_task_result(task=task)
        _, delegation_payload, message_payload = self._delegated_lifecycle_payloads(result)
        try:
            appended = session_event_appender.append_session_event(
                workspace=self._workspace,
                session_id=parent_session_id,
                event_type=RUNTIME_BACKGROUND_TASK_WAITING_APPROVAL,
                source="runtime",
                payload={
                    "task_id": task.task.id,
                    "parent_session_id": parent_session_id,
                    "child_session_id": child_session_id,
                    "status": "running",
                    "approval_blocked": True,
                    "delegation": delegation_payload,
                    "message": message_payload,
                    **self._concurrency_payload_for_event(task),
                    **({"approval_request_id": approval_request_id} if approval_request_id is not None else {}),
                },
                dedupe_key=dedupe_key,
            )
            if appended is not None:
                self.run_background_task_lifecycle_surface(
                    task=task,
                    surface="background_task_notification_enqueued",
                    session_id=parent_session_id,
                    extra_payload={
                        "notification_event_type": RUNTIME_BACKGROUND_TASK_WAITING_APPROVAL,
                        "notification_event_sequence": appended.sequence,
                    },
                )
            acp_payload: dict[str, object] = {
                "task_id": task.task.id,
                "parent_session_id": parent_session_id,
                "child_session_id": child_session_id,
                "approval_request_id": approval_request_id,
                "status": "running",
                "approval_blocked": True,
            }
            append_parent_acp_delegated_lifecycle_event(
                self._session_store,
                workspace=self._workspace,
                task=task,
                lifecycle_status="waiting_approval",
                approval_blocked=True,
                payload=acp_payload,
            )
            publish_delegated_acp_event(
                self._acp_adapter,
                task=task,
                lifecycle_status="waiting_approval",
                approval_blocked=True,
                payload=acp_payload,
            )
        except UnknownSessionError:
            logger.debug(
                "skipping background waiting event for unavailable parent session: %s",
                parent_session_id,
            )

    def emit_background_task_awaiting_steer(
        self,
        *,
        task: BackgroundTaskState,
        session_response: RuntimeResponse,
    ) -> None:
        """Notify the parent that a keep-alive task parked ``idle`` (awaiting steer).

        Emitted by the worker after a keep-alive turn without a submit_result
        handoff. The event type is part of
        ``DELEGATED_BACKGROUND_TASK_EVENT_TYPES``, so it can land on an
        already-sealed parent session row; the dedupe key is per-turn (the
        child session's last event sequence) because the same task parks idle
        again on every steer round.
        """
        parent_session_id = task.parent_session_id
        child_session_id = task.session_id
        if parent_session_id is None or child_session_id is None:
            return
        session_event_appender = self._session_store
        if not isinstance(session_event_appender, SessionEventAppender):
            logger.debug("skipping background awaiting-steer event for session store without append support")
            return
        result = self.background_task_result(task=task)
        _, delegation_payload, message_payload = self._delegated_lifecycle_payloads(result)
        turn_sequence = session_response.events[-1].sequence if session_response.events else 0
        try:
            appended = session_event_appender.append_session_event(
                workspace=self._workspace,
                session_id=parent_session_id,
                event_type=RUNTIME_BACKGROUND_TASK_AWAITING_STEER,
                source="runtime",
                payload={
                    "task_id": task.task.id,
                    "parent_session_id": parent_session_id,
                    "child_session_id": child_session_id,
                    "status": "idle",
                    "result_available": result.result_available,
                    "delegation": delegation_payload,
                    "message": message_payload,
                    **self._concurrency_payload_for_event(task),
                },
                dedupe_key=f"{RUNTIME_BACKGROUND_TASK_AWAITING_STEER}:{task.task.id}:{turn_sequence}",
            )
            if appended is not None:
                self.run_background_task_lifecycle_surface(
                    task=task,
                    surface="background_task_notification_enqueued",
                    session_id=parent_session_id,
                    extra_payload={
                        "notification_event_type": RUNTIME_BACKGROUND_TASK_AWAITING_STEER,
                        "notification_event_sequence": appended.sequence,
                    },
                )
            acp_payload: dict[str, object] = {
                "task_id": task.task.id,
                "parent_session_id": parent_session_id,
                "child_session_id": child_session_id,
                "status": "idle",
            }
            append_parent_acp_delegated_lifecycle_event(
                self._session_store,
                workspace=self._workspace,
                task=task,
                lifecycle_status="idle",
                result_available=result.result_available,
                payload=acp_payload,
            )
            publish_delegated_acp_event(
                self._acp_adapter,
                task=task,
                lifecycle_status="idle",
                result_available=result.result_available,
                payload=acp_payload,
            )
        except UnknownSessionError:
            logger.debug(
                "skipping background awaiting-steer event for unavailable parent session: %s",
                parent_session_id,
            )
        except SessionSealedError:
            logger.debug(
                "dropping background awaiting-steer event for sealed parent session: %s",
                parent_session_id,
            )

    def emit_background_task_idle_reminder_for_waiting_child(
        self,
        *,
        task: BackgroundTaskState,
        child_response: RuntimeResponse,
    ) -> None:
        if not self._config.background_task.delegated_reminders_enabled:
            return
        parent_session_id = task.parent_session_id
        child_session_id = task.session_id
        if parent_session_id is None or child_session_id is None:
            return
        if task.status != "running" or child_response.session.status != "waiting":
            return
        idle_event = self._latest_session_idle_event(child_response.events)
        waiting_reason = waiting_reason_from_session(child_response.session)
        idle_event_sequence = idle_event.sequence if idle_event is not None else self._latest_waiting_episode_sequence(child_response.events)
        self.emit_background_task_idle_reminder(
            task=task,
            child_session_id=child_session_id,
            child_session_status=child_response.session.status,
            idle_event_sequence=idle_event_sequence,
            idle_reason=(idle_event.payload.get("reason", waiting_reason) if idle_event is not None else waiting_reason),
        )

    def emit_background_task_idle_reminder(
        self,
        *,
        task: BackgroundTaskState,
        child_session_id: str,
        child_session_status: str,
        idle_event_sequence: int,
        idle_reason: object,
    ) -> None:
        if not self._config.background_task.delegated_reminders_enabled:
            return
        parent_session_id = task.parent_session_id
        if parent_session_id is None:
            return
        if task.status != "running" or child_session_status != "waiting":
            return
        idle_episode_id = f"{child_session_id}:{idle_event_sequence}"
        now_unix_ms = self._current_unix_ms()
        reminder_state = task.delegated_reminder
        if reminder_state is not None:
            if reminder_state.idle_episode_id == idle_episode_id:
                if not reminder_state.eligible:
                    return
            elif reminder_state.reminder_sent_at_unix_ms is not None:
                cooldown_ms = self._config.background_task.delegated_reminder_cooldown_seconds * 1000
                if now_unix_ms - reminder_state.reminder_sent_at_unix_ms < cooldown_ms:
                    return
        eligible_task = self._session_store.record_background_task_idle_reminder_eligible(
            workspace=self._workspace,
            task_id=task.task.id,
            child_session_id=child_session_id,
            idle_episode_id=idle_episode_id,
            idle_detected_at_unix_ms=now_unix_ms,
        )
        reminder_state = eligible_task.delegated_reminder
        if reminder_state is None or not reminder_state.eligible:
            return
        appended = self._append_background_task_idle_reminder_event(
            task=eligible_task,
            child_session_status=child_session_status,
            idle_event_sequence=idle_event_sequence,
            idle_reason=idle_reason,
            idle_episode_id=idle_episode_id,
        )
        if appended is None:
            return
        sent_task = self._session_store.mark_background_task_idle_reminder_sent(
            workspace=self._workspace,
            task_id=task.task.id,
            idle_episode_id=idle_episode_id,
            reminder_sent_at_unix_ms=self._current_unix_ms(),
        )
        self.run_background_task_lifecycle_surface(
            task=sent_task,
            surface="background_task_notification_enqueued",
            session_id=parent_session_id,
            extra_payload={
                "notification_event_type": _RUNTIME_BACKGROUND_TASK_IDLE_REMINDER,
                "notification_event_sequence": appended.sequence,
                "idle_episode_id": idle_episode_id,
            },
        )

    def _append_background_task_idle_reminder_event(
        self,
        *,
        task: BackgroundTaskState,
        child_session_status: str,
        idle_event_sequence: int,
        idle_reason: object,
        idle_episode_id: str,
    ) -> EventEnvelope | None:
        parent_session_id = task.parent_session_id
        child_session_id = task.session_id
        if parent_session_id is None or child_session_id is None:
            return None
        session_event_appender = self._session_store
        if not isinstance(session_event_appender, SessionEventAppender):
            logger.debug("skipping background idle reminder for session store without append support")
            return None
        result = self.background_task_result(task=task)
        _, delegation_payload, message_payload = self._delegated_lifecycle_payloads(result)
        payload: dict[str, object] = {
            "task_id": task.task.id,
            "parent_session_id": parent_session_id,
            "child_session_id": child_session_id,
            "status": "running",
            "result_available": result.result_available,
            "idle_episode_id": idle_episode_id,
            "idle_event_sequence": idle_event_sequence,
            "idle_reason": idle_reason,
            "reminder": ("Delegated child session is idle and waiting for external action; inspect or resume the child session when ready."),
            "delegation": delegation_payload,
            "message": message_payload,
            **self._concurrency_payload_for_event(task),
        }
        if child_session_status == "waiting":
            payload["child_session_status"] = "waiting"
        try:
            return session_event_appender.append_session_event(
                workspace=self._workspace,
                session_id=parent_session_id,
                event_type=_RUNTIME_BACKGROUND_TASK_IDLE_REMINDER,
                source="runtime",
                payload=payload,
                dedupe_key=f"{_RUNTIME_BACKGROUND_TASK_IDLE_REMINDER}:{task.task.id}:{idle_episode_id}",
            )
        except UnknownSessionError:
            logger.debug(
                "skipping background idle reminder for unavailable parent session: %s",
                parent_session_id,
            )
            return None

    @staticmethod
    def _latest_session_idle_event(events: tuple[EventEnvelope, ...]) -> EventEnvelope | None:
        for event in reversed(events):
            if event.event_type == RUNTIME_SESSION_IDLE:
                return event
        return None

    @staticmethod
    def _latest_waiting_episode_sequence(events: tuple[EventEnvelope, ...]) -> int:
        if events:
            return events[-1].sequence
        return 0

    @staticmethod
    def _current_unix_ms() -> int:
        return int(time.time() * 1000)

    def finalize_background_task_from_session_response(
        self,
        *,
        session_response: RuntimeResponse,
    ) -> None:
        metadata = session_response.session.metadata
        background_task_id = metadata.get("background_task_id")
        background_run = metadata.get("background_run")
        if not isinstance(background_task_id, str) or background_run is not True:
            return
        current_task = self._session_store.load_background_task(
            workspace=self._workspace,
            task_id=background_task_id,
        )
        if session_response.session.status == "waiting":
            if is_background_task_terminal(current_task.status):
                return
            self.emit_background_task_idle_reminder_for_waiting_child(
                task=current_task,
                child_response=session_response,
            )
            self.emit_background_task_waiting_approval(
                task=current_task,
                child_response=session_response,
            )
            return
        terminal_status = child_terminal_outcome(session_response)
        if terminal_status is None:
            # Resumable child (``interrupted`` without a submit_result handoff):
            # never seal the session row nor terminalize the task.
            return
        # Seal the child session row whenever it is not already durably
        # terminal. The run loop's own seal (``_persist_response`` → ``save_run``
        # inside the stream generator) can be skipped entirely (generator
        # abandoned by a worker early-exit or thread death) or downgraded to
        # ``interrupted`` (the ``_persist_response`` overlap guard) even though
        # every event — including the final ``graph.response_ready`` — was
        # already persisted. Finalizing the task from the in-memory response
        # must therefore also repair the session row, or a completed child can
        # be left ``interrupted`` forever with a stale ``last_event_sequence``
        # while its background task is terminal.
        self._seal_child_session_from_response(
            task=current_task,
            session_response=session_response,
            terminal_status=terminal_status,
        )
        if current_task.status == terminal_status and current_task.error is None:
            return
        if is_background_task_terminal(current_task.status) and current_task.status != "interrupted" and current_task.status != terminal_status:
            return
        error: str | None = None
        if terminal_status == "failed":
            for event in reversed(session_response.events):
                if event.event_type == RUNTIME_FAILED:
                    event_error = event.payload.get("error")
                    error = str(event_error) if event_error is not None else None
                    break
        terminal_task = self._session_store.mark_background_task_terminal(
            workspace=self._workspace,
            task_id=background_task_id,
            status=terminal_status,
            error=error,
        )
        self.run_background_task_lifecycle_hook(terminal_task)

    def _seal_child_session_from_response(
        self,
        *,
        task: BackgroundTaskState,
        session_response: RuntimeResponse,
        terminal_status: Literal["completed", "failed"],
    ) -> None:
        """Durably seal the child session row to ``terminal_status`` when unsealed.

        No-op when the row is already durably sealed to the same terminal
        status (the run's own seal won). A row carrying a different terminal
        ``completed``/``failed`` outcome is authoritative and is never
        overwritten. Only an unsealed row (``interrupted``/``running``/missing)
        is repaired, via the runtime's canonical terminal-seal path
        (``_persist_response`` → ``save_run``), so the repaired row gets the
        same status/output/metadata/notification handling as a normal seal and
        ``last_event_sequence`` clamps to the persisted event log.
        """
        runtime = self._surface
        child_session_id = task.session_id
        if child_session_id is None:
            return
        try:
            row_status = self._session_store.load_session_status(workspace=self._workspace, session_id=child_session_id)
        except UnknownSessionError:
            return
        if row_status == terminal_status:
            return
        if row_status in ("completed", "failed") and row_status != terminal_status:
            return
        # Overlap guard: a session can carry a newer, still-active run (e.g. a
        # second background task routed to the same default session). Sealing
        # now would freeze the row while that run is mid-stream
        # (``SessionSealedError`` on its next append). Only the last-finishing
        # run may seal; leave the row for the active run's own terminal seal.
        if (
            ACTIVE_SESSION_REGISTRY.active_run_count(
                workspace=self._workspace,
                session_id=child_session_id,
            )
            > 0
        ):
            return
        request = RuntimeRequest(
            prompt=task.request.prompt,
            session_id=task.request.session_id,
            parent_session_id=task.request.parent_session_id,
            metadata=cast(RuntimeRequestMetadataPayload, task.request.metadata),
            allocate_session_id=task.request.allocate_session_id,
        )
        sealed_response = RuntimeResponse(
            session=SessionState(
                session=session_response.session.session,
                status=terminal_status,
                turn=session_response.session.turn,
                metadata=session_response.session.metadata,
            ),
            events=session_response.events,
            output=session_response.output,
        )
        try:
            runtime.persist_response(request=request, response=sealed_response)
        except Exception:
            logger.exception(
                "background task %s could not seal child session %s to %s",
                task.task.id,
                child_session_id,
                terminal_status,
            )

    def run_background_task_lifecycle_hook(self, task: BackgroundTaskState) -> None:
        surface_by_status: dict[BackgroundTaskStatus, RuntimeHookSurface] = {
            "completed": "background_task_completed",
            "failed": "background_task_failed",
            "cancelled": "background_task_cancelled",
            "interrupted": "background_task_interrupted",
        }
        surface = surface_by_status.get(task.status)
        if surface is None:
            return
        self.run_background_task_lifecycle_surface(
            task=task,
            surface=surface,
            session_id=task.session_id or task.request.session_id or "runtime",
        )
        self.emit_background_task_parent_terminal_event(task=task)
        if task.status == "completed" and task.parent_session_id is not None:
            self.run_background_task_lifecycle_surface(
                task=task,
                surface="delegated_result_available",
                session_id=task.parent_session_id,
                extra_payload={
                    "delegated_session_id": task.session_id or "",
                    "parent_session_id": task.parent_session_id,
                },
            )

    def run_background_task_lifecycle_surface(
        self,
        *,
        task: BackgroundTaskState,
        surface: RuntimeHookSurface,
        session_id: str,
        extra_payload: dict[str, object] | None = None,
    ) -> None:
        hooks = self._config.hooks
        if hooks is None or hooks.enabled is not True:
            return
        if not hooks.commands_for_surface(surface):
            return
        result = self.background_task_result(task=task)
        selected_preset = result.delegated_execution.selected_preset
        child_session_id = task.session_id
        parent_session_id = task.parent_session_id
        outcome = run_lifecycle_hooks(
            LifecycleHookExecutionRequest(
                hooks=self._config.hooks,
                workspace=self._workspace,
                session_id=session_id,
                surface=surface,
                recursion_env_var=HOOK_RECURSION_ENV_VAR,
                environment=os.environ,
                sequence_start=0,
                payload={
                    "task_id": task.task.id,
                    "background_task_id": task.task.id,
                    "background_task_status": task.status,
                    "parent_session_id": parent_session_id,
                    "child_session_id": child_session_id,
                    "preset": selected_preset,
                    "lifecycle_surface": surface,
                    **({"background_task_error": task.error} if task.error is not None else {}),
                    **(extra_payload or {}),
                },
                policy=hook_execution_policy_from_metadata(task.request.metadata),
            )
        )
        if outcome.failed_error is not None:
            logger.warning("background task lifecycle hook failed: %s", outcome.failed_error)

    def reconcile_background_tasks_if_needed(self) -> None:
        if self._reconciled:
            return
        task_summaries = self._session_store.list_background_tasks(workspace=self._workspace)
        for task_summary in task_summaries:
            if task_summary.status != "running" or task_summary.session_id is None:
                continue
            task = self._session_store.load_background_task(
                workspace=self._workspace,
                task_id=task_summary.task.id,
            )
            child_response = self.load_background_task_child_response(task=task)
            if child_response is None:
                continue
            child_status = child_response.session.status
            # ``waiting`` (approval/question) survives restarts by design; a
            # terminal child (row-sealed, or transcript-proven via a
            # submit_result handoff even when the row is still ``interrupted``)
            # finalizes the task — and repairs the unsealed row.
            if child_status == "waiting" or child_terminal_outcome(child_response) is not None:
                self.finalize_background_task_from_session_response(session_response=child_response)
        failed_tasks = self._session_store.fail_incomplete_background_tasks(
            workspace=self._workspace,
            message="background task interrupted before completion",
            include_queued=False,
        )
        for failed_task in failed_tasks:
            self.run_background_task_lifecycle_hook(failed_task)
        # Queued orphans whose parent session is durably terminal can never be
        # owned again (the process that created them is gone or the parent
        # finished before dispatch). Terminalize them so no cross-process
        # ``queued`` orphans survive; tasks with active/interrupted parents are
        # re-dispatched by the drain at the end of this pass.
        for orphan_task in self._terminalize_queued_orphans_with_terminal_parent():
            self.run_background_task_lifecycle_hook(orphan_task)
        # Keep-alive is a process-lifetime concept: after a restart no worker
        # threads and no leader context survive, so alive keep-alive tasks
        # (``idle`` awaiting steer, or ``running`` from a crashed turn) must
        # not persist as cross-process orphans. Terminalize them
        # ``interrupted`` (resumable — the child session and full transcript
        # stay intact; the leader continues via the ``task`` tool
        # ``session_id`` continuation or ``tasks retry``).
        for task_summary in self._session_store.list_background_tasks(workspace=self._workspace):
            if task_summary.status not in ("idle", "running") or not task_summary.keep_alive:
                continue
            try:
                terminal_task = self._session_store.mark_background_task_terminal(
                    workspace=self._workspace,
                    task_id=task_summary.task.id,
                    status="interrupted",
                    error="runtime exited while keep-alive worker was awaiting steer",
                )
            except Exception as exc:
                logger.debug(
                    "background task %s keep-alive terminalization failed: %s",
                    task_summary.task.id,
                    exc,
                )
                continue
            if terminal_task.status != "interrupted":
                continue
            self.run_background_task_lifecycle_hook(terminal_task)
        task_summaries = self._session_store.list_background_tasks(workspace=self._workspace)
        for task_summary in task_summaries:
            task = self._session_store.load_background_task(
                workspace=self._workspace,
                task_id=task_summary.task.id,
            )
            self.backfill_parent_background_task_event(task=task)
        self._reconciled = True
        self._drain_background_task_queue()

    def _terminalize_queued_orphans_with_terminal_parent(self) -> tuple[BackgroundTaskState, ...]:
        """Terminalize queued tasks whose parent session is completed/failed.

        Runs once per supervisor lifetime from the startup reconcile: a queued
        task whose parent is already terminal is an orphan left behind by an
        earlier process (or by a parent that finished while the task was never
        dispatched). ``cancelled`` is the terminal status — the parent is gone,
        so the delegation cannot proceed — with a durable error reason.
        """
        terminalized: list[BackgroundTaskState] = []
        for summary in self._session_store.list_background_tasks(workspace=self._workspace):
            if summary.status != "queued":
                continue
            task = self._session_store.load_background_task(
                workspace=self._workspace,
                task_id=summary.task.id,
            )
            if task.parent_session_id is None or not self._parent_session_is_terminal(task.parent_session_id):
                continue
            # Routing-invalid tasks must keep the drain's own routing-failed
            # outcome (``failed`` + the routing error): do not shadow it with
            # the orphan cancellation.
            request = RuntimeRequest(
                prompt=task.request.prompt,
                session_id=task.request.session_id,
                parent_session_id=task.request.parent_session_id,
                metadata=cast(RuntimeRequestMetadataPayload, task.request.metadata),
                allocate_session_id=task.request.allocate_session_id,
            )
            try:
                _ = self._concurrency_identity_for_task(task)
                _ = resolve_runtime_session_routing(request)
            except (RuntimeRequestError, ValueError):
                continue
            try:
                terminal_task = self._session_store.mark_background_task_terminal(
                    workspace=self._workspace,
                    task_id=task.task.id,
                    status="cancelled",
                    error="cancelled because parent session is terminal before delegated task started",
                )
            except Exception as exc:
                logger.debug("background task %s orphan terminalization failed: %s", task.task.id, exc)
                continue
            with self._queue_lock:
                self._queued_waiting_reasons.pop(task.task.id, None)
            terminalized.append(terminal_task)
        return tuple(terminalized)

    def run_background_task_worker(self, task_id: str) -> None:
        runtime = self._surface
        slot_identity: _BackgroundTaskConcurrencyIdentity | None = None
        slot_reserved = False
        try:
            task = self._load_background_task(task_id)
            if task.status == "cancelled":
                return
            request = RuntimeRequest(
                prompt=task.request.prompt,
                session_id=task.request.session_id,
                parent_session_id=task.request.parent_session_id,
                metadata=cast(RuntimeRequestMetadataPayload, task.request.metadata),
                allocate_session_id=task.request.allocate_session_id,
            )
            slot_identity = self._concurrency_identity_for_request(request)
            if task.status == "queued":
                routing = resolve_runtime_session_routing(request)
                session_id = routing.session_id
                with self._queue_lock:
                    if not self._can_start_task(slot_identity):
                        return
                    self._reserve_slot(slot_identity)
                    slot_reserved = True
                running_task = self._session_store.mark_background_task_running(
                    workspace=self._workspace,
                    task_id=task_id,
                    session_id=session_id,
                )
                if running_task.status != "running":
                    with self._queue_lock:
                        self._release_slot(slot_identity)
                    slot_reserved = False
                    slot_identity = None
                else:
                    self.run_background_task_lifecycle_surface(
                        task=running_task,
                        surface="background_task_started",
                        session_id=running_task.session_id or running_task.parent_session_id or "runtime",
                    )
            else:
                running_task = task
                slot_reserved = True
                session_id = task.session_id or resolve_runtime_session_routing(request).session_id
            if running_task.status != "running":
                return
            dispatch_task = self._load_background_task(task_id)
            if dispatch_task.status != "running":
                return
            if dispatch_task.cancel_requested_at is not None:
                terminal_task = self._session_store.mark_background_task_terminal(
                    workspace=self._workspace,
                    task_id=task_id,
                    status="cancelled",
                    error="cancelled before dispatch",
                )
                self.run_background_task_lifecycle_hook(terminal_task)
                return
            retry_count = 0
            # Keep-alive turns run on the persisted steer prompt when present
            # (written by ``mark_background_task_steered``); the first turn
            # uses the original request prompt. ``keep_alive_turn`` is the
            # internal metadata the run loop gates on (D3): it skips the
            # one-shot submit_result requirement and parks the final step as
            # ``interrupted`` (resumable child) instead of ``completed``.
            keep_alive_turn = dispatch_task.request.metadata.get("keep_alive") is True
            turn_prompt = dispatch_task.steer_prompt or dispatch_task.request.prompt
            while True:
                events: list[EventEnvelope] = []
                output: str | None = None
                final_session: Any | None = None
                internal_request = RuntimeRequest(
                    prompt=turn_prompt,
                    session_id=session_id,
                    parent_session_id=dispatch_task.request.parent_session_id,
                    metadata=cast(
                        InternalRuntimeRequestMetadata,
                        {
                            **dispatch_task.request.metadata,
                            **({"background_rate_limit_retry": True} if retry_count < _BACKGROUND_TASK_RATE_LIMIT_RETRIES else {}),
                            "background_task_id": task_id,
                            "background_run": True,
                            **({"keep_alive_turn": True} if keep_alive_turn else {}),
                        },
                    ),
                    allocate_session_id=False,
                )
                for chunk in runtime.run_with_persistence(
                    internal_request,
                    allow_internal_metadata=True,
                ):
                    final_session = chunk.session
                    if chunk.event is not None:
                        events.append(chunk.event)
                        self.run_background_task_lifecycle_surface(
                            task=dispatch_task,
                            surface="background_task_progress",
                            session_id=session_id,
                            extra_payload={
                                "progress_event_type": chunk.event.event_type,
                                "progress_event_sequence": chunk.event.sequence,
                            },
                        )
                        if chunk.event.event_type == RUNTIME_SESSION_IDLE:
                            current_task = self._session_store.load_background_task(
                                workspace=self._workspace,
                                task_id=task_id,
                            )
                            self.emit_background_task_idle_reminder(
                                task=current_task,
                                child_session_id=session_id,
                                child_session_status=chunk.session.status,
                                idle_event_sequence=chunk.event.sequence,
                                idle_reason=chunk.event.payload.get("reason", "waiting"),
                            )
                        fallback_identity = self._fallback_identity_for_event(chunk.event)
                        if fallback_identity is not None and fallback_identity != slot_identity:
                            with self._queue_lock:
                                if slot_identity is not None and slot_reserved:
                                    self._release_slot(slot_identity)
                                    slot_reserved = False
                            self._drain_background_task_queue()
                            if self._wait_for_slot_or_cancel(
                                task_id=task_id,
                                identity=fallback_identity,
                            ):
                                self._mark_background_task_cancelled_during_retry_wait(
                                    task_id=task_id,
                                )
                                return
                            slot_identity = fallback_identity
                            slot_reserved = True
                    if chunk.kind == "output":
                        output = chunk.output
                    current_task_state = self._session_store.load_background_task(
                        workspace=self._workspace,
                        task_id=task_id,
                    )
                    if current_task_state.cancel_requested_at is not None:
                        cancel_metadata = dict(final_session.metadata)
                        cancel_metadata["abort_requested"] = True
                        cancel_failure_payload: dict[str, object] = {
                            "error": "cancelled by parent during delegated execution",
                            "cancelled": True,
                            "delegated_task_id": task_id,
                        }
                        cancelled_response = RuntimeResponse(
                            session=SessionState(
                                session=final_session.session,
                                status="failed",
                                turn=final_session.turn,
                                metadata=cancel_metadata,
                            ),
                            events=tuple(events)
                            + (
                                EventEnvelope(
                                    session_id=session_id,
                                    sequence=(events[-1].sequence if events else 0) + 1,
                                    event_type=RUNTIME_FAILED,
                                    source="runtime",
                                    payload=cancel_failure_payload,
                                ),
                            ),
                            output=output,
                        )
                        self._session_store.append_session_events(
                            workspace=self._workspace,
                            session_id=session_id,
                            events=((RUNTIME_FAILED, "runtime", cancel_failure_payload, None),),
                        )
                        self._session_store.save_run(
                            workspace=self._workspace,
                            request=internal_request,
                            response=cancelled_response,
                        )
                        terminal_task = self._session_store.mark_background_task_terminal(
                            workspace=self._workspace,
                            task_id=task_id,
                            status="cancelled",
                            error="cancelled by parent during delegated execution",
                        )
                        self.run_background_task_lifecycle_hook(terminal_task)
                        return
                if final_session is None:
                    raise ValueError("runtime stream emitted no chunks")
                if final_session.status == "waiting":
                    final_session = reload_persisted_session(self._session_store, self._workspace, session_id=final_session.session.id)
                response = RuntimeResponse(
                    session=final_session,
                    events=tuple(events),
                    output=output,
                )
                if self._response_has_rate_limit_error(response) and retry_count < _BACKGROUND_TASK_RATE_LIMIT_RETRIES and slot_identity is not None:
                    retry_count += 1
                    with self._queue_lock:
                        self._release_slot(slot_identity)
                        slot_reserved = False
                    self._drain_background_task_queue()
                    if self._wait_for_rate_limit_backoff_or_cancel(
                        task_id=task_id,
                        retry_count=retry_count,
                    ):
                        self._mark_background_task_cancelled_during_retry_wait(task_id=task_id)
                        return
                    if self._wait_for_slot_or_cancel(
                        task_id=task_id,
                        identity=slot_identity,
                    ):
                        self._mark_background_task_cancelled_during_retry_wait(task_id=task_id)
                        return
                    slot_reserved = True
                    continue
                if keep_alive_turn:
                    # Keep-alive finalize: only durable outcomes finalize the
                    # task — a ``failed`` turn, a ``waiting`` turn (existing
                    # waiting path keeps the task ``running`` and emits the
                    # idle reminder + waiting approval), or a transcript-proven
                    # submit_result handoff (``child_transcript_proves_completed``
                    # only trusts transcript evidence, never the bare row
                    # status). Any other finished turn parks the task ``idle``
                    # (awaiting steer) and exits this thread; the next steer
                    # dispatches a fresh worker against the same child session.
                    if (
                        response.session.status == "failed"
                        or response.session.status == "waiting"
                        or child_transcript_proves_completed(response.events)
                    ):
                        self.finalize_background_task_from_session_response(session_response=response)
                    else:
                        idle_task = self._session_store.mark_background_task_idle(
                            workspace=self._workspace,
                            task_id=task_id,
                        )
                        self.emit_background_task_awaiting_steer(
                            task=idle_task,
                            session_response=response,
                        )
                        return
                else:
                    self.finalize_background_task_from_session_response(session_response=response)
                return
        except Exception as exc:
            logger.exception("background task failed: %s", task_id)
            try:
                terminal_task = self._session_store.mark_background_task_terminal(
                    workspace=self._workspace,
                    task_id=task_id,
                    status="failed",
                    error=str(exc),
                )
            except Exception as terminal_exc:
                if self._shutdown_requested:
                    logger.debug(
                        "background task %s skipped terminal update during shutdown: %s",
                        task_id,
                        terminal_exc,
                    )
                    return
                if "unknown background task" in str(terminal_exc):
                    logger.debug(
                        "background task %s disappeared before terminal update: %s",
                        task_id,
                        terminal_exc,
                    )
                    return
                logger.exception(
                    "background task %s could not persist terminal failure state",
                    task_id,
                )
                return
            self.run_background_task_lifecycle_hook(terminal_task)
        finally:
            if slot_identity is not None and slot_reserved:
                with self._queue_lock:
                    self._release_slot(slot_identity)
            with self._queue_lock:
                # Only the current worker may unregister itself; a newer
                # steer-dispatched worker for the same task id must not have
                # its registration removed by this worker's teardown. Held
                # under the queue lock to stay atomic with the drain
                # orphan-scan's liveness read. Mirrors the shutdown-join
                # identity guard.
                if self._threads.get(task_id) is threading.current_thread():
                    self._threads.pop(task_id, None)
            if not self._shutdown_requested:
                try:
                    self._drain_background_task_queue()
                except (RuntimeError, ValueError) as drain_exc:
                    logger.debug(
                        "background task %s skipped queue drain during worker cleanup: %s",
                        task_id,
                        drain_exc,
                    )

    @staticmethod
    def _response_has_rate_limit_error(response: RuntimeResponse) -> bool:
        if response.session.status != "failed":
            return False
        for event in reversed(response.events):
            if event.event_type != RUNTIME_FAILED:
                continue
            return event.payload.get("provider_error_kind") == "rate_limit"
        return False

    @staticmethod
    def _rate_limit_backoff_seconds(retry_count: int) -> float:
        return _BACKGROUND_TASK_RATE_LIMIT_BASE_BACKOFF_SECONDS * (2 ** max(0, retry_count - 1))
