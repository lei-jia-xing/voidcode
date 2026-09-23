from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel

from .._pydantic_args import NonEmptyCommand, OptionalDescription, parse_tool_args
from ..contracts import RuntimeToolTimeoutError, ToolCall, ToolResult
from ..runtime_context import current_runtime_tool_context

if TYPE_CHECKING:
    from .background_process import BackgroundProcessRuntime


class _BackgroundProcessStartArgs(BaseModel):
    command: NonEmptyCommand
    description: OptionalDescription = None


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
    name = "background_process_start"

    def __init__(self, *, runtime: BackgroundProcessRuntime) -> None:
        self._runtime = runtime

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        args = parse_tool_args(_BackgroundProcessStartArgs, call.arguments, tool_name=self.name)
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
            tool_name=self.name,
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


__all__ = ["BackgroundProcessStartTool"]
