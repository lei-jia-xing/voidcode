from __future__ import annotations

from pathlib import Path

import pytest

from voidcode.core.tool_context import ToolContext
from voidcode.runtime.background.models import (
    BackgroundTaskRef,
    BackgroundTaskRequestSnapshot,
    BackgroundTaskState,
)
from voidcode.runtime.contracts import BackgroundTaskResult, RuntimeRequest, RuntimeResponse
from voidcode.runtime.execution.delegation.task import TaskCommand
from voidcode.runtime.session import SessionRef, SessionState
from voidcode.tools.contracts import ToolCall
from voidcode.tools.delegation.task import TaskTool


class _StubTaskRuntime:
    def __init__(self) -> None:
        self.requests: list[RuntimeRequest] = []

    def run(self, request: RuntimeRequest) -> RuntimeResponse:
        self.requests.append(request)
        child_session_id = request.session_id or "child-session"
        return RuntimeResponse(
            session=SessionState(
                session=SessionRef(id=child_session_id, parent_id=request.parent_session_id),
                status="completed",
                turn=1,
            ),
            events=(),
            output="child done",
        )

    def start_background_task(self, request: RuntimeRequest) -> BackgroundTaskState:
        self.requests.append(request)
        return BackgroundTaskState(
            task=BackgroundTaskRef(id="task-123"),
            status="queued",
            request=BackgroundTaskRequestSnapshot(
                prompt=request.prompt,
                session_id=request.session_id,
                parent_session_id=request.parent_session_id,
                metadata={key: value for key, value in request.metadata.items()},
                allocate_session_id=request.allocate_session_id,
            ),
        )

    def load_background_task_result(self, task_id: str) -> BackgroundTaskResult:
        raise AssertionError(task_id)

    def cancel_background_task(self, task_id: str) -> BackgroundTaskState:
        raise AssertionError(task_id)

    def list_background_tasks(self):
        return ()

    def session_result(self, *, session_id: str):
        raise AssertionError(session_id)


def _task_context(runtime: _StubTaskRuntime, *, workspace: Path) -> ToolContext:
    return ToolContext(
        workspace=workspace,
        session_id="leader-session",
        task_runtime=TaskCommand(runtime=runtime).invoke,
    )


@pytest.mark.parametrize("subagent_type", ("leader", "unknown"))
def test_task_tool_rejects_invalid_direct_child_subagent_presets_before_dispatch(
    tmp_path: Path,
    subagent_type: str,
) -> None:
    runtime = _StubTaskRuntime()
    tool = TaskTool()

    context = _task_context(runtime, workspace=tmp_path)
    with pytest.raises(ValueError):
        tool.invoke(
            ToolCall(
                tool_name="task",
                arguments={
                    "prompt": "Handle delegated work",
                    "run_in_background": False,
                    "load_skills": [],
                    "subagent_type": subagent_type,
                },
            ),
            context=context,
        )

    assert runtime.requests == []
