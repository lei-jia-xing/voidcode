from __future__ import annotations

from pathlib import Path

import pytest

from voidcode.runtime.contracts import RuntimeRequest
from voidcode.runtime.task import BackgroundTaskRef, BackgroundTaskRequestSnapshot, BackgroundTaskState
from voidcode.tools import TaskBatchTool, ToolCall
from voidcode.tools.runtime_context import RuntimeToolInvocationContext, bind_runtime_tool_context


class _BatchRuntime:
    def __init__(self, *, fail_index: int | None = None) -> None:
        self.requests: list[RuntimeRequest] = []
        self.fail_index = fail_index

    def start_background_task(self, request: RuntimeRequest) -> BackgroundTaskState:
        index = len(self.requests)
        self.requests.append(request)
        if index == self.fail_index:
            raise RuntimeError("capacity unavailable")
        task_id = f"task-{index}"
        return BackgroundTaskState(
            task=BackgroundTaskRef(id=task_id),
            status="queued",
            request=BackgroundTaskRequestSnapshot(
                prompt=request.prompt,
                session_id=request.session_id,
                parent_session_id=request.parent_session_id,
                metadata=dict(request.metadata),
                allocate_session_id=request.allocate_session_id,
            ),
        )


def _call(items: list[dict[str, object]]) -> ToolCall:
    return ToolCall(tool_name="task_batch", arguments={"tasks": items})


def _item(prompt: str, preset: str = "worker") -> dict[str, object]:
    return {"prompt": prompt, "load_skills": [], "subagent_type": preset}


def test_task_batch_dispatches_one_owned_group_with_bounded_metadata(tmp_path: Path) -> None:
    runtime = _BatchRuntime()
    tool = TaskBatchTool(runtime=runtime)

    with bind_runtime_tool_context(RuntimeToolInvocationContext(session_id="leader-session")):
        result = tool.invoke(_call([_item("first"), _item("second", "explore")]), workspace=tmp_path)

    assert result.status == "ok"
    assert result.data["created_count"] == 2
    assert result.data["failed_count"] == 0
    assert result.data["partial"] is False
    group_id = result.data["parallel_group_id"]
    assert isinstance(group_id, str) and group_id.startswith("batch-")
    assert result.data["parallel_group_size"] == 2
    assert result.data["task_ids"] == ["task-0", "task-1"]
    assert f'background_output(parallel_group_id="{group_id}")' in str(result.data["retrieval_instruction"])
    assert all(request.parent_session_id == "leader-session" for request in runtime.requests)
    delegations = [request.metadata["delegation"] for request in runtime.requests]
    assert all(isinstance(metadata, dict) for metadata in delegations)
    assert {metadata["parallel_group_id"] for metadata in delegations} == {group_id}
    assert {metadata["parallel_group_size"] for metadata in delegations} == {2}
    assert all(metadata["mode"] == "background" for metadata in delegations)


def test_task_batch_preflights_invalid_preset_and_schema_without_side_effect(tmp_path: Path) -> None:
    runtime = _BatchRuntime()
    tool = TaskBatchTool(runtime=runtime)

    with bind_runtime_tool_context(RuntimeToolInvocationContext(session_id="leader-session")):
        with pytest.raises(ValueError, match="item 1"):
            tool.invoke(_call([_item("valid"), _item("bad", "leader")]), workspace=tmp_path)
        with pytest.raises(ValueError, match="outputSchema"):
            tool.invoke(
                _call([{"prompt": "strict", "load_skills": [], "subagent_type": "worker", "schemaMode": "strict"}]),
                workspace=tmp_path,
            )

    assert runtime.requests == []


def test_task_batch_rejects_empty_oversized_and_duplicate_items(tmp_path: Path) -> None:
    runtime = _BatchRuntime()
    tool = TaskBatchTool(runtime=runtime)
    context = RuntimeToolInvocationContext(session_id="leader-session")
    with bind_runtime_tool_context(context):
        with pytest.raises(ValueError, match="at least one"):
            tool.invoke(_call([]), workspace=tmp_path)
        with pytest.raises(ValueError, match="at most 100"):
            tool.invoke(_call([_item(str(index)) for index in range(101)]), workspace=tmp_path)
        with pytest.raises(ValueError, match="duplicates"):
            tool.invoke(_call([_item("same"), _item("same")]), workspace=tmp_path)
    assert runtime.requests == []


def test_task_batch_reports_partial_dispatch_without_hiding_created_tasks(tmp_path: Path) -> None:
    runtime = _BatchRuntime(fail_index=1)
    tool = TaskBatchTool(runtime=runtime)

    with bind_runtime_tool_context(RuntimeToolInvocationContext(session_id="leader-session")):
        result = tool.invoke(_call([_item("created"), _item("failed"), _item("created too")]), workspace=tmp_path)

    assert result.status == "ok"
    assert result.data["partial"] is True
    assert result.data["task_ids"] == ["task-0", "task-2"]
    assert result.data["created_count"] == 2
    assert result.data["failed_count"] == 1
    assert result.data["failed"][0]["index"] == 1
    assert "no automatic retry or cancellation" in (result.content or "")
