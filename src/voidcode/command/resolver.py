from __future__ import annotations

from .models import CommandInvocation, CommandResolution
from .registry import CommandRegistry
from .templating import render_command_template, split_command_arguments


def is_prompt_command(prompt: str) -> bool:
    return prompt.strip().startswith("/")


def resolve_prompt_command(prompt: str, registry: CommandRegistry) -> CommandResolution | None:
    first_line, raw_arguments = _parse_slash_command(prompt)
    if first_line is None:
        return None
    definition = registry.get(first_line)
    if definition is None:
        raise ValueError(f"unknown command: /{first_line}")
    if not definition.enabled:
        raise ValueError(f"command is disabled: /{definition.name}")
    arguments = split_command_arguments(raw_arguments)
    rendered_prompt = render_command_template(
        definition.template,
        raw_arguments=raw_arguments,
        arguments=arguments,
    )
    return CommandResolution(
        definition=definition,
        invocation=CommandInvocation(
            name=definition.name,
            source=definition.source,
            arguments=arguments,
            raw_arguments=raw_arguments,
            original_prompt=prompt,
            rendered_prompt=rendered_prompt,
        ),
    )


def _parse_slash_command(prompt: str) -> tuple[str | None, str]:
    stripped = prompt.strip()
    if not stripped.startswith("/"):
        return None, ""
    first_line = stripped.splitlines()[0]
    command_text = first_line[1:].strip()
    if not command_text:
        raise ValueError("command name must not be empty")
    name, separator, raw_arguments = command_text.partition(" ")
    if not separator:
        raw_arguments = ""
    return name, raw_arguments.strip()
