from __future__ import annotations

from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ValidationError, field_validator

from ._pydantic_args import format_validation_error
from .background_process_start import BackgroundProcessManager
from .contracts import ToolCall, ToolDefinition, ToolResult


class _BackgroundProcessStopArgs(BaseModel):
    process_id: str

    @field_validator("process_id", mode="after")
    @classmethod
    def _validate_process_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("process_id must be a non-empty string")
        return value


class BackgroundProcessStopRuntime(Protocol):
    @property
    def background_process_manager(self) -> BackgroundProcessManager: ...


class BackgroundProcessStopTool:
    definition = ToolDefinition(
        name="background_process_stop",
        description="Stop a background process by id.",
        input_schema={"process_id": {"type": "string"}},
        read_only=False,
    )

    def __init__(self, *, runtime: BackgroundProcessStopRuntime) -> None:
        self._runtime = runtime

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        _ = workspace
        try:
            args = _BackgroundProcessStopArgs.model_validate(call.arguments)
        except ValidationError as exc:
            raise ValueError(format_validation_error(self.definition.name, exc)) from exc

        state = self._runtime.background_process_manager.stop(args.process_id)
        return ToolResult(
            tool_name=self.definition.name,
            status="ok",
            content=f"Stopped background process {state.process_id}.",
            data={
                "process_id": state.process_id,
                "exit_code": state.process.poll(),
                "running": state.process.poll() is None,
            },
        )
