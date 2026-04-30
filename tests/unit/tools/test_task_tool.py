from __future__ import annotations

from pathlib import Path

import pytest

from voidcode.runtime.contracts import BackgroundTaskResult, RuntimeRequest, RuntimeResponse
from voidcode.runtime.session import SessionRef, SessionState
from voidcode.runtime.task import (
    BackgroundTaskRef,
    BackgroundTaskRequestSnapshot,
    BackgroundTaskState,
    SubagentRoutingIdentity,
    resolve_subagent_route,
    supported_subagent_categories,
)
from voidcode.tools import TaskTool, ToolCall
from voidcode.tools.runtime_context import RuntimeToolInvocationContext, bind_runtime_tool_context


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
                metadata=dict(request.metadata),
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


def test_task_tool_starts_background_task_with_parent_context(tmp_path: Path) -> None:
    runtime = _StubTaskRuntime()
    tool = TaskTool(runtime=runtime)

    with bind_runtime_tool_context(RuntimeToolInvocationContext(session_id="leader-session")):
        result = tool.invoke(
            ToolCall(
                tool_name="task",
                arguments={
                    "prompt": "Investigate this",
                    "run_in_background": True,
                    "load_skills": ["demo"],
                    "category": "quick",
                },
            ),
            workspace=tmp_path,
        )

    assert result.status == "ok"
    assert result.data["task_id"] == "task-123"
    assert result.data["parent_session_id"] == "leader-session"
    # A queued background task has not allocated a child session or result yet.
    assert result.data["child_session_id"] is None
    assert result.data["status"] == "queued"
    assert result.data["result_available"] is False
    assert result.data["delegation"] == {"mode": "background", "category": "quick"}
    assert runtime.requests[0].parent_session_id == "leader-session"
    assert runtime.requests[0].metadata == {
        "force_load_skills": ["demo"],
        "delegation": {"mode": "background", "category": "quick"},
    }


def test_task_tool_runs_sync_child_session(tmp_path: Path) -> None:
    runtime = _StubTaskRuntime()
    tool = TaskTool(runtime=runtime)

    with bind_runtime_tool_context(RuntimeToolInvocationContext(session_id="leader-session")):
        result = tool.invoke(
            ToolCall(
                tool_name="task",
                arguments={
                    "prompt": "Do it now",
                    "run_in_background": False,
                    "load_skills": [],
                    "subagent_type": "explore",
                },
            ),
            workspace=tmp_path,
        )

    assert result.status == "ok"
    assert result.content == "child done"
    assert result.data["session_id"] == "child-session"
    assert result.data["parent_session_id"] == "leader-session"
    assert result.data["status"] == "completed"
    assert result.data["requested_subagent_type"] == "explore"
    assert result.data["load_skills"] == []
    assert result.data["output"] == "child done"
    assert runtime.requests[0].parent_session_id == "leader-session"
    assert runtime.requests[0].session_id is None
    assert runtime.requests[0].allocate_session_id is True
    assert runtime.requests[0].metadata == {
        "force_load_skills": [],
        "delegation": {"mode": "sync", "subagent_type": "explore"},
    }
    assert runtime.requests[0].prompt.startswith("Delegated runtime task.\nRequested mode: sync")
    assert "Requested subagent_type: explore" in runtime.requests[0].prompt


@pytest.mark.parametrize("subagent_type", ("worker", "advisor", "explore", "researcher", "product"))
def test_task_tool_accepts_valid_direct_child_subagent_presets(
    tmp_path: Path,
    subagent_type: str,
) -> None:
    runtime = _StubTaskRuntime()
    tool = TaskTool(runtime=runtime)

    with bind_runtime_tool_context(RuntimeToolInvocationContext(session_id="leader-session")):
        result = tool.invoke(
            ToolCall(
                tool_name="task",
                arguments={
                    "prompt": "Handle delegated work",
                    "run_in_background": False,
                    "load_skills": ["demo"],
                    "subagent_type": subagent_type,
                },
            ),
            workspace=tmp_path,
        )

    assert result.status == "ok"
    assert runtime.requests[0].metadata == {
        "force_load_skills": ["demo"],
        "delegation": {"mode": "sync", "subagent_type": subagent_type},
    }


@pytest.mark.parametrize(
    ("subagent_type", "message"),
    (
        ("leader", "subagent_type 'leader' is not a callable child preset"),
        ("unknown", "unknown subagent_type 'unknown'"),
    ),
)
def test_task_tool_rejects_invalid_direct_child_subagent_presets_before_dispatch(
    tmp_path: Path,
    subagent_type: str,
    message: str,
) -> None:
    runtime = _StubTaskRuntime()
    tool = TaskTool(runtime=runtime)

    with bind_runtime_tool_context(RuntimeToolInvocationContext(session_id="leader-session")):
        with pytest.raises(ValueError, match=message):
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
                workspace=tmp_path,
            )

    assert runtime.requests == []


def test_task_tool_rejects_unsupported_category_before_dispatch(tmp_path: Path) -> None:
    runtime = _StubTaskRuntime()
    tool = TaskTool(runtime=runtime)

    with bind_runtime_tool_context(RuntimeToolInvocationContext(session_id="leader-session")):
        with pytest.raises(ValueError, match="unsupported task category 'slow'"):
            tool.invoke(
                ToolCall(
                    tool_name="task",
                    arguments={
                        "prompt": "Handle delegated work",
                        "run_in_background": True,
                        "load_skills": [],
                        "category": "slow",
                    },
                ),
                workspace=tmp_path,
            )

    assert runtime.requests == []


def test_task_category_mapping_contract_is_exact() -> None:
    assert set(supported_subagent_categories()) == {
        "quick",
        "low",
        "deep",
        "high",
        "brain",
        "writing",
        "visual-engineering",
    }
    assert {
        category: resolve_subagent_route(
            SubagentRoutingIdentity(mode="background", category=category)
        ).selected_preset
        for category in supported_subagent_categories()
    } == {
        "quick": "worker",
        "low": "worker",
        "deep": "worker",
        "high": "worker",
        "brain": "advisor",
        "writing": "product",
        "visual-engineering": "product",
    }


def test_task_tool_sync_path_preserves_explicit_child_session_id(tmp_path: Path) -> None:
    runtime = _StubTaskRuntime()
    tool = TaskTool(runtime=runtime)

    with bind_runtime_tool_context(RuntimeToolInvocationContext(session_id="leader-session")):
        result = tool.invoke(
            ToolCall(
                tool_name="task",
                arguments={
                    "prompt": "Do it now",
                    "run_in_background": False,
                    "load_skills": ["demo"],
                    "subagent_type": "explore",
                    "session_id": "child-existing",
                },
            ),
            workspace=tmp_path,
        )

    assert result.status == "ok"
    assert result.data["session_id"] == "child-existing"
    assert result.data["parent_session_id"] == "leader-session"
    assert runtime.requests[0].session_id == "child-existing"
    assert runtime.requests[0].allocate_session_id is False
    assert runtime.requests[0].metadata == {
        "force_load_skills": ["demo"],
        "delegation": {"mode": "sync", "subagent_type": "explore"},
    }


def test_task_tool_requires_runtime_context(tmp_path: Path) -> None:
    tool = TaskTool(runtime=_StubTaskRuntime())

    with pytest.raises(RuntimeError, match="active runtime tool invocation context"):
        tool.invoke(
            ToolCall(
                tool_name="task",
                arguments={
                    "prompt": "Do it now",
                    "run_in_background": True,
                    "load_skills": [],
                    "category": "quick",
                },
            ),
            workspace=tmp_path,
        )
