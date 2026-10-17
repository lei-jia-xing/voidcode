from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, ClassVar, cast, final

from pydantic import BaseModel

from ..core.tool_context import ToolContext
from ._pydantic_args import NonEmptyCommand, OptionalDescription, parse_tool_args
from .contracts import (
    OutputBounds,
    OutputReference,
    RuntimeToolTimeoutError,
    TextOutput,
    ToolCall,
    ToolDefinition,
    ToolEffect,
    ToolFailure,
    ToolResult,
    ToolSuccess,
)
from .process_output import ProcessOutputCapture, artifact_uri

DEFAULT_TIMEOUT_SECONDS = 120
MAX_TIMEOUT_SECONDS = 600

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


_SHELL_PROGRESS_CHUNK_BYTES = 8192
_SHELL_PROGRESS_CHUNK_CHARS = 12_000


@dataclass(slots=True)
class _ShellProgressState:
    """Assign one ordering domain and byte offsets across both output pipes."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    next_ordinal: int = 1
    offsets: dict[str, int] = field(default_factory=dict)

    def emit(
        self,
        *,
        stream_name: str,
        byte_count: int,
        payload: dict[str, object],
        callback: Callable[[Mapping[str, object]], None],
    ) -> None:
        # Hold the lock through callback invocation so ordinal assignment and
        # queue insertion cannot be reordered by the two reader threads.
        with self.lock:
            payload["ordinal"] = self.next_ordinal
            self.next_ordinal += 1
            payload["offset"] = self.offsets.get(stream_name, 0)
            self.offsets[stream_name] = int(payload["offset"]) + byte_count
            callback(payload)


def _decode_process_output(payload: bytes | None) -> str:
    if payload is None:
        return ""
    decoded = payload.decode("utf-8", errors="replace")
    return decoded.replace("\r\n", "\n")


def _bounded_progress_chunk(text: str) -> tuple[str, bool]:
    if len(text) <= _SHELL_PROGRESS_CHUNK_CHARS:
        return text, False
    return text[:_SHELL_PROGRESS_CHUNK_CHARS], True


def _safe_emit_shell_progress(
    emit_progress: Callable[[Mapping[str, object]], None] | None,
    *,
    stream_name: str,
    chunk: bytes,
    progress_state: _ShellProgressState,
    run_id: str | None,
    invocation_id: str | None,
) -> None:
    if emit_progress is None or not chunk:
        return
    text = _decode_process_output(chunk)
    bounded_text, truncated = _bounded_progress_chunk(text)
    payload: dict[str, object] = {
        "stream": stream_name,
        "chunk": bounded_text,
        "chunk_char_count": len(text),
        "byte_count": len(chunk),
        "truncated": truncated,
    }
    if run_id is not None:
        payload["run_id"] = run_id
    if invocation_id is not None:
        payload["invocation_id"] = invocation_id
        payload["tool_call_id"] = invocation_id
    try:
        progress_state.emit(
            stream_name=stream_name,
            byte_count=len(chunk),
            payload=payload,
            callback=emit_progress,
        )
    except Exception:
        # Progress is observational only; never let streaming failures alter the command result.
        return


def _read_pipe_incrementally(
    pipe: BinaryIO,
    *,
    capture: ProcessOutputCapture,
    errors: list[BaseException],
    emit_progress: Callable[[Mapping[str, object]], None] | None,
    progress_state: _ShellProgressState,
    run_id: str | None,
    invocation_id: str | None,
) -> None:
    try:
        while True:
            reader = getattr(pipe, "read1", None)
            chunk = reader(_SHELL_PROGRESS_CHUNK_BYTES) if callable(reader) else pipe.read(_SHELL_PROGRESS_CHUNK_BYTES)
            if not chunk:
                break
            capture.append(chunk)
            _safe_emit_shell_progress(
                emit_progress,
                stream_name=capture.stream_name,
                chunk=chunk,
                progress_state=progress_state,
                run_id=run_id,
                invocation_id=invocation_id,
            )
    except OSError as exc:
        errors.append(exc)


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_after_process_kill(process: subprocess.Popen[Any], *, timeout: float = 1.0) -> bool:
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return False
    if _is_windows_platform():
        return process.poll() is not None
    deadline = time.monotonic() + timeout
    while _process_group_exists(process.pid) and time.monotonic() < deadline:
        time.sleep(0.02)
    return not _process_group_exists(process.pid)


def _join_reader_thread(thread: threading.Thread, pipe: BinaryIO, *, timeout: float = 1.0) -> None:
    thread.join(timeout=timeout)
    if not thread.is_alive():
        return
    try:
        pipe.close()
    except OSError:
        pass
    thread.join(timeout=timeout)


def kill_timed_out_process(process: subprocess.Popen[Any]) -> None:
    taskkill = _taskkill_command()
    if taskkill is not None:
        taskkill_succeeded = False
        try:
            completed = subprocess.run(
                [taskkill, "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                check=False,
            )
            taskkill_succeeded = completed.returncode == 0
        except OSError:
            pass
        if taskkill_succeeded:
            return
        try:
            process.kill()
        except ProcessLookupError:
            pass
        return
    killpg = getattr(os, "killpg", None)
    sigkill = getattr(signal, "SIGKILL", None)
    if killpg is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        return
    if sigkill is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        return
    try:
        killpg(process.pid, sigkill)
    except AttributeError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    except ProcessLookupError:
        pass


def _taskkill_command() -> str | None:
    if not _is_windows_platform():
        return None
    taskkill = shutil.which("taskkill")
    if taskkill is not None:
        return taskkill
    return "taskkill"


def _is_windows_platform() -> bool:
    return os.name == "nt"


@final
class ShellExecTool:
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="shell_exec",
        description="Execute a command inside the current workspace.",
        input_schema={
            "command": {"type": "string", "minLength": 1, "description": "Shell command to execute in the workspace."},
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds (max 600)",
            },
            "description": {
                "type": "string",
                "description": "Human-readable description of the command",
            },
            "required": ["command"],
        },
        effects=frozenset({ToolEffect.EXECUTE, ToolEffect.SPAWN}),
    )

    def invoke(self, call: ToolCall, *, context: ToolContext) -> ToolResult:
        return self._invoke(call, context=context, runtime_timeout_seconds=None)

    def invoke_with_runtime_timeout(
        self,
        call: ToolCall,
        *,
        context: ToolContext,
        timeout_seconds: int,
    ) -> ToolResult:
        return self._invoke(call, context=context, runtime_timeout_seconds=timeout_seconds)

    def _invoke(
        self,
        call: ToolCall,
        *,
        context: ToolContext,
        runtime_timeout_seconds: int | None,
    ) -> ToolResult:
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
        runtime_timeout_selected = runtime_timeout_seconds is not None and runtime_timeout_seconds < timeout_seconds
        if runtime_timeout_selected:
            timeout_seconds = runtime_timeout_seconds

        try:
            process = subprocess.Popen(
                command_text,
                cwd=workspace_root,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
        except OSError as exc:
            raise ValueError(f"shell_exec failed to execute command: {exc}") from exc

        abort_signal = context.abort_signal
        emit_progress = context.emit_tool_progress
        progress_run_id = context.run_id
        progress_invocation_id = context.invocation_id or call.tool_call_id
        deadline = time.monotonic() + timeout_seconds
        stdout_capture = ProcessOutputCapture.create("stdout")
        stderr_capture = ProcessOutputCapture.create("stderr")
        reader_errors: list[BaseException] = []
        progress_state = _ShellProgressState()
        timed_out = False
        aborted = False
        stop_confirmed = True
        # Boundary: subprocess.Popen types the PIPEs as IO[bytes] | None; the
        # cast pins the BinaryIO shape the incremental reader expects.
        stdout_pipe = cast(BinaryIO, process.stdout)
        stderr_pipe = cast(BinaryIO, process.stderr)
        stdout_reader = threading.Thread(
            target=_read_pipe_incrementally,
            kwargs={
                "pipe": stdout_pipe,
                "capture": stdout_capture,
                "errors": reader_errors,
                "emit_progress": emit_progress,
                "progress_state": progress_state,
                "run_id": progress_run_id,
                "invocation_id": progress_invocation_id,
            },
            name="shell-exec-stdout-reader",
            daemon=True,
        )
        stderr_reader = threading.Thread(
            target=_read_pipe_incrementally,
            kwargs={
                "pipe": stderr_pipe,
                "capture": stderr_capture,
                "errors": reader_errors,
                "emit_progress": emit_progress,
                "progress_state": progress_state,
                "run_id": progress_run_id,
                "invocation_id": progress_invocation_id,
            },
            name="shell-exec-stderr-reader",
            daemon=True,
        )
        stdout_reader.start()
        stderr_reader.start()
        while True:
            if reader_errors:
                kill_timed_out_process(process)
                stop_confirmed = _wait_after_process_kill(process)
                break
            if abort_signal is not None and abort_signal.cancelled:
                aborted = True
                kill_timed_out_process(process)
                stop_confirmed = _wait_after_process_kill(process)
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                kill_timed_out_process(process)
                stop_confirmed = _wait_after_process_kill(process)
                break
            try:
                process.wait(timeout=min(0.05, remaining))
                break
            except subprocess.TimeoutExpired:
                continue

        _join_reader_thread(stdout_reader, stdout_pipe)
        _join_reader_thread(stderr_reader, stderr_pipe)

        stdout = stdout_capture.preview()
        stderr = stderr_capture.preview()
        stdout_artifact = stdout_capture.artifact(
            session_id=context.session_id,
            tool_call_id=progress_invocation_id,
            tool_name=self.definition.name,
        )
        stderr_artifact = stderr_capture.artifact(
            session_id=context.session_id,
            tool_call_id=progress_invocation_id,
            tool_name=self.definition.name,
        )
        output_limit_exceeded = stdout_capture.limit_exceeded or stderr_capture.limit_exceeded
        output_truncated = stdout_capture.preview_truncated or stderr_capture.preview_truncated or output_limit_exceeded
        output_reference = _artifact_reference(stdout_artifact) or _artifact_reference(stderr_artifact)
        stdout_capture.close()
        stderr_capture.close()

        if reader_errors:
            first_error = reader_errors[0]
            message = f"shell_exec failed while reading process output: {first_error}"
            raise ValueError(message) from first_error

        output = f"{stderr}{stdout}" if output_truncated and stderr else f"{stdout}{stderr}"
        artifact_refs = [uri for artifact in (stdout_artifact, stderr_artifact) if (uri := artifact_uri(artifact)) is not None]
        if output_truncated and artifact_refs:
            output = "Full process output: " + ", ".join(artifact_refs) + "\n\n" + output

        if timed_out:
            if runtime_timeout_selected:
                content = f"tool '{self.definition.name}' exceeded runtime timeout of {timeout_seconds}s"
                partial_result = ToolFailure(
                    tool_name=self.definition.name,
                    error=content,
                    output=TextOutput(
                        output,
                        bounds=OutputBounds(
                            truncated=output_truncated,
                            partial=True,
                            reference=output_reference,
                        ),
                    ),
                    body=ShellExecResultBody(
                        command=command_text,
                        cwd=str(workspace_root),
                        exit_code=process.returncode,
                        stdout=stdout,
                        stderr=stderr,
                        timeout=timeout_seconds,
                        truncated=output_truncated,
                        stdout_truncated=stdout_capture.preview_truncated or stdout_capture.limit_exceeded,
                        stderr_truncated=stderr_capture.preview_truncated or stderr_capture.limit_exceeded,
                        output_char_count=len(output),
                        interrupted=True,
                        timed_out=True,
                        stdout_artifact=stdout_artifact,
                        stderr_artifact=stderr_artifact,
                        output_limit_exceeded=output_limit_exceeded,
                        stop_confirmed=stop_confirmed,
                    ),
                    timeout_seconds=timeout_seconds,
                )
                raise RuntimeToolTimeoutError(
                    content,
                    partial_result=partial_result,
                    execution_stopped=stop_confirmed,
                )
            return ToolFailure(
                tool_name=self.definition.name,
                error=f"shell_exec command timed out after {timeout_seconds}s",
                output=TextOutput(
                    output,
                    bounds=OutputBounds(
                        truncated=output_truncated,
                        partial=True,
                        reference=output_reference,
                    ),
                ),
                body=ShellExecResultBody(
                    command=command_text,
                    cwd=str(workspace_root),
                    exit_code=process.returncode,
                    stdout=stdout,
                    stderr=stderr,
                    timeout=timeout_seconds,
                    truncated=output_truncated,
                    stdout_truncated=stdout_capture.preview_truncated or stdout_capture.limit_exceeded,
                    stderr_truncated=stderr_capture.preview_truncated or stderr_capture.limit_exceeded,
                    output_char_count=len(output),
                    interrupted=True,
                    timed_out=True,
                    stdout_artifact=stdout_artifact,
                    stderr_artifact=stderr_artifact,
                    output_limit_exceeded=output_limit_exceeded,
                    stop_confirmed=stop_confirmed,
                ),
                timeout_seconds=timeout_seconds,
            )

        if aborted:
            reason = abort_signal.reason if abort_signal is not None else None
            content = "User aborted the command."
            return ToolFailure(
                tool_name=self.definition.name,
                error=content,
                output=TextOutput(
                    f"{content}\n\n{output}" if output else content,
                    bounds=OutputBounds(
                        truncated=output_truncated,
                        partial=True,
                        reference=output_reference,
                    ),
                ),
                body=ShellExecResultBody(
                    command=command_text,
                    cwd=str(workspace_root),
                    exit_code=process.returncode,
                    stdout=stdout,
                    stderr=stderr,
                    timeout=timeout_seconds,
                    truncated=output_truncated,
                    stdout_truncated=stdout_capture.preview_truncated or stdout_capture.limit_exceeded,
                    stderr_truncated=stderr_capture.preview_truncated or stderr_capture.limit_exceeded,
                    output_char_count=len(output),
                    interrupted=True,
                    cancelled=True,
                    reason=reason,
                    stdout_artifact=stdout_artifact,
                    stderr_artifact=stderr_artifact,
                    output_limit_exceeded=output_limit_exceeded,
                    stop_confirmed=stop_confirmed,
                ),
                timeout_seconds=timeout_seconds,
            )

        body = ShellExecResultBody(
            command=command_text,
            cwd=str(workspace_root),
            exit_code=process.returncode,
            stdout=stdout,
            stderr=stderr,
            timeout=timeout_seconds,
            truncated=output_truncated,
            stdout_truncated=stdout_capture.preview_truncated or stdout_capture.limit_exceeded,
            stderr_truncated=stderr_capture.preview_truncated or stderr_capture.limit_exceeded,
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
        if output_limit_exceeded:
            return ToolFailure(
                tool_name=self.definition.name,
                error="shell_exec output exceeded the 128 MiB capture limit; partial output is attached.",
                output=TextOutput(output, bounds=bounds),
                body=body,
            )
        if process.returncode != 0:
            return ToolFailure(
                tool_name=self.definition.name,
                error=stderr or f"Command exited with code {process.returncode}.",
                output=TextOutput(stdout, bounds=bounds),
                body=body,
            )
        return ToolSuccess(
            tool_name=self.definition.name,
            output=TextOutput(output, bounds=bounds),
            body=body,
        )
