from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import cast

from voidcode.runtime.config import RuntimeConfig
from voidcode.runtime.service import ToolRegistry, VoidCodeRuntime
from voidcode.runtime.session import SessionRef, SessionState
from voidcode.tools.contracts import ToolCall, ToolDefinition, ToolResult


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

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        _ = call, workspace
        self.started = True
        time.sleep(9999)
        return ToolResult(tool_name=self.definition.name, status="ok", content="unreachable")


def test_progress_capable_running_tool_interrupts_on_abort_signal(tmp_path: Path) -> None:
    tool = _ProgressHangingTool()
    abort_signal = _AbortSignal()
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        tool_registry=ToolRegistry.from_tools([tool]),
        config=RuntimeConfig(approval_mode="allow", execution_engine="deterministic"),
    )
    session = SessionState(SessionRef(id="tool-abort"), status="running")
    stream = runtime._run_loop_coordinator._invoke_tool_with_progress_events(
        tool=tool,
        tool_call=ToolCall(tool_name=tool.definition.name, arguments={}),
        workspace=tmp_path,
        tool_timeout=None,
        session=session,
        start_sequence=1,
        tool_call_id="tool-abort-call",
        abort_signal=abort_signal,
        parent_session_id=None,
        delegation_depth=0,
        remaining_spawn_budget=None,
    )
    tool_outcome: list[tuple[object, int]] = []
    errors: list[BaseException] = []

    def _consume_stream() -> None:
        try:
            while True:
                _ = next(stream)
        except StopIteration as exc:
            tool_outcome.append(cast(tuple[object, int], exc.value))
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
    assert isinstance(tool_outcome[0][0], RuntimeError)
    assert "stop tool" in str(tool_outcome[0][0])
