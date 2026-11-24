"""The governed ``!`` command, executed through the invocation's process service."""

from __future__ import annotations

from concurrent.futures import CancelledError
from typing import ClassVar

from voidcode.capabilities.exec.shell_exec import SHELL_CAPTURE_BYTES, shell_argv
from voidcode.sdk.commands import CommandDefinition, CommandInvocation, CommandResult
from voidcode.sdk.json_values import json_wire_object
from voidcode.sdk.services import ArtifactReference, PluginServices, ProcessResult
from voidcode.sdk.tools import ToolEffect

from .user_shell import (
    CAPTURE_TRUNCATION_MARKER,
    DEFAULT_TIMEOUT_SECONDS,
    MAX_SHELL_EVENT_BYTES,
    MAX_TIMEOUT_SECONDS,
    PREVIEW_BYTES,
    ShellErrorCode,
    bounded_command,
    bounded_event_payload,
    error_guidance,
    error_message,
    event_payload_size,
    preview_text,
)

_ARTIFACT_MIME = "text/plain; charset=utf-8"
_MAX_DETAIL_BYTES = 512


def _command_text(value: object) -> str | None:
    """Return the client-provided command verbatim; only emptiness is a rejection."""
    if not isinstance(value, str) or not value.strip() or value.strip() == "!":
        return None
    return value


def _timeout_seconds(value: object) -> int | None:
    if value is None:
        return DEFAULT_TIMEOUT_SECONDS
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if not 1 <= value <= MAX_TIMEOUT_SECONDS:
        return None
    return value


def _abort_cancelled(invocation: CommandInvocation) -> bool:
    signal = invocation.context.abort_signal
    return bool(signal is not None and signal.cancelled)


def _artifact_payload(reference: ArtifactReference | None) -> dict[str, object] | None:
    return None if reference is None else reference.as_payload()


def _artifact_uri(reference: ArtifactReference | None) -> str | None:
    return None if reference is None else reference.uri


def _artifact_hint(stdout_ref: ArtifactReference | None, stderr_ref: ArtifactReference | None) -> str:
    refs = [
        f"stdout {stdout_ref.uri}" if stdout_ref is not None else "",
        f"stderr {stderr_ref.uri}" if stderr_ref is not None else "",
    ]
    rendered = "; ".join(part for part in refs if part)
    return rendered or "output artifacts unavailable"


def _base_data(command: str, timeout: int | None) -> dict[str, object]:
    bounded, truncated = bounded_command(command)
    return {
        "command": bounded,
        "command_truncated": truncated,
        "exit_code": None,
        "status": "failed",
        "duration_seconds": 0.0,
        "timeout_seconds": timeout,
        "capture_truncated": False,
        "preview_truncated": False,
        "timed_out": False,
        "cancelled": False,
        "stdout_artifact": None,
        "stderr_artifact": None,
        "stdout_uri": None,
        "stderr_uri": None,
    }


def _with_error(data: dict[str, object], code: ShellErrorCode, *, detail: str | None = None) -> dict[str, object]:
    failed = dict(data)
    failed["status"] = "cancelled" if code is ShellErrorCode.CANCELLED else "timed_out" if code is ShellErrorCode.TIMED_OUT else "failed"
    failed["error"] = code.value
    failed["guidance"] = error_guidance(code)
    if detail:
        failed["detail"] = detail
    return failed


def _failure(code: ShellErrorCode, *, output: str | None = None, data: dict[str, object] | None = None, detail: str | None = None) -> CommandResult:
    body = _with_error(data or {}, code, detail=detail)
    return CommandResult(status="error", error=error_message(code), output=output, data=body)


def _bounded_detail(exc: BaseException) -> str:
    return str(exc).encode("utf-8")[:_MAX_DETAIL_BYTES].decode("utf-8", errors="ignore")


def _process_error_code(exc: BaseException) -> ShellErrorCode:
    message = str(exc)
    if "sandbox helper failed" in message:
        return ShellErrorCode.SANDBOX_FAILED
    if "while reading process output" in message or "while reading helper output" in message:
        return ShellErrorCode.CAPTURE_FAILED
    if "failed to start" in message or "sandbox run failed to start" in message:
        return ShellErrorCode.START_FAILED
    return ShellErrorCode.PROCESS_FAILED


def _spill_streams(
    services: PluginServices, result: ProcessResult, *, cancelled: bool
) -> tuple[ArtifactReference | None, ArtifactReference | None, BaseException | None]:
    """Store captured streams once each; cancellation retains only the bounded preview."""
    if cancelled:
        return None, None, None
    stdout_ref: ArtifactReference | None = None
    stderr_ref: ArtifactReference | None = None
    try:
        stdout_ref = services.spill_artifact(result.stdout, media_type=_ARTIFACT_MIME)
        stderr_ref = services.spill_artifact(result.stderr, media_type=_ARTIFACT_MIME)
    except CancelledError as exc:
        return None, None, exc
    except Exception as exc:  # noqa: BLE001 - report a stable artifact failure, never a raw crash
        return stdout_ref, stderr_ref, exc
    return stdout_ref, stderr_ref, None


def _preview_output(
    result: ProcessResult,
    *,
    cancelled: bool,
    stdout_ref: ArtifactReference | None,
    stderr_ref: ArtifactReference | None,
) -> tuple[str, bool]:
    preview, preview_truncated = preview_text(result.stdout, result.stderr)
    lines: list[str] = []
    if cancelled:
        lines.append("command cancelled")
    elif result.timed_out:
        lines.append("command timed out")
    if preview:
        lines.append(preview)
    if result.truncated:
        lines.append(CAPTURE_TRUNCATION_MARKER.lstrip("\n"))
    if preview_truncated:
        marker = f"[preview truncated at {PREVIEW_BYTES} bytes"
        refs = [
            f"stdout {stdout_ref.uri}" if stdout_ref is not None else "",
            f"stderr {stderr_ref.uri}" if stderr_ref is not None else "",
        ]
        rendered = "; ".join(part for part in refs if part)
        if rendered:
            marker = f"{marker}; {rendered}"
        elif cancelled:
            marker = f"{marker}; full output is not retained after cancellation"
        lines.append(f"{marker}]")
    output = "\n".join(lines)
    if not output:
        output = f"(no output; exit code {result.exit_code})"
    return output, preview_truncated


class ShellCommand:
    definition: ClassVar[CommandDefinition] = CommandDefinition(
        name="shell",
        description="Run a shell command on the core workspace (the `!` escape).",
        argument_schema={
            "type": "object",
            "properties": {
                "command": {"type": "string", "minLength": 1, "description": "Shell command line to run."},
                "timeout": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_TIMEOUT_SECONDS,
                    "default": DEFAULT_TIMEOUT_SECONDS,
                    "description": "Timeout in seconds (max 600).",
                },
            },
            "required": ["command"],
            "additionalProperties": False,
        },
        effects=frozenset({ToolEffect.EXECUTE}),
        busy_gate=False,
        client_syntax="!",
        # The only client entry is `!`; the exact command id remains callable by RPC/CLI.
        slash_visible=False,
        usage="!<command>",
        examples=("!git status",),
        group="Shell",
        # A user shell keeps an explicit cancelled result and its bounded partial preview.
        preserve_cancelled_result=True,
    )

    def invoke(self, invocation: CommandInvocation, services: PluginServices) -> CommandResult:
        command = _command_text(invocation.arguments.get("command"))
        if command is None:
            return _failure(ShellErrorCode.EMPTY_COMMAND, data=_base_data("", None))
        timeout = _timeout_seconds(invocation.arguments.get("timeout"))
        if timeout is None:
            return _failure(ShellErrorCode.INVALID_TIMEOUT, data=_base_data(command, None))
        try:
            workspace = invocation.context.require_workspace()
        except RuntimeError:
            return _failure(ShellErrorCode.WORKSPACE_UNAVAILABLE, data=_base_data(command, timeout))

        try:
            result = services.run_process(
                shell_argv(command),
                cwd=workspace,
                timeout_seconds=timeout,
                max_output_bytes=SHELL_CAPTURE_BYTES,
            )
        except Exception as exc:  # noqa: BLE001 - stable code/guidance: the process may have run
            code = _process_error_code(exc)
            return _failure(code, data=_base_data(command, timeout), detail=_bounded_detail(exc))

        cancelled = result.cancelled or _abort_cancelled(invocation)
        stdout_ref, stderr_ref, artifact_error = _spill_streams(services, result, cancelled=cancelled)
        if isinstance(artifact_error, CancelledError):
            cancelled = True
            stdout_ref = stderr_ref = None
            artifact_error = None

        output, preview_truncated = _preview_output(
            result,
            cancelled=cancelled,
            stdout_ref=stdout_ref,
            stderr_ref=stderr_ref,
        )
        data = _base_data(command, timeout)
        data.update(
            {
                "exit_code": result.exit_code,
                "duration_seconds": result.duration_ms / 1000,
                "capture_truncated": result.truncated,
                "preview_truncated": preview_truncated,
                "timed_out": result.timed_out,
                "cancelled": cancelled,
                "stdout_artifact": _artifact_payload(stdout_ref),
                "stderr_artifact": _artifact_payload(stderr_ref),
                "stdout_uri": _artifact_uri(stdout_ref),
                "stderr_uri": _artifact_uri(stderr_ref),
            }
        )

        if cancelled:
            code = ShellErrorCode.CANCELLED
            data = _with_error(data, code)
            message = "shell command was cancelled; only the bounded partial preview is retained"
        elif artifact_error is not None:
            code = ShellErrorCode.ARTIFACT_FAILED
            data = _with_error(data, code, detail=_bounded_detail(artifact_error))
            message = error_message(code)
        elif result.timed_out:
            code = ShellErrorCode.TIMED_OUT
            data = _with_error(data, code)
            message = f"shell command timed out after {timeout}s; captured prefix: {_artifact_hint(stdout_ref, stderr_ref)}"
        elif result.exit_code != 0:
            code = ShellErrorCode.NONZERO_EXIT
            data = _with_error(data, code)
            message = f"shell command exited with code {result.exit_code}; full output: {_artifact_hint(stdout_ref, stderr_ref)}"
        else:
            code = None
            data["status"] = "completed"
            message = None

        if code is not None:
            result = CommandResult(status="error", error=message or error_message(code), output=output, data=data)
        else:
            result = CommandResult(status="ok", output=output, data=data)
        if event_payload_size(result.as_payload()) > MAX_SHELL_EVENT_BYTES:
            result = _failure(ShellErrorCode.EVENT_TOO_LARGE, data=_base_data(command, timeout))

        try:
            event_payload = bounded_event_payload(json_wire_object(result.data or {}))
            services.stage_event("shell_executed", event_payload)
        except Exception as exc:  # noqa: BLE001 - never commit an oversized/refused receipt
            return _failure(
                ShellErrorCode.EVENT_TOO_LARGE, output=result.output, data=json_wire_object(result.data or {}), detail=_bounded_detail(exc)
            )
        return result
