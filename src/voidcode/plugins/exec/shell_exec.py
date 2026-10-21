"""The `shell_exec` tool: one workspace command run under the kernel's process lease.

The lease owns the machine — it spawns the process group, bounds each stream's capture, stops the
group on timeout or lease cancellation, and hands back the decoded text. This tool owns the
contract around it: the command interceptor, the timeout clamp, the preview/artifact split per
stream, and the failure shapes a caller reads off the result body.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import ClassVar, final

from pydantic import BaseModel

from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.services import PluginServices
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

from .args import NonEmptyCommand, OptionalDescription, parse_tool_args
from .artifacts import artifact_uri, create_artifact

DEFAULT_TIMEOUT_SECONDS = 120
MAX_TIMEOUT_SECONDS = 600

#: Per-stream capture bound handed to the lease. What the lease captured is the artifact's copy of
#: the stream; the body itself carries only a bounded preview of it.
_CAPTURE_BYTES = 128 * 1024 * 1024
#: Bytes kept inline from each end of a stream. The middle is omitted from the preview, never from
#: the capture: the artifact keeps the whole bounded stream.
_PREVIEW_BYTES = 25 * 1024
_ARTIFACT_MIME = "text/plain; charset=utf-8"
_ARTIFACT_SOURCE = "process_output"
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
    output_char_count: int | None = None
    cwd: str | None = None
    stdout_artifact: dict[str, object] | None = None
    stderr_artifact: dict[str, object] | None = None
    output_limit_exceeded: bool = False
    stop_confirmed: bool | None = None
    interrupted: bool = False
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
        if self.output_char_count is not None:
            payload["output_char_count"] = self.output_char_count
        if self.cwd is not None:
            payload["cwd"] = self.cwd
        if self.stdout_artifact is not None:
            payload["stdout_artifact"] = self.stdout_artifact
        if self.stderr_artifact is not None:
            payload["stderr_artifact"] = self.stderr_artifact
        if self.output_limit_exceeded:
            payload["output_limit_exceeded"] = True
        if self.stop_confirmed is not None:
            payload["stop_confirmed"] = self.stop_confirmed
        if self.interrupted:
            payload["interrupted"] = True
        if self.timed_out:
            payload["timed_out"] = True
        if self.cancelled:
            payload.update(cancelled=True, reason=self.reason)
        return payload


def _artifact_reference(artifact: dict[str, object] | None) -> OutputReference | None:
    artifact_id = artifact.get("artifact_id") if artifact is not None else None
    if not isinstance(artifact_id, str):
        return None
    return OutputReference(f"voidcode://artifact/{artifact_id}", artifact)


class ShellExecArgs(BaseModel):
    command: NonEmptyCommand
    description: OptionalDescription = None


def _shell_argv(command: str) -> list[str]:
    """The shell wrapper the lease spawns directly; the lease never interprets a command string."""
    if os.name == "nt":
        return ["cmd.exe", "/c", command]
    return ["/bin/sh", "-c", command]


def _preview(text: str) -> str:
    """Head and tail of one stream with the omitted middle marked; short streams pass through."""
    if len(text) <= _PREVIEW_BYTES * 2:
        return text
    return text[:_PREVIEW_BYTES] + "\n... output preview omitted ...\n" + text[-_PREVIEW_BYTES:]


def _preview_truncated(text: str) -> bool:
    return len(text) > _PREVIEW_BYTES * 2


def _spill_stream(services: object, stream_name: str, text: str) -> dict[str, object] | None:
    """The stream's full bounded capture as an artifact; `None` when empty or unserved."""
    if not text:
        return None
    created = create_artifact(
        services,
        kind=stream_name,
        content=text,
        mime=_ARTIFACT_MIME,
        source=_ARTIFACT_SOURCE,
    )
    return dict(created) if created is not None else None


def _progress_sink(context: ToolContext) -> Callable[[str, bytes], None] | None:
    """One observational hook: every chunk the lease reads is forwarded to the context's sink.

    The lease already swallows sink failures, so a broken sink can never alter the command result.
    """
    emit = getattr(context, "emit_tool_progress", None)
    if not callable(emit):
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
        args = parse_tool_args(
            ShellExecArgs,
            {
                "command": call.arguments.get("command"),
                "description": call.arguments.get("description"),
            },
            tool_name=self.definition.name,
        )
        command_text = args.command
        # Interception is a pre-flight refusal: a refused command never reaches the lease.
        intercepted = _check_bash_interception(command_text)
        if intercepted is not None:
            raise ValueError(intercepted)

        timeout_value = call.arguments.get("timeout", DEFAULT_TIMEOUT_SECONDS)
        workspace_root = Path(workspace).resolve()
        if not workspace_root.exists() or not workspace_root.is_dir():
            raise ValueError("shell_exec workspace must be an existing directory")
        timeout_seconds = (
            min(int(timeout_value), MAX_TIMEOUT_SECONDS) if isinstance(timeout_value, (int, float)) and timeout_value > 0 else DEFAULT_TIMEOUT_SECONDS
        )

        run_process = getattr(services, "run_process", None)
        if not callable(run_process):
            raise ValueError("shell_exec requires the kernel process lease")

        result = run_process(
            _shell_argv(command_text),
            cwd=workspace_root,
            timeout_seconds=float(timeout_seconds),
            max_output_bytes=_CAPTURE_BYTES,
            on_output=_progress_sink(context),
        )

        stdout = _preview(result.stdout)
        stderr = _preview(result.stderr)
        stdout_artifact = _spill_stream(services, "stdout", result.stdout)
        stderr_artifact = _spill_stream(services, "stderr", result.stderr)
        output_limit_exceeded = result.truncated
        output_truncated = _preview_truncated(result.stdout) or _preview_truncated(result.stderr) or output_limit_exceeded
        output_reference = _artifact_reference(stdout_artifact) or _artifact_reference(stderr_artifact)
        output = f"{stderr}{stdout}" if output_truncated and stderr else f"{stdout}{stderr}"
        artifact_refs = [uri for artifact in (stdout_artifact, stderr_artifact) if (uri := artifact_uri(artifact)) is not None]
        if output_truncated and artifact_refs:
            output = "Full process output: " + ", ".join(artifact_refs) + "\n\n" + output

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
            output_char_count=len(output),
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
            # The lease stopped the group itself and drained its pipes, so the stop is confirmed;
            # `exit_code` is None because the child never reported one of its own.
            stopped_body = replace(body, interrupted=True, timed_out=True, stop_confirmed=True)
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
                body=replace(body, interrupted=True, cancelled=True, reason=reason, stop_confirmed=True),
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
