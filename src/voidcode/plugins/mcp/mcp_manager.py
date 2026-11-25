"""MCP manager facade: configuration, public API, and factory.

The manager stays the plugin's single import surface. Runtime loop/connection
lifecycle, session startup/discovery, dispatch/calls, and shutdown snapshots
live in private responsibility mixins; wire types remain in `types.py`.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

from voidcode.sdk.provider import ProviderAbortSignal

from ._dispatch import _DispatchMixin
from ._runtime import _RunningMcpServer, _ServerRuntimeMixin
from ._session import _SessionMixin
from ._session import _validate_call_arguments_against_schema as _validate_call_arguments_against_schema
from ._session import _validate_input_schema as _validate_input_schema
from ._shutdown import _ShutdownMixin, _ShutdownSnapshot
from .types import (
    McpCachedToolSurface,
    McpCallError,
    McpConfig,
    McpConfigState,
    McpManager,
    McpManagerState,
    McpServerRuntimeState,
    McpToolCallResult,
    McpToolDescriptor,
)

DEFAULT_MCP_REQUEST_TIMEOUT_SECONDS = 30.0
_MAX_DISCOVERY_PAGES = 100
_SERVER_TEARDOWN_TIMEOUT_SECONDS = 10.0


class DisabledMcpManager:
    def __init__(self, config: McpConfig | None = None) -> None:
        self._configuration = McpConfigState.from_config(config)

    @property
    def configuration(self) -> McpConfigState:
        return self._configuration

    def current_state(self) -> McpManagerState:
        return McpManagerState(configuration=self._configuration)

    def list_tools(
        self,
        *,
        workspace: Path,
        server_name: str | None = None,
    ) -> tuple[McpToolDescriptor, ...]:
        _ = workspace, server_name
        raise ValueError("MCP support is disabled")

    def cached_surface(
        self,
        *,
        workspace: Path,
    ) -> McpCachedToolSurface:
        _ = workspace
        return McpCachedToolSurface()

    def descriptor_for(
        self,
        *,
        server_name: str,
        tool_name: str,
    ) -> McpToolDescriptor | None:
        _ = server_name, tool_name
        return None

    def call_tool(
        self,
        *,
        server_name: str,
        tool_name: str,
        arguments: dict[str, object],
        workspace: Path,
        abort_signal: ProviderAbortSignal | None = None,
    ) -> McpToolCallResult:
        _ = server_name, tool_name, arguments, workspace, abort_signal
        raise McpCallError(
            "MCP support is disabled",
            boundary="pre_send",
            request_may_have_started=False,
        )

    def shutdown(self) -> None:
        return None


class ManagedMcpManager(
    _ServerRuntimeMixin,
    _SessionMixin,
    _DispatchMixin,
    _ShutdownMixin,
):
    """Plugin-owned MCP lifecycle management backed by the official SDK."""

    def __init__(
        self,
        config: McpConfig,
    ) -> None:
        self._configuration = McpConfigState.from_config(config)
        self._running_servers: dict[str, _RunningMcpServer] = {}
        self._server_states = {name: McpServerRuntimeState() for name in self._configuration.servers}
        self._request_timeout_seconds = config.request_timeout_seconds or DEFAULT_MCP_REQUEST_TIMEOUT_SECONDS
        self._tool_descriptors_by_server: dict[str, dict[str, McpToolDescriptor]] = {}
        self._discovered_tools: dict[str, tuple[McpToolDescriptor, ...]] = {}
        self._startup_lock = asyncio.Lock()
        self._shutdown_called = False
        self._shutdown_snapshot: _ShutdownSnapshot | None = None
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(target=self._serve_loop, name="mcp-loop", daemon=True)
        self._loop_thread.start()

    def _max_discovery_pages(self) -> int:
        """Read the facade-level cap so module monkeypatches keep working."""
        return _MAX_DISCOVERY_PAGES

    def _teardown_timeout_seconds(self) -> float:
        """Read the facade-level teardown bound so module monkeypatches keep working."""
        return _SERVER_TEARDOWN_TIMEOUT_SECONDS

    @property
    def configuration(self) -> McpConfigState:
        return self._configuration

    def current_state(self) -> McpManagerState:
        return self._passive(self._current_state_owned, lambda snapshot: snapshot.state)

    def list_tools(
        self,
        *,
        workspace: Path,
        server_name: str | None = None,
    ) -> tuple[McpToolDescriptor, ...]:
        """Discover tools, connecting to configured servers.

        ``server_name`` narrows discovery to one server so an on-demand MCP
        tool call only pays for the server it actually needs.
        """
        return self._submit(self._list_tools_owned(workspace=workspace, server_name=server_name))

    def cached_surface(
        self,
        *,
        workspace: Path,
    ) -> McpCachedToolSurface:
        """Read discovered tools without connecting; absent servers remain unknown."""
        return self._passive(lambda: self._cached_surface_owned(workspace=workspace), lambda snapshot: snapshot.surface)

    def descriptor_for(
        self,
        *,
        server_name: str,
        tool_name: str,
    ) -> McpToolDescriptor | None:
        """Descriptor discovered for one remote tool, or ``None`` when unknown.

        Passive: it answers only for a surface this manager already discovered
        (``call_tool`` discovers before it calls), never connecting on its own.
        """
        return self._passive(
            lambda: self._descriptor_for_owned(server_name=server_name, tool_name=tool_name),
            lambda snapshot: snapshot.descriptors.get(server_name, {}).get(tool_name),
        )


def build_mcp_manager(
    config: McpConfig | None,
) -> McpManager:
    """Build an MCP manager based on configuration.

    Anything that cannot reach a server (no config, disabled, no servers) gets
    the disabled manager, so the tool reports "disabled" instead of pretending
    a connection attempt happened.
    """
    if config is None:
        return DisabledMcpManager(None)
    state = McpConfigState.from_config(config)
    if state.configured_enabled is not True or not state.servers:
        return DisabledMcpManager(config)
    return ManagedMcpManager(
        config,
    )
