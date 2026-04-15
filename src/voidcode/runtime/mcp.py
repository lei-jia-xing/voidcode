"""
MCP Runtime Implementation

This module provides the runtime-owned MCP lifecycle management, including
subprocess spawning, stdio communication, and tool discovery.

IMPORTANT: Static types and contracts are now in src/voidcode/mcp/.
This module imports from there and adds runtime-specific implementation.

Issue: https://github.com/lei-jia-xing/voidcode/issues/107
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from ..mcp import (
    MCP_CLIENT_NAME,
    MCP_CLIENT_VERSION,
    MCP_PROTOCOL_VERSION,
    McpConfigState,
    McpDiagnosticSeverity,
    McpManager,
    McpManagerState,
    McpRuntimeEvent,
    McpToolCallResult,
    McpToolDescriptor,
    create_diagnostic,
)
from .config import RuntimeMcpConfig

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


# =============================================================================
# Runtime MCP Server (process lifecycle)
# =============================================================================


@dataclass(slots=True)
class _RunningMcpServer:
    """Runtime-specific: process handle and state."""

    process: subprocess.Popen[str]
    workspace_root: Path
    initialized: bool = False


# =============================================================================
# Managed MCP Manager
# =============================================================================


class ManagedMcpManager:
    """Runtime-owned MCP lifecycle management."""

    _request_timeout_seconds = 2.0

    def __init__(self, config: RuntimeMcpConfig) -> None:
        self._configuration = McpConfigState.from_runtime_config(config)
        self._running_servers: dict[str, _RunningMcpServer] = {}
        self._pending_events: list[McpRuntimeEvent] = []
        self._next_id = 0

    @property
    def configuration(self) -> McpConfigState:
        return self._configuration

    def current_state(self) -> McpManagerState:
        return McpManagerState(mode="managed", configuration=self._configuration)

    def list_tools(self, *, workspace: Path) -> tuple[McpToolDescriptor, ...]:
        tools: list[McpToolDescriptor] = []
        for server_name in self._configuration.servers:
            running = self._ensure_running(server_name=server_name, workspace=workspace)
            result = self._send_request(running, method="tools/list")
            for tool_obj in cast(list[object], result.get("tools", [])):
                if not isinstance(tool_obj, dict):
                    continue
                tool_payload = cast(dict[str, object], tool_obj)
                tool_name = tool_payload.get("name")
                if not isinstance(tool_name, str):
                    continue
                description = tool_payload.get("description")
                input_schema = tool_payload.get("inputSchema")
                tools.append(
                    McpToolDescriptor(
                        server_name=server_name,
                        tool_name=tool_name,
                        description=description if isinstance(description, str) else "",
                        input_schema=(
                            cast(dict[str, Any], input_schema)
                            if isinstance(input_schema, dict)
                            else {}
                        ),
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
    ) -> McpToolCallResult:
        running = self._ensure_running(server_name=server_name, workspace=workspace)
        result = self._send_request(
            running,
            method="tools/call",
            params={"name": tool_name, "arguments": arguments},
        )
        content = result.get("content")
        is_error = result.get("isError")
        return McpToolCallResult(
            content=cast(list[dict[str, Any]], content if isinstance(content, list) else []),
            is_error=bool(is_error),
        )

    def shutdown(self) -> tuple[McpRuntimeEvent, ...]:
        for server_name in tuple(self._running_servers):
            self._stop_running_server(server_name)
        return self.drain_events()

    def drain_events(self) -> tuple[McpRuntimeEvent, ...]:
        events = tuple(self._pending_events)
        self._pending_events.clear()
        return events

    def _ensure_running(self, *, server_name: str, workspace: Path) -> _RunningMcpServer:
        """Ensure MCP server is running, start if needed."""
        running = self._running_servers.get(server_name)
        if running is not None and running.process.poll() is None:
            if not running.initialized:
                self._initialize_server(running)
            return running

        server_config = self._configuration.servers.get(server_name)
        if server_config is None:
            # Note: diagnostic created for observability (future use)
            _ = create_diagnostic(
                severity=McpDiagnosticSeverity.ERROR,
                category="startup",
                code="server_not_found",
                server_name=server_name,
            )
            raise ValueError(
                f"MCP[{server_name}]: server not found in configuration. "
                f"Available servers: {list(self._configuration.servers.keys())}"
            )

        # Note: diagnostic created for observability (future use)
        if not server_config.command:
            _ = create_diagnostic(
                severity=McpDiagnosticSeverity.ERROR,
                category="startup",
                code="server_command_missing",
                server_name=server_name,
            )
            raise ValueError(
                f"MCP[{server_name}]: command is empty. "
                "Please configure mcp.servers.{server_name}.command in .voidcode.json"
            )

        try:
            process = subprocess.Popen(
                list(server_config.command),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                cwd=workspace.resolve(),
                env={**os.environ, **server_config.env},
                bufsize=1,
            )
        except FileNotFoundError as exc:
            # Note: diagnostic created for observability (future use)
            _ = create_diagnostic(
                severity=McpDiagnosticSeverity.ERROR,
                category="startup",
                code="server_startup_failed",
                server_name=server_name,
                command=server_config.command[0] if server_config.command else "unknown",
            )
            raise ValueError(
                f"MCP[{server_name}]: failed to start server - command not found: "
                f"{server_config.command[0]}"
            ) from exc
        except OSError as exc:
            # Note: diagnostic created for observability (future use)
            _ = create_diagnostic(
                severity=McpDiagnosticSeverity.ERROR,
                category="startup",
                code="server_startup_failed",
                server_name=server_name,
                error=str(exc),
            )
            raise ValueError(f"MCP[{server_name}]: failed to start server: {exc}") from exc

        if process.stdin is None or process.stdout is None:
            process.kill()
            process.wait(timeout=1)
            # Note: diagnostic created for observability (future use)
            _ = create_diagnostic(
                severity=McpDiagnosticSeverity.ERROR,
                category="startup",
                code="stdio_pipe_failed",
                server_name=server_name,
            )
            raise ValueError(
                f"MCP[{server_name}]: failed to start server - stdio pipe unavailable. "
                "The server command may not be a valid executable."
            )

        running = _RunningMcpServer(process=process, workspace_root=workspace.resolve())
        self._running_servers[server_name] = running
        self._record_event(
            McpRuntimeEvent(
                event_type="runtime.mcp_server_started",
                payload={"server": server_name, "workspace_root": str(workspace.resolve())},
            )
        )
        self._initialize_server(running)
        return running

    def _initialize_server(self, running: _RunningMcpServer) -> None:
        """Initialize MCP server with protocol handshake."""
        initialize_payload = {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": MCP_CLIENT_NAME, "version": MCP_CLIENT_VERSION},
        }
        self._send_request(
            running,
            method="initialize",
            params=cast(dict[str, object], initialize_payload),
        )
        self._send_notification(running, method="notifications/initialized")
        running.initialized = True

    def _send_notification(
        self,
        running: _RunningMcpServer,
        *,
        method: str,
        params: dict[str, object] | None = None,
    ) -> None:
        if running.process.stdin is None:
            raise ValueError("MCP server stdin is unavailable")
        payload: dict[str, object] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        running.process.stdin.write(json.dumps(payload) + "\n")
        running.process.stdin.flush()

    def _send_request(
        self,
        running: _RunningMcpServer,
        *,
        method: str,
        params: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """Send JSON-RPC request and wait for response."""
        if running.process.stdin is None or running.process.stdout is None:
            # Note: diagnostic created for observability (future use)
            _ = create_diagnostic(
                severity=McpDiagnosticSeverity.ERROR,
                category="communication",
                code="stdio_unavailable",
                server_name=next(
                    (n for n, r in self._running_servers.items() if r is running),
                    "unknown",
                ),
            )
            raise ValueError(
                "MCP server stdio is unavailable. The server process may have crashed."
            )

        self._next_id += 1
        request_id = self._next_id
        payload: dict[str, object] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        running.process.stdin.write(json.dumps(payload) + "\n")
        running.process.stdin.flush()

        while True:
            response_queue: queue.Queue[str] = queue.Queue(maxsize=1)

            def _readline(target_queue: queue.Queue[str]) -> None:
                assert running.process.stdout is not None
                target_queue.put(running.process.stdout.readline())

            reader = threading.Thread(target=_readline, args=(response_queue,), daemon=True)
            reader.start()

            try:
                line = response_queue.get(timeout=self._request_timeout_seconds)
            except queue.Empty as exc:
                self._stop_running_server_by_process(running)
                # Note: diagnostic created for observability (future use)
                _ = create_diagnostic(
                    severity=McpDiagnosticSeverity.ERROR,
                    category="timeout",
                    code="request_timeout",
                    method=method,
                    timeout_seconds=self._request_timeout_seconds,
                )
                raise ValueError(
                    f"MCP server timed out waiting for response to {method} after "
                    f"{self._request_timeout_seconds:.1f}s. The server may be unresponsive."
                ) from exc

            if not line:
                # Note: diagnostic created for observability (future use)
                _ = create_diagnostic(
                    severity=McpDiagnosticSeverity.ERROR,
                    category="communication",
                    code="stdio_closed",
                )
                raise ValueError(
                    "MCP server closed stdout before responding. "
                    "The server may have crashed or produced invalid output."
                )

            try:
                response = json.loads(line)
            except json.JSONDecodeError as exc:
                # Note: diagnostic created for observability (future use)
                _ = create_diagnostic(
                    severity=McpDiagnosticSeverity.ERROR,
                    category="communication",
                    code="json_decode_failed",
                    error=str(exc),
                    raw_line=line[:200] if len(line) > 200 else line,
                )
                raise ValueError(
                    f"MCP server returned invalid JSON: {exc}. Server output: {line[:100]}..."
                ) from exc

            if not isinstance(response, dict):
                # Note: diagnostic created for observability (future use)
                _ = create_diagnostic(
                    severity=McpDiagnosticSeverity.ERROR,
                    category="communication",
                    code="unexpected_response_type",
                    response_type=type(response).__name__,
                )
                raise ValueError(
                    f"MCP server returned non-object response: {type(response).__name__}"
                )

            response_payload = cast(dict[str, object], response)
            if response_payload.get("id") != request_id:
                continue
            error_obj = response_payload.get("error")
            if isinstance(error_obj, dict):
                error_payload = cast(dict[str, object], error_obj)
                message = error_payload.get("message")
                # Note: diagnostic created for observability (future use)
                _ = create_diagnostic(
                    severity=McpDiagnosticSeverity.ERROR,
                    category="communication",
                    code="server_error",
                    method=method,
                    error=error_payload,
                )
                raise ValueError(
                    f"MCP server error: {str(message) if isinstance(message, str) else error_obj}"
                )
            result = response_payload.get("result")
            return cast(dict[str, object], result if isinstance(result, dict) else {})

    def _stop_running_server(self, server_name: str) -> None:
        running = self._running_servers.pop(server_name, None)
        if running is None:
            return
        self._terminate_running_server(running)
        self._record_event(
            McpRuntimeEvent(
                event_type="runtime.mcp_server_stopped",
                payload={"server": server_name, "workspace_root": str(running.workspace_root)},
            )
        )

    def _stop_running_server_by_process(self, running: _RunningMcpServer) -> None:
        server_name = next(
            (name for name, active in self._running_servers.items() if active is running),
            None,
        )
        if server_name is None:
            return
        self._running_servers.pop(server_name, None)
        self._terminate_running_server(running)
        self._record_event(
            McpRuntimeEvent(
                event_type="runtime.mcp_server_stopped",
                payload={"server": server_name, "workspace_root": str(running.workspace_root)},
            )
        )

    @staticmethod
    def _terminate_running_server(running: _RunningMcpServer) -> None:
        if running.process.stdin is not None:
            running.process.stdin.close()
        try:
            running.process.terminate()
            running.process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            running.process.kill()
            running.process.wait(timeout=1)

    def _record_event(self, event: McpRuntimeEvent) -> None:
        self._pending_events.append(event)


# =============================================================================
# Factory
# =============================================================================


def build_mcp_manager(config: RuntimeMcpConfig | None) -> McpManager:
    """Build an MCP manager based on configuration."""
    configuration = McpConfigState.from_runtime_config(config)
    if configuration.configured_enabled is not True:
        return DisabledMcpManager(config)
    return ManagedMcpManager(config or RuntimeMcpConfig())
