from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel

from ....core.tool_context import ToolContext
from ....tools._pydantic_args import NonEmptyProcessId, parse_tool_args
from ....tools.contracts import ToolCall, ToolResult

if TYPE_CHECKING:
    from .background_process import BackgroundProcessRuntime


class _BackgroundProcessStopArgs(BaseModel):
    process_id: NonEmptyProcessId


class BackgroundProcessStopTool:
    name = "background_process_stop"

    def __init__(self, *, runtime: BackgroundProcessRuntime) -> None:
        self._runtime = runtime

    def invoke(self, call: ToolCall, *, context: ToolContext) -> ToolResult:
        workspace = context.require_workspace()
        caller_session_id = context.require_session_id()
        args = parse_tool_args(_BackgroundProcessStopArgs, call.arguments, tool_name=self.name)

        manager = self._runtime.background_process_manager
        state = manager.load(
            args.process_id,
            workspace=workspace,
            owner_session_id=caller_session_id,
            enforce_owner=True,
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
            owner_session_id=caller_session_id,
            enforce_owner=True,
        )
        return ToolResult(
            tool_name=self.name,
            status="ok",
            content=f"Stopped background process {state.process_id}.",
            data={"process_id": state.process_id, "exit_code": state.process.poll(), "running": state.process.poll() is None},
        )
