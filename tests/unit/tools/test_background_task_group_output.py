from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from voidcode.runtime.contracts import BackgroundTaskGroupResult, BackgroundTaskResult
from voidcode.tools.contracts import ToolCall
from voidcode.tools.delegation.background_output import BackgroundOutputTool
from voidcode.tools.runtime_context import RuntimeToolInvocationContext, bind_runtime_tool_context


def _result(task_id: str, status: str, *, structured: dict[str, object] | None = None) -> BackgroundTaskResult:
    return BackgroundTaskResult(
        task_id=task_id,
        parent_session_id="leader",
        child_session_id=f"child-{task_id}",
        status=status,  # type: ignore[arg-type]
        summary_output=f"summary-{task_id}",
        error=("failed child" if status == "failed" else None),
        result_available=status == "completed",
        structured_output=structured,
    )


class _GroupRuntime:
    def __init__(self, initial: BackgroundTaskGroupResult, waited: BackgroundTaskGroupResult | None = None) -> None:
        self.initial = initial
        self.waited = waited
        self.load_calls: list[dict[str, Any]] = []
        self.wait_calls: list[dict[str, Any]] = []

    def load_background_task_group_result(self, **kwargs: Any) -> BackgroundTaskGroupResult:
        self.load_calls.append(kwargs)
        return self.initial if len(self.load_calls) == 1 else (self.waited or self.initial)

    def wait_for_background_task_group(self, **kwargs: Any) -> BackgroundTaskGroupResult:
        self.wait_calls.append(kwargs)
        assert self.waited is not None
        return self.waited


def _invoke(runtime: _GroupRuntime, arguments: dict[str, object], *, session_id: str = "leader"):
    tool = BackgroundOutputTool(runtime=runtime)
    with bind_runtime_tool_context(RuntimeToolInvocationContext(session_id=session_id)):
        return tool.invoke(ToolCall(tool_name="background_output", arguments=arguments), workspace=Path("."))


def test_group_output_forwards_parent_context_and_task_ids() -> None:
    group = BackgroundTaskGroupResult(
        parallel_group_id=None,
        expected_task_count=2,
        results=(_result("a", "completed"), _result("b", "failed")),
    )
    runtime = _GroupRuntime(group)

    result = _invoke(runtime, {"task_ids": ["a", "b"]})

    assert result.data["task_ids"] == ["a", "b"]
    assert result.data["counts"]["completed"] == 1
    assert result.data["counts"]["failed"] == 1
    assert runtime.load_calls[0]["task_ids"] == ("a", "b")
    assert runtime.load_calls[0]["parallel_group_id"] is None
    assert runtime.load_calls[0]["parent_session_id"] == "leader"


def test_group_selector_blocks_until_aggregate_or_reports_timeout() -> None:
    initial = BackgroundTaskGroupResult(
        parallel_group_id="group-1",
        expected_task_count=2,
        results=(_result("a", "completed"), _result("b", "running")),
    )
    timed_out = BackgroundTaskGroupResult(
        parallel_group_id="group-1",
        expected_task_count=2,
        results=initial.results,
        timed_out=True,
    )
    runtime = _GroupRuntime(initial, timed_out)

    result = _invoke(runtime, {"parallel_group_id": "group-1", "block": True, "timeout": 1000})

    assert result.data["parallel_group_id"] == "group-1"
    assert result.data["status"] == "running"
    assert result.data["timed_out"] is True
    assert result.data["block_timed_out"] is True
    assert runtime.wait_calls[0]["parallel_group_id"] == "group-1"
    assert runtime.wait_calls[0]["parent_session_id"] == "leader"
    assert runtime.wait_calls[0]["timeout_seconds"] == 1.0


def test_group_result_structured_output_and_summary_are_bounded() -> None:
    long_value = "x" * 10_000
    group = BackgroundTaskGroupResult(
        parallel_group_id="group-structured",
        expected_task_count=1,
        results=(_result("a", "completed", structured={"payload": long_value}),),
    )
    runtime = _GroupRuntime(group)

    result = _invoke(runtime, {"parallel_group_id": "group-structured"})

    item = result.data["results"][0]
    assert item["structured_output"]["truncated"] is True
    assert len(item["structured_output"]["preview"]) == 4000
    assert len(result.data["summary"]) <= 4000
    assert "child transcript" not in result.content


def test_group_read_requires_runtime_parent_context() -> None:
    group = BackgroundTaskGroupResult(parallel_group_id="group-1", expected_task_count=1, results=(_result("a", "completed"),))
    runtime = _GroupRuntime(group)
    tool = BackgroundOutputTool(runtime=runtime)

    with pytest.raises(RuntimeError, match="active runtime tool invocation context"):
        tool.invoke(ToolCall(tool_name="background_output", arguments={"parallel_group_id": "group-1"}), workspace=Path("."))
