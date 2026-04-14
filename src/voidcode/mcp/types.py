from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class McpToolDescriptor:
    """Static MCP tool metadata - does not depend on runtime lifecycle."""

    server_name: str
    tool_name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True, slots=True)
class McpToolCallResult:
    """Static MCP tool call result - does not depend on runtime lifecycle."""

    content: list[dict[str, Any]]
    is_error: bool = False


@dataclass(frozen=True, slots=True)
class McpRuntimeEvent:
    """Static MCP runtime event - does not depend on runtime lifecycle."""

    event_type: str
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class McpConfigState:
    """Static MCP configuration state - does not depend on runtime lifecycle."""

    configured_enabled: bool = False
    servers: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class McpManagerState:
    """Static MCP manager state - does not depend on runtime lifecycle."""

    mode: str = "disabled"
    configuration: McpConfigState | None = None


# Protocol definitions for runtime implementation


class McpManager(Protocol):
    """Protocol for MCP manager - implemented by runtime."""

    @property
    def configuration(self) -> McpConfigState: ...

    def current_state(self) -> McpManagerState: ...

    def list_tools(self, *, workspace: Any) -> tuple[McpToolDescriptor, ...]: ...

    def call_tool(
        self,
        *,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
        workspace: Any,
    ) -> McpToolCallResult: ...

    def shutdown(self) -> tuple[McpRuntimeEvent, ...]: ...

    def drain_events(self) -> tuple[McpRuntimeEvent, ...]: ...


# Constants

MCP_PROTOCOL_VERSION = "2025-11-25"
MCP_CLIENT_NAME = "voidcode-runtime"
MCP_CLIENT_VERSION = "0.1.0"
