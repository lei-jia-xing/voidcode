from __future__ import annotations

import re
from dataclasses import dataclass

from ..tools.contracts import ToolCall, ToolDefinition
from .models import CommandInvocation, CommandResolution
from .registry import CommandRegistry
from .templating import render_command_template, split_command_arguments

READ_REQUEST_PATTERN = re.compile(r"^(read|show)\s+(?P<path>.+)$", re.IGNORECASE)
GREP_REQUEST_PATTERN = re.compile(r"^grep\s+(?P<pattern>.+?)\s+(?P<path>\S+)$", re.IGNORECASE)
RUN_REQUEST_PATTERN = re.compile(r"^run\s+(?P<command>.+)$", re.IGNORECASE)
WRITE_REQUEST_PATTERN = re.compile(r"^write\s+(?P<path>\S+)\s+(?P<content>.+)$", re.IGNORECASE)
UNSUPPORTED_TOOL_COMMAND_MESSAGE = (
    "unsupported request: use 'read <relative-path>', 'show <relative-path>', "
    "'grep <pattern> <relative-path>', 'run <command>', or "
    "'write <relative-path> <content>'"
)


@dataclass(frozen=True, slots=True)
class ToolCommandResolution:
    tool_call: ToolCall


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


def resolve_tool_instruction(
    instruction: str,
    available_tools: tuple[ToolDefinition, ...],
    *,
    unavailable_message_suffix: str,
) -> ToolCommandResolution:
    read_match = READ_REQUEST_PATTERN.match(instruction)
    if read_match is not None:
        path_text = read_match.group("path").strip()
        if not path_text:
            raise ValueError("request path must not be empty")
        _ensure_tool(
            available_tools, "read_file", read_only=True, suffix=unavailable_message_suffix
        )
        return ToolCommandResolution(
            ToolCall(tool_name="read_file", arguments={"filePath": path_text})
        )

    grep_match = GREP_REQUEST_PATTERN.match(instruction)
    if grep_match is not None:
        pattern_text = grep_match.group("pattern").strip()
        path_text = grep_match.group("path").strip()
        if not pattern_text:
            raise ValueError("request pattern must not be empty")
        if not path_text:
            raise ValueError("request path must not be empty")
        _ensure_tool(available_tools, "grep", read_only=True, suffix=unavailable_message_suffix)
        return ToolCommandResolution(
            ToolCall(tool_name="grep", arguments={"pattern": pattern_text, "path": path_text})
        )

    run_match = RUN_REQUEST_PATTERN.match(instruction)
    if run_match is not None:
        command_text = run_match.group("command").strip()
        if not command_text:
            raise ValueError("request command must not be empty")
        _ensure_tool(
            available_tools, "shell_exec", read_only=False, suffix=unavailable_message_suffix
        )
        return ToolCommandResolution(
            ToolCall(tool_name="shell_exec", arguments={"command": command_text})
        )

    write_match = WRITE_REQUEST_PATTERN.match(instruction)
    if write_match is not None:
        path_text = write_match.group("path").strip()
        content_text = write_match.group("content")
        if not path_text:
            raise ValueError("request path must not be empty")
        if not content_text:
            raise ValueError("request content must not be empty")
        _ensure_tool(
            available_tools, "write_file", read_only=False, suffix=unavailable_message_suffix
        )
        return ToolCommandResolution(
            ToolCall(tool_name="write_file", arguments={"path": path_text, "content": content_text})
        )

    raise ValueError(UNSUPPORTED_TOOL_COMMAND_MESSAGE)


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


def _ensure_tool(
    tools: tuple[ToolDefinition, ...],
    tool_name: str,
    *,
    read_only: bool,
    suffix: str,
) -> None:
    if any(tool.name == tool_name and tool.read_only is read_only for tool in tools):
        return
    raise ValueError(f"{tool_name} tool is not registered for {suffix}")
