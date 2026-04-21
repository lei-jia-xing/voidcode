from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from typing import ClassVar, final

from pydantic import ValidationError

from ._pydantic_args import ShellExecArgs
from .contracts import RuntimeToolTimeoutError, ToolCall, ToolDefinition, ToolResult

DEFAULT_TIMEOUT_SECONDS = 30
MAX_TIMEOUT_SECONDS = 120
MAX_OUTPUT_CHARS = 200_000


def _truncate(text: str) -> tuple[str, bool]:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text, False
    return text[:MAX_OUTPUT_CHARS], True


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

        command_parts = shlex.split(command_text, posix=True)
        if not command_parts:
            raise ValueError("shell_exec command must not be empty")

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
            completed = subprocess.run(
                command_parts,
                cwd=workspace.resolve(),
                capture_output=True,
                text=True,
                check=False,
                shell=False,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            if runtime_timeout_selected:
                raise RuntimeToolTimeoutError(
                    f"tool '{self.definition.name}' exceeded runtime timeout of {timeout_seconds}s"
                ) from exc
            raise ValueError(f"shell_exec command timed out after {timeout_seconds}s") from exc
        except OSError as exc:
            raise ValueError(f"shell_exec failed to execute command: {exc}") from exc

        output = completed.stdout
        if completed.stderr:
            output = f"{output}{completed.stderr}" if output else completed.stderr

        content, content_truncated = _truncate(output)
        stdout, stdout_truncated = _truncate(completed.stdout)
        stderr, stderr_truncated = _truncate(completed.stderr)

        return ToolResult(
            tool_name=self.definition.name,
            status="ok",
            content=content,
            data={
                "command": command_text,
                "exit_code": completed.returncode,
                "stdout": stdout,
                "stderr": stderr,
                "timeout": timeout_seconds,
                "truncated": content_truncated or stdout_truncated or stderr_truncated,
            },
        )
