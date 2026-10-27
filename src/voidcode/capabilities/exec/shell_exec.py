"""The `shell_exec` tool: one workspace command run under the core's process lease.

The lease owns the machine — it spawns the process group, bounds each stream's capture, stops the
group on timeout or lease cancellation, and hands back the decoded text. This tool owns the
contract around it: timeout validation, the preview/artifact split per
stream, and the failure shapes a caller reads off the result body.
"""

from __future__ import annotations

import codecs
import os
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import ClassVar, final

from pydantic import BaseModel, ConfigDict, Field

from voidcode.sdk.icons import Icon
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.services import ArtifactReference, PluginServices
from voidcode.sdk.tools import (
    OutputBounds,
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


class ShellExecArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    timeout: int = Field(default=DEFAULT_TIMEOUT_SECONDS, ge=1, le=MAX_TIMEOUT_SECONDS, description="Timeout in seconds (max 600).")
    command: NonEmptyCommand = Field(description="Shell command to execute in the workspace.")
    description: OptionalDescription = Field(default=None, description="Human-readable description of the command.")


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
    decoders = {stream: codecs.getincrementaldecoder("utf-8")(errors="replace") for stream in ("stdout", "stderr")}

    def on_output(stream_name: str, chunk: bytes) -> None:
        text = decoders[stream_name].decode(chunk).replace("\r\n", "\n")
        for start in range(0, len(text), _PROGRESS_CHUNK_CHARS):
            payload: dict[str, object] = {"stream": stream_name, "text": text[start : start + _PROGRESS_CHUNK_CHARS]}
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
        icon=Icon(glyph="⌁", fallback="$"),
        description="Execute a command inside the current workspace.",
        input_schema=ShellExecArgs.model_json_schema(),
        effects=frozenset({ToolEffect.EXECUTE, ToolEffect.SPAWN}),
    )

    def invoke(self, call: ToolCall, *, context: ToolContext, services: PluginServices) -> ToolResult:
        workspace = context.require_workspace()
        args = ShellExecArgs.model_validate(dict(call.arguments))
        command_text = args.command
        workspace_root = Path(workspace).resolve()
        if not workspace_root.exists() or not workspace_root.is_dir():
            raise ValueError("shell_exec workspace must be an existing directory")
        timeout_seconds = args.timeout

        result = services.run_process(
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
        output_reference = stdout_artifact or stderr_artifact
        output = f"{stderr}{stdout}" if output_truncated and stderr else f"{stdout}{stderr}"
        artifact_refs = [artifact.uri for artifact in (stdout_artifact, stderr_artifact) if artifact is not None]
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
