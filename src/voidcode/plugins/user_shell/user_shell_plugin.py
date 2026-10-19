"""User shell plugin: the user's own ``!`` escape, as an explicit direct-user command.

Ports the legacy ``user_shell`` escape (``src/voidcode/runtime/user_shell.py``: one-bang parse,
login-shell execution, 3600 s timeout, middle-truncated 10k output, the
``<user_shell_command>`` record) onto the kernel's direct-user command authority:

- the command declares ``direct_user`` and the client syntax ``!``, so only authentic user
  ingress reaches it and the kernel attributes it to ``actor="user"``; the kernel itself never
  parses the prefix, clients route it from the command catalog;
- being the user's own action, it is exempt from the approval gate and from read-only/plan
  overlays, exactly like the legacy escape, while schema validation, capabilities and effect
  declarations still apply;
- the invocation is recorded as one durable ``shell_executed`` event with the exit code,
  duration, truncation and timed-out flags, and the model-visible record text stays available
  for export.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import ClassVar

from voidcode.sdk.commands import CommandDefinition, CommandInvocation, CommandResult
from voidcode.sdk.plugin import Plugin
from voidcode.sdk.services import PluginServices
from voidcode.sdk.tools import ToolEffect

from .user_shell import (
    USER_SHELL_TIMEOUT_SECONDS,
    UserShellResult,
    execute_user_shell_command,
    format_user_shell_record,
    parse_shell_escape,
)


def _command_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped:
        return None
    # Accept the raw client remainder including a stray leading "!".
    return parse_shell_escape(stripped) or stripped


class ShellCommand:
    definition: ClassVar[CommandDefinition] = CommandDefinition(
        name="shell",
        description="Run a shell command locally as the user (the `!` escape).",
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
        direct_user=True,
        client_syntax="!",
    )

    def invoke(self, invocation: CommandInvocation, services: PluginServices) -> CommandResult:
        command = _command_text(invocation.arguments.get("command"))
        if command is None:
            return CommandResult(status="error", error="shell requires a non-empty command")
        workspace = invocation.context.require_workspace()
        result: UserShellResult = execute_user_shell_command(
            command,
            cwd=workspace,
            timeout_seconds=USER_SHELL_TIMEOUT_SECONDS,
        )
        record = format_user_shell_record(command, result)
        services.stage_event(
            "shell_executed",
            {
                "command": command,
                "exit_code": result.exit_code,
                "status": result.status,
                "duration_seconds": result.duration_seconds,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "output": result.output,
                "truncated": result.truncated,
                "timed_out": result.timed_out,
                "record": record,
            },
        )
        return CommandResult(
            status="ok",
            output=result.output if result.output else f"(no output; exit code {result.exit_code})",
            data={
                "command": command,
                "exit_code": result.exit_code,
                "status": result.status,
                "duration_seconds": result.duration_seconds,
                "truncated": result.truncated,
                "timed_out": result.timed_out,
                "record": record,
            },
        )


def create_plugin(_config: Mapping[str, object] | None = None) -> Plugin:
    """Plugin entrypoint registered in voidcode-plugin.json."""
    return Plugin(commands=(ShellCommand(),))
