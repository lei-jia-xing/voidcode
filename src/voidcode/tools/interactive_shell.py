from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import ClassVar, final

from pydantic import BaseModel, ValidationError, field_validator

from ._pydantic_args import format_validation_error
from .contracts import ToolCall, ToolDefinition, ToolResult

_BLOCKED_SUBCOMMANDS = frozenset(
    {
        "attach-session",
        "attach",
        "kill-pane",
        "kill-server",
        "kill-session",
        "wait-for",
    }
)


class InteractiveShellArgs(BaseModel):
    tmux_command: str
    description: str | None = None

    @field_validator("tmux_command")
    @classmethod
    def _validate_tmux_command(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("tmux_command must not be empty")
        return normalized

    @field_validator("description")
    @classmethod
    def _validate_description(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("description must not be empty when provided")
        return normalized


@final
class InteractiveShellTool:
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="interactive_shell",
        description="Execute a tmux subcommand for interactive terminal sessions.",
        input_schema={
            "tmux_command": {
                "type": "string",
                "description": "tmux subcommand and arguments, without the 'tmux' prefix",
            },
            "description": {
                "type": "string",
                "description": "Human-readable description of the tmux action",
            },
        },
        read_only=False,
    )

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        try:
            args = InteractiveShellArgs.model_validate(
                {
                    "tmux_command": call.arguments.get("tmux_command"),
                    "description": call.arguments.get("description"),
                }
            )
        except ValidationError as exc:
            raise ValueError(format_validation_error(self.definition.name, exc)) from exc

        if os.name == "nt":
            raise ValueError(
                "interactive_shell currently requires tmux and is unsupported on Windows"
            )

        tmux_binary = shutil.which("tmux")
        if tmux_binary is None:
            raise ValueError("interactive_shell requires tmux to be installed and on PATH")

        try:
            command_parts = shlex.split(args.tmux_command)
        except ValueError as exc:
            raise ValueError(f"interactive_shell failed to parse tmux_command: {exc}") from exc
        if not command_parts:
            raise ValueError("interactive_shell tmux_command must not be empty")

        subcommand = command_parts[0]
        if subcommand in _BLOCKED_SUBCOMMANDS:
            raise ValueError(f"interactive_shell blocks risky tmux subcommand '{subcommand}'")

        completed = subprocess.run(
            [tmux_binary, *command_parts],
            cwd=workspace,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        stdout = completed.stdout.replace("\r\n", "\n")
        stderr = completed.stderr.replace("\r\n", "\n")
        output = stdout if not stderr else f"{stdout}{stderr}" if stdout else stderr
        status = "ok" if completed.returncode == 0 else "error"
        error = (
            None if status == "ok" else output or f"tmux exited with status {completed.returncode}"
        )
        return ToolResult(
            tool_name=self.definition.name,
            status=status,
            content=output,
            error=error,
            data={
                "tmux_command": args.tmux_command,
                "subcommand": subcommand,
                "exit_code": completed.returncode,
                "stdout": stdout,
                "stderr": stderr,
                "cwd": str(workspace),
            },
        )
