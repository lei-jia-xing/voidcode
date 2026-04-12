from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, Literal, Protocol, runtime_checkable

type ToolResultStatus = Literal["ok", "error"]


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, object] = field(default_factory=dict)
    read_only: bool = True


@dataclass(frozen=True, slots=True)
class ToolCall:
    tool_name: str
    arguments: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolResult:
    tool_name: str
    status: ToolResultStatus
    content: str | None = None
    data: dict[str, object] = field(default_factory=dict)
    error: str | None = None

    def __post_init__(self) -> None:
        if self.status == "error" and self.error is None:
            raise ValueError("error results must include an error message")
        if self.status == "ok" and self.error is not None:
            raise ValueError("successful results cannot include an error message")


@runtime_checkable
class StaticTool(Protocol):
    definition: ClassVar[ToolDefinition]

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult: ...


@runtime_checkable
class DynamicTool(Protocol):
    @property
    def definition(self) -> ToolDefinition: ...

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult: ...


type Tool = StaticTool | DynamicTool
