from __future__ import annotations

from pathlib import Path

import jsonschema
import pytest

from voidcode.runtime.background.models import BackgroundTaskRef, BackgroundTaskRequestSnapshot, BackgroundTaskState
from voidcode.runtime.contracts import BackgroundTaskResult, RuntimeSessionResult
from voidcode.runtime.permission import PermissionPolicy, resolve_permission
from voidcode.runtime.permission_context import operation_class_for_tool
from voidcode.runtime.session import SessionRef, SessionState
from voidcode.tools import TaskControlTool, ToolCall
from voidcode.tools.runtime_context import RuntimeToolInvocationContext, bind_runtime_tool_context


class _Runtime:
    def __init__(self) -> None:
        self.steers: list[tuple[str, str]] = []

    def authorize_background_task_owner(self, task_id: str, *, parent_session_id: str | None) -> None:
        if task_id == "missing":
            raise ValueError("unknown background task")

    def load_background_task_result(self, task_id: str, *, emit_result_read_hook: bool = True) -> BackgroundTaskResult:
        _ = emit_result_read_hook
        return BackgroundTaskResult(
            task_id=task_id,
            parent_session_id="parent",
            child_session_id="child",
            status="completed",
            summary_output="done",
            result_available=True,
        )

    def wait_for_background_task(self, task_id: str, *, timeout_seconds: float) -> BackgroundTaskState:
        _ = task_id, timeout_seconds
        return self.load_background_task(task_id)

    def load_background_task_group_result(self, **kwargs: object) -> object:
        _ = kwargs
        raise AssertionError("group output is covered by the existing aggregate contract tests")

    def wait_for_background_task_group(self, **kwargs: object) -> object:
        _ = kwargs
        raise AssertionError("group output is covered by the existing aggregate contract tests")

    def session_result(self, *, session_id: str) -> RuntimeSessionResult:
        assert session_id == "child"
        return RuntimeSessionResult(
            session=SessionState(session=SessionRef(id="child", parent_id="parent"), status="completed", turn=1),
            prompt="work",
            status="completed",
            summary="done",
            output="done",
        )

    def cancel_background_task(self, task_id: str) -> BackgroundTaskState:
        return BackgroundTaskState(
            task=BackgroundTaskRef(id=task_id),
            status="cancelled",
            request=BackgroundTaskRequestSnapshot(prompt="work", parent_session_id="parent"),
            error="cancelled",
        )

    def background_task_roster(self, *, parent_session_id: str) -> dict[str, object]:
        return {"parent_session_id": parent_session_id, "tasks": [], "task_count": 0, "total_task_count": 0, "truncated": False}

    def load_background_task(self, task_id: str) -> BackgroundTaskState:
        return BackgroundTaskState(
            task=BackgroundTaskRef(id=task_id),
            status="idle",
            request=BackgroundTaskRequestSnapshot(prompt="work", parent_session_id="parent", metadata={"keep_alive": True}),
            session_id="child",
            keep_alive=True,
        )

    def steer_background_task(self, task_id: str, content: str) -> BackgroundTaskState:
        self.steers.append((task_id, content))
        task = self.load_background_task(task_id)
        return BackgroundTaskState(
            task=task.task,
            status="running",
            request=task.request,
            session_id=task.session_id,
            keep_alive=True,
            steer_prompt=content,
        )


def _invoke(runtime: _Runtime, arguments: dict[str, object], *, session_id: str = "parent"):
    with bind_runtime_tool_context(RuntimeToolInvocationContext(session_id=session_id)):
        return TaskControlTool(runtime=runtime).invoke(ToolCall(tool_name="task", arguments=arguments), workspace=Path("."))


def test_background_task_schema_is_strictly_discriminated() -> None:
    schema = TaskControlTool.definition.input_schema
    assert schema["additionalProperties"] is False
    assert len(schema["oneOf"]) == 4  # type: ignore[arg-type]
    runtime = _Runtime()
    with pytest.raises(ValueError, match="task Validation error"):
        _invoke(runtime, {"operation": "output", "task_id": "a", "task_ids": ["b"]})
    with pytest.raises(ValueError, match="task Validation error"):
        _invoke(runtime, {"operation": "unknown", "task_id": "a"})
    with pytest.raises(ValueError, match="task Validation error"):
        _invoke(runtime, {"operation": "ps", "task_id": "a"})

    output_variant = schema["oneOf"][0]  # type: ignore[index]
    assert "full_session" in output_variant["properties"]  # type: ignore[index]
    result = _invoke(runtime, {"operation": "output", "task_id": "task-1", "full_session": True})
    assert result.data["session"]["session_id"] == "child"  # type: ignore[index]
    assert result.data["session"]["message_limit"] == 20  # type: ignore[index]
    with pytest.raises(ValueError, match="task Validation error"):
        _invoke(runtime, {"operation": "output", "task_ids": ["task-1"], "full_session": True})


def test_full_session_schema_matches_model_validation() -> None:
    schema = TaskControlTool.definition.input_schema
    validator = jsonschema.Draft202012Validator(schema)
    valid = {"operation": "output", "task_id": "task-1", "full_session": True, "timeout": 1000}
    invalid = {"operation": "output", "task_ids": ["task-1"], "full_session": True}
    invalid_timeout = {"operation": "output", "task_id": "task-1", "block": True, "timeout": 999}
    assert list(validator.iter_errors(valid)) == []
    assert list(validator.iter_errors(invalid))
    assert list(validator.iter_errors(invalid_timeout))
    result = _invoke(_Runtime(), valid)
    assert result.data["session"]["session_id"] == "child"  # type: ignore[index]


def test_background_task_output_uses_unified_model_name_and_operation_guidance() -> None:
    result = _invoke(_Runtime(), {"operation": "output", "task_id": "task-1"})
    assert result.tool_name == "task"
    assert result.data["retrieval_instruction"] == 'task(operation="output", task_id="task-1")'


def test_background_task_controls_require_operation_specific_permissions() -> None:
    runtime = _Runtime()
    tool = TaskControlTool(runtime=runtime)
    for operation, expected in (("output", "read"), ("ps", "read"), ("cancel", "execute"), ("steer", "write")):
        call_args: dict[str, object] = {"operation": operation}
        if operation in {"output", "cancel", "steer"}:
            call_args["task_id"] = "task-1"
        if operation == "steer":
            call_args["prompt"] = "continue"
        call = ToolCall(tool_name="task", arguments=call_args)
        assert operation_class_for_tool("task", tool.definition.read_only, tool_instance=tool, arguments=call.arguments) == expected
        outcome = resolve_permission(
            tool.definition,
            call,
            policy=PermissionPolicy(mode="allow"),
            operation_class=expected,  # type: ignore[arg-type]
            read_only=True,
        )
        if operation == "output" or operation == "ps":
            assert outcome.decision == "allow"
        else:
            assert outcome.decision == "deny"


def test_background_task_steer_uses_parent_context() -> None:
    runtime = _Runtime()
    result = _invoke(runtime, {"operation": "steer", "task_id": "task-1", "prompt": "continue"})
    assert result.tool_name == "task"
    assert runtime.steers == [("task-1", "continue")]
