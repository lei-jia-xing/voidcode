"""Static MCP types, config records, and event contracts owned by the plugin.

Ported from ``voidcode.mcp`` (types/contract/observability) and the runtime MCP
config records, minus every runtime-lifecycle dependency: the plugin may only
import the SDK and its own modules.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal, Protocol


@dataclass(frozen=True, slots=True)
class McpToolSafety:
    """Advisory metadata derived from MCP server-provided safety hints.

    These values are retained for inspection only; runtime permissions and
    replay eligibility come from local policy and never from remote hints.
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
    content: list[dict[str, Any]]
    structured_content: dict[str, Any] | None = None
    is_error: bool = False


# =============================================================================
# Configuration (mirrors the runtime's RuntimeMcpServerConfig/RuntimeMcpConfig)
# =============================================================================

type McpTransport = Literal["stdio", "remote-http"]


@dataclass(frozen=True, slots=True)
class McpServerConfig:
    """One configured MCP server."""

    transport: McpTransport = "stdio"
    command: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    url: str | None = None


@dataclass(frozen=True, slots=True)
class McpConfig:
    """The plugin's ``mcp`` configuration section."""

    enabled: bool | None = None
    servers: Mapping[str, McpServerConfig] | None = None
    request_timeout_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class McpConfigState:
    """Static MCP configuration state - does not depend on runtime lifecycle."""

    configured_enabled: bool = False
    servers: dict[str, McpServerConfig] = field(default_factory=dict)

    @classmethod
    def from_config(cls, config: McpConfig | None) -> McpConfigState:
        if config is None:
            return cls()
        # Unset ``enabled`` (None) means enabled by default; only an explicit
        # ``enabled: false`` disables the capability (the runtime's
        # ``runtime_capability_enabled`` is the shared convention).
        return cls(
            configured_enabled=config.enabled is not False,
            servers=dict(config.servers or {}),
        )


# =============================================================================
# Diagnostics (ported from voidcode.mcp.observability/contract)
# =============================================================================


class McpErrorCode:
    """Standard MCP error codes used by the runtime."""

    TOOL_NOT_FOUND = "tool_not_found"
    INVALID_REQUEST = "invalid_request"


class McpDiagnosticSeverity(StrEnum):
    """Diagnostic severity levels."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True)
class McpDiagnostic:
    """Represents an MCP diagnostic event."""

    severity: McpDiagnosticSeverity
    category: str
    message: str
    server_name: str | None = None
    tool_name: str | None = None
    details: dict[str, Any] | None = None


class McpDiagnosticsCollector(Protocol):
    """Protocol for collecting MCP diagnostics."""

    def record_diagnostic(self, diagnostic: McpDiagnostic) -> None: ...


def diagnostic_message(
    code: str,
    *,
    server_name: str | None = None,
    tool_name: str | None = None,
    **details: Any,
) -> str:
    """Generate a standardized diagnostic message."""
    messages: dict[str, str] = {
        "server_startup_failed": "MCP server failed to start",
        "server_command_missing": "MCP server command is missing",
        "server_url_missing": "MCP server URL is missing for remote-http transport",
        "stdio_pipe_failed": "MCP server stdio pipe failed",
        "json_decode_failed": "MCP server returned invalid JSON",
        "request_timeout": "MCP server request timed out",
        "server_not_found": "MCP server not found in configuration",
        "tool_not_found": "MCP tool not found",
        "unexpected_response": "MCP server returned unexpected response",
        "remote_connection_failed": "Failed to connect to remote MCP server",
        "remote_http_error": "Remote MCP server returned HTTP error",
        "remote_timeout": "Remote MCP server request timed out",
    }
    base = messages.get(code, f"MCP[{code}]")
    if server_name:
        base = f"{base} (server: {server_name})"
    if tool_name:
        base = f"{base} (tool: {tool_name})"
    if details:
        detail_str = ", ".join(f"{k}={v}" for k, v in details.items())
        base = f"{base} [{detail_str}]"
    return base


def create_diagnostic(
    severity: McpDiagnosticSeverity,
    category: str,
    code: str,
    *,
    server_name: str | None = None,
    tool_name: str | None = None,
    **details: Any,
) -> McpDiagnostic:
    """Factory function to create a diagnostic with standardized message."""
    message = diagnostic_message(code, server_name=server_name, tool_name=tool_name, **details)

    return McpDiagnostic(
        severity=severity,
        category=category,
        message=message,
        server_name=server_name,
        tool_name=tool_name,
        details=details if details else None,
    )


# =============================================================================
# Manager state and surface
# =============================================================================


@dataclass(frozen=True, slots=True)
class McpCachedToolSurface:
    """Passively known MCP tool surface, per server, without connecting.

    ``servers`` is keyed by configured server name; a server present with an
    empty tuple is known to expose no tools, which is different from a server
    whose surface was never discovered.
    """

    servers: dict[str, tuple[McpToolDescriptor, ...]] = field(default_factory=dict)

    def descriptors(self) -> tuple[McpToolDescriptor, ...]:
        return tuple(tool for tools in self.servers.values() for tool in tools)

    def covers(self, server_names: Any) -> bool:
        return set(server_names) <= set(self.servers)


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
    retry_available: bool = False


# Protocol definitions for the plugin manager implementation


class McpManager(Protocol):
    """Protocol for the MCP manager - implemented by this plugin."""

    @property
    def configuration(self) -> McpConfigState: ...

    def current_state(self) -> McpManagerState: ...

    def list_tools(
        self,
        *,
        workspace: Any,
        server_name: str | None = None,
    ) -> tuple[McpToolDescriptor, ...]: ...

    def cached_surface(
        self,
        *,
        workspace: Any,
    ) -> McpCachedToolSurface: ...

    def descriptor_for(
        self,
        *,
        server_name: str,
        tool_name: str,
    ) -> McpToolDescriptor | None: ...

    def call_tool(
        self,
        *,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
        workspace: Any,
    ) -> McpToolCallResult: ...

    def shutdown(self) -> None: ...

    def retry_connections(self, *, workspace: Any) -> None: ...


# Constants

MCP_CLIENT_NAME = "voidcode-runtime"
MCP_CLIENT_VERSION = "0.1.0"
