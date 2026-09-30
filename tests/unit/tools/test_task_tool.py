from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from voidcode.runtime.background.models import (
    BackgroundTaskRef,
    BackgroundTaskRequestSnapshot,
    BackgroundTaskState,
)
from voidcode.runtime.background.routing import (
    SubagentRoutingIdentity,
    resolve_subagent_route,
)
from voidcode.runtime.contracts import BackgroundTaskResult, RuntimeRequest, RuntimeResponse
from voidcode.runtime.session import SessionRef, SessionState
from voidcode.tools.contracts import ToolCall
from voidcode.tools.delegation.task import TaskTool
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


def test_task_tool_exposes_agent_friendly_json_schema_contract() -> None:
    schema = TaskTool.definition.input_schema

    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert schema["required"] == []

    properties = cast(dict[str, object], schema["properties"])
    run_in_background = cast(dict[str, object], properties["run_in_background"])
    load_skills = cast(dict[str, object], properties["load_skills"])
    subagent_type = cast(dict[str, object], properties["subagent_type"])

    assert run_in_background["type"] == "boolean"
    assert "Required." in cast(str, run_in_background["description"])
    assert load_skills["type"] == "array"
    assert "Pass []" in cast(str, load_skills["description"])
    assert subagent_type["type"] == "string"
    assert "Required." in cast(str, subagent_type["description"])

    assert "oneOf" not in schema
    alternatives = cast(list[dict[str, object]], schema["anyOf"])
    assert alternatives == [
        {"required": ["prompt", "run_in_background", "load_skills", "subagent_type"]},
        {"required": ["operation"]},
    ]
    examples = cast(list[object], schema["examples"])
    assert cast(dict[str, object], examples[0])["run_in_background"] is True
    assert cast(dict[str, object], examples[1])["run_in_background"] is False


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
                    "subagent_type": "worker",
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
    assert result.content is not None
    assert "Continue other work; use task(operation=output) only when a status check is needed." in result.content
    assert "Wait for a completion reminder or use task(operation=output, block=true) intentionally." in result.content
    assert result.data["delegation"] == {"mode": "background", "subagent_type": "worker"}
    assert runtime.requests[0].parent_session_id == "leader-session"
    assert runtime.requests[0].metadata == {
        "force_load_skills": ["demo"],
        "delegation": {"mode": "background", "subagent_type": "worker"},
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
    assert runtime.requests[0].prompt == "Do it now"


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


def test_task_direct_subagent_preset_mapping_is_exact() -> None:
    assert {
        preset: resolve_subagent_route(SubagentRoutingIdentity(mode="background", subagent_type=preset)).selected_preset
        for preset in ("advisor", "explore", "researcher", "worker", "product")
    } == {
        "advisor": "advisor",
        "explore": "explore",
        "researcher": "researcher",
        "worker": "worker",
        "product": "product",
    }
