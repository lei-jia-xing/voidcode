from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from typing import ClassVar, final

from .contracts import ToolCall, ToolDefinition, ToolResult

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
        command_value = call.arguments.get("command")
        if not isinstance(command_value, str):
            raise ValueError("shell_exec requires a string command argument")

        command_text = command_value.strip()
        if not command_text:
            raise ValueError("shell_exec command must not be empty")

        command_parts = shlex.split(command_text, posix=True)
        if not command_parts:
            raise ValueError("shell_exec command must not be empty")

        timeout_value = call.arguments.get("timeout", DEFAULT_TIMEOUT_SECONDS)
        if isinstance(timeout_value, (int, float)) and timeout_value > 0:
            timeout_seconds = min(int(timeout_value), MAX_TIMEOUT_SECONDS)
        else:
            timeout_seconds = DEFAULT_TIMEOUT_SECONDS

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
