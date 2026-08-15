from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from voidcode.runtime.tool_execution import RuntimeToolExecutor, ToolExecutionProgress
from voidcode.tools.contracts import ToolCall, ToolResult
from voidcode.tools.runtime_context import (
    RuntimeLspToolFacade,
    RuntimeMemoryToolFacade,
    current_runtime_tool_context,
)


class _ToolFacade:
    def __getattr__(self, _name: str) -> Any:
        raise AssertionError("facade method should not be called")


def _executor(tmp_path: Path) -> RuntimeToolExecutor:
    facade = _ToolFacade()
    return RuntimeToolExecutor(
        workspace=tmp_path,
        memory=cast(RuntimeMemoryToolFacade, facade),
        lsp=cast(RuntimeLspToolFacade, facade),
    )


class _ContextProbeTool:
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
        assert context.memory is not None
        assert context.lsp is not None
        return ToolResult(tool_name=call.tool_name, status="ok", content=str(workspace))


class _ProgressTool:
    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        _ = workspace
        context = current_runtime_tool_context()
        assert context is not None
        assert context.emit_tool_progress is not None
        context.emit_tool_progress({"stream": "stdout", "chunk": "working"})
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

    progress, result = _drain_execution(
        executor.invoke(
            tool=_ContextProbeTool(),
            tool_call=ToolCall(tool_name="probe", arguments={}),
            read_paths=frozenset({"README.md"}),
            read_lines={"README.md": frozenset({1, 2, 3})},
            tool_timeout=None,
            session_id="session-1",
            parent_session_id="parent-1",
            delegation_depth=2,
            remaining_spawn_budget=3,
            abort_signal=None,
            model="model-1",
        )
    )

    assert progress == []
    assert isinstance(result, ToolResult)
    assert result.content == str(tmp_path)


def test_runtime_tool_executor_streams_progress_without_runtime_kernel(tmp_path: Path) -> None:
    executor = _executor(tmp_path)

    progress, result = _drain_execution(
        executor.invoke(
            tool=_ProgressTool(),
            tool_call=ToolCall(tool_name="shell_exec", arguments={}),
            read_paths=frozenset(),
            read_lines={},
            tool_timeout=None,
            session_id="session-1",
            parent_session_id=None,
            delegation_depth=0,
            remaining_spawn_budget=None,
            abort_signal=None,
            model=None,
        )
    )

    assert [item.payload for item in progress] == [{"tool": "shell_exec", "stream": "stdout", "chunk": "working"}]
    assert isinstance(result, ToolResult)
    assert result.content == "done"
