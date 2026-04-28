from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path
from typing import ClassVar, final

from pydantic import ValidationError

from ._pydantic_args import ShellExecArgs
from .contracts import RuntimeToolTimeoutError, ToolCall, ToolDefinition, ToolResult

DEFAULT_TIMEOUT_SECONDS = 30
MAX_TIMEOUT_SECONDS = 120
MAX_OUTPUT_CHARS = 200_000


def _truncate(text: str | None) -> tuple[str, bool]:
    if text is None:
        return "", False
    if len(text) <= MAX_OUTPUT_CHARS:
        return text, False
    return text[:MAX_OUTPUT_CHARS], True


def kill_timed_out_process(process: subprocess.Popen[str]) -> None:
    if os.name == "nt":
        try:
            _ = subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                check=False,
                text=True,
            )
            return
        except OSError:
            pass
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
        if isinstance(timeout_value, (int, float)) and timeout_value > 0:
            local_timeout_seconds = min(int(timeout_value), MAX_TIMEOUT_SECONDS)
        else:
            local_timeout_seconds = DEFAULT_TIMEOUT_SECONDS

        timeout_seconds = local_timeout_seconds
        runtime_timeout_selected = False
        if runtime_timeout_seconds is not None and runtime_timeout_seconds < timeout_seconds:
            timeout_seconds = runtime_timeout_seconds
            runtime_timeout_selected = True

        try:
            creationflags = 0
            if os.name == "nt":
                creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
            process = subprocess.Popen(
                command_text,
                cwd=workspace.resolve(),
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                start_new_session=True,
                creationflags=creationflags,
            )
        except OSError as exc:
            raise ValueError(f"shell_exec failed to execute command: {exc}") from exc

        try:
            stdout, stderr = process.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            kill_timed_out_process(process)
            process.communicate()
            if runtime_timeout_selected:
                raise RuntimeToolTimeoutError(
                    f"tool '{self.definition.name}' exceeded runtime timeout of {timeout_seconds}s"
                ) from exc
            raise ValueError(f"shell_exec command timed out after {timeout_seconds}s") from exc

        output = stdout
        if stderr:
            output = f"{output}{stderr}" if output else stderr

        content, content_truncated = _truncate(output)
        stdout, stdout_truncated = _truncate(stdout)
        stderr, stderr_truncated = _truncate(stderr)

        return ToolResult(
            tool_name=self.definition.name,
            status="ok",
            content=content,
            data={
                "command": command_text,
                "exit_code": process.returncode,
                "stdout": stdout,
                "stderr": stderr,
                "timeout": timeout_seconds,
                "truncated": content_truncated or stdout_truncated or stderr_truncated,
            },
            truncated=content_truncated or stdout_truncated or stderr_truncated,
            partial=content_truncated or stdout_truncated or stderr_truncated,
            timeout_seconds=timeout_seconds,
        )
