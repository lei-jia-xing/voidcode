from __future__ import annotations

from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ValidationError, field_validator

from ...runtime.background.process import BackgroundProcessManager
from .._pydantic_args import format_validation_error
from ..contracts import RuntimeToolTimeoutError, ToolCall, ToolDefinition, ToolResult
from ..runtime_context import current_runtime_tool_context


class _BackgroundProcessStartArgs(BaseModel):
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


class BackgroundProcessStartRuntime(Protocol):
    @property
    def background_process_manager(self) -> BackgroundProcessManager: ...


def _background_process_start_guidance(*, process_id: str, reused: bool, stale_process_id: str | None = None) -> str:
    if stale_process_id is not None:
        stale_note = (
            f" Prior process '{stale_process_id}' was detected after runtime restart and was not "
            "reattached; it is externally managed/unavailable to this runtime. Do not use that "
            "stale id for logs, send, or stop."
        )
    else:
        stale_note = ""
    if reused:
        return (
            f"An exact-match process is already running; reuse process_id '{process_id}' "
            "instead of calling background_process with op=start again for the same trimmed command "
            "in this workspace." + stale_note
        )
    return f"Track this process by process_id '{process_id}'. Use background_process with op=logs, send, or stop." + stale_note


class BackgroundProcessStartTool:
    definition = ToolDefinition(
        name="background_process_start",
        description="Start a long-running non-interactive process without blocking the current turn.",
        input_schema={
            "command": {"type": "string", "description": "Shell command to start as a long-running background process"},
            "description": {"type": "string", "description": "Human-readable description"},
        },
        read_only=False,
    )

    def __init__(self, *, runtime: BackgroundProcessStartRuntime) -> None:
        self._runtime = runtime

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        try:
            args = _BackgroundProcessStartArgs.model_validate(call.arguments)
        except ValidationError as exc:
            raise ValueError(format_validation_error(self.definition.name, exc)) from exc
        runtime_context = current_runtime_tool_context()
        if runtime_context is not None and runtime_context.abort_signal is not None and runtime_context.abort_signal.cancelled:
            raise RuntimeToolTimeoutError("background_process_start aborted before launching process")
        owner_session_id = runtime_context.session_id if runtime_context is not None else None
        enforce_owner = runtime_context is not None
        manager = self._runtime.background_process_manager
        existing = manager.load_running(
            command=args.command,
            workspace=workspace,
            owner_session_id=owner_session_id,
            enforce_owner=enforce_owner,
        )
        stale_matches = manager.find_stale(
            command=args.command,
            workspace=workspace,
            owner_session_id=owner_session_id,
            enforce_owner=enforce_owner,
        )
        state = manager.start(
            command=args.command,
            workspace=workspace,
            owner_session_id=owner_session_id,
            enforce_owner=enforce_owner,
        )
        reused = existing is not None
        stale_process_id = stale_matches[0].process_id if stale_matches else None
        guidance = _background_process_start_guidance(
            process_id=state.process_id,
            reused=reused,
            stale_process_id=stale_process_id,
        )
        return ToolResult(
            tool_name=self.definition.name,
            status="ok",
            content=(
                f"{'Reusing' if reused else 'Started'} background process {state.process_id} "
                f"(pid={state.process.pid}) for command: {args.command}. Guidance: {guidance}"
            ),
            data={
                "process_id": state.process_id,
                "pid": state.process.pid,
                "command": args.command,
                "cwd": state.cwd,
                "running": state.process.poll() is None,
                "reused": reused,
                "stale_process_id": stale_process_id,
                "guidance": guidance,
            },
        )


__all__ = ["BackgroundProcessStartRuntime", "BackgroundProcessStartTool"]
