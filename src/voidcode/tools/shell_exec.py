from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from typing import ClassVar, final

from .contracts import ToolCall, ToolDefinition, ToolResult


@final
class ShellExecTool:
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="shell_exec",
        description="Execute a command inside the current workspace.",
        input_schema={"command": {"type": "string"}},
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

        completed = subprocess.run(
            command_parts,
            cwd=workspace.resolve(),
            capture_output=True,
            text=True,
            check=False,
            shell=False,
        )

        output = completed.stdout
        if completed.stderr:
            output = f"{output}{completed.stderr}" if output else completed.stderr

        return ToolResult(
            tool_name=self.definition.name,
            status="ok",
            content=output,
            data={
                "command": command_text,
                "exit_code": completed.returncode,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
            },
        )
