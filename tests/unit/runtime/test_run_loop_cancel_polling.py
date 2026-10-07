from __future__ import annotations

import threading
import time
from pathlib import Path

from voidcode.core.tool_context import ToolContext
from voidcode.runtime.config import RuntimeConfig
from voidcode.runtime.service import ToolRegistry, VoidCodeRuntime
from voidcode.runtime.tool_execution import RuntimeToolExecutor
from voidcode.tools.contracts import TextOutput, ToolCall, ToolDefinition, ToolInvocation, ToolResult, ToolSuccess


class _AbortSignal:
    def __init__(self) -> None:
        self._cancelled = False
        self.reason: str | None = None

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def cancel(self, reason: str) -> None:
        self._cancelled = True
        self.reason = reason


class _ProgressHangingTool:
    definition = ToolDefinition(name="shell_exec", description="Progress-capable hang.")

    def __init__(self) -> None:
        self.started = False

    def invoke(self, call: ToolCall, *, context: ToolContext) -> ToolResult:
        _ = call, context
        self.started = True
        time.sleep(9999)
        return ToolSuccess(tool_name=self.definition.name, output=TextOutput("unreachable"))


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

    while not tool.started:
        time.sleep(0.01)
    abort_signal.cancel("stop tool")
    consumer.join(timeout=2.0)

    assert consumer.is_alive() is False
    assert errors == []
    assert tool_outcome
    assert isinstance(tool_outcome[0], RuntimeError)
    assert "stop tool" in str(tool_outcome[0])
