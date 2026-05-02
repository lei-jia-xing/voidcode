"""
MCP Runtime Implementation

This module provides the runtime-owned MCP lifecycle management layer.
VoidCode owns configuration, diagnostics, events, and tool governance while the
official Python MCP SDK owns protocol framing, initialize negotiation,
request/response correlation, and stdio process teardown.

IMPORTANT: Static types and contracts are in src/voidcode/mcp/.
This module imports from there and adds runtime-specific implementation.
"""

from __future__ import annotations

import os
import threading
import time
from contextlib import AbstractContextManager, suppress
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import IO, Any, Literal, cast

from anyio.from_thread import BlockingPortal, start_blocking_portal
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import McpError
from mcp.types import CallToolResult, Implementation, InitializeResult, ListToolsResult, Tool

from ..mcp import (
    MCP_CLIENT_NAME,
    MCP_CLIENT_VERSION,
    McpConfigState,
    McpDiagnostic,
    McpDiagnosticsCollector,
    McpDiagnosticSeverity,
    McpErrorCode,
    McpManager,
    McpManagerState,
    McpRuntimeEvent,
    McpServerRuntimeState,
    McpToolCallResult,
    McpToolDescriptor,
    McpToolSafety,
    create_diagnostic,
    redact_mcp_command,
)
from .config import RuntimeMcpConfig
from .events import (
    RUNTIME_MCP_SERVER_ACQUIRED,
    RUNTIME_MCP_SERVER_FAILED,
    RUNTIME_MCP_SERVER_IDLE_CLEANED,
    RUNTIME_MCP_SERVER_RELEASED,
    RUNTIME_MCP_SERVER_REUSED,
    RUNTIME_MCP_SERVER_STARTED,
    RUNTIME_MCP_SERVER_STOPPED,
)

DEFAULT_MCP_REQUEST_TIMEOUT_SECONDS = 2.0
DEFAULT_SESSION_MCP_IDLE_TIMEOUT_SECONDS = 300.0
_RECOVERABLE_MCP_CALL_ERROR_CODES = frozenset(
    {
        McpErrorCode.TOOL_NOT_FOUND,
        McpErrorCode.INVALID_REQUEST,
        -32602,  # JSON-RPC invalid params
        -32601,  # JSON-RPC method not found
        -32600,  # JSON-RPC invalid request
    }
)


def _validate_input_schema(raw_schema: object) -> dict[str, object]:
    if not isinstance(raw_schema, dict):
        raise ValueError("inputSchema must be a JSON object")
    schema = dict(cast(dict[str, object], raw_schema))
    schema_type = schema.get("type")
    if schema_type is not None and schema_type != "object":
        raise ValueError("inputSchema type must be 'object'")
    required = schema.get("required")
    if required is not None:
        if not isinstance(required, list):
            raise ValueError("inputSchema required must be an array of strings")
        required_items = cast(list[object], required)
        if not all(isinstance(item, str) for item in required_items):
            raise ValueError("inputSchema required must be an array of strings")
    properties = schema.get("properties")
    if properties is not None and not isinstance(properties, dict):
        raise ValueError("inputSchema properties must be an object")
    return schema


def _read_write_streams(transport_streams: tuple[object, ...]) -> tuple[object, object]:
    if len(transport_streams) < 2:
        raise ValueError("MCP transport context must provide read and write streams")
    return transport_streams[0], transport_streams[1]


def _validate_call_arguments_against_schema(
    *,
    tool_name: str,
    arguments: dict[str, object],
    input_schema: dict[str, object],
) -> None:
    required = input_schema.get("required")
    if isinstance(required, list):
        required_items = cast(list[object], required)
        required_names = [name for name in required_items if isinstance(name, str)]
        missing = [name for name in required_names if name not in arguments]
        if missing:
            joined = ", ".join(missing)
            raise ValueError(f"MCP[{tool_name}]: missing required arguments: {joined}")

    properties = input_schema.get("properties")
    if not isinstance(properties, dict):
        return

    raw_properties = cast(dict[object, object], properties)
    for raw_key, raw_expected_schema in raw_properties.items():
        if not isinstance(raw_key, str):
            continue
        if raw_key not in arguments:
            continue
        if not isinstance(raw_expected_schema, dict):
            continue
        expected_schema = cast(dict[str, object], raw_expected_schema)
        expected_type = expected_schema.get("type")
        value = arguments[raw_key]
        if expected_type == "string" and not isinstance(value, str):
            raise ValueError(f"MCP[{tool_name}]: argument '{raw_key}' must be a string")
        if expected_type == "integer" and (isinstance(value, bool) or not isinstance(value, int)):
            raise ValueError(f"MCP[{tool_name}]: argument '{raw_key}' must be an integer")
        if expected_type == "number" and (
            isinstance(value, bool) or not isinstance(value, (int, float))
        ):
            raise ValueError(f"MCP[{tool_name}]: argument '{raw_key}' must be a number")
        if expected_type == "boolean" and not isinstance(value, bool):
            raise ValueError(f"MCP[{tool_name}]: argument '{raw_key}' must be a boolean")
        if expected_type == "array" and not isinstance(value, list):
            raise ValueError(f"MCP[{tool_name}]: argument '{raw_key}' must be an array")
        if expected_type == "object" and not isinstance(value, dict):
            raise ValueError(f"MCP[{tool_name}]: argument '{raw_key}' must be an object")


# =============================================================================
# Disabled MCP Manager
# =============================================================================


class DisabledMcpManager:
    def __init__(self, config: RuntimeMcpConfig | None = None) -> None:
        self._configuration = McpConfigState.from_runtime_config(config)

    @property
    def configuration(self) -> McpConfigState:
        return self._configuration

    def current_state(self) -> McpManagerState:
        return McpManagerState(configuration=self._configuration)

    def list_tools(
        self,
        *,
        workspace: Path,
        owner_session_id: str | None = None,
        parent_session_id: str | None = None,
    ) -> tuple[McpToolDescriptor, ...]:
        _ = workspace, owner_session_id, parent_session_id
        raise ValueError("MCP runtime support is disabled")

    def call_tool(
        self,
        *,
        server_name: str,
        tool_name: str,
        arguments: dict[str, object],
        workspace: Path,
        owner_session_id: str | None = None,
        parent_session_id: str | None = None,
    ) -> McpToolCallResult:
        _ = server_name, tool_name, arguments, workspace, owner_session_id, parent_session_id
        raise ValueError("MCP runtime support is disabled")

    def shutdown(self) -> tuple[McpRuntimeEvent, ...]:
        return ()

    def drain_events(self) -> tuple[McpRuntimeEvent, ...]:
        return ()

    def retry_connections(self, *, workspace: Path) -> None:
        _ = workspace
        return None


# =============================================================================
# Runtime MCP Server (SDK session lifecycle)
# =============================================================================


class _RunningMcpServer:
    """Runtime-specific SDK session and context-manager handles."""

    scope: Literal["runtime", "session"]
    owner_session_id: str | None
    transport: Literal["stdio", "remote-http"]

    def __init__(
        self,
        *,
        server_name: str,
        workspace_root: Path,
        transport_context: AbstractContextManager[tuple[object, ...]],
        session_context: AbstractContextManager[ClientSession],
        session: ClientSession,
        stderr_log: IO[str] | None,
        initialize_result: InitializeResult,
        scope: Literal["runtime", "session"],
        owner_session_id: str | None,
        transport: Literal["stdio", "remote-http"] = "stdio",
    ) -> None:
        self.server_name = server_name
        self.workspace_root = workspace_root
        self.transport_context = transport_context
        self.session_context = session_context
        self.session = session
        self.stderr_log = stderr_log
        self.initialize_result = initialize_result
        self.scope = scope
        self.owner_session_id = owner_session_id
        self.transport = transport
        self.references = 0
        self.last_used_at = time.monotonic()
        # The Python SDK ClientSession is shared per configured server process.
        # Serialize list/call operations per server so concurrent runtime and
        # subagent calls do not interleave mutable SDK session state.
        self.call_lock = threading.RLock()


@dataclass(frozen=True, slots=True)
class _McpServerKey:
    server_name: str
    scope: Literal["runtime", "session"]
    owner_session_id: str | None = None


# =============================================================================
# Managed MCP Manager
# =============================================================================


class ManagedMcpManager:
    """Runtime-owned MCP lifecycle management backed by the official SDK."""

    def __init__(
        self,
        config: RuntimeMcpConfig,
        *,
        diagnostics_collector: McpDiagnosticsCollector | None = None,
    ) -> None:
        self._configuration = McpConfigState.from_runtime_config(config)
        self._running_servers: dict[_McpServerKey, _RunningMcpServer] = {}
        self._pending_events: list[McpRuntimeEvent] = []
        self._state_lock = threading.RLock()
        self._diagnostics_collector = diagnostics_collector
        self._server_states: dict[str, McpServerRuntimeState] = {
            name: McpServerRuntimeState(
                server_name=name,
                status="stopped",
                command=list(server.command),
                url=getattr(server, "url", None),
                scope=getattr(server, "scope", "runtime"),
            )
            for name, server in self._configuration.servers.items()
        }
        self._request_timeout_seconds = (
            config.request_timeout_seconds or DEFAULT_MCP_REQUEST_TIMEOUT_SECONDS
        )
        self._portal_context: AbstractContextManager[BlockingPortal] | None = None
        self._portal: BlockingPortal | None = None
        self._tool_descriptors_by_server: dict[_McpServerKey, dict[str, McpToolDescriptor]] = {}

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
        owner_session_id: str | None = None,
        parent_session_id: str | None = None,
    ) -> tuple[McpToolDescriptor, ...]:
        _ = parent_session_id
        tools: list[McpToolDescriptor] = []
        for server_name in self._configuration.servers:
            tools.extend(
                self._list_tools_for_server(
                    server_name=server_name,
                    workspace=workspace,
                    owner_session_id=owner_session_id,
                )
            )
        return tuple(tools)

    def call_tool(
        self,
        *,
        server_name: str,
        tool_name: str,
        arguments: dict[str, object],
        workspace: Path,
        owner_session_id: str | None = None,
        parent_session_id: str | None = None,
    ) -> McpToolCallResult:
        _ = parent_session_id
        server_config = self._configuration.servers.get(server_name)
        if server_config is None:
            raise ValueError(f"MCP[{server_name}]: server not found in configuration")
        key = self._server_key(
            server_name=server_name,
            scope=getattr(server_config, "scope", "runtime"),
            owner_session_id=owner_session_id,
        )
        running = self._ensure_running(
            server_name=server_name,
            workspace=workspace,
            owner_session_id=owner_session_id,
        )
        descriptor = self._tool_descriptors_by_server.get(key, {}).get(tool_name)
        if descriptor is None:
            _ = self._list_tools_for_server(
                server_name=server_name,
                workspace=workspace,
                owner_session_id=owner_session_id,
            )
            descriptor = self._tool_descriptors_by_server.get(key, {}).get(tool_name)

        if descriptor is not None and not descriptor.enabled:
            raise ValueError(
                f"MCP[{server_name}/{tool_name}] is disabled: "
                f"{descriptor.disabled_reason or 'invalid schema'}"
            )

        if descriptor is not None:
            try:
                _validate_call_arguments_against_schema(
                    tool_name=f"{server_name}/{tool_name}",
                    arguments=arguments,
                    input_schema=descriptor.input_schema,
                )
            except ValueError as exc:
                diagnostic = create_diagnostic(
                    severity=McpDiagnosticSeverity.ERROR,
                    category="call",
                    code="invalid_params",
                    server_name=server_name,
                    tool_name=tool_name,
                    reason=str(exc),
                )
                self._record_diagnostic(diagnostic)
                raise

        result = self._call_sdk(
            running,
            stage="call",
            method="tools/call",
            tool_name=tool_name,
            operation=lambda session=running.session: session.call_tool(
                tool_name,
                dict(arguments),
                read_timeout_seconds=self._request_timeout,
            ),
        )
        call_result = cast(CallToolResult, result)
        return McpToolCallResult(
            content=[
                item.model_dump(by_alias=True, exclude_none=True) for item in call_result.content
            ],
            is_error=bool(call_result.isError),
        )

    def shutdown(self) -> tuple[McpRuntimeEvent, ...]:
        with self._state_lock:
            for key in tuple(self._running_servers):
                self._stop_running_server(key)
            if self._portal_context is not None:
                self._portal_context.__exit__(None, None, None)
                self._portal_context = None
                self._portal = None
        return self.drain_events()

    def drain_events(self) -> tuple[McpRuntimeEvent, ...]:
        with self._state_lock:
            events = tuple(self._pending_events)
            self._pending_events.clear()
            return events

    def retry_connections(self, *, workspace: Path) -> None:
        for server_name, server_config in self._configuration.servers.items():
            if getattr(server_config, "scope", "runtime") == "session":
                continue
            self._ensure_running(server_name=server_name, workspace=workspace)

    def release_session(self, *, session_id: str) -> tuple[McpRuntimeEvent, ...]:
        with self._state_lock:
            for key in tuple(self._running_servers):
                if key.scope == "session" and key.owner_session_id == session_id:
                    self._record_server_released(key=key, reason="session_finished")
                    self._stop_running_server(key)
        return self.drain_events()

    def cleanup_idle_session_servers(
        self,
        *,
        max_idle_seconds: float = DEFAULT_SESSION_MCP_IDLE_TIMEOUT_SECONDS,
        active_session_ids: set[str] | None = None,
    ) -> tuple[McpRuntimeEvent, ...]:
        now = time.monotonic()
        active_ids = active_session_ids or set()
        with self._state_lock:
            for key, running in tuple(self._running_servers.items()):
                if key.scope != "session":
                    continue
                abandoned = (
                    active_session_ids is not None and key.owner_session_id not in active_ids
                )
                idle = now - running.last_used_at >= max_idle_seconds
                if abandoned or idle:
                    self._record_server_idle_cleaned(
                        key=key,
                        workspace_root=running.workspace_root,
                        reason="abandoned" if abandoned else "idle_timeout",
                    )
                    self._stop_running_server(key)
        return self.drain_events()

    @property
    def _request_timeout(self) -> timedelta:
        return timedelta(seconds=self._request_timeout_seconds)

    def _ensure_running(
        self,
        *,
        server_name: str,
        workspace: Path,
        owner_session_id: str | None = None,
    ) -> _RunningMcpServer:
        """Ensure MCP server is running, start if needed."""
        server_config = self._configuration.servers.get(server_name)
        if server_config is None:
            diagnostic = create_diagnostic(
                severity=McpDiagnosticSeverity.ERROR,
                category="startup",
                code="server_not_found",
                server_name=server_name,
            )
            self._record_diagnostic(diagnostic)
            message = (
                f"MCP[{server_name}]: server not found in configuration. "
                f"Available servers: {list(self._configuration.servers.keys())}"
            )
            self._record_failure_event(
                key=_McpServerKey(server_name=server_name, scope="runtime"),
                workspace_root=workspace.resolve(),
                stage="startup",
                error=message,
                diagnostic=diagnostic,
            )
            raise ValueError(message)

        transport = getattr(server_config, "transport", "stdio")
        scope = getattr(server_config, "scope", "runtime")
        key = self._server_key(
            server_name=server_name,
            scope=scope,
            owner_session_id=owner_session_id,
        )
        with self._state_lock:
            running = self._running_servers.get(key)
            if running is not None:
                running.last_used_at = time.monotonic()
                self._record_server_reused(key=key, workspace_root=running.workspace_root)
                self._record_server_acquired(key=key, workspace_root=running.workspace_root)
                return running

        if transport == "stdio" and not getattr(server_config, "command", None):
            diagnostic = create_diagnostic(
                severity=McpDiagnosticSeverity.ERROR,
                category="startup",
                code="server_command_missing",
                server_name=server_name,
            )
            self._record_diagnostic(diagnostic)
            message = (
                f"MCP[{server_name}]: command is empty. "
                "Please configure mcp.servers.{server_name}.command in .voidcode.json"
            )
            self._record_failure_event(
                key=key,
                workspace_root=workspace.resolve(),
                stage="startup",
                error=message,
                diagnostic=diagnostic,
            )
            raise ValueError(message)

        if transport == "remote-http" and not getattr(server_config, "url", None):
            diagnostic = create_diagnostic(
                severity=McpDiagnosticSeverity.ERROR,
                category="startup",
                code="server_url_missing",
                server_name=server_name,
            )
            self._record_diagnostic(diagnostic)
            message = (
                f"MCP[{server_name}]: url is empty. "
                "Please configure mcp.servers.{server_name}.url in .voidcode.json"
            )
            self._record_failure_event(
                key=key,
                workspace_root=workspace.resolve(),
                stage="startup",
                error=message,
                diagnostic=diagnostic,
            )
            raise ValueError(message)

        workspace_root = workspace.resolve()
        stderr_log: IO[str] | None = None
        transport_context: AbstractContextManager[tuple[object, ...]] | None = None
        session_context: AbstractContextManager[ClientSession] | None = None

        with self._state_lock:
            running = self._running_servers.get(key)
            if running is not None:
                running.last_used_at = time.monotonic()
                self._record_server_reused(key=key, workspace_root=running.workspace_root)
                self._record_server_acquired(key=key, workspace_root=running.workspace_root)
                return running

            try:
                portal = self._ensure_portal()
                if transport == "remote-http":
                    url = server_config.url
                    pending_transport_context = portal.wrap_async_context_manager(
                        cast(Any, streamable_http_client(url))
                    )
                    read_stream, write_stream = _read_write_streams(
                        pending_transport_context.__enter__()
                    )
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
                    pending_transport_context = portal.wrap_async_context_manager(
                        cast(Any, stdio_client(params, errlog=stderr_log))
                    )
                    read_stream, write_stream = _read_write_streams(
                        pending_transport_context.__enter__()
                    )
                    transport_context = pending_transport_context
                pending_session_context = portal.wrap_async_context_manager(
                    ClientSession(
                        cast(Any, read_stream),
                        cast(Any, write_stream),
                        read_timeout_seconds=self._request_timeout,
                        client_info=Implementation(
                            name=MCP_CLIENT_NAME,
                            version=MCP_CLIENT_VERSION,
                        ),
                    )
                )
                session = pending_session_context.__enter__()
                session_context = pending_session_context
                if transport == "stdio":
                    self._record_server_started(
                        key=key,
                        workspace_root=workspace_root,
                        command=list(server_config.command),
                    )
                else:
                    self._record_server_started(
                        key=key,
                        workspace_root=workspace_root,
                        command=[],
                        url=server_config.url,
                    )
                initialize_result = cast(
                    InitializeResult,
                    self._call_sdk_session(
                        session,
                        stage="startup",
                        method="initialize",
                        server_name=server_name,
                        workspace_root=workspace_root,
                        operation=lambda session=session: session.initialize(),
                    ),
                )
            except FileNotFoundError as exc:
                self._close_partial_server(
                    session_context=session_context,
                    transport_context=transport_context,
                    stderr_log=stderr_log,
                )
                diagnostic = create_diagnostic(
                    severity=McpDiagnosticSeverity.ERROR,
                    category="startup",
                    code="server_startup_failed",
                    server_name=server_name,
                    command=server_config.command[0] if transport == "stdio" else None,
                )
                self._record_diagnostic(diagnostic)
                if transport == "stdio":
                    message = (
                        f"MCP[{server_name}]: failed to start server - cmd not found "
                        f"(command not found): "
                        f"{server_config.command[0]}"
                    )
                else:
                    message = f"MCP[{server_name}]: failed to connect to remote server"
                self._record_failure_event(
                    key=key,
                    workspace_root=workspace_root,
                    stage="startup",
                    error=message,
                    command=list(server_config.command) if transport == "stdio" else None,
                    diagnostic=diagnostic,
                )
                raise ValueError(message) from exc
            except Exception as exc:
                self._close_partial_server(
                    session_context=session_context,
                    transport_context=transport_context,
                    stderr_log=stderr_log,
                )
                diagnostic = self._diagnostic_for_exception(
                    exc,
                    server_name=server_name,
                    stage="startup",
                    method="initialize",
                )
                self._record_diagnostic(diagnostic)
                message = self._message_for_exception(
                    exc,
                    fallback=f"MCP[{server_name}]: failed to initialize server",
                )
                self._record_failure_event(
                    key=key,
                    workspace_root=workspace_root,
                    stage="startup",
                    error=message,
                    method="initialize",
                    command=list(server_config.command) if transport == "stdio" else None,
                    diagnostic=diagnostic,
                )
                if transport_context is not None:
                    self._record_server_stopped(
                        key=key,
                        workspace_root=workspace_root,
                        preserve_failed_state=True,
                    )
                raise ValueError(message) from exc

            running = _RunningMcpServer(
                server_name=server_name,
                workspace_root=workspace_root,
                transport_context=transport_context,
                session_context=session_context,
                session=session,
                stderr_log=stderr_log,
                initialize_result=initialize_result,
                scope=key.scope,
                owner_session_id=key.owner_session_id,
                transport=transport,
            )
            self._running_servers[key] = running
            self._record_server_acquired(key=key, workspace_root=running.workspace_root)
            _ = initialize_result
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
        tool_name: str | None = None,
    ) -> object:
        try:
            with running.call_lock:
                running.last_used_at = time.monotonic()
                return self._ensure_portal().call(operation)
        except Exception as exc:
            diagnostic = self._diagnostic_for_exception(
                exc,
                server_name=running.server_name,
                stage=stage,
                method=method,
                tool_name=tool_name,
            )
            self._record_diagnostic(diagnostic)
            message = self._message_for_exception(
                exc,
                fallback=f"MCP[{running.server_name}]: {method} failed",
            )
            self._record_failure_event(
                key=_McpServerKey(
                    server_name=running.server_name,
                    scope=running.scope,
                    owner_session_id=(
                        running.owner_session_id if running.scope == "session" else None
                    ),
                ),
                workspace_root=running.workspace_root,
                stage=stage,
                error=message,
                method=method,
                diagnostic=diagnostic,
            )
            if self._should_stop_running_server(exc, stage=stage):
                self._stop_running_server_by_name(
                    server_name=running.server_name,
                    scope=running.scope,
                    owner_session_id=running.owner_session_id,
                )
            raise ValueError(message) from exc

    def _should_stop_running_server(self, exc: Exception, *, stage: str) -> bool:
        return not self._is_recoverable_call_error(exc, stage=stage)

    def _is_recoverable_call_error(self, exc: Exception, *, stage: str) -> bool:
        if stage != "call" or not isinstance(exc, McpError) or self._is_timeout_error(exc):
            return False
        code = getattr(exc.error, "code", None)
        return code in _RECOVERABLE_MCP_CALL_ERROR_CODES

    def _call_sdk_session(
        self,
        session: ClientSession,
        *,
        stage: str,
        method: str,
        server_name: str,
        workspace_root: Path,
        operation: Any,
    ) -> object:
        _ = session, stage, method, server_name, workspace_root
        return self._ensure_portal().call(operation)

    def _descriptor_from_sdk_tool(self, *, server_name: str, tool: Tool) -> McpToolDescriptor:
        annotations = tool.annotations
        safety = (
            McpToolSafety.from_hints(
                read_only_hint=annotations.readOnlyHint,
                destructive_hint=annotations.destructiveHint,
                idempotent_hint=annotations.idempotentHint,
                open_world_hint=annotations.openWorldHint,
            )
            if annotations is not None
            else McpToolSafety()
        )
        try:
            input_schema = _validate_input_schema(tool.inputSchema)
            return McpToolDescriptor(
                server_name=server_name,
                tool_name=tool.name,
                description=tool.description or "",
                input_schema=input_schema,
                safety=safety,
                enabled=True,
            )
        except ValueError as exc:
            diagnostic = create_diagnostic(
                severity=McpDiagnosticSeverity.ERROR,
                category="discovery",
                code="invalid_tool_schema",
                server_name=server_name,
                tool_name=tool.name,
                reason=str(exc),
            )
            self._record_diagnostic(diagnostic)
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
        owner_session_id: str | None,
    ) -> tuple[McpToolDescriptor, ...]:
        server_config = self._configuration.servers.get(server_name)
        if server_config is None:
            return ()
        key = self._server_key(
            server_name=server_name,
            scope=getattr(server_config, "scope", "runtime"),
            owner_session_id=owner_session_id,
        )
        running = self._ensure_running(
            server_name=server_name,
            workspace=workspace,
            owner_session_id=owner_session_id,
        )
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
        return tuple(descriptors)

    @staticmethod
    def _server_key(
        *,
        server_name: str,
        scope: str,
        owner_session_id: str | None,
    ) -> _McpServerKey:
        parsed_scope: Literal["runtime", "session"] = "session" if scope == "session" else "runtime"
        owner = owner_session_id if parsed_scope == "session" else None
        if parsed_scope == "session" and not owner:
            raise ValueError(
                f"MCP[{server_name}]: session-scoped server requires an owning session id"
            )
        return _McpServerKey(
            server_name=server_name,
            scope=parsed_scope,
            owner_session_id=owner,
        )

    def _stop_running_server(self, key: _McpServerKey) -> None:
        self._tool_descriptors_by_server.pop(key, None)
        running = self._running_servers.pop(key, None)
        if running is None:
            return
        self._terminate_running_server(running)
        self._record_server_stopped(key=key, workspace_root=running.workspace_root)

    def _stop_running_server_by_name(
        self,
        *,
        server_name: str,
        scope: Literal["runtime", "session"],
        owner_session_id: str | None,
    ) -> None:
        key = _McpServerKey(
            server_name=server_name,
            scope=scope,
            owner_session_id=owner_session_id if scope == "session" else None,
        )
        self._tool_descriptors_by_server.pop(key, None)
        with self._state_lock:
            running = self._running_servers.pop(key, None)
        if running is None:
            return
        self._terminate_running_server(running)
        self._record_server_stopped(key=key, workspace_root=running.workspace_root)

    def _record_failure_event(
        self,
        *,
        key: _McpServerKey,
        workspace_root: Path,
        stage: str,
        error: str,
        method: str | None = None,
        command: list[str] | None = None,
        diagnostic: McpDiagnostic | None = None,
    ) -> None:
        server_name = key.server_name
        payload: dict[str, object] = {
            "server": server_name,
            "scope": key.scope,
            **({"owner_session_id": key.owner_session_id} if key.owner_session_id else {}),
            "workspace_root": str(workspace_root),
            "state": "failed",
            "stage": stage,
            "error": error,
        }
        if method is not None:
            payload["method"] = method
        if command is not None:
            redacted_command = redact_mcp_command(command)
            payload["command"] = redacted_command
            payload["cmd"] = redacted_command
        if diagnostic is not None:
            payload["diagnostic"] = {
                "severity": diagnostic.severity,
                "category": diagnostic.category,
                "message": diagnostic.message,
                "details": diagnostic.details or {},
            }
        with self._state_lock:
            self._server_states[server_name] = McpServerRuntimeState(
                server_name=server_name,
                status="failed",
                workspace_root=str(workspace_root),
                stage=stage,
                error=error,
                command=list(
                    command
                    or self._server_states.get(
                        server_name, McpServerRuntimeState(server_name=server_name)
                    ).command
                ),
                scope=key.scope,
                retry_available=True,
            )
            self._record_event(
                McpRuntimeEvent(event_type=RUNTIME_MCP_SERVER_FAILED, payload=payload)
            )

    def _record_server_started(
        self,
        *,
        key: _McpServerKey,
        workspace_root: Path,
        command: list[str] | None = None,
        url: str | None = None,
    ) -> None:
        server_name = key.server_name
        with self._state_lock:
            existing = self._server_states.get(
                server_name, McpServerRuntimeState(server_name=server_name)
            )
            self._server_states[server_name] = McpServerRuntimeState(
                server_name=server_name,
                status="running",
                workspace_root=str(workspace_root),
                command=list(command or existing.command),
                url=url or existing.url,
                scope=key.scope,
                retry_available=False,
            )
            self._record_event(
                McpRuntimeEvent(
                    event_type=RUNTIME_MCP_SERVER_STARTED,
                    payload={
                        "server": server_name,
                        "scope": key.scope,
                        **(
                            {"owner_session_id": key.owner_session_id}
                            if key.owner_session_id
                            else {}
                        ),
                        "workspace_root": str(workspace_root),
                        "state": "starting",
                        "client_foundation": "python-mcp-sdk",
                        **({"url": url} if url else {}),
                    },
                )
            )

    def _record_server_stopped(
        self,
        *,
        key: _McpServerKey,
        workspace_root: Path,
        preserve_failed_state: bool = False,
    ) -> None:
        server_name = key.server_name
        with self._state_lock:
            existing_state = self._server_states.get(
                server_name, McpServerRuntimeState(server_name=server_name)
            )
            remaining_session_owner = self._remaining_session_owner_for_server(key)
            if remaining_session_owner is not None:
                self._server_states[server_name] = McpServerRuntimeState(
                    server_name=server_name,
                    status="running",
                    workspace_root=str(remaining_session_owner.workspace_root),
                    command=list(existing_state.command),
                    scope=remaining_session_owner.scope,
                    retry_available=False,
                )
            elif not (
                preserve_failed_state
                and existing_state.status == "failed"
                and existing_state.workspace_root == str(workspace_root)
            ):
                self._server_states[server_name] = McpServerRuntimeState(
                    server_name=server_name,
                    status="stopped",
                    workspace_root=str(workspace_root),
                    command=list(existing_state.command),
                    scope=key.scope,
                    retry_available=bool(self._configuration.servers),
                )
            self._record_event(
                McpRuntimeEvent(
                    event_type=RUNTIME_MCP_SERVER_STOPPED,
                    payload={
                        "server": server_name,
                        "scope": key.scope,
                        **(
                            {"owner_session_id": key.owner_session_id}
                            if key.owner_session_id
                            else {}
                        ),
                        "workspace_root": str(workspace_root),
                    },
                )
            )

    def _remaining_session_owner_for_server(self, key: _McpServerKey) -> _RunningMcpServer | None:
        if key.scope != "session":
            return None
        for running_key, running in self._running_servers.items():
            if running_key.scope == "session" and running_key.server_name == key.server_name:
                return running
        return None

    def _record_server_reused(self, *, key: _McpServerKey, workspace_root: Path) -> None:
        self._record_event(
            McpRuntimeEvent(
                event_type=RUNTIME_MCP_SERVER_REUSED,
                payload={
                    "server": key.server_name,
                    "scope": key.scope,
                    **({"owner_session_id": key.owner_session_id} if key.owner_session_id else {}),
                    "workspace_root": str(workspace_root),
                },
            )
        )

    def _record_server_acquired(self, *, key: _McpServerKey, workspace_root: Path) -> None:
        running = self._running_servers.get(key)
        if running is not None:
            running.references += 1
        self._record_event(
            McpRuntimeEvent(
                event_type=RUNTIME_MCP_SERVER_ACQUIRED,
                payload={
                    "server": key.server_name,
                    "scope": key.scope,
                    **({"owner_session_id": key.owner_session_id} if key.owner_session_id else {}),
                    "workspace_root": str(workspace_root),
                },
            )
        )

    def _record_server_released(self, *, key: _McpServerKey, reason: str) -> None:
        running = self._running_servers.get(key)
        workspace_root = running.workspace_root if running is not None else None
        if running is not None and running.references > 0:
            running.references -= 1
        self._record_event(
            McpRuntimeEvent(
                event_type=RUNTIME_MCP_SERVER_RELEASED,
                payload={
                    "server": key.server_name,
                    "scope": key.scope,
                    **({"owner_session_id": key.owner_session_id} if key.owner_session_id else {}),
                    **(
                        {"workspace_root": str(workspace_root)}
                        if workspace_root is not None
                        else {}
                    ),
                    "reason": reason,
                },
            )
        )

    def _record_server_idle_cleaned(
        self,
        *,
        key: _McpServerKey,
        workspace_root: Path,
        reason: str,
    ) -> None:
        self._record_event(
            McpRuntimeEvent(
                event_type=RUNTIME_MCP_SERVER_IDLE_CLEANED,
                payload={
                    "server": key.server_name,
                    "scope": key.scope,
                    **({"owner_session_id": key.owner_session_id} if key.owner_session_id else {}),
                    "workspace_root": str(workspace_root),
                    "reason": reason,
                },
            )
        )

    def _record_diagnostic(self, diagnostic: McpDiagnostic) -> None:
        if self._diagnostics_collector is not None:
            self._diagnostics_collector.record_diagnostic(diagnostic)

    def _diagnostic_for_exception(
        self,
        exc: Exception,
        *,
        server_name: str,
        stage: str,
        method: str,
        tool_name: str | None = None,
    ) -> McpDiagnostic:
        if self._is_timeout_error(exc):
            return create_diagnostic(
                severity=McpDiagnosticSeverity.ERROR,
                category="timeout",
                code="request_timeout",
                server_name=server_name,
                tool_name=tool_name,
                method=method,
                timeout_seconds=self._request_timeout_seconds,
            )
        if isinstance(exc, McpError):
            return create_diagnostic(
                severity=McpDiagnosticSeverity.ERROR,
                category="communication",
                code="server_error",
                server_name=server_name,
                tool_name=tool_name,
                stage=stage,
                method=method,
                error=str(exc),
            )
        return create_diagnostic(
            severity=McpDiagnosticSeverity.ERROR,
            category="communication" if stage != "startup" else "startup",
            code="unexpected_response",
            server_name=server_name,
            tool_name=tool_name,
            stage=stage,
            method=method,
            error=str(exc),
        )

    def _message_for_exception(self, exc: Exception, *, fallback: str) -> str:
        if self._is_timeout_error(exc):
            return (
                f"MCP server timed out after {self._request_timeout_seconds:.1f}s. "
                "The server may be unresponsive."
            )
        if isinstance(exc, McpError):
            return f"MCP server error: {exc}"
        return f"{fallback}: {exc}"

    @staticmethod
    def _is_timeout_error(exc: Exception) -> bool:
        if isinstance(exc, TimeoutError):
            return True
        if isinstance(exc, McpError):
            code = getattr(exc.error, "code", None)
            return code == 408 or "timed out" in str(exc).lower()
        return False

    @staticmethod
    def _close_partial_server(
        *,
        session_context: AbstractContextManager[ClientSession] | None,
        transport_context: AbstractContextManager[tuple[object, object]] | None,
        stderr_log: IO[str] | None,
    ) -> None:
        if session_context is not None:
            with suppress(Exception):
                session_context.__exit__(None, None, None)
        if transport_context is not None:
            with suppress(Exception):
                transport_context.__exit__(None, None, None)
        if stderr_log is not None:
            stderr_log.close()

    @staticmethod
    def _terminate_running_server(running: _RunningMcpServer) -> None:
        running.session_context.__exit__(None, None, None)
        running.transport_context.__exit__(None, None, None)
        if running.stderr_log is not None:
            running.stderr_log.close()

    def _record_event(self, event: McpRuntimeEvent) -> None:
        with self._state_lock:
            self._pending_events.append(event)


# =============================================================================
# Factory
# =============================================================================


def build_mcp_manager(
    config: RuntimeMcpConfig | None,
    *,
    diagnostics_collector: McpDiagnosticsCollector | None = None,
) -> McpManager:
    """Build an MCP manager based on configuration."""
    configuration = McpConfigState.from_runtime_config(config)
    if configuration.configured_enabled is not True:
        return DisabledMcpManager(config)
    return ManagedMcpManager(
        config or RuntimeMcpConfig(),
        diagnostics_collector=diagnostics_collector,
    )
