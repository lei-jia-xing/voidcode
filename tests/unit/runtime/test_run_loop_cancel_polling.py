from __future__ import annotations

import threading
from pathlib import Path

from voidcode.core.tool_context import ToolContext
from voidcode.runtime.config import RuntimeConfig
from voidcode.runtime.service import ToolRegistry, VoidCodeRuntime
from voidcode.runtime.tool_execution import RuntimeToolExecutor
from voidcode.tools.contracts import (
    TextOutput,
    ToolCall,
    ToolDefinition,
    ToolFailure,
    ToolInvocation,
    ToolResult,
    ToolSuccess,
    UnconfirmedStop,
)


class _AbortSignal:
    def __init__(self) -> None:
        self._cancelled = False
        self.reason: str | None = None

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def set_cancelled(self, value: bool, *, reason: str | None = None) -> None:
        self._cancelled = value
        if value:
            self.reason = reason


class _ProgressHangingTool:
    definition = ToolDefinition(name="shell_exec", description="Progress-capable hang.")

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()

    def invoke(self, call: ToolCall, *, context: ToolContext) -> ToolResult:
        _ = call, context
        self.started.set()
        try:
            assert self.release.wait(timeout=5.0), "tool was not released"
            return ToolSuccess(tool_name=self.definition.name, output=TextOutput("released"))
        finally:
            self.finished.set()


def test_progress_capable_running_tool_interrupts_on_abort_signal(tmp_path: Path) -> None:
    tool = _ProgressHangingTool()
    abort_signal = _AbortSignal()
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        tool_registry=ToolRegistry.from_tools([tool]),
        config=RuntimeConfig(approval_mode="yolo", execution_engine="deterministic"),
    )
    stream = RuntimeToolExecutor(
        workspace=tmp_path,
        lsp=runtime,
    ).invoke(
        tool=tool,
        invocation=ToolInvocation(
            tool_call=ToolCall(tool_name=tool.definition.name, arguments={}),
            tool_definition=tool.definition,
            context=ToolContext(
                workspace=tmp_path,
                session_id="tool-abort",
                abort_signal=abort_signal,
            ),
        ),
    )
    tool_outcome: list[object] = []
    errors: list[BaseException] = []

    def _consume_stream() -> None:
        try:
            while True:
                _ = next(stream)
        except StopIteration as exc:
            tool_outcome.append(exc.value)
        except BaseException as exc:  # pragma: no cover - asserted via errors list
            errors.append(exc)

    consumer = threading.Thread(target=_consume_stream)
    consumer.start()
    try:
        assert tool.started.wait(timeout=5.0)
        abort_signal.set_cancelled(True, reason="stop tool")
        consumer.join(timeout=2.0)

        assert consumer.is_alive() is False
        assert errors == []
        assert tool_outcome
        assert tool.finished.is_set() is False
        failure = tool_outcome[0]
        assert isinstance(failure, ToolFailure)
        assert isinstance(failure.execution, UnconfirmedStop)
        assert failure.execution.cancellation_signalled is True
        assert failure.execution.execution_stopped is False
    finally:
        tool.release.set()
        consumer.join(timeout=2.0)
        if tool.started.is_set():
            assert tool.finished.wait(timeout=2.0)
