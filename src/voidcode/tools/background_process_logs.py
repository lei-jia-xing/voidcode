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
                "to inspect full logs.]"
            )
            output = f"{output}\n\n{hint}" if output else hint
        return ToolResult(
            tool_name=self.definition.name,
            status="ok",
            content=output,
            data={
                "process_id": state.process_id,
                "running": state.process.poll() is None,
                "exit_code": state.process.poll(),
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
            },
            truncated=truncated,
            partial=truncated,
            reference=references[0] if references else None,
        )
