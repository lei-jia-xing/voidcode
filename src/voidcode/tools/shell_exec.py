from __future__ import annotations

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

        completed = subprocess.run(
            command_text,
            cwd=workspace.resolve(),
            capture_output=True,
            text=True,
            check=False,
            shell=True,
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
