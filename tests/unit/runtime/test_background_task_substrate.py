"""Unit tests for the runtime-owned task substrate contracts and delegation adapters."""

from __future__ import annotations

from typing import Any, cast

from voidcode.core.tool_context import ToolContext
from voidcode.runtime.background.models import (
    BackgroundTaskState,
)
from voidcode.runtime.background.substrate import (
    TaskHandle,
    TaskResult,
    TaskSpec,
    TaskSubstrate,
)
from voidcode.runtime.contracts import BackgroundTaskResult, RuntimeRequest
from voidcode.runtime.execution.delegation.task import TaskCommand
from voidcode.runtime.execution.delegation.task_cancel import TaskCancelTool
from voidcode.runtime.execution.delegation.task_steer import TaskSteerTool
from voidcode.tools.contracts import ToolCall, ToolSuccess


class _FakeSubstrate:
    def __init__(self) -> None:
        self.started_specs: list[TaskSpec] = []
        self.cancelled_tasks: list[str] = []
        self.steered_tasks: list[tuple[str, str]] = []
        self.waited_tasks: list[tuple[str, float]] = []

    def start_task(self, spec: TaskSpec, *, composition: object = None) -> TaskHandle:
        self.started_specs.append(spec)
        return TaskHandle(
            task_id="task-123",
            status="running",
            session_id=spec.session_id or "child-session-1",
            parent_session_id=spec.parent_session_id,
            keep_alive=spec.keep_alive,
            _substrate=cast(TaskSubstrate, self),
        )

    def load_task(self, task_id: str) -> TaskHandle:
        return TaskHandle(
            task_id=task_id,
            status="idle",
            session_id="child-session-1",
            parent_session_id="parent-session-1",
            keep_alive=True,
            _substrate=cast(TaskSubstrate, self),
        )

    def cancel_task(self, task_id: str) -> TaskHandle:
        self.cancelled_tasks.append(task_id)
        return TaskHandle(
            task_id=task_id,
            status="cancelled",
            session_id="child-session-1",
            parent_session_id="parent-session-1",
            cancel_requested=True,
            _substrate=cast(TaskSubstrate, self),
        )

    def steer_task(self, task_id: str, content: str) -> TaskHandle:
        self.steered_tasks.append((task_id, content))
        return TaskHandle(
            task_id=task_id,
            status="running",
            session_id="child-session-1",
            parent_session_id="parent-session-1",
            keep_alive=True,
            steer_prompt=content,
            _substrate=cast(TaskSubstrate, self),
        )

    def wait_task(self, task_id: str, *, timeout_seconds: float) -> TaskHandle:
        self.waited_tasks.append((task_id, timeout_seconds))
        return TaskHandle(
            task_id=task_id,
            status="completed",
            session_id="child-session-1",
            parent_session_id="parent-session-1",
            result_available=True,
            _substrate=cast(TaskSubstrate, self),
        )

    def load_task_result(self, task_id: str) -> TaskResult:
        return TaskResult(
            task_id=task_id,
            status="completed",
            output="test output",
            session_id="child-session-1",
            parent_session_id="parent-session-1",
            result_available=True,
        )

    def list_tasks(self) -> tuple:
        return ()

    def shutdown(self, *, timeout_seconds: float = 2.0) -> None:
        pass


class _FakeRuntime:
    def __init__(self, substrate: _FakeSubstrate) -> None:
        self.task_substrate = substrate

    def authorize_background_task_owner(self, task_id: str, *, parent_session_id: str | None) -> None:
        pass

    def run(self, request: RuntimeRequest) -> object:
        raise NotImplementedError

    def start_background_task(self, request: RuntimeRequest) -> BackgroundTaskState:
        raise NotImplementedError

    def cancel_background_task(self, task_id: str) -> BackgroundTaskState:
        raise NotImplementedError

    def load_background_task(self, task_id: str) -> BackgroundTaskState:
        raise NotImplementedError

    def steer_background_task(self, task_id: str, content: str) -> BackgroundTaskState:
        raise NotImplementedError

    def wait_for_background_task(self, task_id: str, *, timeout_seconds: float) -> BackgroundTaskState:
        raise NotImplementedError

    def load_background_task_result(self, task_id: str, *, emit_result_read_hook: bool = True) -> BackgroundTaskResult:
        return BackgroundTaskResult(
            task_id=task_id,
            status="completed",
            parent_session_id="parent-session-1",
            child_session_id="child-session-1",
            summary_output="test output",
            result_available=True,
        )

    def background_task_roster(self, *, parent_session_id: str) -> dict[str, object]:
        return {
            "tasks": [],
            "parent_session_id": parent_session_id,
            "total_task_count": 0,
            "truncated": False,
            "limit": 10,
        }

    def session_result(self, *, session_id: str) -> object:
        raise NotImplementedError


def test_task_spec_conversion() -> None:
    request = RuntimeRequest(
        prompt="do something",
        session_id="s-1",
        parent_session_id="p-1",
        metadata={"delegation": {"mode": "background"}},
        allocate_session_id=True,
    )
    spec = TaskSpec.from_runtime_request(request, keep_alive=True)
    assert spec.prompt == "do something"
    assert spec.session_id == "s-1"
    assert spec.parent_session_id == "p-1"
    assert spec.keep_alive is True

    converted_request = spec.as_runtime_request()
    assert converted_request.prompt == "do something"
    assert converted_request.session_id == "s-1"
    assert converted_request.parent_session_id == "p-1"
    assert converted_request.allocate_session_id is True


def test_task_handle_and_result_contracts() -> None:
    substrate = _FakeSubstrate()
    handle = TaskHandle(
        task_id="task-1",
        status="idle",
        session_id="child-1",
        parent_session_id="parent-1",
        keep_alive=True,
        _substrate=cast(TaskSubstrate, substrate),
    )
    assert handle.task.id == "task-1"
    assert handle.child_session_id == "child-1"
    assert handle.is_terminal is False

    steered = handle.steer("next prompt")
    assert steered.status == "running"
    assert substrate.steered_tasks == [("task-1", "next prompt")]

    cancelled = handle.cancel()
    assert cancelled.status == "cancelled"
    assert substrate.cancelled_tasks == ["task-1"]

    waited = handle.wait(timeout_seconds=5.0)
    assert waited.status == "completed"
    assert substrate.waited_tasks == [("task-1", 5.0)]

    res = handle.result()
    assert res.task_id == "task-1"
    assert res.output == "test output"
    assert res.is_terminal is True


def test_delegation_adapters_consume_substrate() -> None:
    substrate = _FakeSubstrate()
    runtime = _FakeRuntime(substrate)

    # 1. TaskCancelTool consumes substrate
    cancel_tool = TaskCancelTool(runtime=cast(Any, runtime), substrate=cast(TaskSubstrate, substrate))
    ctx = ToolContext(session_id="parent-session-1")
    cancel_result = cancel_tool.invoke(ToolCall(tool_name="task_cancel", arguments={"taskId": "task-abc"}), context=ctx)
    assert isinstance(cancel_result, ToolSuccess)
    assert substrate.cancelled_tasks == ["task-abc"]

    # 2. TaskSteerTool consumes substrate
    steer_tool = TaskSteerTool(runtime=cast(Any, runtime), substrate=cast(TaskSubstrate, substrate))
    steer_result = steer_tool.invoke(
        ToolCall(tool_name="task_steer", arguments={"task_id": "task-abc", "prompt": "continue"}),
        context=ctx,
    )
    assert isinstance(steer_result, ToolSuccess)
    assert ("task-abc", "continue") in substrate.steered_tasks

    # 3. TaskCommand with run_in_background consumes substrate
    task_cmd = TaskCommand(runtime=cast(Any, runtime), substrate=cast(TaskSubstrate, substrate))
    task_result = task_cmd.invoke(
        ToolCall(
            tool_name="task",
            arguments={"prompt": "subtask prompt", "run_in_background": True, "subagent_type": "worker", "load_skills": []},
        ),
        context=ctx,
    )
    assert isinstance(task_result, ToolSuccess)
    assert len(substrate.started_specs) == 1
    assert substrate.started_specs[0].prompt == "subtask prompt"
