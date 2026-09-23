from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel

from .._pydantic_args import NonEmptyProcessId, parse_tool_args
from ..contracts import ToolCall, ToolResult
from ..runtime_context import current_runtime_tool_context

if TYPE_CHECKING:
    from .background_process import BackgroundProcessRuntime


class _BackgroundProcessStopArgs(BaseModel):
    process_id: NonEmptyProcessId


class BackgroundProcessStopTool:
    name = "background_process_stop"

    def __init__(self, *, runtime: BackgroundProcessRuntime) -> None:
        self._runtime = runtime

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        args = parse_tool_args(_BackgroundProcessStopArgs, call.arguments, tool_name=self.name)

        context = current_runtime_tool_context()
        manager = self._runtime.background_process_manager
        state = manager.load(
            args.process_id,
            workspace=workspace,
            owner_session_id=context.session_id if context is not None else None,
            enforce_owner=context is not None,
        )
        if state is None:
            raise ValueError(f"unknown background process: {args.process_id}")
        if state.prior_runtime:
            message = state.reconciliation_reason or ("Prior-runtime process is externally managed/unavailable to this runtime")
            return ToolResult(
                tool_name=self.name,
                status="error",
                content=message,
                error=message,
                data={
                    "process_id": state.process_id,
                    "pid": state.process.pid,
                    "status": "stale",
                    "prior_runtime": True,
                    "running": None,
                    "observed_running": state.observed_running,
                    "identity_match": state.identity_match,
                    "controllable": False,
                },
            )
        state = manager.stop(
            args.process_id,
            workspace=workspace,
            owner_session_id=context.session_id if context is not None else None,
            enforce_owner=context is not None,
        )
        return ToolResult(
            tool_name=self.name,
            status="ok",
            content=f"Stopped background process {state.process_id}.",
            data={"process_id": state.process_id, "exit_code": state.process.poll(), "running": state.process.poll() is None},
        )
