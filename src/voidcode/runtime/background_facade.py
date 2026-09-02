"""Small forwarding seam for the runtime's background-task supervisor.

The runtime service remains the public boundary and owns request validation,
workspace/session-store access, and read-path reconciliation.  This adapter
only keeps the service's direct supervisor forwarding calls in one place; it
does not add validation, persistence, lifecycle, or result semantics.
"""

from __future__ import annotations

from typing import final

from .background_tasks import RuntimeBackgroundTaskSupervisor
from .contracts import RuntimeRequest
from .task import BackgroundTaskState


@final
class _RuntimeBackgroundTaskFacade:
    """Forward already-governed calls from ``VoidCodeRuntime`` to its supervisor."""

    def __init__(self, supervisor: RuntimeBackgroundTaskSupervisor) -> None:
        self._supervisor = supervisor

    def shutdown(self, *, timeout_seconds: float = 2.0) -> None:
        self._supervisor.shutdown(timeout_seconds=timeout_seconds)

    def start(self, validated_request: RuntimeRequest) -> BackgroundTaskState:
        return self._supervisor.start_background_task(validated_request)

    def cancel(self, task_id: str) -> BackgroundTaskState:
        return self._supervisor.cancel_background_task(task_id)

    def retry(self, task_id: str) -> BackgroundTaskState:
        return self._supervisor.retry_background_task(task_id)

    def authorize_owner(self, task_id: str, *, parent_session_id: str | None) -> None:
        self._supervisor.authorize_background_task_owner(
            task_id,
            parent_session_id=parent_session_id,
        )

    def steer(self, task_id: str, content: str) -> BackgroundTaskState:
        return self._supervisor.steer_background_task(task_id, content)
