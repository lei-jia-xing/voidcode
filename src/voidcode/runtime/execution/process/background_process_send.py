from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, field_validator

from ....core.tool_context import ToolContext
from ....tools._pydantic_args import NonEmptyProcessId, parse_tool_args
from ....tools.contracts import TextOutput, ToolCall, ToolFailure, ToolResult, ToolSuccess

if TYPE_CHECKING:
    from .background_process import BackgroundProcessRuntime

from .background_process_results import BackgroundProcessSendBody, BackgroundProcessStaleBody


class _BackgroundProcessSendArgs(BaseModel):
    process_id: NonEmptyProcessId
    input: str
    newline: bool = True

    @field_validator("input", mode="after")
    @classmethod
    def _validate_input(cls, value: str) -> str:
        if not value:
            raise ValueError("input must be a non-empty string")
        return value


class BackgroundProcessSendTool:
    name = "background_process_send"

    def __init__(self, *, runtime: BackgroundProcessRuntime) -> None:
        self._runtime = runtime

    def invoke(self, call: ToolCall, *, context: ToolContext) -> ToolResult:
        workspace = context.require_workspace()
        caller_session_id = context.require_session_id()
        args = parse_tool_args(_BackgroundProcessSendArgs, call.arguments, tool_name=self.name)

        text = args.input if not args.newline else f"{args.input}\n"
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
            return ToolFailure(
                tool_name=self.name,
                error=message,
                output=TextOutput(message),
                body=BackgroundProcessStaleBody(
                    process_id=state.process_id,
                    pid=state.process.pid,
                    status=state.status,
                    prior_runtime=True,
                    reconciliation_reason=None,
                    running=None,
                    observed_running=state.observed_running,
                    identity_match=state.identity_match,
                    controllable=False,
                ),
            )
        manager.write(
            args.process_id,
            text,
            workspace=workspace,
            owner_session_id=caller_session_id,
            enforce_owner=True,
        )
        return ToolSuccess(
            tool_name=self.name,
            output=TextOutput(f"Sent input to background process {args.process_id}."),
            body=BackgroundProcessSendBody(
                process_id=args.process_id,
                input=args.input,
                newline=args.newline,
            ),
        )
