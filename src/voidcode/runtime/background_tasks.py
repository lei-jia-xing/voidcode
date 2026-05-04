from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from ..hook.config import RuntimeHookSurface
from ..hook.executor import LifecycleHookExecutionRequest, run_lifecycle_hooks
from ..provider.models import ResolvedProviderConfig
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
    RUNTIME_BACKGROUND_TASK_CANCELLED,
    RUNTIME_BACKGROUND_TASK_COMPLETED,
    RUNTIME_BACKGROUND_TASK_FAILED,
    RUNTIME_BACKGROUND_TASK_WAITING_APPROVAL,
    RUNTIME_FAILED,
    RUNTIME_PROVIDER_FALLBACK,
    RUNTIME_TOOL_COMPLETED,
    EventEnvelope,
)
from .session import SessionState
from .storage import SessionEventAppender
from .task import (
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
    from .service import VoidCodeRuntime

logger = logging.getLogger(__name__)

_BACKGROUND_TASK_RATE_LIMIT_RETRIES = 2
_BACKGROUND_TASK_RATE_LIMIT_BASE_BACKOFF_SECONDS = 0.05


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
    def __init__(self, runtime: VoidCodeRuntime) -> None:
        self._runtime = runtime
        self._queue_lock = threading.RLock()
        self._slot_available = threading.Condition(self._queue_lock)
        self._threads: dict[str, threading.Thread] = {}
        self._shutdown_requested = False
        self._reconciled = False
        self._provider_running_counts: dict[str, int] = {}
        self._model_running_counts: dict[str, int] = {}
        self._rate_limit_retries: dict[str, _BackgroundTaskRetrySnapshot] = {}

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
        self._shutdown_requested = True
        deadline = time.monotonic() + max(timeout_seconds, 0.0)
        while True:
            with self._queue_lock:
                threads = tuple(self._threads.items())
            if not threads:
                return
            for task_id, thread in threads:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                thread.join(timeout=min(remaining, 0.1))
                if not thread.is_alive():
                    with self._queue_lock:
                        if self._threads.get(task_id) is thread:
                            self._threads.pop(task_id, None)

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
        runtime = self._runtime
        queued_summaries = runtime._session_store.list_queued_background_tasks(
            workspace=runtime._workspace
        )
        tasks_by_id = {task.task.id: task}
        for summary in queued_summaries:
            if summary.task.id in tasks_by_id:
                continue
            tasks_by_id[summary.task.id] = runtime._session_store.load_background_task(
                workspace=runtime._workspace,
                task_id=summary.task.id,
            )
        context = self._observability_context(
            queued_summaries=queued_summaries,
            tasks_by_id=tasks_by_id,
        )
        return replace(task, observability=self.task_observability(task, context=context))

    def summary_with_observability(
        self, summary: StoredBackgroundTaskSummary
    ) -> StoredBackgroundTaskSummary:
        task = self._runtime._session_store.load_background_task(
            workspace=self._runtime._workspace,
            task_id=summary.task.id,
        )
        return replace(summary, observability=self.task_observability(task))

    def summaries_with_observability(
        self,
        summaries: tuple[StoredBackgroundTaskSummary, ...],
    ) -> tuple[StoredBackgroundTaskSummary, ...]:
        if not summaries:
            return ()
        runtime = self._runtime
        queued_summaries = runtime._session_store.list_queued_background_tasks(
            workspace=runtime._workspace
        )
        task_ids_to_load = {summary.task.id for summary in summaries}
        task_ids_to_load.update(summary.task.id for summary in queued_summaries)
        tasks_by_id = {
            task_id: runtime._session_store.load_background_task(
                workspace=runtime._workspace,
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
        for summary in self._runtime._session_store.list_background_tasks(
            workspace=self._runtime._workspace
        ):
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
        runtime = self._runtime
        with self._queue_lock:
            queued = [
                summary.task.id
                for summary in runtime._session_store.list_queued_background_tasks(
                    workspace=runtime._workspace
                )
            ]
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
        queued_positions = {
            summary.task.id: index for index, summary in enumerate(queued_summaries, start=1)
        }
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
            queued_provider_counts[identity.provider] = (
                queued_provider_counts.get(identity.provider, 0) + 1
            )
            queued_model_counts[identity.model_key] = (
                queued_model_counts.get(identity.model_key, 0) + 1
            )
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

    @staticmethod
    def _waiting_reason(
        *,
        task: BackgroundTaskState,
        retry: BackgroundTaskRetryObservability | None,
    ) -> str:
        if task.status == "queued":
            return "queued"
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
        runtime = self._runtime
        self.reconcile_background_tasks_if_needed()
        validated_request = runtime._validated_request(request)
        task_id = f"task-{uuid4().hex}"
        initial_state = BackgroundTaskState(
            task=BackgroundTaskRef(id=task_id),
            status="queued",
            request=BackgroundTaskRequestSnapshot(
                prompt=validated_request.prompt,
                session_id=validated_request.session_id,
                parent_session_id=validated_request.parent_session_id,
                metadata={key: value for key, value in validated_request.metadata.items()},
                allocate_session_id=validated_request.allocate_session_id,
            ),
        )
        runtime._session_store.create_background_task(
            workspace=runtime._workspace, task=initial_state
        )
        registered_task = runtime._session_store.load_background_task(
            workspace=runtime._workspace,
            task_id=task_id,
        )
        self.run_background_task_lifecycle_surface(
            task=registered_task,
            surface="background_task_registered",
            session_id=registered_task.parent_session_id
            or registered_task.request.session_id
            or "runtime",
        )
        self._drain_background_task_queue()
        return runtime.load_background_task(task_id)

    def retry_background_task(self, task_id: str) -> BackgroundTaskState:
        runtime = self._runtime
        self.reconcile_background_tasks_if_needed()
        validate_background_task_id(task_id)
        previous_task = runtime._session_store.load_background_task(
            workspace=runtime._workspace,
            task_id=task_id,
        )
        if previous_task.status not in ("failed", "cancelled", "interrupted"):
            raise ValueError(
                "background task retry requires a failed, cancelled, or interrupted task; "
                f"task {task_id} is {previous_task.status}"
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

    def _concurrency_identity_for_request(
        self, request: RuntimeRequest
    ) -> _BackgroundTaskConcurrencyIdentity:
        effective_config = self._runtime._runtime_config_for_request(request)
        return self._concurrency_identity_for_resolved_provider(
            effective_config.resolved_provider,
        )

    def _concurrency_identity_for_resolved_provider(
        self, resolved_provider: ResolvedProviderConfig
    ) -> _BackgroundTaskConcurrencyIdentity:
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
        background_task_config = self._runtime._config.background_task
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

    def _concurrency_identity_for_task(
        self, task: BackgroundTaskState
    ) -> _BackgroundTaskConcurrencyIdentity:
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
        self._provider_running_counts[identity.provider] = (
            self._provider_running_counts.get(identity.provider, 0) + 1
        )
        self._model_running_counts[identity.model_key] = (
            self._model_running_counts.get(identity.model_key, 0) + 1
        )

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
        task = self._runtime._session_store.load_background_task(
            workspace=self._runtime._workspace,
            task_id=task_id,
        )
        return task.status == "cancelled" or task.cancel_requested_at is not None

    def _mark_background_task_cancelled_during_retry_wait(
        self,
        *,
        task_id: str,
    ) -> None:
        terminal_task = self._runtime._session_store.mark_background_task_terminal(
            workspace=self._runtime._workspace,
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

    def _queued_counts_for_identity(
        self, identity: _BackgroundTaskConcurrencyIdentity
    ) -> tuple[int, int, int]:
        runtime = self._runtime
        queued_provider = 0
        queued_model = 0
        queued_total = 0
        for summary in runtime._session_store.list_queued_background_tasks(
            workspace=runtime._workspace
        ):
            queued_total += 1
            task = runtime._session_store.load_background_task(
                workspace=runtime._workspace,
                task_id=summary.task.id,
            )
            task_identity = self._concurrency_identity_for_task(task)
            if task_identity.provider == identity.provider:
                queued_provider += 1
            if task_identity.model_key == identity.model_key:
                queued_model += 1
        return queued_provider, queued_model, queued_total

    def _concurrency_snapshot(
        self, task: BackgroundTaskState
    ) -> _BackgroundTaskConcurrencySnapshot:
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
        if self._runtime._config.background_task.default_concurrency == 5 and not (
            self._runtime._config.background_task.provider_concurrency
            or self._runtime._config.background_task.model_concurrency
        ):
            return {}
        try:
            return {"concurrency": self._concurrency_snapshot(task).as_payload()}
        except (RuntimeRequestError, ValueError):
            return {}

    def _drain_background_task_queue(self) -> None:
        runtime = self._runtime
        if self._shutdown_requested:
            return
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
            summaries = sorted(
                runtime._session_store.list_background_tasks(workspace=runtime._workspace),
                key=lambda summary: (summary.created_at, summary.task.id),
            )
            for summary in summaries:
                if summary.status != "queued":
                    continue
                task = runtime._session_store.load_background_task(
                    workspace=runtime._workspace,
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
                    routing = runtime._session_routing_for_request(request)
                except (RuntimeRequestError, ValueError) as exc:
                    failed_task = runtime._session_store.mark_background_task_terminal(
                        workspace=runtime._workspace,
                        task_id=task.task.id,
                        status="failed",
                        error=str(exc),
                    )
                    failed_tasks.append(failed_task)
                    continue
                if not self._can_start_task(identity):
                    continue
                self._reserve_slot(identity)
                running_task = runtime._session_store.mark_background_task_running(
                    workspace=runtime._workspace,
                    task_id=task.task.id,
                    session_id=routing.session_id,
                )
                if running_task.status != "running":
                    self._release_slot(identity)
                    continue
                worker_start_gate = threading.Event()

                def run_worker_after_started_hook(
                    *,
                    background_task_id: str = task.task.id,
                    reserved_identity: _BackgroundTaskConcurrencyIdentity = identity,
                    start_gate: threading.Event = worker_start_gate,
                ) -> None:
                    try:
                        start_gate.wait()
                        if self._shutdown_requested:
                            try:
                                self._mark_background_task_interrupted_before_worker(
                                    task_id=background_task_id
                                )
                            finally:
                                with self._queue_lock:
                                    self._release_slot(reserved_identity)
                            return
                        runtime._run_background_task_worker(background_task_id)
                    finally:
                        with self._queue_lock:
                            self._threads.pop(background_task_id, None)

                worker = threading.Thread(
                    target=run_worker_after_started_hook,
                    name=f"voidcode-background-task-{task.task.id}",
                    daemon=True,
                )
                self._threads[task.task.id] = worker
                started_tasks.append((running_task, worker, identity, worker_start_gate))
        for started_task, worker, identity, worker_start_gate in started_tasks:
            try:
                worker.start()
            except RuntimeError as exc:
                with self._queue_lock:
                    self._threads.pop(started_task.task.id, None)
                    self._release_slot(identity)
                failed_task = runtime._session_store.mark_background_task_terminal(
                    workspace=runtime._workspace,
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
                    session_id=(
                        started_task.session_id or started_task.parent_session_id or "runtime"
                    ),
                )
            finally:
                worker_start_gate.set()
        for failed_task in failed_tasks:
            self.run_background_task_lifecycle_hook(failed_task)

    def _mark_background_task_interrupted_before_worker(self, *, task_id: str) -> None:
        runtime = self._runtime
        try:
            terminal_task = runtime._session_store.mark_background_task_terminal(
                workspace=runtime._workspace,
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
        task = self._runtime.load_background_task(task_id)
        self.backfill_parent_background_task_event(task=task)
        result = self.background_task_result(task=task)
        if emit_result_read_hook:
            self.run_background_task_lifecycle_surface(
                task=task,
                surface="background_task_result_read",
                session_id=task.parent_session_id
                or task.session_id
                or task.request.session_id
                or "runtime",
            )
        return result

    def cancel_background_task(self, task_id: str) -> BackgroundTaskState:
        runtime = self._runtime
        validate_background_task_id(task_id)
        previous_task = runtime._session_store.load_background_task(
            workspace=runtime._workspace,
            task_id=task_id,
        )
        task = runtime._session_store.request_background_task_cancel(
            workspace=runtime._workspace,
            task_id=task_id,
        )
        if task.status == "running" and task.session_id is not None:
            child_response = self.load_background_task_child_response(task=task)
            if child_response is not None and child_response.session.status == "waiting":
                runtime._session_store.clear_pending_approval(
                    workspace=runtime._workspace,
                    session_id=task.session_id,
                )
                runtime._session_store.clear_pending_question(
                    workspace=runtime._workspace,
                    session_id=task.session_id,
                )
                cancelled_metadata = dict(child_response.session.metadata)
                cancelled_metadata["abort_requested"] = True
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
                            sequence=(
                                child_response.events[-1].sequence if child_response.events else 0
                            )
                            + 1,
                            event_type=RUNTIME_FAILED,
                            source="runtime",
                            payload={
                                "error": "cancelled by parent while child session was waiting",
                                "cancelled": True,
                                "delegated_task_id": task.task.id,
                            },
                        ),
                    ),
                    output=child_response.output,
                )
                runtime._session_store.save_run(
                    workspace=runtime._workspace,
                    request=RuntimeRequest(
                        prompt=runtime._prompt_from_events(child_response.events),
                        session_id=task.session_id,
                        parent_session_id=task.parent_session_id,
                        metadata=cast(RuntimeRequestMetadataPayload, cancelled_metadata),
                    ),
                    response=cancelled_response,
                )
                task = runtime._session_store.mark_background_task_terminal(
                    workspace=runtime._workspace,
                    task_id=task_id,
                    status="cancelled",
                    error="cancelled by parent while child session was waiting",
                )
        if previous_task.status != "cancelled" and task.status == "cancelled":
            self.run_background_task_lifecycle_hook(task)
        return self.task_with_observability(task)

    def load_background_task_child_response(
        self,
        *,
        task: BackgroundTaskState,
    ) -> RuntimeResponse | None:
        runtime = self._runtime
        child_session_id = task.session_id
        if child_session_id is None:
            return None
        try:
            response = runtime._session_store.load_session(
                workspace=runtime._workspace,
                session_id=child_session_id,
            )
        except UnknownSessionError:
            return None
        runtime._validate_session_workspace(response.session, session_id=child_session_id)
        return response

    def load_background_task_child_result(
        self,
        *,
        task: BackgroundTaskState,
    ) -> RuntimeSessionResult | None:
        runtime = self._runtime
        child_session_id = task.session_id
        if child_session_id is None:
            return None
        try:
            result = runtime._session_store.load_session_result(
                workspace=runtime._workspace,
                session_id=child_session_id,
            )
        except UnknownSessionError:
            return None
        runtime._validate_session_workspace(result.session, session_id=child_session_id)
        return result

    def background_task_result(self, *, task: BackgroundTaskState) -> BackgroundTaskResult:
        child_result = self.load_background_task_child_result(task=task)
        approval_blocked = child_result is not None and child_result.status == "waiting"
        summary_output = self._leader_safe_child_summary(
            child_result=child_result,
        )
        error = (
            child_result.error if child_result is not None and child_result.error else task.error
        )
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
        return BackgroundTaskResult(
            task_id=task.task.id,
            parent_session_id=task.parent_session_id,
            child_session_id=task.session_id,
            status=task.status,
            requested_child_session_id=task.request.session_id or task.session_id,
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
        )

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
        return sum(
            1 for event in child_result.transcript if event.event_type == RUNTIME_TOOL_COMPLETED
        )

    @staticmethod
    def _leader_safe_child_summary(
        *,
        child_result: RuntimeSessionResult | None,
    ) -> str | None:
        if child_result is None:
            return None
        child_session_id = child_result.session.session.id
        if child_result.status == "completed":
            return (
                f"Completed child session {child_session_id}; full output is preserved outside "
                "active context."
            )
        if child_result.status == "waiting":
            return child_result.summary
        if child_result.status == "failed":
            return child_result.summary
        return f"Background child session {child_session_id}: {child_result.status}"

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
        runtime = self._runtime
        parent_session_id = task.parent_session_id
        if parent_session_id is None or not is_background_task_terminal(task.status):
            return
        session_event_appender = runtime._session_store
        if not isinstance(session_event_appender, SessionEventAppender):
            logger.debug(
                "skipping background terminal parent event for session store without append support"
            )
            return
        result = self.background_task_result(task=task)
        event_type_by_status: dict[BackgroundTaskStatus, str] = {
            "completed": RUNTIME_BACKGROUND_TASK_COMPLETED,
            "failed": RUNTIME_BACKGROUND_TASK_FAILED,
            "cancelled": RUNTIME_BACKGROUND_TASK_CANCELLED,
            "interrupted": RUNTIME_BACKGROUND_TASK_FAILED,
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
                workspace=runtime._workspace,
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
            runtime._append_parent_acp_delegated_lifecycle_event(
                task=task,
                lifecycle_status=task.status,
                result_available=result.result_available,
                payload=payload,
            )
            runtime._publish_delegated_acp_event(
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
        self.emit_background_task_waiting_approval(
            task=task,
            child_response=child_response,
        )

    def reconcile_parent_background_task_events_for_session(
        self,
        *,
        parent_session_id: str,
    ) -> None:
        runtime = self._runtime
        task_summaries = runtime._session_store.list_background_tasks_by_parent_session(
            workspace=runtime._workspace,
            parent_session_id=parent_session_id,
        )
        for task_summary in task_summaries:
            task = runtime._session_store.load_background_task(
                workspace=runtime._workspace,
                task_id=task_summary.task.id,
            )
            if task.status == "running" and task.session_id is not None:
                child_response = self.load_background_task_child_response(task=task)
                if child_response is not None and child_response.session.status in (
                    "waiting",
                    "completed",
                    "failed",
                ):
                    self.finalize_background_task_from_session_response(
                        session_response=child_response
                    )
                    continue
            self.backfill_parent_background_task_event(task=task)

    def emit_background_task_waiting_approval(
        self,
        *,
        task: BackgroundTaskState,
        child_response: RuntimeResponse,
    ) -> None:
        runtime = self._runtime
        parent_session_id = task.parent_session_id
        child_session_id = task.session_id
        if parent_session_id is None or child_session_id is None:
            return
        approval_request_id = runtime._approval_request_id_from_waiting_response(child_response)
        dedupe_key = (
            f"background_task_waiting_approval:{task.task.id}:{approval_request_id}"
            if approval_request_id is not None
            else f"background_task_waiting_approval:{task.task.id}:{child_session_id}"
        )
        session_event_appender = runtime._session_store
        if not isinstance(session_event_appender, SessionEventAppender):
            logger.debug(
                "skipping background waiting event for session store without append support"
            )
            return
        result = self.background_task_result(task=task)
        result, delegation_payload, message_payload = self._delegated_lifecycle_payloads(result)
        try:
            appended = session_event_appender.append_session_event(
                workspace=runtime._workspace,
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
                    **(
                        {"approval_request_id": approval_request_id}
                        if approval_request_id is not None
                        else {}
                    ),
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
            runtime._append_parent_acp_delegated_lifecycle_event(
                task=task,
                lifecycle_status="waiting_approval",
                approval_blocked=True,
                payload=acp_payload,
            )
            runtime._publish_delegated_acp_event(
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

    def finalize_background_task_from_session_response(
        self,
        *,
        session_response: RuntimeResponse,
    ) -> None:
        runtime = self._runtime
        metadata = session_response.session.metadata
        background_task_id = metadata.get("background_task_id")
        background_run = metadata.get("background_run")
        if not isinstance(background_task_id, str) or background_run is not True:
            return
        current_task = runtime._session_store.load_background_task(
            workspace=runtime._workspace,
            task_id=background_task_id,
        )
        if is_background_task_terminal(current_task.status):
            return
        if session_response.session.status == "waiting":
            self.emit_background_task_waiting_approval(
                task=current_task,
                child_response=session_response,
            )
            return
        terminal_status: BackgroundTaskStatus = (
            "completed" if session_response.session.status == "completed" else "failed"
        )
        if current_task.status == terminal_status:
            return
        error: str | None = None
        if terminal_status == "failed":
            for event in reversed(session_response.events):
                if event.event_type == RUNTIME_FAILED:
                    event_error = event.payload.get("error")
                    error = str(event_error) if event_error is not None else None
                    break
        terminal_task = runtime._session_store.mark_background_task_terminal(
            workspace=runtime._workspace,
            task_id=background_task_id,
            status=terminal_status,
            error=error,
        )
        self.run_background_task_lifecycle_hook(terminal_task)

    def run_background_task_lifecycle_hook(self, task: BackgroundTaskState) -> None:
        surface_by_status: dict[BackgroundTaskStatus, RuntimeHookSurface] = {
            "completed": "background_task_completed",
            "failed": "background_task_failed",
            "cancelled": "background_task_cancelled",
            "interrupted": "background_task_failed",
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
        runtime = self._runtime
        hooks = runtime._config.hooks
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
                hooks=runtime._config.hooks,
                workspace=runtime._workspace,
                session_id=session_id,
                surface=surface,
                recursion_env_var=runtime._hook_recursion_env_var,
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
            )
        )
        if outcome.failed_error is not None:
            logger.warning("background task lifecycle hook failed: %s", outcome.failed_error)

    def reconcile_background_tasks_if_needed(self) -> None:
        runtime = self._runtime
        if self._reconciled:
            return
        task_summaries = runtime._session_store.list_background_tasks(workspace=runtime._workspace)
        for task_summary in task_summaries:
            if task_summary.status != "running" or task_summary.session_id is None:
                continue
            task = runtime._session_store.load_background_task(
                workspace=runtime._workspace,
                task_id=task_summary.task.id,
            )
            child_response = self.load_background_task_child_response(task=task)
            if child_response is None:
                continue
            if child_response.session.status in ("waiting", "completed", "failed"):
                self.finalize_background_task_from_session_response(session_response=child_response)
        fail_incomplete = getattr(runtime._session_store, "fail_incomplete_background_tasks", None)
        if callable(fail_incomplete):
            failed_tasks = cast(
                tuple[BackgroundTaskState, ...],
                fail_incomplete(
                    workspace=runtime._workspace,
                    message="background task interrupted before completion",
                    include_queued=False,
                ),
            )
            for failed_task in failed_tasks:
                self.run_background_task_lifecycle_hook(failed_task)
        task_summaries = runtime._session_store.list_background_tasks(workspace=runtime._workspace)
        for task_summary in task_summaries:
            task = runtime._session_store.load_background_task(
                workspace=runtime._workspace,
                task_id=task_summary.task.id,
            )
            self.backfill_parent_background_task_event(task=task)
        self._reconciled = True
        self._drain_background_task_queue()

    def run_background_task_worker(self, task_id: str) -> None:
        runtime = self._runtime
        slot_identity: _BackgroundTaskConcurrencyIdentity | None = None
        slot_reserved = False
        try:
            task = runtime.load_background_task(task_id)
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
                routing = runtime._session_routing_for_request(request)
                session_id = routing.session_id
                with self._queue_lock:
                    if not self._can_start_task(slot_identity):
                        return
                    self._reserve_slot(slot_identity)
                    slot_reserved = True
                running_task = runtime._session_store.mark_background_task_running(
                    workspace=runtime._workspace,
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
                        session_id=running_task.session_id
                        or running_task.parent_session_id
                        or "runtime",
                    )
            else:
                running_task = task
                slot_reserved = True
                session_id = (
                    task.session_id or runtime._session_routing_for_request(request).session_id
                )
            if running_task.status != "running":
                return
            dispatch_task = runtime.load_background_task(task_id)
            if dispatch_task.status != "running":
                return
            if dispatch_task.cancel_requested_at is not None:
                terminal_task = runtime._session_store.mark_background_task_terminal(
                    workspace=runtime._workspace,
                    task_id=task_id,
                    status="cancelled",
                    error="cancelled before dispatch",
                )
                self.run_background_task_lifecycle_hook(terminal_task)
                return
            retry_count = 0
            while True:
                events: list[EventEnvelope] = []
                output: str | None = None
                final_session: Any | None = None
                internal_request = RuntimeRequest(
                    prompt=dispatch_task.request.prompt,
                    session_id=session_id,
                    parent_session_id=dispatch_task.request.parent_session_id,
                    metadata=cast(
                        InternalRuntimeRequestMetadata,
                        {
                            **dispatch_task.request.metadata,
                            **(
                                {"background_rate_limit_retry": True}
                                if retry_count < _BACKGROUND_TASK_RATE_LIMIT_RETRIES
                                else {}
                            ),
                            "background_task_id": task_id,
                            "background_run": True,
                        },
                    ),
                    allocate_session_id=False,
                )
                for chunk in runtime._run_with_persistence(
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
                    current_task_state = runtime._session_store.load_background_task(
                        workspace=runtime._workspace,
                        task_id=task_id,
                    )
                    if current_task_state.cancel_requested_at is not None:
                        if final_session is None:
                            raise ValueError("runtime stream emitted no chunks")
                        cancel_metadata = dict(final_session.metadata)
                        cancel_metadata["abort_requested"] = True
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
                                    payload={
                                        "error": "cancelled by parent during delegated execution",
                                        "cancelled": True,
                                        "delegated_task_id": task_id,
                                    },
                                ),
                            ),
                            output=output,
                        )
                        runtime._session_store.save_run(
                            workspace=runtime._workspace,
                            request=internal_request,
                            response=cancelled_response,
                        )
                        terminal_task = runtime._session_store.mark_background_task_terminal(
                            workspace=runtime._workspace,
                            task_id=task_id,
                            status="cancelled",
                            error="cancelled by parent during delegated execution",
                        )
                        self.run_background_task_lifecycle_hook(terminal_task)
                        return
                if final_session is None:
                    raise ValueError("runtime stream emitted no chunks")
                if final_session.status == "waiting":
                    final_session = runtime._reload_persisted_session(
                        session_id=final_session.session.id
                    )
                response = RuntimeResponse(
                    session=final_session,
                    events=tuple(events),
                    output=output,
                )
                if (
                    self._response_has_rate_limit_error(response)
                    and retry_count < _BACKGROUND_TASK_RATE_LIMIT_RETRIES
                    and slot_identity is not None
                ):
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
                self.finalize_background_task_from_session_response(session_response=response)
                return
        except Exception as exc:
            logger.exception("background task failed: %s", task_id)
            try:
                terminal_task = runtime._session_store.mark_background_task_terminal(
                    workspace=runtime._workspace,
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
