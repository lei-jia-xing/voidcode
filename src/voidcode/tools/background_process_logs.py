from __future__ import annotations

from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ValidationError, field_validator

from ._pydantic_args import format_validation_error
from .background_process_start import BackgroundProcessManager
from .contracts import ToolCall, ToolDefinition, ToolResult
from .output import _artifact_metadata


class _BackgroundProcessLogsArgs(BaseModel):
    process_id: str

    @field_validator("process_id", mode="after")
    @classmethod
    def _validate_process_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("process_id must be a non-empty string")
        return value


class BackgroundProcessLogsRuntime(Protocol):
    @property
    def background_process_manager(self) -> BackgroundProcessManager: ...


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
    definition = ToolDefinition(
        name="background_process_logs",
        description="Read accumulated stdout/stderr from a background process.",
        input_schema={"process_id": {"type": "string"}},
        read_only=True,
    )

    def __init__(self, *, runtime: BackgroundProcessLogsRuntime) -> None:
        self._runtime = runtime

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        _ = workspace
        try:
            args = _BackgroundProcessLogsArgs.model_validate(call.arguments)
        except ValidationError as exc:
            raise ValueError(format_validation_error(self.definition.name, exc)) from exc

        state = self._runtime.background_process_manager.load(args.process_id)
        if state is None:
            raise ValueError(f"unknown background process: {args.process_id}")
        stdout_artifact = getattr(state, "stdout_artifact", None)
        stderr_artifact = getattr(state, "stderr_artifact", None)
        if state.stdout_dropped_lines > 0 and stdout_artifact is None:
            stdout_artifact = _artifact_metadata(
                session_id=None,
                tool_call_id=state.process_id,
                tool_name=self.definition.name,
                content="".join(state.stdout_chunks),
                kind="content",
            )
            state.stdout_artifact = stdout_artifact
        if state.stderr_dropped_lines > 0 and stderr_artifact is None:
            stderr_artifact = _artifact_metadata(
                session_id=None,
                tool_call_id=state.process_id,
                tool_name=self.definition.name,
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
            references.append(f"artifact:{stdout_artifact['artifact_id']}")
        if stderr_artifact is not None:
            references.append(f"artifact:{stderr_artifact['artifact_id']}")
        if truncated and references:
            hint = (
                f"[Background process logs truncated: use {', '.join(references)} "
                "to inspect retained log tails. Earlier dropped lines are no longer available.]"
            )
            output = f"{output}\n\n{hint}" if output else hint
        running = state.process.poll() is None
        exit_code = state.process.poll()
        guidance = _background_process_logs_guidance(running=running)
        output = f"{output}\n\nGuidance: {guidance}" if output else f"Guidance: {guidance}"
        return ToolResult(
            tool_name=self.definition.name,
            status="ok",
            content=output,
            data={
                "process_id": state.process_id,
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
            retry_guidance=guidance,
        )
