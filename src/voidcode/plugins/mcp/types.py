"""MCP configuration validation, tool metadata, and manager contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

if TYPE_CHECKING:
    from voidcode.sdk.provider import ProviderAbortSignal


class McpCallStage(StrEnum):
    """Closed manager operation kind; never compare raw stage strings."""

    DISCOVERY = "discovery"
    CALL = "call"


type McpCallBoundary = Literal["pre_send", "in_flight", "cancelled"]
type McpManagerMode = Literal["disabled", "managed"]
type McpServerStatus = Literal["starting", "running", "stopped", "failed"]
type McpSafetySource = Literal["default-deny", "server-annotations"]


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
    source: McpSafetySource = "default-deny"

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


class McpCallError(ValueError):
    """Typed manager failure carrying the execution boundary as data.

    ``request_may_have_started`` is conservative: it is true once the SDK call
    has been entered, even if the remote server never observed the request.
    """

    def __init__(
        self,
        message: str,
        *,
        boundary: McpCallBoundary,
        request_may_have_started: bool,
        cancelled: bool = False,
        timed_out: bool = False,
    ) -> None:
        super().__init__(message)
        self.boundary = boundary
        self.request_may_have_started = request_may_have_started
        self.cancelled = cancelled
        self.timed_out = timed_out


# =============================================================================
# Configuration (mirrors the runtime's RuntimeMcpServerConfig/RuntimeMcpConfig)
# =============================================================================

type McpTransport = Literal["stdio", "remote-http"]
STDIO_TRANSPORT: Final[McpTransport] = "stdio"
REMOTE_HTTP_TRANSPORT: Final[McpTransport] = "remote-http"


class McpServerConfig(BaseModel):
    """Validate one configured server before any process or connection is created."""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    transport: McpTransport = STDIO_TRANSPORT
    command: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict, repr=False)
    url: str | None = None

    @model_validator(mode="after")
    def _transport_fields(self) -> Self:
        if self.transport == STDIO_TRANSPORT:
            if not self.command or any(not part or "\0" in part for part in self.command):
                raise ValueError("stdio command must contain non-empty argv strings")
            if self.url is not None:
                raise ValueError("stdio servers do not accept url")
        else:
            if self.command or self.env:
                raise ValueError("remote-http servers do not accept command or env")
            if self.url is None or urlsplit(self.url).scheme not in {"http", "https"} or not urlsplit(self.url).hostname:
                raise ValueError("remote-http url must be an absolute HTTP(S) URL")
        return self


class McpConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    enabled: bool = True
    servers: dict[str, McpServerConfig] = Field(default_factory=dict)
    request_timeout_seconds: float | None = Field(default=None, gt=0, allow_inf_nan=False)

    @field_validator("servers")
    @classmethod
    def _server_names(cls, servers: dict[str, McpServerConfig]) -> dict[str, McpServerConfig]:
        if any(not name.strip() for name in servers):
            raise ValueError("server names must not be empty")
        return servers


@dataclass(frozen=True, slots=True)
class McpConfigState:
    """Static MCP configuration state - does not depend on runtime lifecycle."""

    configured_enabled: bool = False
    servers: dict[str, McpServerConfig] = field(default_factory=dict)

    @classmethod
    def from_config(cls, config: McpConfig | None) -> McpConfigState:
        if config is None:
            return cls()
        return cls(
            configured_enabled=config.enabled,
            servers=dict(config.servers),
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


@dataclass(frozen=True, slots=True)
class McpManagerState:
    """Snapshot of configured servers and their current connection states."""

    mode: McpManagerMode = "disabled"
    configuration: McpConfigState = field(default_factory=McpConfigState)
    servers: dict[str, McpServerRuntimeState] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class McpServerRuntimeState:
    """Runtime-owned connection state for a configured MCP server."""

    status: McpServerStatus = "stopped"
    error: str | None = None


# Protocol definitions for the plugin manager implementation


class McpManager(Protocol):
    """Protocol for the MCP manager - implemented by this plugin."""

    @property
    def configuration(self) -> McpConfigState: ...

    def current_state(self) -> McpManagerState: ...

    def list_tools(
        self,
        *,
        workspace: Path,
        server_name: str | None = None,
    ) -> tuple[McpToolDescriptor, ...]: ...

    def cached_surface(
        self,
        *,
        workspace: Path,
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
        workspace: Path,
        abort_signal: ProviderAbortSignal | None = None,
    ) -> McpToolCallResult: ...

    def shutdown(self) -> None: ...


# Constants

MCP_CLIENT_NAME = "voidcode-runtime"
MCP_CLIENT_VERSION = "0.1.0"
