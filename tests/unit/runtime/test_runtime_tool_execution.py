from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from voidcode.runtime.tool_execution import RuntimeToolExecutor, ToolExecutionProgress
from voidcode.tools.contracts import ToolCall, ToolDefinition, ToolInvocation, ToolResult
from voidcode.tools.runtime_context import (
    RuntimeLspToolFacade,
    RuntimeToolInvocationContext,
    current_runtime_tool_context,
)


class _ToolFacade:
    def __getattr__(self, _name: str) -> Any:
        raise AssertionError("facade method should not be called")


def _executor(tmp_path: Path) -> RuntimeToolExecutor:
    facade = _ToolFacade()
    return RuntimeToolExecutor(
        workspace=tmp_path,
        lsp=cast(RuntimeLspToolFacade, facade),
    )


class _ContextProbeTool:
    definition = ToolDefinition(name="probe", description="reads runtime context")

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        context = current_runtime_tool_context()
        assert context is not None
        assert context.session_id == "session-1"
        assert context.parent_session_id == "parent-1"
        assert context.delegation_depth == 2
        assert context.remaining_spawn_budget == 3
        assert context.read_paths == frozenset({"README.md"})
        assert context.read_lines == {"README.md": frozenset({1, 2, 3})}
        assert context.model == "model-1"
        assert context.lsp is not None
        return ToolResult(tool_name=call.tool_name, status="ok", content=str(workspace))


class _ProgressTool:
    definition = ToolDefinition(name="shell_exec", description="emits progress")

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        _ = workspace
        context = current_runtime_tool_context()
        assert context is not None
        assert context.emit_tool_progress is not None
        context.emit_tool_progress({"stream": "stdout", "chunk": "working"})
        return ToolResult(tool_name=call.tool_name, status="ok", content="done")


class _BurstProgressTool:
    definition = ToolDefinition(name="shell_exec", description="emits many progress events")

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        _ = workspace
        context = current_runtime_tool_context()
        assert context is not None
        assert context.emit_tool_progress is not None
        for index in range(10_000):
            context.emit_tool_progress({"stream": "stdout", "chunk": f"chunk-{index}"})
        return ToolResult(tool_name=call.tool_name, status="ok", content="done")


def _drain_execution(
    execution: Any,
) -> tuple[list[ToolExecutionProgress], ToolResult | Exception]:
    progress: list[ToolExecutionProgress] = []
    while True:
        try:
            progress.append(next(execution))
        except StopIteration as completed:
            return progress, completed.value


def test_runtime_tool_executor_binds_context_without_runtime_kernel(tmp_path: Path) -> None:
    executor = _executor(tmp_path)
    invocation = ToolInvocation(
        tool_call=ToolCall(tool_name="probe", arguments={}),
        tool_definition=_ContextProbeTool.definition,
        context=RuntimeToolInvocationContext(
            session_id="session-1",
            parent_session_id="parent-1",
            delegation_depth=2,
            remaining_spawn_budget=3,
            read_paths=frozenset({"README.md"}),
            read_lines={"README.md": frozenset({1, 2, 3})},
            model="model-1",
        ),
    )

    progress, result = _drain_execution(executor.invoke(tool=_ContextProbeTool(), invocation=invocation))

    assert progress == []
    assert isinstance(result, ToolResult)
    assert result.content == str(tmp_path)


def test_runtime_tool_executor_streams_progress_without_runtime_kernel(tmp_path: Path) -> None:
    executor = _executor(tmp_path)
    invocation = ToolInvocation(
        tool_call=ToolCall(tool_name="shell_exec", arguments={}, tool_call_id="call-1"),
        tool_definition=_ProgressTool.definition,
        context=RuntimeToolInvocationContext(session_id="session-1", run_id="run-1", invocation_id="call-1"),
    )

    progress, result = _drain_execution(executor.invoke(tool=_ProgressTool(), invocation=invocation))

    assert [item.payload for item in progress] == [
        {
            "tool": "shell_exec",
            "stream": "stdout",
            "chunk": "working",
            "run_id": "run-1",
            "invocation_id": "call-1",
            "tool_call_id": "call-1",
            "ordinal": 1,
        }
    ]
    assert isinstance(result, ToolResult)
    assert result.content == "done"


def test_runtime_tool_executor_reports_progress_queue_loss_without_blocking_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("voidcode.runtime.tool_execution._PROGRESS_QUEUE_MAX_ITEMS", 1)
    executor = _executor(tmp_path)
    invocation = ToolInvocation(
        tool_call=ToolCall(tool_name="shell_exec", arguments={}, tool_call_id="call-burst"),
        tool_definition=_BurstProgressTool.definition,
        context=RuntimeToolInvocationContext(
            session_id="session-1",
            run_id="run-burst",
            invocation_id="call-burst",
        ),
    )

    progress, result = _drain_execution(executor.invoke(tool=_BurstProgressTool(), invocation=invocation))

    assert isinstance(result, ToolResult)
    assert result.content == "done"
    gaps = [item.payload for item in progress if item.payload.get("gap") is True]
    assert gaps
    assert sum(int(item["dropped_count"]) for item in gaps) > 0
    assert all(item["loss_reason"] == "progress_queue_full" for item in gaps)
    assert all(item["run_id"] == "run-burst" for item in gaps)
    assert all(item["invocation_id"] == "call-burst" for item in gaps)
    ordinals = [int(item.payload["ordinal"]) for item in progress]
    assert ordinals == sorted(ordinals)


def test_runtime_tool_executor_rejects_legacy_keyword_invocation(tmp_path: Path) -> None:
    executor = _executor(tmp_path)
    legacy_invoke = cast(Any, executor.invoke)
    with pytest.raises(TypeError):
        legacy_invoke(
            tool=_ProgressTool(),
            tool_call=ToolCall(tool_name="shell_exec", arguments={}),
            session_id="session-1",
        )


def test_tool_invocation_rejects_call_definition_name_mismatch() -> None:
    with pytest.raises(ValueError, match="same tool"):
        ToolInvocation(
            tool_call=ToolCall(tool_name="read"),
            tool_definition=ToolDefinition(name="write", description="write"),
            context=RuntimeToolInvocationContext(session_id="session-1"),
        )


class _TimeoutContextProbeTool:
    definition = ToolDefinition(name="timeout_probe", description="reads timeout")

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        _ = workspace
        context = current_runtime_tool_context()
        assert context is not None
        assert context.tool_timeout_seconds == 7
        return ToolResult(tool_name=call.tool_name, status="ok", content="timeout-propagated")


def test_runtime_tool_executor_propagates_invocation_context_timeout(tmp_path: Path) -> None:
    executor = _executor(tmp_path)
    invocation = ToolInvocation(
        tool_call=ToolCall(tool_name="timeout_probe"),
        tool_definition=_TimeoutContextProbeTool.definition,
        context=RuntimeToolInvocationContext(session_id="session-1", tool_timeout_seconds=7),
    )
    progress, result = _drain_execution(executor.invoke(tool=_TimeoutContextProbeTool(), invocation=invocation))

    assert progress == []
    assert isinstance(result, ToolResult)
    assert result.content == "timeout-propagated"
