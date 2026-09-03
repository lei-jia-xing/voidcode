from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator

from ._pydantic_args import format_validation_error
from .background_process_logs import BackgroundProcessLogsTool
from .background_process_send import BackgroundProcessSendTool
from .background_process_start import BackgroundProcessManager, BackgroundProcessStartTool
from .background_process_stop import BackgroundProcessStopTool
from .contracts import ToolCall, ToolDefinition, ToolResult
from .runtime_context import current_runtime_tool_context

_MAX_BACKGROUND_PROCESS_ROWS = 64


class _StrictArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _StartArgs(_StrictArgs):
    op: Literal["start"]
    command: str
    description: str | None = None

    @field_validator("command", mode="after")
    @classmethod
    def _validate_command(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("command must not be empty")
        return value

    @field_validator("description", mode="after")
    @classmethod
    def _validate_description(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("description must not be empty when provided")
        return value


class _PsArgs(_StrictArgs):
    op: Literal["ps"]


class _ProcessIdArgs(_StrictArgs):
    process_id: str

    @field_validator("process_id", mode="after")
    @classmethod
    def _validate_process_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("process_id must be a non-empty string")
        return value


class _LogsArgs(_ProcessIdArgs):
    op: Literal["logs"]


class _StopArgs(_ProcessIdArgs):
    op: Literal["stop"]


class _SendArgs(_ProcessIdArgs):
    op: Literal["send"]
    input: str
    newline: bool = True

    @field_validator("input", mode="after")
    @classmethod
    def _validate_input(cls, value: str) -> str:
        if not value:
            raise ValueError("input must be a non-empty string")
        return value


_BackgroundProcessArgs = Annotated[
    _StartArgs | _PsArgs | _LogsArgs | _SendArgs | _StopArgs,
    Field(discriminator="op"),
]
_ARGS_ADAPTER = TypeAdapter(_BackgroundProcessArgs)


class BackgroundProcessRuntime(Protocol):
    @property
    def background_process_manager(self) -> BackgroundProcessManager: ...


class BackgroundProcessTool:
    """Unified model-facing facade for runtime-owned background processes."""

    definition = ToolDefinition(
        name="background_process",
        description=(
            "Manage a long-running background process. Use op=start to launch, ps to list the "
            "current workspace processes, logs to read bounded output, send to write stdin, or "
            "stop to terminate a process. Process rows are scoped to the current owner and workspace."
        ),
        input_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": True,
            "oneOf": [
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["op", "command"],
                    "properties": {
                        "op": {"const": "start"},
                        "command": {"type": "string"},
                        "description": {"type": "string"},
                    },
                },
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["op"],
                    "properties": {"op": {"const": "ps"}},
                },
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["op", "process_id"],
                    "properties": {
                        "op": {"const": "logs"},
                        "process_id": {"type": "string"},
                    },
                },
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["op", "process_id", "input"],
                    "properties": {
                        "op": {"const": "send"},
                        "process_id": {"type": "string"},
                        "input": {"type": "string"},
                        "newline": {"type": "boolean", "default": True},
                    },
                },
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["op", "process_id"],
                    "properties": {
                        "op": {"const": "stop"},
                        "process_id": {"type": "string"},
                    },
                },
            ],
        },
        # This is intentionally false: operation_class_for_tool classifies ps/logs as reads
        # and start/send/stop as executes before the facade is invoked.
        read_only=False,
    )

    def __init__(self, *, runtime: BackgroundProcessRuntime) -> None:
        self._runtime = runtime
        self._start = BackgroundProcessStartTool(runtime=runtime)
        self._logs = BackgroundProcessLogsTool(runtime=runtime)
        self._send = BackgroundProcessSendTool(runtime=runtime)
        self._stop = BackgroundProcessStopTool(runtime=runtime)

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        try:
            args = _ARGS_ADAPTER.validate_python(call.arguments)
        except ValidationError as exc:
            raise ValueError(format_validation_error(self.definition.name, exc)) from exc

        if isinstance(args, _StartArgs):
            return self._rename(self._start.invoke(self._delegated_call(call, args.model_dump(exclude={"op"})), workspace=workspace))
        if isinstance(args, _LogsArgs):
            return self._rename(self._logs.invoke(self._delegated_call(call, args.model_dump(exclude={"op"})), workspace=workspace))
        if isinstance(args, _SendArgs):
            return self._rename(self._send.invoke(self._delegated_call(call, args.model_dump(exclude={"op"})), workspace=workspace))
        if isinstance(args, _StopArgs):
            return self._rename(self._stop.invoke(self._delegated_call(call, args.model_dump(exclude={"op"})), workspace=workspace))
        return self._ps(workspace=workspace)

    @staticmethod
    def _delegated_call(call: ToolCall, arguments: dict[str, object]) -> ToolCall:
        return ToolCall(tool_name=call.tool_name, arguments=arguments, tool_call_id=call.tool_call_id)

    def _ps(self, *, workspace: Path) -> ToolResult:
        context = current_runtime_tool_context()
        states = self._runtime.background_process_manager.list_processes(
            workspace=workspace,
            owner_session_id=context.session_id if context is not None else None,
            enforce_owner=context is not None,
            limit=_MAX_BACKGROUND_PROCESS_ROWS,
        )
        rows: list[dict[str, object]] = []
        for state in states:
            stale = state.prior_runtime or state.status == "stale"
            running = None if stale else state.process.poll() is None
            rows.append(
                {
                    "process_id": state.process_id,
                    "pid": state.process.pid,
                    "command": state.command,
                    "cwd": state.cwd,
                    "status": "stale" if stale else state.status,
                    "running": running,
                    "exit_code": None if stale else state.process.poll(),
                    "prior_runtime": state.prior_runtime,
                    "observed_running": state.observed_running,
                    "identity_match": state.identity_match,
                    "controllable": not stale,
                }
            )
        return ToolResult(
            tool_name=self.definition.name,
            status="ok",
            content=f"Background process list: {len(rows)} process(es).",
            data={"processes": rows, "count": len(rows), "limit": _MAX_BACKGROUND_PROCESS_ROWS},
        )

    @staticmethod
    def _rename(result: ToolResult) -> ToolResult:
        return ToolResult(
            tool_name="background_process",
            status=result.status,
            content=result.content,
            data=result.data,
            error=result.error,
            diagnostics=result.diagnostics,
            truncated=result.truncated,
            partial=result.partial,
            timeout_seconds=result.timeout_seconds,
            source=result.source,
            fallback_reason=result.fallback_reason,
            reference=result.reference,
        )


__all__ = ["BackgroundProcessRuntime", "BackgroundProcessTool", "_MAX_BACKGROUND_PROCESS_ROWS"]
