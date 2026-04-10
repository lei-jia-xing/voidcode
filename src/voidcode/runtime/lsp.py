from __future__ import annotations

import json
import os
import select
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol, cast

from .config import RuntimeLspConfig, RuntimeLspServerConfig


@dataclass(frozen=True, slots=True)
class LspConfigState:
    configured_enabled: bool = False
    servers: dict[str, RuntimeLspServerConfig] = field(default_factory=dict)

    @classmethod
    def from_runtime_config(cls, config: RuntimeLspConfig | None) -> LspConfigState:
        if config is None:
            return cls()
        return cls(
            configured_enabled=bool(config.enabled),
            servers={name: definition for name, definition in (config.servers or {}).items()},
        )

    def resolve(self, server_name: str) -> RuntimeLspServerConfig | None:
        return self.servers.get(server_name)

    def default_server_name(self) -> str | None:
        if not self.servers:
            return None
        return next(iter(self.servers))


@dataclass(frozen=True, slots=True)
class LspServerState:
    name: str
    configured: bool = True
    status: Literal["stopped", "starting", "running", "failed"] = "stopped"
    available: bool = False
    last_error: str | None = None


@dataclass(frozen=True, slots=True)
class LspManagerState:
    mode: Literal["disabled", "managed"] = "disabled"
    configuration: LspConfigState = field(default_factory=LspConfigState)
    servers: dict[str, LspServerState] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LspRuntimeEvent:
    event_type: str
    payload: dict[str, object]


@dataclass(frozen=True, slots=True)
class LspRequest:
    server_name: str | None
    method: str
    params: dict[str, object]
    workspace: Path


@dataclass(frozen=True, slots=True)
class LspRequestResult:
    response: dict[str, object]


class LspManager(Protocol):
    @property
    def configuration(self) -> LspConfigState: ...

    def current_state(self) -> LspManagerState: ...

    def request(self, request: LspRequest) -> LspRequestResult: ...

    def shutdown(self) -> tuple[LspRuntimeEvent, ...]: ...

    def drain_events(self) -> tuple[LspRuntimeEvent, ...]: ...


class DisabledLspManager:
    def __init__(self, config: RuntimeLspConfig | None = None) -> None:
        self._configuration = LspConfigState.from_runtime_config(config)

    @property
    def configuration(self) -> LspConfigState:
        return self._configuration

    def resolve(self, server_name: str) -> RuntimeLspServerConfig | None:
        return self._configuration.resolve(server_name)

    def current_state(self) -> LspManagerState:
        servers: dict[str, LspServerState] = {
            name: LspServerState(name=name) for name in self._configuration.servers
        }
        return LspManagerState(configuration=self._configuration, servers=servers)

    def request(self, request: LspRequest) -> LspRequestResult:
        _ = request
        raise ValueError("LSP runtime support is disabled")

    def shutdown(self) -> tuple[LspRuntimeEvent, ...]:
        return ()

    def drain_events(self) -> tuple[LspRuntimeEvent, ...]:
        return ()


@dataclass(slots=True)
class _RunningLspServer:
    config: RuntimeLspServerConfig
    process: subprocess.Popen[bytes]
    initialized: bool = False


class ManagedLspManager:
    def __init__(self, config: RuntimeLspConfig) -> None:
        self._configuration = LspConfigState.from_runtime_config(config)
        self._server_states: dict[str, LspServerState] = {
            name: LspServerState(name=name) for name in self._configuration.servers
        }
        self._running_servers: dict[str, _RunningLspServer] = {}
        self._pending_events: list[LspRuntimeEvent] = []

    @property
    def configuration(self) -> LspConfigState:
        return self._configuration

    def current_state(self) -> LspManagerState:
        return LspManagerState(
            mode="managed",
            configuration=self._configuration,
            servers=dict(self._server_states),
        )

    def request(self, request: LspRequest) -> LspRequestResult:
        server_name = request.server_name or self._configuration.default_server_name()
        if server_name is None:
            raise ValueError("no LSP server is configured")

        server_config = self._configuration.resolve(server_name)
        if server_config is None:
            raise ValueError(f"unknown LSP server: {server_name}")

        running_server = self._ensure_running(
            server_name=server_name,
            server_config=server_config,
            workspace=request.workspace,
        )
        response = self._send_request(
            running_server,
            method=request.method,
            params=request.params,
            server_name=server_name,
        )
        return LspRequestResult(response=response)

    def shutdown(self) -> tuple[LspRuntimeEvent, ...]:
        for server_name, running_server in list(self._running_servers.items()):
            process = running_server.process
            if process.poll() is None:
                self._send_request(
                    running_server,
                    method="shutdown",
                    params={},
                    server_name=server_name,
                )
                self._send_notification(process, method="exit", params={})
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=1)

            self._running_servers.pop(server_name, None)
            self._server_states[server_name] = LspServerState(
                name=server_name,
                status="stopped",
                available=False,
            )
            self._record_event(
                LspRuntimeEvent(
                    event_type="runtime.lsp_server_stopped",
                    payload={
                        "server": server_name,
                        "command": list(running_server.config.command),
                        "state": "stopped",
                    },
                )
            )
        return self.drain_events()

    def drain_events(self) -> tuple[LspRuntimeEvent, ...]:
        events = tuple(self._pending_events)
        self._pending_events.clear()
        return events

    def _ensure_running(
        self,
        *,
        server_name: str,
        server_config: RuntimeLspServerConfig,
        workspace: Path,
    ) -> _RunningLspServer:
        running_server = self._running_servers.get(server_name)
        if running_server is not None and running_server.process.poll() is None:
            if not running_server.initialized:
                self._initialize_server(
                    running_server,
                    server_name=server_name,
                    workspace=workspace,
                )
            return running_server

        self._server_states[server_name] = LspServerState(
            name=server_name,
            status="starting",
            available=False,
        )
        try:
            process = subprocess.Popen(
                list(server_config.command),
                cwd=workspace,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except (FileNotFoundError, OSError) as exc:
            message = f"failed to start LSP server {server_name}: {exc}"
            self._mark_failed(server_name=server_name, error=message)
            self._record_event(
                self._failed_event(
                    server_name=server_name, server_config=server_config, error=message
                )
            )
            raise ValueError(message) from exc

        if process.stdin is None or process.stdout is None:
            process.kill()
            process.wait(timeout=1)
            message = f"failed to start LSP server {server_name}: missing stdio pipe"
            self._mark_failed(server_name=server_name, error=message)
            self._record_event(
                self._failed_event(
                    server_name=server_name, server_config=server_config, error=message
                )
            )
            raise ValueError(message)

        running_server = _RunningLspServer(config=server_config, process=process)
        self._running_servers[server_name] = running_server
        self._initialize_server(
            running_server,
            server_name=server_name,
            workspace=workspace,
        )
        return running_server

    def _initialize_server(
        self,
        running_server: _RunningLspServer,
        *,
        server_name: str,
        workspace: Path,
    ) -> None:
        init_params: dict[str, object] = {
            "processId": os.getpid(),
            "clientInfo": {"name": "voidcode", "version": "0.1.0"},
            "locale": "zh-CN",
            "rootUri": workspace.as_uri(),
            "workspaceFolders": [
                {
                    "uri": workspace.as_uri(),
                    "name": workspace.name or str(workspace),
                }
            ],
            "capabilities": {},
        }
        try:
            response = self._send_request(
                running_server,
                method="initialize",
                params=init_params,
                server_name=server_name,
            )
        except ValueError as exc:
            message = str(exc)
            self._mark_failed(
                server_name=server_name,
                error=message,
            )
            self._record_event(
                self._failed_event(
                    server_name=server_name,
                    server_config=running_server.config,
                    error=message,
                )
            )
            raise

        init_error = response.get("error")
        if init_error is not None:
            message = f"LSP initialize failed for {server_name}: {init_error}"
            self._mark_failed(
                server_name=server_name,
                error=message,
            )
            self._record_event(
                self._failed_event(
                    server_name=server_name,
                    server_config=running_server.config,
                    error=message,
                )
            )
            raise ValueError(message)

        self._send_notification(running_server.process, method="initialized", params={})
        running_server.initialized = True
        self._server_states[server_name] = LspServerState(
            name=server_name,
            status="running",
            available=True,
        )
        self._record_event(
            LspRuntimeEvent(
                event_type="runtime.lsp_server_started",
                payload={
                    "server": server_name,
                    "command": list(running_server.config.command),
                    "state": "running",
                },
            )
        )

    def _record_event(self, event: LspRuntimeEvent) -> None:
        self._pending_events.append(event)

    def _mark_failed(
        self,
        *,
        server_name: str,
        error: str,
    ) -> None:
        running_server = self._running_servers.pop(server_name, None)
        if running_server is not None and running_server.process.poll() is None:
            running_server.process.terminate()
            try:
                running_server.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                running_server.process.kill()
                running_server.process.wait(timeout=1)
        self._server_states[server_name] = LspServerState(
            name=server_name,
            status="failed",
            available=False,
            last_error=error,
        )

    @staticmethod
    def _failed_event(
        *,
        server_name: str,
        server_config: RuntimeLspServerConfig,
        error: str,
    ) -> LspRuntimeEvent:
        return LspRuntimeEvent(
            event_type="runtime.lsp_server_failed",
            payload={
                "server": server_name,
                "command": list(server_config.command),
                "state": "failed",
                "error": error,
            },
        )

    def _send_request(
        self,
        running_server: _RunningLspServer,
        *,
        method: str,
        params: dict[str, object],
        server_name: str,
    ) -> dict[str, object]:
        process = running_server.process
        self._write_message(
            process=process,
            message={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        )
        response = self._read_message(process=process)
        while response is not None and response.get("id") != 1:
            response = self._read_message(process=process)
        if response is None:
            raise ValueError(f"No response from LSP server {server_name} for {method}")
        return response

    def _send_notification(
        self,
        process: subprocess.Popen[bytes],
        *,
        method: str,
        params: dict[str, object],
    ) -> None:
        self._write_message(
            process=process, message={"jsonrpc": "2.0", "method": method, "params": params}
        )

    @staticmethod
    def _write_message(process: subprocess.Popen[bytes], message: dict[str, object]) -> None:
        if process.stdin is None:
            raise ValueError("LSP process stdin is unavailable")
        body = json.dumps(message).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        try:
            process.stdin.write(header + body)
            process.stdin.flush()
        except BrokenPipeError as exc:
            raise ValueError("LSP server pipe closed unexpectedly") from exc

    @staticmethod
    def _read_message(
        process: subprocess.Popen[bytes], *, timeout: float = 30.0
    ) -> dict[str, object] | None:
        if process.stdout is None:
            raise ValueError("LSP process stdout is unavailable")

        # Read header with timeout
        header = b""
        while b"\r\n\r\n" not in header:
            ready, _, _ = select.select([process.stdout], [], [], timeout)
            if not ready:
                raise TimeoutError(f"LSP server did not respond within {timeout}s")
            chunk = process.stdout.read(1)
            if not chunk:
                return None
            header += chunk

        header_text = header.decode("ascii", errors="ignore")
        content_length = None
        for line in header_text.split("\r\n"):
            if line.lower().startswith("content-length:"):
                content_length = int(line.split(":", 1)[1].strip())
                break
        if content_length is None:
            return None

        # Read body with timeout
        ready, _, _ = select.select([process.stdout], [], [], timeout)
        if not ready:
            raise TimeoutError(f"LSP server did not send body within {timeout}s")
        body = process.stdout.read(content_length)
        if not body:
            return None
        return cast(dict[str, object], json.loads(body.decode("utf-8")))


def build_lsp_manager(config: RuntimeLspConfig | None) -> LspManager:
    configuration = LspConfigState.from_runtime_config(config)
    if configuration.configured_enabled is not True or not configuration.servers:
        return DisabledLspManager(config)
    return ManagedLspManager(config or RuntimeLspConfig())
