from __future__ import annotations

from collections.abc import Iterable

from .models import CommandDefinition, UICommandDefinition, normalize_command_name


class CommandRegistry:
    """Mergeable registry for prompt/slash commands.

    Later registrations override earlier definitions with the same name, which lets project-local
    commands override builtin commands without special-case lookup logic.
    """

    def __init__(self, commands: Iterable[CommandDefinition] = ()) -> None:
        self._commands: dict[str, CommandDefinition] = {}
        for command in commands:
            self.register(command)

    def register(self, command: CommandDefinition) -> None:
        self._commands[command.name] = command

    def get(self, name: str) -> CommandDefinition | None:
        return self._commands.get(normalize_command_name(name))

    def list(
        self, *, include_hidden: bool = False, include_disabled: bool = False
    ) -> tuple[CommandDefinition, ...]:
        commands = sorted(self._commands.values(), key=lambda command: command.name)
        return tuple(
            command
            for command in commands
            if (include_hidden or not command.hidden) and (include_disabled or command.enabled)
        )


class UICommandRegistry:
    """Registry for local TUI commands that do not enter the LLM/runtime prompt path."""

    def __init__(self, commands: Iterable[UICommandDefinition] = ()) -> None:
        self._commands: dict[str, UICommandDefinition] = {}
        for command in commands:
            self.register(command)

    def register(self, command: UICommandDefinition) -> None:
        self._commands[command.id] = command

    def get(self, command_id: str) -> UICommandDefinition | None:
        return self._commands.get(command_id)

    def list(
        self, *, include_hidden: bool = False, include_disabled: bool = False
    ) -> tuple[UICommandDefinition, ...]:
        commands = sorted(self._commands.values(), key=lambda command: command.title)
        return tuple(
            command
            for command in commands
            if (include_hidden or not command.hidden) and (include_disabled or command.enabled)
        )
