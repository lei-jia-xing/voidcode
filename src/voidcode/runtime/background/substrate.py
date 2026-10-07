"""Runtime-owned task substrate contracts.

Defines the minimal primitives (TaskSpec, TaskHandle, TaskResult) and the
TaskSubstrate protocol for background asynchronous work in VoidCode.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from .models import (
    BackgroundTaskObservability,
    BackgroundTaskRef,
    BackgroundTaskStatus,
    StoredBackgroundTaskSummary,
    is_background_task_terminal,
)

if TYPE_CHECKING:
    from ..composition import CompositionRef, FrozenComposition
    from ..contracts import BackgroundTaskResult, RuntimeRequest
    from .models import BackgroundTaskState


@dataclass(frozen=True, slots=True)
class TaskSpec:
    """Minimal specification for submitting a background task to the runtime substrate."""

    prompt: str
    session_id: str | None = None
    parent_session_id: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)
    composition_ref: CompositionRef | None = None
    allocate_session_id: bool = False
    keep_alive: bool = False

    def as_runtime_request(self) -> RuntimeRequest:
        from ..contracts import RuntimeRequest

        request_metadata = {key: value for key, value in self.metadata.items() if key != "execution_composition"}
        return RuntimeRequest(
            prompt=self.prompt,
            session_id=self.session_id,
            parent_session_id=self.parent_session_id,
            metadata=request_metadata,
            allocate_session_id=self.allocate_session_id,
        )

    @classmethod
    def from_runtime_request(
        cls,
        request: RuntimeRequest,
        *,
        composition_ref: CompositionRef | None = None,
        keep_alive: bool = False,
    ) -> TaskSpec:
        return cls(
            prompt=request.prompt,
            session_id=request.session_id,
            parent_session_id=request.parent_session_id,
            metadata=dict(request.metadata),
            composition_ref=composition_ref,
            allocate_session_id=request.allocate_session_id,
            keep_alive=keep_alive or request.metadata.get("keep_alive") is True,
        )


@dataclass(frozen=True, slots=True)
class TaskHandle:
    """Runtime handle for controlling and observing a background task."""

    task_id: str
    status: BackgroundTaskStatus
    session_id: str | None = None
    parent_session_id: str | None = None
    result_available: bool = False
    error: str | None = None
    cancellation_cause: str | None = None
    cancel_requested: bool = False
    keep_alive: bool = False
    steer_prompt: str | None = None
    observability: BackgroundTaskObservability | None = None
    _substrate: TaskSubstrate | None = field(default=None, repr=False, compare=False)

    @property
    def task(self) -> BackgroundTaskRef:
        return BackgroundTaskRef(id=self.task_id)

    @property
    def child_session_id(self) -> str | None:
        return self.session_id

    @property
    def is_terminal(self) -> bool:
        return is_background_task_terminal(self.status)

    @property
    def cancel_requested_at(self) -> int | None:
        return 1 if self.cancel_requested else None

    def cancel(self) -> TaskHandle:
        if self._substrate is None:
            raise RuntimeError(f"task handle {self.task_id} is not bound to a substrate")
        return self._substrate.cancel_task(self.task_id)

    def steer(self, content: str) -> TaskHandle:
        if self._substrate is None:
            raise RuntimeError(f"task handle {self.task_id} is not bound to a substrate")
        return self._substrate.steer_task(self.task_id, content)

    def wait(self, *, timeout_seconds: float) -> TaskHandle:
        if self._substrate is None:
            raise RuntimeError(f"task handle {self.task_id} is not bound to a substrate")
        return self._substrate.wait_task(self.task_id, timeout_seconds=timeout_seconds)

    def result(self) -> TaskResult:
        if self._substrate is None:
            raise RuntimeError(f"task handle {self.task_id} is not bound to a substrate")
        return self._substrate.load_task_result(self.task_id)

    @classmethod
    def from_state(
        cls,
        state: BackgroundTaskState,
        *,
        substrate: TaskSubstrate | None = None,
    ) -> TaskHandle:
        return cls(
            task_id=state.task.id,
            status=state.status,
            session_id=state.session_id,
            parent_session_id=state.parent_session_id,
            result_available=state.result_available,
            error=state.error,
            cancellation_cause=state.cancellation_cause,
            cancel_requested=state.cancel_requested_at is not None,
            keep_alive=state.keep_alive,
            steer_prompt=state.steer_prompt,
            observability=state.observability,
            _substrate=substrate,
        )


@dataclass(frozen=True, slots=True)
class TaskResult:
    """Substrate-level execution result for a background task."""

    task_id: str
    status: BackgroundTaskStatus
    output: str | None = None
    structured_output: dict[str, object] | None = None
    error: str | None = None
    cancellation_cause: str | None = None
    duration_seconds: float | None = None
    tool_call_count: int = 0
    result_available: bool = False
    session_id: str | None = None
    parent_session_id: str | None = None
    observability: BackgroundTaskObservability | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        return is_background_task_terminal(self.status)

    @classmethod
    def from_background_task_result(cls, result: BackgroundTaskResult) -> TaskResult:
        return cls(
            task_id=result.task_id,
            status=result.status,
            output=result.summary_output,
            structured_output=dict(result.structured_output) if result.structured_output is not None else None,
            error=result.error,
            cancellation_cause=result.cancellation_cause,
            duration_seconds=result.duration_seconds,
            tool_call_count=result.tool_call_count,
            result_available=result.result_available,
            session_id=result.child_session_id,
            parent_session_id=result.parent_session_id,
            observability=result.observability,
            metadata=dict(result.hook_reminder) if result.hook_reminder is not None else {},
        )


class TaskSubstrate(Protocol):
    """Runtime-owned task substrate protocol."""

    def start_task(
        self,
        spec: TaskSpec,
        *,
        composition: FrozenComposition | None = None,
    ) -> TaskHandle: ...

    def load_task(self, task_id: str) -> TaskHandle: ...

    def cancel_task(self, task_id: str) -> TaskHandle: ...

    def steer_task(self, task_id: str, content: str) -> TaskHandle: ...

    def wait_task(self, task_id: str, *, timeout_seconds: float) -> TaskHandle: ...

    def load_task_result(self, task_id: str) -> TaskResult: ...

    def list_tasks(self) -> tuple[StoredBackgroundTaskSummary, ...]: ...

    def shutdown(self, *, timeout_seconds: float = 2.0) -> None: ...


__all__ = [
    "TaskHandle",
    "TaskResult",
    "TaskSpec",
    "TaskSubstrate",
]
