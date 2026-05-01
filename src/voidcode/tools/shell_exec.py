from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, BinaryIO, ClassVar, cast, final

from pydantic import ValidationError

from ..security.shell_policy import (
    DEFAULT_TIMEOUT_SECONDS,
    resolve_shell_execution_policy,
)
from ._pydantic_args import ShellExecArgs
from .contracts import RuntimeToolTimeoutError, ToolCall, ToolDefinition, ToolResult
from .runtime_context import current_runtime_tool_context

_SHELL_PROGRESS_CHUNK_BYTES = 8192
_SHELL_PROGRESS_CHUNK_CHARS = 12_000


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
) -> None:
    if emit_progress is None or not chunk:
        return
    text = _decode_process_output(chunk)
    bounded_text, truncated = _bounded_progress_chunk(text)
    try:
        emit_progress(
            {
                "stream": stream_name,
                "chunk": bounded_text,
                "chunk_char_count": len(text),
                "truncated": truncated,
            }
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
            )
    except OSError as exc:
        errors.append(exc)


def kill_timed_out_process(process: subprocess.Popen[Any]) -> None:
    if os.name == "nt":
        taskkill_succeeded = False
        try:
            completed = subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
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
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except AttributeError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    except ProcessLookupError:
        pass


@final
class ShellExecTool:
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="shell_exec",
        description="Execute a command inside the current workspace.",
        input_schema={
            "command": {"type": "string"},
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds (max 120)",
            },
            "description": {
                "type": "string",
                "description": "Human-readable description of the command",
            },
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
            args = ShellExecArgs.model_validate({"command": call.arguments.get("command")})
        except ValidationError as exc:
            first_error = exc.errors()[0]
            if first_error.get("type") == "value_error":
                raise ValueError("shell_exec command must not be empty") from exc
            raise ValueError("shell_exec requires a string command argument") from exc

        command_text = args.command.strip()

        timeout_value = call.arguments.get("timeout", DEFAULT_TIMEOUT_SECONDS)
        policy = resolve_shell_execution_policy(
            workspace=workspace,
            timeout_argument=timeout_value,
            runtime_timeout_seconds=runtime_timeout_seconds,
        )
        timeout_seconds = policy.timeout_seconds
        runtime_timeout_selected = policy.runtime_timeout_selected

        try:
            creationflags = 0
            if os.name == "nt":
                creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
            command_text = command_text.encode("utf-8", errors="replace").decode("utf-8")
            process = subprocess.Popen(
                command_text,
                cwd=policy.workspace_root,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                creationflags=creationflags,
            )
        except OSError as exc:
            raise ValueError(f"shell_exec failed to execute command: {exc}") from exc

        runtime_context = current_runtime_tool_context()
        abort_signal = runtime_context.abort_signal if runtime_context is not None else None
        emit_progress = runtime_context.emit_tool_progress if runtime_context is not None else None
        deadline = time.monotonic() + timeout_seconds
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []
        reader_errors: list[BaseException] = []
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
            },
            name="shell-exec-stderr-reader",
            daemon=True,
        )
        stdout_reader.start()
        stderr_reader.start()
        while True:
            if reader_errors:
                kill_timed_out_process(process)
                process.wait()
                break
            if abort_signal is not None and abort_signal.cancelled:
                aborted = True
                kill_timed_out_process(process)
                process.wait()
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                kill_timed_out_process(process)
                process.wait()
                break
            try:
                process.wait(timeout=min(0.05, remaining))
                break
            except subprocess.TimeoutExpired:
                continue

        stdout_reader.join()
        stderr_reader.join()

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
                content = (
                    f"tool '{self.definition.name}' exceeded runtime timeout of {timeout_seconds}s"
                )
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
                "cwd": str(policy.workspace_root),
                "exit_code": process.returncode,
                "stdout": stdout,
                "stderr": stderr,
                "timeout": timeout_seconds,
                "stdout_truncated": False,
                "stderr_truncated": False,
                "truncated": False,
                "output_char_count": len(output),
            },
            truncated=False,
            partial=False,
            timeout_seconds=timeout_seconds,
        )
