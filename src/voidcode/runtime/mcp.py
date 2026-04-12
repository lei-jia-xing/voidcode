from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol, cast

from .config import RuntimeMcpConfig, RuntimeMcpServerConfig


@dataclass(frozen=True, slots=True)
class McpConfigState:
    configured_enabled: bool = False
    servers: dict[str, RuntimeMcpServerConfig] = field(default_factory=dict)

    @classmethod
    def from_runtime_config(cls, config: RuntimeMcpConfig | None) -> McpConfigState:
        if config is None:
            return cls()
        return cls(
            configured_enabled=bool(config.enabled),
            servers=dict(config.servers or {}),
        )


@dataclass(frozen=True, slots=True)
class McpToolDescriptor:
    server_name: str
    tool_name: str
    description: str
    input_schema: dict[str, object]


@dataclass(frozen=True, slots=True)
class McpToolCallResult:
    content: list[dict[str, object]]
    is_error: bool = False


@dataclass(frozen=True, slots=True)
class McpRuntimeEvent:
    event_type: str
    payload: dict[str, object]


@dataclass(frozen=True, slots=True)
class McpManagerState:
    mode: Literal["disabled", "managed"] = "disabled"
    configuration: McpConfigState = field(default_factory=McpConfigState)


class McpManager(Protocol):
    @property
    def configuration(self) -> McpConfigState: ...

    def current_state(self) -> McpManagerState: ...

    def list_tools(self, *, workspace: Path) -> tuple[McpToolDescriptor, ...]: ...

    def call_tool(
        self,
        *,
        server_name: str,
        tool_name: str,
        arguments: dict[str, object],
        workspace: Path,
    ) -> McpToolCallResult: ...

    def shutdown(self) -> tuple[McpRuntimeEvent, ...]: ...

    def drain_events(self) -> tuple[McpRuntimeEvent, ...]: ...


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


@dataclass(slots=True)
class _RunningMcpServer:
    process: subprocess.Popen[str]
    workspace_root: Path
    initialized: bool = False


class ManagedMcpManager:
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
                            cast(dict[str, object], input_schema)
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
            content=cast(list[dict[str, object]], content if isinstance(content, list) else []),
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
        running = self._running_servers.get(server_name)
        if running is not None and running.process.poll() is None:
            if not running.initialized:
                self._initialize_server(running)
            return running

        server_config = self._configuration.servers.get(server_name)
        if server_config is None:
            raise ValueError(f"unknown MCP server: {server_name}")

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
        if process.stdin is None or process.stdout is None:
            process.kill()
            process.wait(timeout=1)
            raise ValueError(f"failed to start MCP server {server_name}: missing stdio pipe")

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
        initialize_payload = {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "voidcode-runtime", "version": "0.1.0"},
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
        if running.process.stdin is None or running.process.stdout is None:
            raise ValueError("MCP server stdio is unavailable")

        self._next_id += 1
        request_id = self._next_id
        payload: dict[str, object] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        running.process.stdin.write(json.dumps(payload) + "\n")
        running.process.stdin.flush()

        while True:
            line = running.process.stdout.readline()
            if not line:
                raise ValueError("MCP server closed stdout before responding")
            try:
                response = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"MCP server returned invalid JSON: {exc}") from exc
            if not isinstance(response, dict):
                raise ValueError("MCP server returned non-object response")
            response_payload = cast(dict[str, object], response)
            if response_payload.get("id") != request_id:
                continue
            error_obj = response_payload.get("error")
            if isinstance(error_obj, dict):
                error_payload = cast(dict[str, object], error_obj)
                message = error_payload.get("message")
                raise ValueError(
                    str(message) if isinstance(message, str) else f"MCP error: {error_obj}"
                )
            result = response_payload.get("result")
            return cast(dict[str, object], result if isinstance(result, dict) else {})

    def _stop_running_server(self, server_name: str) -> None:
        running = self._running_servers.pop(server_name, None)
        if running is None:
            return
        if running.process.stdin is not None:
            running.process.stdin.close()
        try:
            running.process.terminate()
            running.process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            running.process.kill()
            running.process.wait(timeout=1)
        self._record_event(
            McpRuntimeEvent(
                event_type="runtime.mcp_server_stopped",
                payload={"server": server_name, "workspace_root": str(running.workspace_root)},
            )
        )

    def _record_event(self, event: McpRuntimeEvent) -> None:
        self._pending_events.append(event)


def build_mcp_manager(config: RuntimeMcpConfig | None) -> McpManager:
    configuration = McpConfigState.from_runtime_config(config)
    if configuration.configured_enabled is not True:
        return DisabledMcpManager(config)
    return ManagedMcpManager(config or RuntimeMcpConfig())
