from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

# Transport types supported by the runtime
McpTransport = Literal["stdio", "remote-http"]


@dataclass(frozen=True, slots=True)
class McpServerConfig:
    """Static MCP server configuration - does not depend on runtime lifecycle.

    This is the pure data structure for MCP server definition.
    Runtime-specific aspects like process handles are NOT included.
    """

    transport: McpTransport = "stdio"
    command: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    url: str | None = None

    def __post_init__(self) -> None:
        if self.transport == "remote-http" and not self.url:
            raise ValueError("remote-http transport requires a url")
        if self.transport == "stdio" and not self.command:
            raise ValueError("stdio transport requires a command")


@dataclass(frozen=True, slots=True)
class McpConfig:
    """Static MCP runtime configuration - does not depend on runtime lifecycle.

    This is the pure data structure for MCP enablement and server definitions.
    """

    enabled: bool | None = None
    servers: dict[str, McpServerConfig] | None = None


# Default values for MCP configuration
DEFAULT_MCP_TRANSPORT: McpTransport = "stdio"


def create_mcp_server_config(
    command: tuple[str, ...] = (),
    *,
    transport: McpTransport = "stdio",
    env: dict[str, str] | None = None,
    url: str | None = None,
) -> McpServerConfig:
    """Factory function to create an McpServerConfig."""
    return McpServerConfig(
        transport=transport,
        command=command,
        env=env or {},
        url=url,
    )


def create_mcp_config(
    servers: dict[str, McpServerConfig] | None = None,
    *,
    enabled: bool = True,
) -> McpConfig:
    """Factory function to create an McpConfig."""
    return McpConfig(
        enabled=enabled,
        servers=servers,
    )
