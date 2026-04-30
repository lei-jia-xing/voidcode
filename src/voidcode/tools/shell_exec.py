from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, ClassVar, cast, final

from pydantic import ValidationError

from ..security.shell_policy import (
    DEFAULT_TIMEOUT_SECONDS,
    resolve_shell_execution_policy,
)
from ._pydantic_args import ShellExecArgs
from .contracts import RuntimeToolTimeoutError, ToolCall, ToolDefinition, ToolResult
from .runtime_context import current_runtime_tool_context


def _decode_process_output(payload: bytes | None) -> str:
    if payload is None:
        return ""
    decoded = payload.decode("utf-8", errors="replace")
    return decoded.replace("\r\n", "\n")


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
        deadline = time.monotonic() + timeout_seconds
        stdout_bytes = b""
        stderr_bytes = b""
        timed_out = False
        aborted = False
        while True:
            if abort_signal is not None and abort_signal.cancelled:
                aborted = True
                kill_timed_out_process(process)
                stdout_bytes, stderr_bytes = process.communicate()
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                kill_timed_out_process(process)
                stdout_bytes, stderr_bytes = process.communicate()
                break
            try:
                stdout_bytes, stderr_bytes = process.communicate(timeout=min(0.05, remaining))
                break
            except subprocess.TimeoutExpired:
                continue

        if timed_out:
            if runtime_timeout_selected:
                raise RuntimeToolTimeoutError(
                    f"tool '{self.definition.name}' exceeded runtime timeout of {timeout_seconds}s"
                )
            raise ValueError(f"shell_exec command timed out after {timeout_seconds}s")

        stdout = _decode_process_output(cast(bytes | None, stdout_bytes))
        stderr = _decode_process_output(cast(bytes | None, stderr_bytes))

        output = stdout
        if stderr:
            output = f"{output}{stderr}" if output else stderr

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
