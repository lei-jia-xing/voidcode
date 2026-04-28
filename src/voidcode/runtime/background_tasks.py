# pyright: reportPrivateUsage=false
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from ..hook.config import RuntimeHookSurface
from ..hook.executor import LifecycleHookExecutionRequest, run_lifecycle_hooks
from ..provider.models import ResolvedProviderConfig
from .contracts import (
    BackgroundTaskResult,
    InternalRuntimeRequestMetadata,
    RuntimeRequest,
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
    EventEnvelope,
)
from .session import SessionState
from .storage import SessionEventAppender
from .task import (
    BackgroundTaskRef,
    BackgroundTaskRequestSnapshot,
    BackgroundTaskState,
    BackgroundTaskStatus,
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
        return {
            "provider": self.provider,
            "model": self.model,
            "limit": self.limit,
            "limit_source": self.limit_source,
            "running_provider": self.running_provider,
            "running_model": self.running_model,
            "running_total": self.running_total,
            "queued_provider": self.queued_provider,
            "queued_model": self.queued_model,
            "queued_total": self.queued_total,
        }


class RuntimeBackgroundTaskSupervisor:
    def __init__(self, runtime: VoidCodeRuntime) -> None:
        self._runtime = runtime
        self._queue_lock = threading.RLock()
        self._provider_running_counts: dict[str, int] = {}
        self._model_running_counts: dict[str, int] = {}

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
                metadata=dict(validated_request.metadata),
                allocate_session_id=validated_request.allocate_session_id,
            ),
        )
        runtime._session_store.create_background_task(
            workspace=runtime._workspace, task=initial_state
        )
        self._drain_background_task_queue()
        return runtime.load_background_task(task_id)

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

    def _queued_counts_for_identity(
        self, identity: _BackgroundTaskConcurrencyIdentity
    ) -> tuple[int, int, int]:
        runtime = self._runtime
        queued_provider = 0
        queued_model = 0
        queued_total = 0
        for summary in runtime._session_store.list_background_tasks(workspace=runtime._workspace):
            if summary.status != "queued":
                continue
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
        return {"concurrency": self._concurrency_snapshot(task).as_payload()}

    def _drain_background_task_queue(self) -> None:
        runtime = self._runtime
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
                if task.status != "queued" or task.task.id in runtime._background_task_threads:
                    continue
                identity = self._concurrency_identity_for_task(task)
                if not self._can_start_task(identity):
                    continue
                request = RuntimeRequest(
                    prompt=task.request.prompt,
                    session_id=task.request.session_id,
                    parent_session_id=task.request.parent_session_id,
                    metadata=cast(RuntimeRequestMetadataPayload, task.request.metadata),
                    allocate_session_id=task.request.allocate_session_id,
                )
                routing = runtime._session_routing_for_request(request)
                self._reserve_slot(identity)
                running_task = runtime._session_store.mark_background_task_running(
                    workspace=runtime._workspace,
                    task_id=task.task.id,
                    session_id=routing.session_id,
                )
                if running_task.status != "running":
                    self._release_slot(identity)
                    continue
                worker = threading.Thread(
                    target=runtime._run_background_task_worker,
                    args=(task.task.id,),
                    name=f"voidcode-background-task-{task.task.id}",
                    daemon=True,
                )
                runtime._background_task_threads[task.task.id] = worker
                worker.start()

    def load_background_task_result(self, task_id: str) -> BackgroundTaskResult:
        task = self._runtime.load_background_task(task_id)
        self.backfill_parent_background_task_event(task=task)
        return self.background_task_result(task=task)

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
        return task

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
        summary_output = child_result.summary if child_result is not None else None
        error = (
            child_result.error if child_result is not None and child_result.error else task.error
        )
        result_available = task.result_available
        if not result_available and task.status != "cancelled" and child_result is not None:
            result_available = True
        return BackgroundTaskResult(
            task_id=task.task.id,
            parent_session_id=task.parent_session_id,
            child_session_id=task.session_id,
            status=task.status,
            requested_child_session_id=task.request.session_id or task.session_id,
            routing=task.routing_identity,
            approval_request_id=task.approval_request_id,
            question_request_id=task.question_request_id,
            approval_blocked=approval_blocked,
            summary_output=summary_output,
            error=error,
            result_available=result_available,
            cancellation_cause=task.cancellation_cause,
        )

    def emit_background_task_parent_terminal_event(self, *, task: BackgroundTaskState) -> None:
        runtime = self._runtime
        parent_session_id = task.parent_session_id
        if parent_session_id is None or task.status not in ("completed", "failed", "cancelled"):
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
        }
        event_type = event_type_by_status[task.status]
        payload: dict[str, object] = {
            "task_id": task.task.id,
            "parent_session_id": parent_session_id,
            "status": task.status,
            "result_available": result.result_available,
            "delegation": result.delegated_execution.as_payload(),
            "message": result.delegated_message.as_payload(),
            **self._concurrency_payload_for_event(task),
        }
        if result.child_session_id is not None:
            payload["child_session_id"] = result.child_session_id
        if task.status == "completed" and result.summary_output is not None:
            payload["summary_output"] = result.summary_output
        if task.status in ("failed", "cancelled") and result.error is not None:
            payload["error"] = result.error
        if task.approval_request_id is not None:
            payload["approval_request_id"] = task.approval_request_id
        if task.question_request_id is not None:
            payload["question_request_id"] = task.question_request_id
        try:
            _ = session_event_appender.append_session_event(
                workspace=runtime._workspace,
                session_id=parent_session_id,
                event_type=event_type,
                source="runtime",
                payload=payload,
                dedupe_key=f"{event_type}:{task.task.id}",
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
        if task.status in ("completed", "failed", "cancelled"):
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
        try:
            _ = session_event_appender.append_session_event(
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
                    "delegation": result.delegated_execution.as_payload(),
                    "message": result.delegated_message.as_payload(),
                    **self._concurrency_payload_for_event(task),
                    **(
                        {"approval_request_id": approval_request_id}
                        if approval_request_id is not None
                        else {}
                    ),
                },
                dedupe_key=dedupe_key,
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
                    "background_task_id": task.task.id,
                    "background_task_status": task.status,
                    **({"background_task_error": task.error} if task.error is not None else {}),
                    **(extra_payload or {}),
                },
            )
        )
        if outcome.failed_error is not None:
            logger.warning("background task lifecycle hook failed: %s", outcome.failed_error)

    def reconcile_background_tasks_if_needed(self) -> None:
        runtime = self._runtime
        if runtime._background_tasks_reconciled:
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
        runtime._background_tasks_reconciled = True

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
                    time.sleep(self._rate_limit_backoff_seconds(retry_count))
                    while True:
                        with self._queue_lock:
                            if self._can_start_task(slot_identity):
                                self._reserve_slot(slot_identity)
                                slot_reserved = True
                                break
                        time.sleep(0.01)
                    continue
                self.finalize_background_task_from_session_response(session_response=response)
                return
        except Exception as exc:
            logger.exception("background task failed: %s", task_id)
            terminal_task = runtime._session_store.mark_background_task_terminal(
                workspace=runtime._workspace,
                task_id=task_id,
                status="failed",
                error=str(exc),
            )
            self.run_background_task_lifecycle_hook(terminal_task)
        finally:
            if slot_identity is not None and slot_reserved:
                with self._queue_lock:
                    self._release_slot(slot_identity)
            runtime._background_task_threads.pop(task_id, None)
            self._drain_background_task_queue()

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
