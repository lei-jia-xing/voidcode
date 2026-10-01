from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel

from ....core.tool_context import ToolContext
from ....tools._pydantic_args import NonEmptyProcessId, parse_tool_args
from ....tools.contracts import ToolCall, ToolResult
from ....tools.output import _artifact_metadata

if TYPE_CHECKING:
    from .background_process import BackgroundProcessRuntime


class _BackgroundProcessLogsArgs(BaseModel):
    process_id: NonEmptyProcessId


def _background_process_logs_guidance(*, running: bool) -> str:
    if running:
        return (
            "This is a bounded retained-tail status read, not a continuous watch loop. "
            "Report the current status, continue other work, or wait for a meaningful "
            "state change before reading logs again; do not immediately reread the same tail."
        )
    return (
        "This is a bounded retained-tail status read, not a continuous watch loop. "
        "The process is no longer running; use this retained tail as the final "
        "process status unless a specific follow-up decision or meaningful state "
        "change needs one last read of the retained tail."
    )


class BackgroundProcessLogsTool:
    name = "background_process_logs"

    def __init__(self, *, runtime: BackgroundProcessRuntime) -> None:
        self._runtime = runtime

    def invoke(self, call: ToolCall, *, context: ToolContext) -> ToolResult:
        workspace = context.require_workspace()
        caller_session_id = context.require_session_id()
        args = parse_tool_args(_BackgroundProcessLogsArgs, call.arguments, tool_name=self.name)

        state = self._runtime.background_process_manager.load(
            args.process_id,
            workspace=workspace,
            owner_session_id=caller_session_id,
            enforce_owner=True,
        )
        if state is None:
            raise ValueError(f"unknown background process: {args.process_id}")
        if state.prior_runtime:
            message = state.reconciliation_reason or (
                "Prior-runtime managed process was observed after restart; it was not reattached "
                "and is externally managed/unavailable to this runtime."
            )
            return ToolResult(
                tool_name=self.name,
                status="error",
                content=message,
                error=message,
                data={
                    "process_id": state.process_id,
                    "pid": state.process.pid,
                    "status": "stale",
                    "prior_runtime": True,
                    "reconciliation_reason": message,
                    "running": None,
                    "observed_running": state.observed_running,
                    "identity_match": state.identity_match,
                    "controllable": False,
                },
            )
        stdout_artifact = state.stdout_artifact
        stderr_artifact = state.stderr_artifact
        if state.stdout_dropped_lines > 0 and stdout_artifact is None:
            stdout_artifact = _artifact_metadata(
                session_id=None,
                tool_call_id=state.process_id,
                tool_name=self.name,
                content="".join(state.stdout_chunks),
                kind="content",
            )
            state.stdout_artifact = stdout_artifact
        if state.stderr_dropped_lines > 0 and stderr_artifact is None:
            stderr_artifact = _artifact_metadata(
                session_id=None,
                tool_call_id=state.process_id,
                tool_name=self.name,
                content="".join(state.stderr_chunks),
                kind="error",
            )
            state.stderr_artifact = stderr_artifact
        stdout = "".join(state.stdout_chunks)
        stderr = "".join(state.stderr_chunks)
        output = stdout if not stderr else f"{stdout}{stderr}" if stdout else stderr
        truncated = state.stdout_dropped_lines > 0 or state.stderr_dropped_lines > 0
        references: list[str] = []
        if stdout_artifact is not None:
            references.append(f"voidcode://artifact/{stdout_artifact['artifact_id']}")
        if stderr_artifact is not None:
            references.append(f"voidcode://artifact/{stderr_artifact['artifact_id']}")
        if truncated and references:
            hint = (
                f"[Background process logs truncated: use {', '.join(references)} "
                "with read to inspect retained log tails. Earlier dropped lines are no longer available.]"
            )
            output = f"{output}\n\n{hint}" if output else hint
        running = state.process.poll() is None
        exit_code = state.process.poll()
        guidance = _background_process_logs_guidance(running=running)
        output = f"{output}\n\nGuidance: {guidance}" if output else f"Guidance: {guidance}"
        return ToolResult(
            tool_name=self.name,
            status="ok",
            content=output,
            data={
                "process_id": state.process_id,
                "status": state.status,
                "prior_runtime": state.prior_runtime,
                "reconciliation_reason": state.reconciliation_reason,
                "running": running,
                "exit_code": exit_code,
                "stdout": stdout,
                "stderr": stderr,
                "stdout_retained_lines": len(state.stdout_chunks),
                "stderr_retained_lines": len(state.stderr_chunks),
                "stdout_dropped_lines": state.stdout_dropped_lines,
                "stderr_dropped_lines": state.stderr_dropped_lines,
                "stdout_artifact": stdout_artifact,
                "stderr_artifact": stderr_artifact,
                "truncated": truncated,
                "references": references,
                "guidance": guidance,
            },
            truncated=truncated,
            partial=truncated,
            reference=references[0] if references else None,
        )
