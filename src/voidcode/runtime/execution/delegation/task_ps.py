from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, cast

from ....core.tool_context import ToolContext
from ....security.json_values import json_wire_object, own_json_object
from ....tools.contracts import TextOutput, ToolCall, ToolResult, ToolSuccess


class TaskPsRuntime(Protocol):
    def background_task_roster(self, *, parent_session_id: str) -> dict[str, object]: ...


@dataclass(frozen=True, slots=True)
class TaskRosterResultBody:
    parent_session_id: str
    tasks: tuple[Mapping[str, object], ...]
    task_count: int
    total_task_count: int
    truncated: bool
    limit: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "tasks", tuple(own_json_object(task) for task in self.tasks))

    def as_payload(self) -> dict[str, object]:
        return {
            "parent_session_id": self.parent_session_id,
            "tasks": [json_wire_object(task) for task in self.tasks],
            "task_count": self.task_count,
            "total_task_count": self.total_task_count,
            "truncated": self.truncated,
            "limit": self.limit,
        }


class TaskPsTool:
    name = "task_ps"

    def __init__(self, *, runtime: TaskPsRuntime) -> None:
        self._runtime = runtime

    def invoke(self, call: ToolCall, *, context: ToolContext) -> ToolResult:
        caller_session_id = context.require_session_id()
        if call.arguments:
            raise ValueError('task(operation="ps") accepts no arguments')
        payload = self._runtime.background_task_roster(parent_session_id=caller_session_id)
        raw_tasks = payload.get("tasks")
        parent_session_id = payload.get("parent_session_id")
        total_task_count = payload.get("total_task_count")
        truncated = payload.get("truncated")
        limit = payload.get("limit")
        if (
            not isinstance(raw_tasks, list)
            or not all(isinstance(task, Mapping) for task in raw_tasks)
            or not isinstance(parent_session_id, str)
            or not isinstance(total_task_count, int)
            or isinstance(total_task_count, bool)
            or not isinstance(truncated, bool)
            or not isinstance(limit, int)
            or isinstance(limit, bool)
        ):
            raise ValueError("background task roster returned an invalid result")
        task_count = len(raw_tasks)
        content = f"Background task roster: {task_count} task(s)"
        return ToolSuccess(
            tool_name=self.name,
            output=TextOutput(content),
            body=TaskRosterResultBody(
                parent_session_id=parent_session_id,
                tasks=tuple(cast(Mapping[str, object], task) for task in raw_tasks),
                task_count=task_count,
                total_task_count=total_task_count,
                truncated=truncated,
                limit=limit,
            ),
        )


__all__ = ["TaskPsRuntime", "TaskPsTool"]
