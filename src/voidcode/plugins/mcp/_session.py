"""MCP session startup and paginated tool discovery.

This module is composed into the `ManagedMcpManager` facade. It enters SDK
transports/sessions, initializes them, and reads every discovered ``tools/list``
page within fixed caps, while startup failures remain inspectable state.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import IO, Any

from jsonschema import Draft202012Validator, SchemaError, ValidationError
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import Implementation, PaginatedRequestParams, Tool
from referencing import Registry
from referencing.exceptions import Unresolvable

from ._runtime import (
    _flatten_exception_group,
    _is_connection_error,
    _is_discovery_connection_closed,
    _ManagerState,
    _OwnedAsyncContext,
    _RunningMcpServer,
)
from .types import (
    MCP_CLIENT_NAME,
    MCP_CLIENT_VERSION,
    REMOTE_HTTP_TRANSPORT,
    STDIO_TRANSPORT,
    McpCallStage,
    McpServerConfig,
    McpToolDescriptor,
    McpToolSafety,
)

_MAX_DISCOVERY_TOOLS = 10_000


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


def _read_write_streams(transport_streams: tuple[object, ...]) -> tuple[Any, Any]:
    if len(transport_streams) < 2:
        raise ValueError("MCP transport context must provide read and write streams")
    return transport_streams[0], transport_streams[1]


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


class _SessionMixin(_ManagerState):
    """Start one SDK session and discover its complete, bounded tool surface."""

    async def _ensure_running_owned(
        self,
        *,
        server_name: str,
        workspace: Path,
    ) -> _RunningMcpServer:
        """Ensure MCP server is running, start if needed."""
        server_config = self._configuration.servers.get(server_name)
        if server_config is None:
            message = f"MCP[{server_name}]: server not found in configuration. Available servers: {list(self._configuration.servers.keys())}"
            self._set_server_state_owned(key=server_name, status="failed", error=message)
            raise ValueError(message)

        running = self._running_servers.get(server_name)
        if running is not None:
            return running

        async with self._startup_lock:
            running = self._running_servers.get(server_name)
            if running is not None:
                return running

            transport = server_config.transport
            key = server_name
            workspace_root = workspace.resolve()
            stderr_log: IO[str] | None = None
            transport_context: _OwnedAsyncContext | None = None
            session_context: _OwnedAsyncContext | None = None

            try:
                transport_context, session_context, session, stderr_log = await self._open_session_owned(
                    server_name=server_name,
                    server_config=server_config,
                    workspace_root=workspace_root,
                )
                self._set_server_state_owned(key=key, status="starting")
                await session.initialize()
                self._set_server_state_owned(key=key, status="running")
            except FileNotFoundError as exc:
                await self._close_partial_server_owned(
                    session_context=session_context,
                    transport_context=transport_context,
                    stderr_log=stderr_log,
                )
                if transport == STDIO_TRANSPORT:
                    message = f"MCP[{server_name}]: failed to start server - cmd not found (command not found): {server_config.command[0]}"
                    self._set_server_state_owned(key=key, status="failed", error=message)
                else:
                    message = self._report_remote_connect_failure_owned(
                        key=key,
                        server_name=server_name,
                    )
                raise ValueError(message) from exc
            except BaseExceptionGroup as exc:
                # SDK transport failures may be grouped. Control exceptions must
                # retain their identity instead of becoming connection errors.
                await self._close_partial_server_owned(
                    session_context=session_context,
                    transport_context=transport_context,
                    stderr_log=stderr_log,
                )
                if not isinstance(exc, Exception):
                    raise
                leaf = _flatten_exception_group(exc)
                if transport == REMOTE_HTTP_TRANSPORT and _is_connection_error(leaf):
                    message = self._report_remote_connect_failure_owned(
                        key=key,
                        server_name=server_name,
                    )
                else:
                    message = self._message_for_exception(
                        leaf,
                        fallback=f"MCP[{server_name}]: failed to initialize server",
                    )
                    self._set_server_state_owned(key=key, status="failed", error=message)
                raise ValueError(message) from exc
            except Exception as exc:
                await self._close_partial_server_owned(
                    session_context=session_context,
                    transport_context=transport_context,
                    stderr_log=stderr_log,
                )
                if transport == REMOTE_HTTP_TRANSPORT and _is_connection_error(exc):
                    message = self._report_remote_connect_failure_owned(
                        key=key,
                        server_name=server_name,
                    )
                else:
                    message = self._message_for_exception(
                        exc,
                        fallback=f"MCP[{server_name}]: failed to initialize server",
                    )
                    self._set_server_state_owned(key=key, status="failed", error=message)
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

    async def _open_session_owned(
        self,
        *,
        server_name: str,
        server_config: McpServerConfig,
        workspace_root: Path,
    ) -> tuple[_OwnedAsyncContext, _OwnedAsyncContext, ClientSession, IO[str] | None]:
        """Enter the transport and session contexts for one server start."""
        _ = server_name
        stderr_log: IO[str] | None = None
        transport_context: _OwnedAsyncContext | None = None
        session_context: _OwnedAsyncContext | None = None
        try:
            if server_config.transport == REMOTE_HTTP_TRANSPORT:
                if server_config.url is None:
                    raise ValueError(f"remote-http MCP server {server_name!r} requires a url")
                transport_context = _OwnedAsyncContext(streamable_http_client(server_config.url))
                read_stream, write_stream = _read_write_streams(await transport_context.enter())
            else:
                stderr_log = open(
                    os.devnull,
                    "w",
                    encoding="utf-8",
                )
                params = StdioServerParameters(
                    command=server_config.command[0],
                    args=list(server_config.command[1:]),
                    env={**os.environ, **server_config.env},
                    cwd=workspace_root,
                )
                transport_context = _OwnedAsyncContext(stdio_client(params, errlog=stderr_log))
                read_stream, write_stream = _read_write_streams(await transport_context.enter())
            session_context = _OwnedAsyncContext(
                ClientSession(
                    read_stream,
                    write_stream,
                    read_timeout_seconds=self._request_timeout_seconds,
                    client_info=Implementation(
                        name=MCP_CLIENT_NAME,
                        version=MCP_CLIENT_VERSION,
                    ),
                )
            )
            session = await session_context.enter()
        except BaseException:
            if session_context is not None:
                with suppress(BaseException):
                    await session_context.exit()
            if transport_context is not None:
                with suppress(BaseException):
                    await transport_context.exit()
            if stderr_log is not None:
                stderr_log.close()
            raise
        return transport_context, session_context, session, stderr_log

    def _report_remote_connect_failure_owned(
        self,
        *,
        key: str,
        server_name: str,
    ) -> str:
        """Record the startup failure for an unreachable remote-http endpoint.

        Returns the clean user-facing message raised as a ``ValueError``.
        """
        message = f"MCP[{server_name}]: failed to connect to remote server"
        self._set_server_state_owned(key=key, status="failed", error=message)
        return message

    async def _list_tools_owned(
        self,
        *,
        workspace: Path,
        server_name: str | None,
    ) -> tuple[McpToolDescriptor, ...]:
        server_names = (server_name,) if server_name is not None else tuple(self._configuration.servers)
        tools: list[McpToolDescriptor] = []
        for name in server_names:
            tools.extend(await self._list_tools_for_server_owned(server_name=name, workspace=workspace))
        return tuple(tools)

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

    async def _list_tools_for_server_owned(
        self,
        *,
        server_name: str,
        workspace: Path,
    ) -> tuple[McpToolDescriptor, ...]:
        server_config = self._configuration.servers.get(server_name)
        if server_config is None:
            message = f"MCP[{server_name}]: server not found in configuration. Available servers: {list(self._configuration.servers.keys())}"
            self._set_server_state_owned(key=server_name, status="failed", error=message)
            raise ValueError(message)
        running = await self._ensure_running_owned(server_name=server_name, workspace=workspace)
        try:
            sdk_tools = await self._list_tools_pages_owned(
                running,
                server_name=server_name,
                suppress_failure_event_when=_is_discovery_connection_closed,
            )
        except ValueError as exc:
            underlying = exc.__cause__ if isinstance(exc.__cause__, BaseException) else exc
            if not _is_discovery_connection_closed(underlying):
                raise
            # The streamable-http transport resolved an in-flight tools/list
            # POST with CONNECTION_CLOSED (-32000): the upstream connection
            # dropped mid-request (e.g. a Cloudflare-flapped remote-http
            # endpoint). That is transient, so retry the whole paginated
            # discovery once with a fresh connection before failing. The
            # failure event was suppressed on the probe attempt and is only
            # recorded if the retry also fails.
            await self._stop_running_server_owned(server_name, expected=running)
            running = await self._ensure_running_owned(server_name=server_name, workspace=workspace)
            sdk_tools = await self._list_tools_pages_owned(running, server_name=server_name)
        server_descriptors: dict[str, McpToolDescriptor] = {}
        descriptors: list[McpToolDescriptor] = []
        for tool in sdk_tools:
            descriptor = self._descriptor_from_sdk_tool(server_name=server_name, tool=tool)
            server_descriptors[descriptor.tool_name] = descriptor
            descriptors.append(descriptor)
        discovered = tuple(descriptors)
        if self._running_servers.get(server_name) is running:
            self._tool_descriptors_by_server[server_name] = server_descriptors
            self._discovered_tools[server_name] = discovered
        return discovered

    async def _list_tools_pages_owned(
        self,
        running: _RunningMcpServer,
        *,
        server_name: str,
        suppress_failure_event_when: Callable[[Exception], bool] | None = None,
    ) -> list[Tool]:
        """Read every ``tools/list`` page, bounded by page and tool caps."""
        tools: list[Tool] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for _ in range(self._max_discovery_pages()):
            params = None if cursor is None else PaginatedRequestParams(cursor=cursor)
            result = await self._call_sdk_owned(
                running,
                stage=McpCallStage.DISCOVERY,
                method="tools/list",
                operation=lambda params=params: running.session.list_tools(params=params),
                suppress_failure_event_when=suppress_failure_event_when,
            )
            tools.extend(result.tools)
            if len(tools) > _MAX_DISCOVERY_TOOLS:
                raise ValueError(f"MCP[{server_name}]: tools/list returned more than {_MAX_DISCOVERY_TOOLS} tools")
            next_cursor = result.next_cursor
            if next_cursor is None:
                return tools
            if next_cursor in seen_cursors:
                raise ValueError(f"MCP[{server_name}]: tools/list returned a repeated cursor; discovery did not complete")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        raise ValueError(f"MCP[{server_name}]: tools/list exceeded {self._max_discovery_pages()} pages; discovery did not complete")
