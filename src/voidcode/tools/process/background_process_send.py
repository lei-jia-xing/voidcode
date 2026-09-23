from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, field_validator

from .._pydantic_args import NonEmptyProcessId, parse_tool_args
from ..contracts import ToolCall, ToolResult
from ..runtime_context import current_runtime_tool_context

if TYPE_CHECKING:
    from .background_process import BackgroundProcessRuntime


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

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        args = parse_tool_args(_BackgroundProcessSendArgs, call.arguments, tool_name=self.name)

        text = args.input if not args.newline else f"{args.input}\n"
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
                    "status": state.status,
                    "prior_runtime": True,
                    "running": None,
                    "observed_running": state.observed_running,
                    "identity_match": state.identity_match,
                    "controllable": False,
                },
            )
        manager.write(
            args.process_id,
            text,
            workspace=workspace,
            owner_session_id=context.session_id if context is not None else None,
            enforce_owner=context is not None,
        )
        return ToolResult(
            tool_name=self.name,
            status="ok",
            content=f"Sent input to background process {args.process_id}.",
            data={"process_id": args.process_id, "input": args.input, "newline": args.newline},
        )
