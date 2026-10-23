"""The executable subset of Claude Code's Markdown subagent definition."""

from dataclasses import dataclass
from typing import Literal

type AgentSourceScope = Literal["builtin", "project", "user"]


@dataclass(frozen=True, slots=True)
class AgentDefinition:
    name: str
    description: str
    prompt: str
    source_scope: AgentSourceScope = "builtin"
    source_path: str | None = None
    readonly: bool = False
    tools: tuple[str, ...] | None = None
    max_turns: int | None = None
