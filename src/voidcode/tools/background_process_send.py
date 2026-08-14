from __future__ import annotations

from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ValidationError, field_validator

from ._pydantic_args import format_validation_error
from .background_process_start import BackgroundProcessManager
from .contracts import ToolCall, ToolDefinition, ToolResult


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
                "description": "Process id returned by background_process_start",
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
        _ = workspace
        try:
            args = _BackgroundProcessSendArgs.model_validate(call.arguments)
        except ValidationError as exc:
            raise ValueError(format_validation_error(self.definition.name, exc)) from exc

        text = args.input if not args.newline else f"{args.input}\n"
        self._runtime.background_process_manager.write(args.process_id, text)
        return ToolResult(
            tool_name=self.definition.name,
            status="ok",
            content=f"Sent input to background process {args.process_id}.",
            data={
                "process_id": args.process_id,
                "input": args.input,
                "newline": args.newline,
            },
        )
