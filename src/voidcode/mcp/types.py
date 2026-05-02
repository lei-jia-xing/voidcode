from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from mcp.types import LATEST_PROTOCOL_VERSION


@dataclass(frozen=True, slots=True)
class McpToolSafety:
    """Governance metadata derived from MCP tool annotations.

    MCP servers may publish advisory safety hints for tools. VoidCode keeps
    those hints in the capability layer so the runtime can map them onto its
    own approval model without treating every discovered MCP tool as the same
    mutating capability.
    """

    read_only: bool = False
    destructive: bool | None = None
    idempotent: bool | None = None
    open_world: bool | None = None
    source: str = "default-deny"

    @classmethod
    def from_hints(
        cls,
        *,
        read_only_hint: bool | None = None,
        destructive_hint: bool | None = None,
        idempotent_hint: bool | None = None,
        open_world_hint: bool | None = None,
    ) -> McpToolSafety:
        """Create safety metadata from MCP tool annotation hints."""

        return cls(
            read_only=read_only_hint is True and destructive_hint is not True,
            destructive=destructive_hint,
            idempotent=idempotent_hint,
            open_world=open_world_hint,
            source="server-annotations",
        )


@dataclass(frozen=True, slots=True)
class McpToolDescriptor:
    """Static MCP tool metadata - does not depend on runtime lifecycle."""

    server_name: str
    tool_name: str
    description: str
    input_schema: dict[str, Any]
    safety: McpToolSafety = field(default_factory=McpToolSafety)
    enabled: bool = True
    disabled_reason: str | None = None


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

    @classmethod
    def from_runtime_config(cls, config: Any | None) -> McpConfigState:
        if config is None:
            return cls()
        return cls(
            configured_enabled=bool(getattr(config, "enabled", None)),
            servers=dict(getattr(config, "servers", None) or {}),
        )


@dataclass(frozen=True, slots=True)
class McpManagerState:
    """Static MCP manager state - does not depend on runtime lifecycle."""

    mode: str = "disabled"
    configuration: McpConfigState = field(default_factory=McpConfigState)
    servers: dict[str, McpServerRuntimeState] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class McpServerRuntimeState:
    """Runtime-owned connection state for a configured MCP server."""

    server_name: str
    status: Literal["running", "stopped", "failed"] = "stopped"
    workspace_root: str | None = None
    stage: str | None = None
    error: str | None = None
    command: list[str] = field(default_factory=list)
    url: str | None = None
    scope: Literal["runtime", "session"] = "runtime"
    retry_available: bool = False


# Protocol definitions for runtime implementation


class McpManager(Protocol):
    """Protocol for MCP manager - implemented by runtime."""

    @property
    def configuration(self) -> McpConfigState: ...

    def current_state(self) -> McpManagerState: ...

    def list_tools(
        self,
        *,
        workspace: Any,
        owner_session_id: str | None = None,
        parent_session_id: str | None = None,
    ) -> tuple[McpToolDescriptor, ...]: ...

    def call_tool(
        self,
        *,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
        workspace: Any,
        owner_session_id: str | None = None,
        parent_session_id: str | None = None,
    ) -> McpToolCallResult: ...

    def shutdown(self) -> tuple[McpRuntimeEvent, ...]: ...

    def drain_events(self) -> tuple[McpRuntimeEvent, ...]: ...

    def retry_connections(self, *, workspace: Any) -> None: ...


# Constants

MCP_PROTOCOL_VERSION = LATEST_PROTOCOL_VERSION
MCP_CLIENT_NAME = "voidcode-runtime"
MCP_CLIENT_VERSION = "0.1.0"
