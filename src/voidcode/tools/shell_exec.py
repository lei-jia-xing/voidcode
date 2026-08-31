from __future__ import annotations

import os
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, ClassVar, cast, final

from pydantic import BaseModel, ValidationError, field_validator

from ..security.shell_policy import (
    DEFAULT_TIMEOUT_SECONDS,
    non_interactive_shell_env,
    resolve_shell_execution_policy,
)
from ._pydantic_args import format_validation_error
from .contracts import RuntimeToolTimeoutError, ToolCall, ToolDefinition, ToolResult
from .runtime_context import current_runtime_tool_context


class ShellExecArgs(BaseModel):
    command: str
    description: str | None = None

    @field_validator("command", mode="after")
    @classmethod
    def _validate_command(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("command must not be empty")
        return value

    @field_validator("description", mode="after")
    @classmethod
    def _validate_description(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("description must not be empty when provided")
        return value


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
    context = current_runtime_tool_context()
    effective_run_id = run_id if run_id is not None else context.run_id if context is not None else None
    effective_invocation_id = invocation_id if invocation_id is not None else context.invocation_id if context is not None else None
    if effective_run_id is not None:
        payload["run_id"] = effective_run_id
    if effective_invocation_id is not None:
        payload["invocation_id"] = effective_invocation_id
        payload["tool_call_id"] = effective_invocation_id
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
    stream_name: str,
    chunks: list[bytes],
    errors: list[BaseException],
    emit_progress: Callable[[Mapping[str, object]], None] | None,
    progress_state: _ShellProgressState,
    run_id: str | None,
    invocation_id: str | None,
) -> None:
    try:
        while True:
            reader = getattr(pipe, "read1", None)
            if callable(reader):
                chunk = cast(bytes, reader(_SHELL_PROGRESS_CHUNK_BYTES))
            else:
                chunk = pipe.read(_SHELL_PROGRESS_CHUNK_BYTES)
            if not chunk:
                break
            chunks.append(chunk)
            _safe_emit_shell_progress(
                emit_progress,
                stream_name=stream_name,
                chunk=chunk,
                progress_state=progress_state,
                run_id=run_id,
                invocation_id=invocation_id,
            )
    except OSError as exc:
        errors.append(exc)


def _wait_after_process_kill(process: subprocess.Popen[Any], *, timeout: float = 1.0) -> None:
    try:
        process.wait(timeout=timeout)
        return
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except ProcessLookupError:
            return
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        return


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
        read_only=False,
    )

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        return self._invoke(call, workspace=workspace, runtime_timeout_seconds=None)

    def invoke_with_runtime_timeout(
        self,
        call: ToolCall,
        *,
        workspace: Path,
        timeout_seconds: int,
    ) -> ToolResult:
        return self._invoke(call, workspace=workspace, runtime_timeout_seconds=timeout_seconds)

    def _invoke(
        self,
        call: ToolCall,
        *,
        workspace: Path,
        runtime_timeout_seconds: int | None,
    ) -> ToolResult:
        try:
            args = ShellExecArgs.model_validate(
                {
                    "command": call.arguments.get("command"),
                    "description": call.arguments.get("description"),
                }
            )
        except ValidationError as exc:
            raise ValueError(format_validation_error(self.definition.name, exc)) from exc

        command_text = args.command.strip()
        injected_env = non_interactive_shell_env(command_text)
        injected_env_keys = tuple(injected_env.keys())

        timeout_value = call.arguments.get("timeout", DEFAULT_TIMEOUT_SECONDS)
        exec_policy = resolve_shell_execution_policy(
            workspace=workspace,
            timeout_argument=timeout_value,
            runtime_timeout_seconds=runtime_timeout_seconds,
        )
        timeout_seconds = exec_policy.timeout_seconds
        runtime_timeout_selected = exec_policy.runtime_timeout_selected

        try:
            command_text = command_text.encode("utf-8", errors="replace").decode("utf-8")
            process = subprocess.Popen(
                command_text,
                cwd=exec_policy.workspace_root,
                env={**os.environ, **injected_env} if injected_env else None,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
        except OSError as exc:
            raise ValueError(f"shell_exec failed to execute command: {exc}") from exc

        runtime_context = current_runtime_tool_context()
        abort_signal = runtime_context.abort_signal if runtime_context is not None else None
        emit_progress = runtime_context.emit_tool_progress if runtime_context is not None else None
        progress_run_id = runtime_context.run_id if runtime_context is not None else None
        progress_invocation_id = runtime_context.invocation_id if runtime_context is not None else call.tool_call_id
        deadline = time.monotonic() + timeout_seconds
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []
        reader_errors: list[BaseException] = []
        progress_state = _ShellProgressState()
        timed_out = False
        aborted = False
        stdout_pipe = cast(BinaryIO, process.stdout)
        stderr_pipe = cast(BinaryIO, process.stderr)
        stdout_reader = threading.Thread(
            target=_read_pipe_incrementally,
            kwargs={
                "pipe": stdout_pipe,
                "stream_name": "stdout",
                "chunks": stdout_chunks,
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
                "stream_name": "stderr",
                "chunks": stderr_chunks,
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
                _wait_after_process_kill(process)
                break
            if abort_signal is not None and abort_signal.cancelled:
                aborted = True
                kill_timed_out_process(process)
                _wait_after_process_kill(process)
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                kill_timed_out_process(process)
                _wait_after_process_kill(process)
                break
            try:
                process.wait(timeout=min(0.05, remaining))
                break
            except subprocess.TimeoutExpired:
                continue

        _join_reader_thread(stdout_reader, stdout_pipe)
        _join_reader_thread(stderr_reader, stderr_pipe)

        stdout_bytes = b"".join(stdout_chunks)
        stderr_bytes = b"".join(stderr_chunks)
        stdout = _decode_process_output(stdout_bytes)
        stderr = _decode_process_output(stderr_bytes)

        if reader_errors:
            first_error = reader_errors[0]
            message = f"shell_exec failed while reading process output: {first_error}"
            raise ValueError(message) from first_error

        output = stdout
        if stderr:
            output = f"{output}{stderr}" if output else stderr

        if timed_out:
            if runtime_timeout_selected:
                content = f"tool '{self.definition.name}' exceeded runtime timeout of {timeout_seconds}s"
                partial_result = ToolResult(
                    tool_name=self.definition.name,
                    status="error",
                    content=output,
                    error=content,
                    data={
                        "command": command_text,
                        "exit_code": process.returncode,
                        "stdout": stdout,
                        "stderr": stderr,
                        "timeout": timeout_seconds,
                        "truncated": False,
                        "interrupted": True,
                        "timed_out": True,
                        "injected_env_keys": injected_env_keys,
                    },
                    truncated=False,
                    partial=True,
                    timeout_seconds=timeout_seconds,
                )
                raise RuntimeToolTimeoutError(
                    content,
                    partial_result=partial_result,
                )
            raise ValueError(f"shell_exec command timed out after {timeout_seconds}s")

        if aborted:
            reason = getattr(abort_signal, "reason", None)
            content = "User aborted the command."
            return ToolResult(
                tool_name=self.definition.name,
                status="error",
                content=content,
                error=content,
                data={
                    "command": command_text,
                    "exit_code": process.returncode,
                    "stdout": stdout,
                    "stderr": stderr,
                    "timeout": timeout_seconds,
                    "truncated": False,
                    "interrupted": True,
                    "cancelled": True,
                    "reason": reason if isinstance(reason, str) else None,
                    "injected_env_keys": injected_env_keys,
                },
                truncated=False,
                partial=False,
                timeout_seconds=timeout_seconds,
            )

        return ToolResult(
            tool_name=self.definition.name,
            status="ok",
            content=output,
            data={
                "command": command_text,
                "cwd": str(exec_policy.workspace_root),
                "exit_code": process.returncode,
                "stdout": stdout,
                "stderr": stderr,
                "timeout": timeout_seconds,
                "stdout_truncated": False,
                "stderr_truncated": False,
                "truncated": False,
                "output_char_count": len(output),
                "injected_env_keys": injected_env_keys,
            },
            truncated=False,
            partial=False,
            timeout_seconds=timeout_seconds,
        )
