"""The `shell_exec` tool: one workspace command run under the core's process lease.

The lease owns the machine: it bounds each stream's in-memory decoded capture, stops the group on
timeout or lease cancellation, and returns captured text (not byte-identical raw output). This tool
stores each captured stream once and returns typed references/status. The host projects a text
viewport; neither tool events nor clients receive another copy of a large capture.
"""

from __future__ import annotations

import codecs
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import ClassVar, final

from pydantic import BaseModel, ConfigDict, Field

from voidcode.sdk.icons import Icon
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.services import ArtifactReference, PluginServices
from voidcode.sdk.tools import (
    EmptyOutput,
    OutputBounds,
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

#: Chosen per-stream in-memory capture policy under the process service's 256 MiB ceiling.
#: The service buffers bytes then joins/decodes them; this is not an output/UI transport threshold.
_CAPTURE_BYTES = 128 * 1024 * 1024
_ARTIFACT_MIME = "text/plain; charset=utf-8"
_PROGRESS_CHUNK_CHARS = 12_000


@dataclass(frozen=True, slots=True)
class ShellExecResultBody:
    command: str
    exit_code: int | None
    stdout: ArtifactReference
    stderr: ArtifactReference
    timeout: int
    truncated: bool = False
    cwd: str | None = None
    output_limit_exceeded: bool = False
    timed_out: bool = False
    cancelled: bool = False
    reason: str | None = None

    def as_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "command": self.command,
            "exit_code": self.exit_code,
            "stdout": self.stdout.as_payload(),
            "stderr": self.stderr.as_payload(),
            "timeout": self.timeout,
            "truncated": self.truncated,
        }
        if self.cwd is not None:
            payload["cwd"] = self.cwd
        if self.output_limit_exceeded:
            payload["output_limit_exceeded"] = True
        if self.timed_out:
            payload["timed_out"] = True
        if self.cancelled:
            payload.update(cancelled=True, reason=self.reason)
        return payload


_ARTIFACT_SCHEMA: Mapping[str, object] = {
    "type": "object",
    "required": ["sha256", "bytes", "media_type"],
    "properties": {
        "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "bytes": {"type": "integer", "minimum": 0},
        "media_type": {"type": "string"},
    },
    "additionalProperties": False,
}

#: Declared contract for ``ShellExecResultBody.as_payload``: the base keys are
#: always present, the rest appear only when the capture observed them.
SHELL_EXEC_RESULT_SCHEMA: Mapping[str, object] = {
    "type": "object",
    "required": ["command", "exit_code", "stdout", "stderr", "timeout", "truncated"],
    "properties": {
        "command": {"type": "string"},
        "exit_code": {"type": ["integer", "null"]},
        "stdout": _ARTIFACT_SCHEMA,
        "stderr": _ARTIFACT_SCHEMA,
        "timeout": {"type": "integer", "minimum": 1},
        "truncated": {"type": "boolean"},
        "cwd": {"type": "string"},
        "output_limit_exceeded": {"type": "boolean"},
        "timed_out": {"type": "boolean"},
        "cancelled": {"type": "boolean"},
        "reason": {"type": ["string", "null"]},
    },
    "additionalProperties": False,
}


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


def _spill_stream(services: PluginServices, text: str) -> ArtifactReference:
    """Store all captured UTF-8 text, including an honestly empty stream."""
    return services.spill_artifact(text, media_type=_ARTIFACT_MIME)


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
        icon=Icon(glyph="⌁"),
        description="Execute a command inside the current workspace.",
        input_schema=ShellExecArgs.model_json_schema(),
        effects=frozenset({ToolEffect.EXECUTE, ToolEffect.SPAWN}),
        output_schema=SHELL_EXEC_RESULT_SCHEMA,
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
        stdout = _spill_stream(services, result.stdout)
        stderr = _spill_stream(services, result.stderr)
        output_limit_exceeded = result.truncated
        primary = stderr if result.exit_code != 0 and stderr.bytes else stdout
        output = f"Captured stdout: {stdout.uri}; stderr: {stderr.uri}. Read the referenced captured text for full output."
        body = ShellExecResultBody(
            command=command_text,
            cwd=str(workspace_root),
            exit_code=result.exit_code,
            stdout=stdout,
            stderr=stderr,
            timeout=timeout_seconds,
            truncated=output_limit_exceeded,
            output_limit_exceeded=output_limit_exceeded,
        )
        bounds = OutputBounds(truncated=output_limit_exceeded, reference=primary)
        partial_bounds = replace(bounds, partial=True)

        if result.timed_out:
            # The host reports timeout separately from a natural child exit code.
            stopped_body = replace(body, timed_out=True)
            return ToolFailure(
                tool_name=self.definition.name,
                error=f"shell_exec command timed out after {timeout_seconds}s",
                output=EmptyOutput(presentation=output, bounds=partial_bounds),
                body=stopped_body,
                timeout_seconds=timeout_seconds,
            )

        if result.cancelled:
            reason = context.abort_signal.reason if context.abort_signal is not None else None
            content = "User aborted the command."
            return ToolFailure(
                tool_name=self.definition.name,
                error=content,
                output=EmptyOutput(presentation=f"{content}\n\n{output}", bounds=partial_bounds),
                body=replace(body, cancelled=True, reason=reason),
                timeout_seconds=timeout_seconds,
            )

        if output_limit_exceeded:
            return ToolFailure(
                tool_name=self.definition.name,
                error="shell_exec output exceeded the 128 MiB capture limit; partial output is attached.",
                output=EmptyOutput(presentation=output, bounds=bounds),
                body=body,
            )
        if result.exit_code != 0:
            return ToolFailure(
                tool_name=self.definition.name,
                error=f"Command exited with code {result.exit_code}; captured stderr: {stderr.uri}.",
                output=EmptyOutput(presentation=output, bounds=bounds),
                body=body,
            )
        return ToolSuccess(
            tool_name=self.definition.name,
            output=EmptyOutput(presentation=output, bounds=bounds),
            body=body,
        )
