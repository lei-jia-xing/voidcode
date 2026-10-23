"""
MCP Manager

This module provides the plugin-owned MCP lifecycle management layer. The
plugin owns configuration, server state, and tool governance while the
official Python MCP SDK owns protocol framing, initialize negotiation,
request/response correlation, and stdio process teardown.

IMPORTANT: Static types and contracts live in this plugin's ``types.py``
(ported from ``voidcode.mcp``); this module adds the implementation.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager, suppress
from pathlib import Path
from typing import IO, Any, Literal, cast

import httpx2
from anyio.from_thread import BlockingPortal, start_blocking_portal
from jsonschema import Draft202012Validator, SchemaError, ValidationError
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp.types import CallToolResult, Implementation, ListToolsResult, Tool
from referencing import Registry
from referencing.exceptions import Unresolvable

from voidcode.sdk.json_values import json_wire_object, own_json_object

from .types import (
    MCP_CLIENT_NAME,
    MCP_CLIENT_VERSION,
    McpCachedToolSurface,
    McpConfig,
    McpConfigState,
    McpManager,
    McpManagerState,
    McpServerRuntimeState,
    McpToolCallResult,
    McpToolDescriptor,
    McpToolSafety,
)

DEFAULT_MCP_REQUEST_TIMEOUT_SECONDS = 30.0
_RECOVERABLE_MCP_CALL_ERROR_CODES = frozenset(
    {
        -32602,  # JSON-RPC invalid params
        -32601,  # JSON-RPC method not found
        -32600,  # JSON-RPC invalid request
    }
)
#: The SDK's CONNECTION_CLOSED JSON-RPC error code (mcp_types.CONNECTION_CLOSED).
#: The streamable-http transport resolves an in-flight request with this error
#: when the underlying connection dies (e.g. the connect POST failed), which is
#: how an unreachable endpoint surfaces through ``ClientSession.initialize``.
_MCP_CONNECTION_CLOSED_CODE = -32000


def _validate_input_schema(raw_schema: object) -> dict[str, object]:
    if not isinstance(raw_schema, dict):
        raise ValueError("inputSchema must be a JSON object")
    schema = dict(raw_schema)
    schema_type = schema.get("type")
    if schema_type is not None and schema_type != "object":
        raise ValueError("inputSchema type must be 'object'")
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise ValueError(f"invalid MCP inputSchema: {exc.message}") from exc
    return schema


def _read_write_streams(transport_streams: tuple[object, ...]) -> tuple[object, object]:
    if len(transport_streams) < 2:
        raise ValueError("MCP transport context must provide read and write streams")
    return transport_streams[0], transport_streams[1]


def _flatten_exception_group(exc: BaseExceptionGroup) -> BaseException:
    """Return a representative leaf exception from an exception group.

    anyio's ``TaskGroup.__aexit__`` raises ``BaseExceptionGroup`` (NOT an
    ``Exception`` subclass) for failures in child tasks, so ``except Exception``
    handlers and ``contextlib.suppress(Exception)`` do not catch it. Unwrap the
    group to its first leaf so the runtime's diagnostic helpers can inspect a
    concrete error; nested groups are unwrapped recursively.
    """
    leaf: BaseException = exc
    while isinstance(leaf, BaseExceptionGroup) and leaf.exceptions:
        leaf = leaf.exceptions[0]
    return leaf


def _is_connection_error(exc: BaseException) -> bool:
    """True for errors indicating the remote endpoint could not be reached.

    A dead remote-http endpoint surfaces either as an ``httpx2.ConnectError``
    (or ``httpx2.ConnectTimeout``) wrapped in an anyio exception group, or -
    when the transport teardown resolves the in-flight initialize request - as
    an ``MCPError`` carrying the SDK's CONNECTION_CLOSED code.
    """
    if isinstance(exc, httpx2.ConnectError | httpx2.ConnectTimeout):
        return True
    if isinstance(exc, MCPError):
        return exc.code == _MCP_CONNECTION_CLOSED_CODE
    return False


def _is_discovery_connection_closed(exc: BaseException) -> bool:
    """True when a discovery call failed because the connection was dropped.

    The streamable-http transport resolves an in-flight request with the SDK's
    CONNECTION_CLOSED error (-32000) when the underlying connection dies (e.g.
    a Cloudflare-flapped remote-http endpoint). That is transient, so the
    runtime retries ``tools/list`` once with a fresh connection before failing.
    """
    return isinstance(exc, MCPError) and _is_connection_error(exc)


def _validate_call_arguments_against_schema(
    *,
    tool_name: str,
    arguments: dict[str, object],
    input_schema: dict[str, object],
) -> None:
    try:
        # Schemas may use local definitions, but validation must not fetch remote references.
        Draft202012Validator(input_schema, registry=Registry()).validate(arguments)
    except Unresolvable as exc:
        raise ValueError(f"MCP[{tool_name}]: unresolved schema reference: {exc.ref}") from exc
    except ValidationError as exc:
        path = ".".join(str(part) for part in exc.absolute_path) or "arguments"
        raise ValueError(f"MCP[{tool_name}]: {path}: {exc.message}") from exc


# =============================================================================
# Disabled MCP Manager
# =============================================================================


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
    ) -> McpToolCallResult:
        _ = server_name, tool_name, arguments, workspace
        raise ValueError("MCP support is disabled")

    def shutdown(self) -> None:
        return None


# =============================================================================
# Runtime MCP Server (SDK session lifecycle)
# =============================================================================


class _RunningMcpServer:
    """Runtime-specific SDK session and context-manager handles."""

    def __init__(
        self,
        *,
        server_name: str,
        transport_context: AbstractContextManager[tuple[object, ...]],
        session_context: AbstractContextManager[ClientSession],
        session: ClientSession,
        stderr_log: IO[str] | None,
    ) -> None:
        self.server_name = server_name
        self.transport_context = transport_context
        self.session_context = session_context
        self.session = session
        self.stderr_log = stderr_log
        # The Python SDK ClientSession is shared per configured server process.
        # Serialize list/call operations per server so concurrent runtime and
        # subagent calls do not interleave mutable SDK session state.
        self.call_lock = threading.RLock()


# =============================================================================
# Managed MCP Manager
# =============================================================================


class ManagedMcpManager:
    """Runtime-owned MCP lifecycle management backed by the official SDK."""

    def __init__(
        self,
        config: McpConfig,
    ) -> None:
        self._configuration = McpConfigState.from_config(config)
        self._running_servers: dict[str, _RunningMcpServer] = {}
        self._state_lock = threading.RLock()
        self._server_states = {name: McpServerRuntimeState() for name in self._configuration.servers}
        self._request_timeout_seconds = config.request_timeout_seconds or DEFAULT_MCP_REQUEST_TIMEOUT_SECONDS
        self._portal_context: AbstractContextManager[BlockingPortal] | None = None
        self._portal: BlockingPortal | None = None
        self._tool_descriptors_by_server: dict[str, dict[str, McpToolDescriptor]] = {}
        self._discovered_tools: dict[str, tuple[McpToolDescriptor, ...]] = {}

    @property
    def configuration(self) -> McpConfigState:
        return self._configuration

    def current_state(self) -> McpManagerState:
        with self._state_lock:
            return McpManagerState(
                mode="managed",
                configuration=self._configuration,
                servers=dict(self._server_states),
            )

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
        server_names = (server_name,) if server_name is not None else tuple(self._configuration.servers)
        tools: list[McpToolDescriptor] = []
        for name in server_names:
            tools.extend(self._list_tools_for_server(server_name=name, workspace=workspace))
        return tuple(tools)

    def cached_surface(
        self,
        *,
        workspace: Path,
    ) -> McpCachedToolSurface:
        """Read discovered tools without connecting; absent servers remain unknown."""
        _ = workspace
        with self._state_lock:
            return McpCachedToolSurface(servers=dict(self._discovered_tools))

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
        key = server_name
        return self._tool_descriptors_by_server.get(key, {}).get(tool_name)

    def call_tool(
        self,
        *,
        server_name: str,
        tool_name: str,
        arguments: dict[str, object],
        workspace: Path,
    ) -> McpToolCallResult:
        if self._configuration.servers.get(server_name) is None:
            raise ValueError(f"MCP[{server_name}]: server not found in configuration")
        key = server_name
        running = self._ensure_running(server_name=server_name, workspace=workspace)
        descriptor = self._tool_descriptors_by_server.get(key, {}).get(tool_name)
        if descriptor is None:
            _ = self._list_tools_for_server(server_name=server_name, workspace=workspace)
            descriptor = self._tool_descriptors_by_server.get(key, {}).get(tool_name)

        if descriptor is not None and not descriptor.enabled:
            disabled_reason = descriptor.disabled_reason
            message = f"MCP[{server_name}/{tool_name}] is disabled"
            if disabled_reason:
                message = f"{message}: {disabled_reason}"
            raise ValueError(message)

        if descriptor is not None:
            _validate_call_arguments_against_schema(
                tool_name=f"{server_name}/{tool_name}",
                arguments=arguments,
                input_schema=descriptor.input_schema,
            )

        result = self._call_sdk(
            running,
            stage="call",
            method="tools/call",
            operation=lambda session=running.session: session.call_tool(
                tool_name,
                dict(arguments),
                read_timeout_seconds=self._request_timeout_seconds,
            ),
        )
        call_result = cast(CallToolResult, result)
        raw_structured_content = call_result.structured_content
        if raw_structured_content is None:
            structured_content = None
        elif isinstance(raw_structured_content, Mapping):
            structured_content = json_wire_object(own_json_object(raw_structured_content))
        else:
            raise ValueError("MCP structured content must be a JSON object")
        return McpToolCallResult(
            content=[item.model_dump(mode="json", by_alias=True, exclude_none=True) for item in call_result.content],
            structured_content=structured_content,
            is_error=call_result.is_error,
        )

    def shutdown(self) -> None:
        with self._state_lock:
            for key in tuple(self._running_servers):
                self._stop_running_server(key)
            if self._portal_context is not None:
                self._portal_context.__exit__(None, None, None)
                self._portal_context = None
                self._portal = None

    def _ensure_running(
        self,
        *,
        server_name: str,
        workspace: Path,
    ) -> _RunningMcpServer:
        """Ensure MCP server is running, start if needed."""
        server_config = self._configuration.servers.get(server_name)
        if server_config is None:
            message = f"MCP[{server_name}]: server not found in configuration. Available servers: {list(self._configuration.servers.keys())}"
            self._set_server_state(key=server_name, status="failed", error=message)
            raise ValueError(message)

        transport = server_config.transport
        key = server_name
        with self._state_lock:
            running = self._running_servers.get(key)
            if running is not None:
                return running

        workspace_root = workspace.resolve()
        stderr_log: IO[str] | None = None
        transport_context: AbstractContextManager[tuple[object, ...]] | None = None
        session_context: AbstractContextManager[ClientSession] | None = None

        with self._state_lock:
            running = self._running_servers.get(key)
            if running is not None:
                return running

            try:
                portal = self._ensure_portal()
                if transport == "remote-http":
                    pending_transport_context = portal.wrap_async_context_manager(cast(Any, streamable_http_client(cast(str, server_config.url))))
                    read_stream, write_stream = _read_write_streams(pending_transport_context.__enter__())
                    transport_context = pending_transport_context
                else:
                    stderr_log = open(
                        os.devnull,
                        "w",
                        encoding="utf-8",
                    )  # noqa: SIM115 - closed in shutdown
                    params = StdioServerParameters(
                        command=server_config.command[0],
                        args=list(server_config.command[1:]),
                        env={**os.environ, **server_config.env},
                        cwd=workspace_root,
                    )
                    pending_transport_context = portal.wrap_async_context_manager(cast(Any, stdio_client(params, errlog=stderr_log)))
                    read_stream, write_stream = _read_write_streams(pending_transport_context.__enter__())
                    transport_context = pending_transport_context
                pending_session_context = portal.wrap_async_context_manager(
                    ClientSession(
                        cast(Any, read_stream),
                        cast(Any, write_stream),
                        read_timeout_seconds=self._request_timeout_seconds,
                        client_info=Implementation(
                            name=MCP_CLIENT_NAME,
                            version=MCP_CLIENT_VERSION,
                        ),
                    )
                )
                session = pending_session_context.__enter__()
                session_context = pending_session_context
                self._set_server_state(key=key, status="running")
                portal.call(session.initialize)
            except FileNotFoundError as exc:
                self._close_partial_server(
                    session_context=session_context,
                    transport_context=transport_context,
                    stderr_log=stderr_log,
                )
                if transport == "stdio":
                    message = f"MCP[{server_name}]: failed to start server - cmd not found (command not found): {server_config.command[0]}"
                    self._set_server_state(key=key, status="failed", error=message)
                else:
                    message = self._report_remote_connect_failure(
                        key=key,
                        server_name=server_name,
                    )
                raise ValueError(message) from exc
            except (ExceptionGroup, BaseExceptionGroup) as exc:
                # anyio raises BaseExceptionGroup (NOT an Exception subclass)
                # when a transport child task fails - e.g. the streamable-http
                # connect POST on an unreachable endpoint - so neither the
                # ``except Exception`` clause nor ``suppress(Exception)`` in the
                # cleanup helpers can see it. Route it through the same clean
                # diagnostic path as other startup failures.
                self._close_partial_server(
                    session_context=session_context,
                    transport_context=transport_context,
                    stderr_log=stderr_log,
                )
                leaf = _flatten_exception_group(exc)
                if transport == "remote-http" and _is_connection_error(leaf):
                    message = self._report_remote_connect_failure(
                        key=key,
                        server_name=server_name,
                    )
                else:
                    message = self._message_for_exception(
                        leaf,
                        fallback=f"MCP[{server_name}]: failed to initialize server",
                    )
                    self._set_server_state(key=key, status="failed", error=message)
                raise ValueError(message) from exc
            except Exception as exc:
                self._close_partial_server(
                    session_context=session_context,
                    transport_context=transport_context,
                    stderr_log=stderr_log,
                )
                if transport == "remote-http" and _is_connection_error(exc):
                    message = self._report_remote_connect_failure(
                        key=key,
                        server_name=server_name,
                    )
                else:
                    message = self._message_for_exception(
                        exc,
                        fallback=f"MCP[{server_name}]: failed to initialize server",
                    )
                    self._set_server_state(key=key, status="failed", error=message)
                raise ValueError(message) from exc

            running = _RunningMcpServer(
                server_name=server_name,
                transport_context=transport_context,
                session_context=session_context,
                session=session,
                stderr_log=stderr_log,
            )
            self._running_servers[key] = running
            return running

    def _ensure_portal(self) -> BlockingPortal:
        if self._portal is not None:
            return self._portal
        portal_context = start_blocking_portal()
        portal = portal_context.__enter__()
        self._portal_context = portal_context
        self._portal = portal
        return portal

    def _call_sdk(
        self,
        running: _RunningMcpServer,
        *,
        stage: str,
        method: str,
        operation: Any,
        suppress_failure_event_when: Callable[[Exception], bool] | None = None,
    ) -> object:
        try:
            with running.call_lock:
                return self._ensure_portal().call(operation)
        except Exception as exc:
            message = self._message_for_exception(
                exc,
                fallback=f"MCP[{running.server_name}]: {method} failed",
            )
            if not self._is_recoverable_call_error(exc, stage=stage):
                self._stop_running_server(running.server_name)
                if suppress_failure_event_when is None or not suppress_failure_event_when(exc):
                    self._set_server_state(key=running.server_name, status="failed", error=message)
            raise ValueError(message) from exc

    def _is_recoverable_call_error(self, exc: Exception, *, stage: str) -> bool:
        if stage != "call" or not isinstance(exc, MCPError) or self._is_timeout_error(exc):
            return False
        return exc.code in _RECOVERABLE_MCP_CALL_ERROR_CODES

    def _descriptor_from_sdk_tool(self, *, server_name: str, tool: Tool) -> McpToolDescriptor:
        annotations = tool.annotations
        safety = (
            McpToolSafety.from_hints(
                read_only_hint=annotations.read_only_hint,
                destructive_hint=annotations.destructive_hint,
                idempotent_hint=annotations.idempotent_hint,
                open_world_hint=annotations.open_world_hint,
            )
            if annotations is not None
            else McpToolSafety()
        )
        try:
            input_schema = _validate_input_schema(tool.input_schema)
            return McpToolDescriptor(
                server_name=server_name,
                tool_name=tool.name,
                description=tool.description or "",
                input_schema=input_schema,
                safety=safety,
                enabled=True,
            )
        except ValueError as exc:
            return McpToolDescriptor(
                server_name=server_name,
                tool_name=tool.name,
                description=tool.description or "",
                input_schema={"type": "object", "properties": {}},
                safety=safety,
                enabled=False,
                disabled_reason=str(exc),
            )

    def _list_tools_for_server(
        self,
        *,
        server_name: str,
        workspace: Path,
    ) -> tuple[McpToolDescriptor, ...]:
        server_config = self._configuration.servers.get(server_name)
        if server_config is None:
            return ()
        key = server_name
        running = self._ensure_running(server_name=server_name, workspace=workspace)
        try:
            result = self._call_sdk(
                running,
                stage="discovery",
                method="tools/list",
                operation=lambda session=running.session: session.list_tools(),
                suppress_failure_event_when=_is_discovery_connection_closed,
            )
        except ValueError as exc:
            underlying = exc.__cause__ if isinstance(exc.__cause__, BaseException) else exc
            if not _is_discovery_connection_closed(underlying):
                raise
            # The streamable-http transport resolved the in-flight tools/list
            # POST with CONNECTION_CLOSED (-32000): the upstream connection
            # dropped mid-request (e.g. a Cloudflare-flapped remote-http
            # endpoint). That is transient, so retry once with a fresh
            # connection before failing. The failure event was suppressed on
            # the probe attempt and is only recorded if the retry also fails.
            self._stop_running_server(server_name)
            running = self._ensure_running(server_name=server_name, workspace=workspace)
            result = self._call_sdk(
                running,
                stage="discovery",
                method="tools/list",
                operation=lambda session=running.session: session.list_tools(),
            )
        list_result = cast(ListToolsResult, result)
        server_descriptors: dict[str, McpToolDescriptor] = {}
        descriptors: list[McpToolDescriptor] = []
        for tool in list_result.tools:
            descriptor = self._descriptor_from_sdk_tool(server_name=server_name, tool=tool)
            server_descriptors[descriptor.tool_name] = descriptor
            descriptors.append(descriptor)
        self._tool_descriptors_by_server[key] = server_descriptors
        discovered = tuple(descriptors)
        with self._state_lock:
            self._discovered_tools[server_name] = discovered
        return discovered

    def _stop_running_server(self, key: str) -> None:
        with self._state_lock:
            self._tool_descriptors_by_server.pop(key, None)
            running = self._running_servers.pop(key, None)
        if running is None:
            return
        self._terminate_running_server(running)
        self._set_server_state(key=key, status="stopped")

    def _set_server_state(self, *, key: str, status: Literal["running", "stopped", "failed"], error: str | None = None) -> None:
        with self._state_lock:
            self._server_states[key] = McpServerRuntimeState(status=status, error=error)

    def _report_remote_connect_failure(
        self,
        *,
        key: str,
        server_name: str,
    ) -> str:
        """Record the startup failure for an unreachable remote-http endpoint.

        Returns the clean user-facing message raised as a ``ValueError``.
        """
        message = f"MCP[{server_name}]: failed to connect to remote server"
        self._set_server_state(key=key, status="failed", error=message)
        return message

    def _message_for_exception(self, exc: BaseException, *, fallback: str) -> str:
        if self._is_timeout_error(exc):
            return f"MCP server timed out after {self._request_timeout_seconds:.1f}s. The server may be unresponsive."
        if isinstance(exc, MCPError):
            return f"MCP server error: {exc}"
        return f"{fallback}: {exc}"

    @staticmethod
    def _is_timeout_error(exc: BaseException) -> bool:
        if isinstance(exc, TimeoutError):
            return True
        if isinstance(exc, MCPError):
            return exc.code == 408 or "timed out" in str(exc).lower()
        return False

    @staticmethod
    def _close_partial_server(
        *,
        session_context: AbstractContextManager[ClientSession] | None,
        transport_context: AbstractContextManager[tuple[object, object]] | None,
        stderr_log: IO[str] | None,
    ) -> None:
        if session_context is not None:
            with suppress(BaseException):
                session_context.__exit__(None, None, None)
        if transport_context is not None:
            with suppress(BaseException):
                transport_context.__exit__(None, None, None)
        if stderr_log is not None:
            stderr_log.close()

    @staticmethod
    def _terminate_running_server(running: _RunningMcpServer) -> None:
        with suppress(BaseException):
            running.session_context.__exit__(None, None, None)
        with suppress(BaseException):
            running.transport_context.__exit__(None, None, None)
        if running.stderr_log is not None:
            running.stderr_log.close()


# =============================================================================
# Factory
# =============================================================================


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
