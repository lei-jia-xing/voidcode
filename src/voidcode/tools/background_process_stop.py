from __future__ import annotations

from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ValidationError, field_validator

from ._pydantic_args import format_validation_error
from .background_process_start import BackgroundProcessManager
from .contracts import ToolCall, ToolDefinition, ToolResult
from .runtime_context import current_runtime_tool_context


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
        input_schema={
            "process_id": {
                "type": "string",
                "description": "Process id returned by background_process_start",
            }
        },
        read_only=False,
    )

    def __init__(self, *, runtime: BackgroundProcessStopRuntime) -> None:
        self._runtime = runtime

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        try:
            args = _BackgroundProcessStopArgs.model_validate(call.arguments)
        except ValidationError as exc:
            raise ValueError(format_validation_error(self.definition.name, exc)) from exc

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
            tool_name=self.definition.name,
            status="ok",
            content=f"Stopped background process {state.process_id}.",
            data={"process_id": state.process_id, "exit_code": state.process.poll(), "running": state.process.poll() is None},
        )
