"""The governed ``!`` command, executed through the invocation's process service."""

from __future__ import annotations

from typing import ClassVar

from voidcode.sdk.commands import CommandDefinition, CommandInvocation, CommandResult
from voidcode.sdk.services import PluginServices
from voidcode.sdk.tools import ToolEffect

from .user_shell import (
    USER_SHELL_TIMEOUT_SECONDS,
    USER_SHELL_TRUNCATION_BYTES,
    format_user_shell_record,
    login_shell_args,
    parse_shell_escape,
    truncate_middle_chars,
)


def _command_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped:
        return None
    # Accept the raw client remainder including a stray leading "!".
    escaped = parse_shell_escape(stripped)
    command = stripped if escaped is None else escaped
    return command if command.strip() else None


class ShellCommand:
    definition: ClassVar[CommandDefinition] = CommandDefinition(
        name="shell",
        description="Run a shell command locally (the `!` escape).",
        argument_schema={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command line to run."},
            },
            "required": ["command"],
            "additionalProperties": False,
        },
        effects=frozenset({ToolEffect.EXECUTE}),
        busy_gate=False,
        client_syntax="!",
    )

    def invoke(self, invocation: CommandInvocation, services: PluginServices) -> CommandResult:
        command = _command_text(invocation.arguments.get("command"))
        if command is None:
            return CommandResult(status="error", error="shell requires a non-empty command")
        workspace = invocation.context.require_workspace()
        result = services.run_process(
            login_shell_args(command),
            cwd=workspace,
            timeout_seconds=USER_SHELL_TIMEOUT_SECONDS,
        )
        if result.cancelled:
            status = "cancelled"
        elif result.timed_out:
            status = "timed_out"
        else:
            status = "completed" if result.exit_code == 0 else "failed"
        raw_output = result.stdout + result.stderr
        if result.truncated:
            raw_output += "\n[process output exceeded capture limit]"
        if result.cancelled or result.timed_out:
            raw_output = f"command {status}\n{raw_output}"
        output = truncate_middle_chars(raw_output, USER_SHELL_TRUNCATION_BYTES)
        record = format_user_shell_record(command, result, output)
        data = {
            "command": command,
            "exit_code": result.exit_code,
            "status": status,
            "duration_seconds": result.duration_ms / 1000,
            "truncated": result.truncated or output != raw_output,
            "timed_out": result.timed_out,
            "cancelled": result.cancelled,
            "record": record,
        }
        services.stage_event("shell_executed", {**data, "stdout": result.stdout, "stderr": result.stderr, "output": output})
        return CommandResult(
            status="ok",
            output=output or f"(no output; exit code {result.exit_code})",
            data=data,
        )
