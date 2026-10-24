"""The `shell_exec` tool: one workspace command run under the core's process lease.

The lease owns the machine — it spawns the process group, bounds each stream's capture, stops the
group on timeout or lease cancellation, and hands back the decoded text. This tool owns the
contract around it: the command interceptor, timeout validation, the preview/artifact split per
stream, and the failure shapes a caller reads off the result body.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import ClassVar, final

from pydantic import BaseModel, ConfigDict, Field

from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.services import ArtifactReference, PluginServices
from voidcode.sdk.tools import (
    OutputBounds,
    OutputReference,
    TextOutput,
    ToolCall,
    ToolDefinition,
    ToolEffect,
    ToolFailure,
    ToolResult,
    ToolSuccess,
)

from .args import NonEmptyCommand, OptionalDescription

DEFAULT_TIMEOUT_SECONDS = 120
MAX_TIMEOUT_SECONDS = 600

#: Per-stream capture bound handed to the lease. What the lease captured is the artifact's copy of
#: the stream; the body itself carries only a bounded preview of it.
_CAPTURE_BYTES = 128 * 1024 * 1024
#: Characters kept inline from each end of a stream. The middle is omitted from the preview, never from
#: the capture: the artifact keeps the whole bounded stream.
_PREVIEW_CHARS = 25 * 1024
_ARTIFACT_MIME = "text/plain; charset=utf-8"
#: Longest progress chunk forwarded to the context's progress sink.
_PROGRESS_CHUNK_CHARS = 12_000

_BASH_INTERCEPTOR_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^\s*cat\s+(?![-<])"), "Use the `read` tool instead of `cat <file>`."),
    (re.compile(r"^\s*(grep|rg)\s+"), "Use the `grep` tool instead of grep/rg."),
    (re.compile(r"^\s*sed\s+(-i|--in-place)"), "Use the `edit` tool instead of sed -i."),
    (re.compile(r"^\s*find\s+"), "Use the `glob` tool instead of find."),
)
_SHELL_SEGMENT_SPLIT = re.compile(r"&&|\|\||;|\|")


def _check_bash_interception(command: str) -> str | None:
    # ponytail: naive split on shell operators ignores quoting, so a quoted
    # "a && cat f" can false-positive; use a real shell tokenizer if that matters.
    for segment in _SHELL_SEGMENT_SPLIT.split(command):
        for pattern, message in _BASH_INTERCEPTOR_RULES:
            if pattern.match(segment):
                return f"Blocked: {message}\n\nOriginal command: {command}"
    return None


@dataclass(frozen=True, slots=True)
class ShellExecResultBody:
    command: str
    exit_code: int | None
    stdout: str
    stderr: str
    timeout: int
    truncated: bool = False
    stdout_truncated: bool | None = None
    stderr_truncated: bool | None = None
    cwd: str | None = None
    stdout_artifact: ArtifactReference | None = None
    stderr_artifact: ArtifactReference | None = None
    output_limit_exceeded: bool = False
    timed_out: bool = False
    cancelled: bool = False
    reason: str | None = None

    def as_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "command": self.command,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "timeout": self.timeout,
            "truncated": self.truncated,
        }
        if self.stdout_truncated is not None:
            payload["stdout_truncated"] = self.stdout_truncated
        if self.stderr_truncated is not None:
            payload["stderr_truncated"] = self.stderr_truncated
        if self.cwd is not None:
            payload["cwd"] = self.cwd
        if self.stdout_artifact is not None:
            payload["stdout_artifact"] = self.stdout_artifact.as_payload()
        if self.stderr_artifact is not None:
            payload["stderr_artifact"] = self.stderr_artifact.as_payload()
        if self.output_limit_exceeded:
            payload["output_limit_exceeded"] = True
        if self.timed_out:
            payload["timed_out"] = True
        if self.cancelled:
            payload.update(cancelled=True, reason=self.reason)
        return payload


def _artifact_reference(artifact: ArtifactReference | None) -> OutputReference | None:
    if artifact is None:
        return None
    return OutputReference(f"voidcode://artifact/{artifact.sha256}", artifact.as_payload())


class ShellExecArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    timeout: int = Field(default=DEFAULT_TIMEOUT_SECONDS, ge=1, le=MAX_TIMEOUT_SECONDS)
    command: NonEmptyCommand
    description: OptionalDescription = None


def _shell_argv(command: str) -> list[str]:
    """The shell wrapper the lease spawns directly; the lease never interprets a command string."""
    if os.name == "nt":
        return ["cmd.exe", "/c", command]
    return ["/bin/sh", "-c", command]


def _preview(text: str) -> str:
    """Head and tail of one stream with the omitted middle marked; short streams pass through."""
    if len(text) <= _PREVIEW_CHARS * 2:
        return text
    return text[:_PREVIEW_CHARS] + "\n... output preview omitted ...\n" + text[-_PREVIEW_CHARS:]


def _preview_truncated(text: str) -> bool:
    return len(text) > _PREVIEW_CHARS * 2


def _spill_stream(services: PluginServices, text: str) -> ArtifactReference | None:
    """The stream's full bounded capture as an artifact; `None` when empty."""
    return services.spill_artifact(text, media_type=_ARTIFACT_MIME) if text else None


def _progress_sink(context: ToolContext) -> Callable[[str, bytes], None] | None:
    """One observational hook: every chunk the lease reads is forwarded to the context's sink.

    The lease already swallows sink failures, so a broken sink can never alter the command result.
    """
    emit = context.emit_tool_progress
    if emit is None:
        return None
    run_id = context.run_id
    invocation_id = context.invocation_id

    def on_output(stream_name: str, chunk: bytes) -> None:
        text = chunk.decode("utf-8", errors="replace").replace("\r\n", "\n")
        payload: dict[str, object] = {"stream": stream_name, "text": text[:_PROGRESS_CHUNK_CHARS]}
        if run_id:
            payload["run_id"] = run_id
        if invocation_id:
            payload["invocation_id"] = invocation_id
        emit(payload)

    return on_output


@final
class ShellExecTool:
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="shell_exec",
        description="Execute a command inside the current workspace.",
        input_schema={
            "type": "object",
            "properties": {
                "command": {"type": "string", "minLength": 1, "description": "Shell command to execute in the workspace."},
                "timeout": {"type": "integer", "minimum": 1, "maximum": 600, "description": "Timeout in seconds (max 600)"},
                "description": {"type": "string", "description": "Human-readable description of the command"},
            },
            "required": ["command"],
            "additionalProperties": False,
        },
        effects=frozenset({ToolEffect.EXECUTE, ToolEffect.SPAWN}),
    )

    def invoke(self, call: ToolCall, *, context: ToolContext, services: PluginServices) -> ToolResult:
        workspace = context.require_workspace()
        args = ShellExecArgs.model_validate(dict(call.arguments))
        command_text = args.command
        # Interception is a pre-flight refusal: a refused command never reaches the lease.
        intercepted = _check_bash_interception(command_text)
        if intercepted is not None:
            raise ValueError(intercepted)

        workspace_root = Path(workspace).resolve()
        if not workspace_root.exists() or not workspace_root.is_dir():
            raise ValueError("shell_exec workspace must be an existing directory")
        timeout_seconds = args.timeout

        run_process = getattr(services, "run_process", None)
        if not callable(run_process):
            raise ValueError("shell_exec requires the core process lease")

        result = run_process(
            _shell_argv(command_text),
            cwd=workspace_root,
            timeout_seconds=float(timeout_seconds),
            max_output_bytes=_CAPTURE_BYTES,
            on_output=_progress_sink(context),
        )

        stdout = _preview(result.stdout)
        stderr = _preview(result.stderr)
        stdout_artifact = _spill_stream(services, result.stdout)
        stderr_artifact = _spill_stream(services, result.stderr)
        output_limit_exceeded = result.truncated
        output_truncated = _preview_truncated(result.stdout) or _preview_truncated(result.stderr) or output_limit_exceeded
        output_reference = _artifact_reference(stdout_artifact) or _artifact_reference(stderr_artifact)
        output = f"{stderr}{stdout}" if output_truncated and stderr else f"{stdout}{stderr}"
        artifact_refs = [f"voidcode://artifact/{artifact.sha256}" for artifact in (stdout_artifact, stderr_artifact) if artifact is not None]
        if output_truncated and artifact_refs:
            output = "Captured process output: " + ", ".join(artifact_refs) + "\n\n" + output

        body = ShellExecResultBody(
            command=command_text,
            cwd=str(workspace_root),
            exit_code=result.exit_code,
            stdout=stdout,
            stderr=stderr,
            timeout=timeout_seconds,
            truncated=output_truncated,
            stdout_truncated=_preview_truncated(result.stdout),
            stderr_truncated=_preview_truncated(result.stderr),
            stdout_artifact=stdout_artifact,
            stderr_artifact=stderr_artifact,
            output_limit_exceeded=output_limit_exceeded,
        )
        bounds = OutputBounds(
            truncated=output_truncated,
            partial=output_truncated,
            reference=output_reference,
        )
        partial_bounds = OutputBounds(
            truncated=output_truncated,
            partial=True,
            reference=output_reference,
        )

        if result.timed_out:
            # The host reports timeout separately from a natural child exit code.
            stopped_body = replace(body, timed_out=True)
            return ToolFailure(
                tool_name=self.definition.name,
                error=f"shell_exec command timed out after {timeout_seconds}s",
                output=TextOutput(output, bounds=partial_bounds),
                body=stopped_body,
                timeout_seconds=timeout_seconds,
            )

        if result.cancelled:
            reason = context.abort_signal.reason if context.abort_signal is not None else None
            content = "User aborted the command."
            return ToolFailure(
                tool_name=self.definition.name,
                error=content,
                output=TextOutput(
                    f"{content}\n\n{output}" if output else content,
                    bounds=partial_bounds,
                ),
                body=replace(body, cancelled=True, reason=reason),
                timeout_seconds=timeout_seconds,
            )

        if output_limit_exceeded:
            return ToolFailure(
                tool_name=self.definition.name,
                error="shell_exec output exceeded the 128 MiB capture limit; partial output is attached.",
                output=TextOutput(output, bounds=bounds),
                body=body,
            )
        if result.exit_code != 0:
            return ToolFailure(
                tool_name=self.definition.name,
                error=stderr or f"Command exited with code {result.exit_code}.",
                output=TextOutput(stdout, bounds=bounds),
                body=body,
            )
        return ToolSuccess(
            tool_name=self.definition.name,
            output=TextOutput(output, bounds=bounds),
            body=body,
        )
