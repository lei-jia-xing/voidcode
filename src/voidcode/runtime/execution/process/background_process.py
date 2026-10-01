from __future__ import annotations

from dataclasses import replace
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator

from ....core.tool_context import ToolContext
from ....tools._pydantic_args import NonEmptyCommand, NonEmptyProcessId, OptionalDescription, format_validation_error
from ....tools.contracts import ToolCall, ToolResult
from ....tools.process.background_process import BackgroundProcessTool
from ...background.process import BackgroundProcessManager
from .background_process_logs import BackgroundProcessLogsTool
from .background_process_send import BackgroundProcessSendTool
from .background_process_start import BackgroundProcessStartTool
from .background_process_stop import BackgroundProcessStopTool

_MAX_BACKGROUND_PROCESS_ROWS = 64


class _StrictArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _StartArgs(_StrictArgs):
    op: Literal["start"]
    command: NonEmptyCommand
    description: OptionalDescription = None


class _PsArgs(_StrictArgs):
    op: Literal["ps"]


class _ProcessIdArgs(_StrictArgs):
    process_id: NonEmptyProcessId


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


class BackgroundProcessCommand:
    def __init__(self, *, runtime: BackgroundProcessRuntime) -> None:
        self._runtime = runtime
        self._start = BackgroundProcessStartTool(runtime=runtime)
        self._logs = BackgroundProcessLogsTool(runtime=runtime)
        self._send = BackgroundProcessSendTool(runtime=runtime)
        self._stop = BackgroundProcessStopTool(runtime=runtime)

    def invoke(self, call: ToolCall, *, context: ToolContext) -> ToolResult:
        if call.tool_name != BackgroundProcessTool.definition.name:
            raise ValueError("process command requires its selected background_process binding")
        context.require_session_id()
        context.require_workspace()
        try:
            args = _ARGS_ADAPTER.validate_python(call.arguments)
        except ValidationError as exc:
            raise ValueError(format_validation_error(BackgroundProcessTool.definition.name, exc)) from exc

        if isinstance(args, _StartArgs):
            return self._rename(self._start.invoke(self._delegated_call(call, args.model_dump(exclude={"op"})), context=context))
        if isinstance(args, _LogsArgs):
            return self._rename(self._logs.invoke(self._delegated_call(call, args.model_dump(exclude={"op"})), context=context))
        if isinstance(args, _SendArgs):
            return self._rename(self._send.invoke(self._delegated_call(call, args.model_dump(exclude={"op"})), context=context))
        if isinstance(args, _StopArgs):
            return self._rename(self._stop.invoke(self._delegated_call(call, args.model_dump(exclude={"op"})), context=context))
        return self._ps(context=context)

    @staticmethod
    def _delegated_call(call: ToolCall, arguments: dict[str, object]) -> ToolCall:
        return ToolCall(tool_name=call.tool_name, arguments=arguments, tool_call_id=call.tool_call_id)

    def _ps(self, *, context: ToolContext) -> ToolResult:
        workspace = context.require_workspace()
        caller_session_id = context.require_session_id()
        states = self._runtime.background_process_manager.list_processes(
            workspace=workspace,
            owner_session_id=caller_session_id,
            enforce_owner=True,
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
            tool_name=BackgroundProcessTool.definition.name,
            status="ok",
            content=f"Background process list: {len(rows)} process(es).",
            data={"processes": rows, "count": len(rows), "limit": _MAX_BACKGROUND_PROCESS_ROWS},
        )

    @staticmethod
    def _rename(result: ToolResult) -> ToolResult:
        return replace(result, tool_name="background_process")
