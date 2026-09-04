from __future__ import annotations

from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ValidationError, field_validator

from ...runtime.background.process import BackgroundProcessManager
from .._pydantic_args import format_validation_error
from ..contracts import ToolCall, ToolDefinition, ToolResult
from ..runtime_context import current_runtime_tool_context


class _BackgroundProcessSendArgs(BaseModel):
    process_id: str
    input: str
    newline: bool = True

    @field_validator("process_id", mode="after")
    @classmethod
    def _validate_process_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("process_id must be a non-empty string")
        return value

    @field_validator("input", mode="after")
    @classmethod
    def _validate_input(cls, value: str) -> str:
        if not value:
            raise ValueError("input must be a non-empty string")
        return value


class BackgroundProcessSendRuntime(Protocol):
    @property
    def background_process_manager(self) -> BackgroundProcessManager: ...


class BackgroundProcessSendTool:
    definition = ToolDefinition(
        name="background_process_send",
        description="Write interactive input to a running background process's stdin.",
        input_schema={
            "process_id": {
                "type": "string",
                "description": "Process id returned by background_process with op=start",
            },
            "input": {
                "type": "string",
                "description": "Text to write to the process's stdin",
            },
            "newline": {
                "type": "boolean",
                "description": "Append a trailing newline so the input is treated as a completed line (default: true)",
            },
        },
        read_only=False,
    )

    def __init__(self, *, runtime: BackgroundProcessSendRuntime) -> None:
        self._runtime = runtime

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        try:
            args = _BackgroundProcessSendArgs.model_validate(call.arguments)
        except ValidationError as exc:
            raise ValueError(format_validation_error(self.definition.name, exc)) from exc

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
                tool_name=self.definition.name,
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
            tool_name=self.definition.name,
            status="ok",
            content=f"Sent input to background process {args.process_id}.",
            data={"process_id": args.process_id, "input": args.input, "newline": args.newline},
        )
