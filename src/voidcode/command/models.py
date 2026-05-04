from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

CommandSource = Literal["builtin", "user", "project", "skill", "mcp"]


@dataclass(frozen=True, slots=True)
class CommandDefinition:
    """A prompt/slash command definition that renders into a runtime prompt."""

    name: str
    description: str
    template: str
    source: CommandSource = "builtin"
    arguments_schema: dict[str, object] | None = None
    agent: str | None = None
    workflow_preset: str | None = None
    model: str | None = None
    subtask: bool = False
    enabled: bool = True
    hidden: bool = False
    path: Path | None = None

    def __post_init__(self) -> None:
        normalized_name = normalize_command_name(self.name)
        if not normalized_name:
            raise ValueError("command name must be a non-empty string")
        object.__setattr__(self, "name", normalized_name)
        if not self.description:
            raise ValueError("command description must be a non-empty string")
        if not self.template.strip():
            raise ValueError("command template must be a non-empty string")
        if self.workflow_preset is not None and not self.workflow_preset.strip():
            raise ValueError("command workflow_preset must be a non-empty string")


@dataclass(frozen=True, slots=True)
class CommandInvocation:
    """Structured representation of a resolved prompt command."""

    name: str
    source: CommandSource
    arguments: tuple[str, ...]
    raw_arguments: str
    original_prompt: str
    rendered_prompt: str


@dataclass(frozen=True, slots=True)
class CommandResolution:
    definition: CommandDefinition
    invocation: CommandInvocation


@dataclass(frozen=True, slots=True)
class UICommandDefinition:
    """A command palette action that executes locally in the TUI."""

    id: str
    title: str
    description: str
    enabled: bool = True
    hidden: bool = False

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("UI command id must be a non-empty string")
        if not self.title.strip():
            raise ValueError("UI command title must be a non-empty string")
        if not self.description.strip():
            raise ValueError("UI command description must be a non-empty string")


def normalize_command_name(name: str) -> str:
    return name.strip().removeprefix("/").replace("\\", "/")
