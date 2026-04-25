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
from contextlib import AbstractContextManager
from datetime import timedelta
from pathlib import Path
from typing import IO, Any, cast

from anyio.from_thread import BlockingPortal, start_blocking_portal
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
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
)
from .config import RuntimeMcpConfig
from .events import (
    RUNTIME_MCP_SERVER_FAILED,
    RUNTIME_MCP_SERVER_STARTED,
    RUNTIME_MCP_SERVER_STOPPED,
)

DEFAULT_MCP_REQUEST_TIMEOUT_SECONDS = 2.0
_RECOVERABLE_MCP_CALL_ERROR_CODES = frozenset(
    {
        McpErrorCode.TOOL_NOT_FOUND,
        McpErrorCode.INVALID_REQUEST,
        -32602,  # JSON-RPC invalid params
        -32601,  # JSON-RPC method not found
        -32600,  # JSON-RPC invalid request
    }
)


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

    def list_tools(self, *, workspace: Path) -> tuple[McpToolDescriptor, ...]:
        _ = workspace
        raise ValueError("MCP runtime support is disabled")

    def call_tool(
        self,
        *,
        server_name: str,
        tool_name: str,
        arguments: dict[str, object],
        workspace: Path,
    ) -> McpToolCallResult:
        _ = server_name, tool_name, arguments, workspace
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

    def __init__(
        self,
        *,
        server_name: str,
        workspace_root: Path,
        transport_context: AbstractContextManager[tuple[object, object]],
        session_context: AbstractContextManager[ClientSession],
        session: ClientSession,
        stderr_log: IO[str],
        initialize_result: InitializeResult,
    ) -> None:
        self.server_name = server_name
        self.workspace_root = workspace_root
        self.transport_context = transport_context
        self.session_context = session_context
        self.session = session
        self.stderr_log = stderr_log
        self.initialize_result = initialize_result


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
        self._running_servers: dict[str, _RunningMcpServer] = {}
        self._pending_events: list[McpRuntimeEvent] = []
        self._diagnostics_collector = diagnostics_collector
        self._server_states: dict[str, McpServerRuntimeState] = {
            name: McpServerRuntimeState(
                server_name=name,
                status="stopped",
                command=list(server.command),
            )
            for name, server in self._configuration.servers.items()
        }
        self._request_timeout_seconds = (
            config.request_timeout_seconds or DEFAULT_MCP_REQUEST_TIMEOUT_SECONDS
        )
        self._portal_context: AbstractContextManager[BlockingPortal] | None = None
        self._portal: BlockingPortal | None = None

    @property
    def configuration(self) -> McpConfigState:
        return self._configuration

    def current_state(self) -> McpManagerState:
        return McpManagerState(
            mode="managed",
            configuration=self._configuration,
            servers=dict(self._server_states),
        )

    def list_tools(self, *, workspace: Path) -> tuple[McpToolDescriptor, ...]:
        tools: list[McpToolDescriptor] = []
        for server_name in self._configuration.servers:
            running = self._ensure_running(server_name=server_name, workspace=workspace)
            result = self._call_sdk(
                running,
                stage="discovery",
                method="tools/list",
                operation=lambda session=running.session: session.list_tools(),
            )
            list_result = cast(ListToolsResult, result)
            for tool in list_result.tools:
                tools.append(self._descriptor_from_sdk_tool(server_name=server_name, tool=tool))
        return tuple(tools)

    def call_tool(
        self,
        *,
        server_name: str,
        tool_name: str,
        arguments: dict[str, object],
        workspace: Path,
    ) -> McpToolCallResult:
        running = self._ensure_running(server_name=server_name, workspace=workspace)
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
        for server_name in tuple(self._running_servers):
            self._stop_running_server(server_name)
        if self._portal_context is not None:
            self._portal_context.__exit__(None, None, None)
            self._portal_context = None
            self._portal = None
        return self.drain_events()

    def drain_events(self) -> tuple[McpRuntimeEvent, ...]:
        events = tuple(self._pending_events)
        self._pending_events.clear()
        return events

    def retry_connections(self, *, workspace: Path) -> None:
        for server_name in self._configuration.servers:
            self._ensure_running(server_name=server_name, workspace=workspace)

    @property
    def _request_timeout(self) -> timedelta:
        return timedelta(seconds=self._request_timeout_seconds)

    def _ensure_running(self, *, server_name: str, workspace: Path) -> _RunningMcpServer:
        """Ensure MCP server is running, start if needed."""
        running = self._running_servers.get(server_name)
        if running is not None:
            return running

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
                server_name=server_name,
                workspace_root=workspace.resolve(),
                stage="startup",
                error=message,
                diagnostic=diagnostic,
            )
            raise ValueError(message)

        if not server_config.command:
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
                server_name=server_name,
                workspace_root=workspace.resolve(),
                stage="startup",
                error=message,
                diagnostic=diagnostic,
            )
            raise ValueError(message)

        portal = self._ensure_portal()
        workspace_root = workspace.resolve()
        stderr_log = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115 - closed in shutdown
        transport_context: AbstractContextManager[tuple[object, object]] | None = None
        session_context: AbstractContextManager[ClientSession] | None = None
        try:
            params = StdioServerParameters(
                command=server_config.command[0],
                args=list(server_config.command[1:]),
                env={**os.environ, **server_config.env},
                cwd=workspace_root,
            )
            pending_transport_context = portal.wrap_async_context_manager(
                cast(Any, stdio_client(params, errlog=stderr_log))
            )
            read_stream, write_stream = pending_transport_context.__enter__()
            transport_context = pending_transport_context
            pending_session_context = portal.wrap_async_context_manager(
                ClientSession(
                    read_stream,
                    write_stream,
                    read_timeout_seconds=self._request_timeout,
                    client_info=Implementation(
                        name=MCP_CLIENT_NAME,
                        version=MCP_CLIENT_VERSION,
                    ),
                )
            )
            session = pending_session_context.__enter__()
            session_context = pending_session_context
            self._record_server_started(
                server_name=server_name,
                workspace_root=workspace_root,
                command=list(server_config.command),
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
                command=server_config.command[0],
            )
            self._record_diagnostic(diagnostic)
            message = (
                f"MCP[{server_name}]: failed to start server - cmd not found "
                f"(command not found): "
                f"{server_config.command[0]}"
            )
            self._record_failure_event(
                server_name=server_name,
                workspace_root=workspace_root,
                stage="startup",
                error=message,
                command=list(server_config.command),
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
                server_name=server_name,
                workspace_root=workspace_root,
                stage="startup",
                error=message,
                method="initialize",
                command=list(server_config.command),
                diagnostic=diagnostic,
            )
            if transport_context is not None:
                self._record_server_stopped(
                    server_name=server_name,
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
        )
        self._running_servers[server_name] = running
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
                server_name=running.server_name,
                workspace_root=running.workspace_root,
                stage=stage,
                error=message,
                method=method,
                diagnostic=diagnostic,
            )
            if self._should_stop_running_server(exc, stage=stage):
                self._stop_running_server_by_name(running.server_name)
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
        return McpToolDescriptor(
            server_name=server_name,
            tool_name=tool.name,
            description=tool.description or "",
            input_schema=dict(tool.inputSchema),
            safety=safety,
        )

    def _stop_running_server(self, server_name: str) -> None:
        running = self._running_servers.pop(server_name, None)
        if running is None:
            return
        self._terminate_running_server(running)
        self._record_server_stopped(server_name=server_name, workspace_root=running.workspace_root)

    def _stop_running_server_by_name(self, server_name: str) -> None:
        self._stop_running_server(server_name)

    def _record_failure_event(
        self,
        *,
        server_name: str,
        workspace_root: Path,
        stage: str,
        error: str,
        method: str | None = None,
        command: list[str] | None = None,
        diagnostic: McpDiagnostic | None = None,
    ) -> None:
        payload: dict[str, object] = {
            "server": server_name,
            "workspace_root": str(workspace_root),
            "state": "failed",
            "stage": stage,
            "error": error,
        }
        if method is not None:
            payload["method"] = method
        if command is not None:
            payload["command"] = command
            payload["cmd"] = command
        if diagnostic is not None:
            payload["diagnostic"] = {
                "severity": diagnostic.severity,
                "category": diagnostic.category,
                "message": diagnostic.message,
                "details": diagnostic.details or {},
            }
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
            retry_available=True,
        )
        self._record_event(McpRuntimeEvent(event_type=RUNTIME_MCP_SERVER_FAILED, payload=payload))

    def _record_server_started(
        self,
        *,
        server_name: str,
        workspace_root: Path,
        command: list[str] | None = None,
    ) -> None:
        self._server_states[server_name] = McpServerRuntimeState(
            server_name=server_name,
            status="running",
            workspace_root=str(workspace_root),
            command=list(
                command
                or self._server_states.get(
                    server_name, McpServerRuntimeState(server_name=server_name)
                ).command
            ),
            retry_available=False,
        )
        self._record_event(
            McpRuntimeEvent(
                event_type=RUNTIME_MCP_SERVER_STARTED,
                payload={
                    "server": server_name,
                    "workspace_root": str(workspace_root),
                    "state": "starting",
                    "client_foundation": "python-mcp-sdk",
                },
            )
        )

    def _record_server_stopped(
        self,
        *,
        server_name: str,
        workspace_root: Path,
        preserve_failed_state: bool = False,
    ) -> None:
        existing_state = self._server_states.get(
            server_name, McpServerRuntimeState(server_name=server_name)
        )
        if not (
            preserve_failed_state
            and existing_state.status == "failed"
            and existing_state.workspace_root == str(workspace_root)
        ):
            self._server_states[server_name] = McpServerRuntimeState(
                server_name=server_name,
                status="stopped",
                workspace_root=str(workspace_root),
                command=list(existing_state.command),
                retry_available=bool(self._configuration.servers),
            )
        self._record_event(
            McpRuntimeEvent(
                event_type=RUNTIME_MCP_SERVER_STOPPED,
                payload={"server": server_name, "workspace_root": str(workspace_root)},
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
        stderr_log: IO[str],
    ) -> None:
        if session_context is not None:
            session_context.__exit__(None, None, None)
        if transport_context is not None:
            transport_context.__exit__(None, None, None)
        stderr_log.close()

    @staticmethod
    def _terminate_running_server(running: _RunningMcpServer) -> None:
        running.session_context.__exit__(None, None, None)
        running.transport_context.__exit__(None, None, None)
        running.stderr_log.close()

    def _record_event(self, event: McpRuntimeEvent) -> None:
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
