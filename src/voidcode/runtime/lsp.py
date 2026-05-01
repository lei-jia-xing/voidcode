from __future__ import annotations

import json
import logging
import os
import select
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol, cast
from urllib.parse import urlparse
from urllib.request import url2pathname

from lsprotocol import converters as lsp_converters
from lsprotocol import types as lsp_types

from ..lsp import (
    ResolvedLspServerConfig,
    discover_workspace_root,
    match_lsp_servers_for_path,
    resolve_lsp_server_configs,
)
from .config import RuntimeLspConfig

logger = logging.getLogger(__name__)

MAX_LSP_HEADER_BYTES = 8192
MAX_LSP_MESSAGE_BYTES = 1024 * 1024


class LspRuntimeError(ValueError):
    """Base class for runtime-managed LSP failures."""


class LspProtocolError(LspRuntimeError):
    """Raised when an LSP server violates JSON-RPC/LSP framing or payload rules."""


class LspMessageBoundsError(LspRuntimeError):
    """Raised when an LSP message exceeds configured runtime bounds."""


@dataclass(frozen=True, slots=True)
class LspConfigState:
    configured_enabled: bool = False
    servers: dict[str, ResolvedLspServerConfig] = field(default_factory=dict)

    @classmethod
    def from_runtime_config(cls, config: RuntimeLspConfig | None) -> LspConfigState:
        if config is None:
            return cls()
        return cls(
            configured_enabled=bool(config.enabled),
            servers=resolve_lsp_server_configs(config.servers),
        )

    def resolve(self, server_name: str) -> ResolvedLspServerConfig | None:
        return self.servers.get(server_name)

    def matching_servers(self, file_path: Path) -> tuple[str, ...]:
        return match_lsp_servers_for_path(self.servers, file_path)

    def default_server_name(self, file_path: Path | None = None) -> str | None:
        if file_path is not None:
            matching_servers = self.matching_servers(file_path)
            if matching_servers:
                return matching_servers[0]
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

    def resolve(self, server_name: str) -> ResolvedLspServerConfig | None:
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
    config: ResolvedLspServerConfig
    process: subprocess.Popen[bytes]
    workspace_root: Path
    initialized: bool = False
    next_request_id: int = 1
    request_lock: threading.Lock = field(default_factory=threading.Lock)


@dataclass(slots=True)
class _SharedLspServerEntry:
    server: _RunningLspServer
    generation: int
    ref_count: int = 0


@dataclass(frozen=True, slots=True)
class _LspRegistryKey:
    server_name: str
    workspace_root: Path


@dataclass(frozen=True, slots=True)
class _LspServerLease:
    key: _LspRegistryKey
    generation: int


class _WorkspaceScopedLspRegistry:
    def __init__(self) -> None:
        self._entries: dict[_LspRegistryKey, _SharedLspServerEntry] = {}
        self._lock = threading.RLock()
        self._next_generation = 1

    def _allocate_generation(self) -> int:
        generation = self._next_generation
        self._next_generation += 1
        return generation

    def acquire(
        self,
        *,
        key: _LspRegistryKey,
        config: ResolvedLspServerConfig,
        factory: Callable[[], _RunningLspServer],
    ) -> tuple[_RunningLspServer, _LspServerLease, Literal["started", "reused"]]:
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and entry.server.process.poll() is None:
                if entry.server.config != config:
                    raise ValueError(
                        "workspace-scoped LSP reuse rejected because the existing "
                        f"{key.server_name} server for {key.workspace_root} was started with "
                        "a different resolved configuration"
                    )
                entry.ref_count += 1
                return (
                    entry.server,
                    _LspServerLease(key=key, generation=entry.generation),
                    "reused",
                )

            if entry is not None:
                self._entries.pop(key, None)

            server = factory()
            generation = self._allocate_generation()
            self._entries[key] = _SharedLspServerEntry(
                server=server,
                generation=generation,
                ref_count=1,
            )
            return server, _LspServerLease(key=key, generation=generation), "started"

    def release(self, *, lease: _LspServerLease) -> _RunningLspServer | None:
        with self._lock:
            entry = self._entries.get(lease.key)
            if entry is None:
                return None
            if entry.generation != lease.generation:
                return None
            if entry.ref_count > 1:
                entry.ref_count -= 1
                return None
            self._entries.pop(lease.key, None)
            return entry.server


_WORKSPACE_SCOPED_LSP_REGISTRY = _WorkspaceScopedLspRegistry()


class ManagedLspManager:
    def __init__(self, config: RuntimeLspConfig) -> None:
        self._configuration = LspConfigState.from_runtime_config(config)
        self._server_states: dict[str, LspServerState] = {
            name: LspServerState(name=name) for name in self._configuration.servers
        }
        self._running_servers: dict[str, _RunningLspServer] = {}
        self._leased_servers: dict[str, _LspServerLease] = {}
        self._pending_events: list[LspRuntimeEvent] = []
        self._converter = lsp_converters.get_converter()

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
        request_path = self._request_file_path(request)
        server_name = request.server_name or self._configuration.default_server_name(request_path)
        if server_name is None:
            raise ValueError("no LSP server is configured")

        server_config = self._configuration.resolve(server_name)
        if server_config is None:
            raise ValueError(f"unknown LSP server: {server_name}")

        workspace_root = self._workspace_root_for_request(
            request=request,
            server_config=server_config,
            request_path=request_path,
        )
        running_server = self._ensure_running(
            server_name=server_name,
            server_config=server_config,
            workspace_root=workspace_root,
        )
        response = self._send_request(
            running_server,
            method=request.method,
            params=request.params,
            server_name=server_name,
        )
        return LspRequestResult(response=response)

    def shutdown(self) -> tuple[LspRuntimeEvent, ...]:
        for server_name in tuple(self._running_servers):
            self._release_server_lease(server_name, record_event=True, reason="shutdown")
        return self.drain_events()

    def drain_events(self) -> tuple[LspRuntimeEvent, ...]:
        events = tuple(self._pending_events)
        self._pending_events.clear()
        return events

    def _ensure_running(
        self,
        *,
        server_name: str,
        server_config: ResolvedLspServerConfig,
        workspace_root: Path,
    ) -> _RunningLspServer:
        running_server = self._running_servers.get(server_name)
        if running_server is not None and running_server.process.poll() is None:
            if running_server.workspace_root == workspace_root:
                return running_server
            self._release_server_lease(server_name, record_event=True, reason="workspace_switch")

        lease_key = _LspRegistryKey(server_name=server_name, workspace_root=workspace_root)

        self._server_states[server_name] = LspServerState(
            name=server_name,
            status="starting",
            available=False,
        )
        try:
            running_server, lease, outcome = _WORKSPACE_SCOPED_LSP_REGISTRY.acquire(
                key=lease_key,
                config=server_config,
                factory=lambda: self._start_running_server(
                    server_name=server_name,
                    server_config=server_config,
                    workspace_root=workspace_root,
                ),
            )
        except ValueError as exc:
            message = str(exc)
            self._mark_failed(server_name=server_name, error=message)
            self._record_event(
                LspRuntimeEvent(
                    event_type=(
                        "runtime.lsp_server_startup_rejected"
                        if "reuse rejected" in message
                        else "runtime.lsp_server_failed"
                    ),
                    payload={
                        "server": server_name,
                        "command": list(server_config.command),
                        "workspace_root": str(workspace_root),
                        "state": "failed" if "reuse rejected" not in message else "rejected",
                        "error": message,
                    },
                )
            )
            raise

        self._running_servers[server_name] = running_server
        self._leased_servers[server_name] = lease
        self._server_states[server_name] = LspServerState(
            name=server_name,
            status="running",
            available=True,
        )
        if outcome == "reused":
            self._record_event(
                LspRuntimeEvent(
                    event_type="runtime.lsp_server_reused",
                    payload={
                        "server": server_name,
                        "command": list(running_server.config.command),
                        "workspace_root": str(running_server.workspace_root),
                        "state": "running",
                    },
                )
            )
        return running_server

    def _start_running_server(
        self,
        *,
        server_name: str,
        server_config: ResolvedLspServerConfig,
        workspace_root: Path,
    ) -> _RunningLspServer:
        try:
            process = subprocess.Popen(
                list(server_config.command),
                cwd=workspace_root,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except (FileNotFoundError, OSError) as exc:
            message = self._startup_failure_message(
                server_name=server_name,
                server_config=server_config,
                details=str(exc),
            )
            logger.error(
                "%s (workspace=%s, command=%s)",
                message,
                workspace_root,
                list(server_config.command),
            )
            raise ValueError(message) from exc

        if process.stdin is None or process.stdout is None:
            process.kill()
            process.wait(timeout=1)
            message = self._startup_failure_message(
                server_name=server_name,
                server_config=server_config,
                details="missing stdio pipe",
            )
            logger.error(
                "%s (workspace=%s, command=%s)",
                message,
                workspace_root,
                list(server_config.command),
            )
            raise ValueError(message)

        running_server = _RunningLspServer(
            config=server_config,
            process=process,
            workspace_root=workspace_root,
        )
        self._running_servers[server_name] = running_server
        self._initialize_server(running_server, server_name=server_name)
        return running_server

    def _initialize_server(
        self,
        running_server: _RunningLspServer,
        *,
        server_name: str,
    ) -> None:
        init_params = lsp_types.InitializeParams(
            process_id=os.getpid(),
            client_info=lsp_types.ClientInfo(name="voidcode", version="0.1.0"),
            locale="zh-CN",
            root_uri=running_server.workspace_root.as_uri(),
            workspace_folders=[
                lsp_types.WorkspaceFolder(
                    uri=running_server.workspace_root.as_uri(),
                    name=running_server.workspace_root.name or str(running_server.workspace_root),
                )
            ],
            capabilities=lsp_types.ClientCapabilities(),
            initialization_options=(
                dict(running_server.config.init_options)
                if running_server.config.init_options
                else None
            ),
        )
        try:
            response = self._send_request(
                running_server,
                method="initialize",
                params=self._converter.unstructure(
                    init_params,
                    unstructure_as=lsp_types.InitializeParams,
                ),
                server_name=server_name,
            )
        except ValueError as exc:
            message = str(exc)
            self._mark_failed(server_name=server_name, error=message)
            self._record_event(
                self._failed_event(
                    server_name=server_name,
                    server_config=running_server.config,
                    workspace_root=running_server.workspace_root,
                    error=message,
                )
            )
            raise

        init_error = response.get("error")
        if init_error is not None:
            message = f"LSP initialize failed for {server_name}: {init_error}"
            self._mark_failed(server_name=server_name, error=message)
            self._record_event(
                self._failed_event(
                    server_name=server_name,
                    server_config=running_server.config,
                    workspace_root=running_server.workspace_root,
                    error=message,
                )
            )
            raise ValueError(message)

        self._send_notification(running_server.process, method="initialized", params={})
        if running_server.config.settings:
            self._send_notification(
                running_server.process,
                method="workspace/didChangeConfiguration",
                params={"settings": dict(running_server.config.settings)},
            )
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
                    "workspace_root": str(running_server.workspace_root),
                    "state": "running",
                },
            )
        )

    def _record_event(self, event: LspRuntimeEvent) -> None:
        self._pending_events.append(event)

    def _mark_failed(self, *, server_name: str, error: str) -> None:
        running_server = self._running_servers.pop(server_name, None)
        self._leased_servers.pop(server_name, None)
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
    def _startup_failure_message(
        *,
        server_name: str,
        server_config: ResolvedLspServerConfig,
        details: str,
    ) -> str:
        binary_name = server_config.command[0] if server_config.command else server_name
        config_path = f"lsp.servers.{server_name}.command"
        return (
            f"failed to start LSP server {server_name}: {details}. "
            f"Ensure '{binary_name}' is installed and available on PATH, "
            f"or override '{config_path}' in .voidcode.json with a custom command."
        )

    @staticmethod
    def _failed_event(
        *,
        server_name: str,
        server_config: ResolvedLspServerConfig,
        workspace_root: Path,
        error: str,
    ) -> LspRuntimeEvent:
        return LspRuntimeEvent(
            event_type="runtime.lsp_server_failed",
            payload={
                "server": server_name,
                "command": list(server_config.command),
                "workspace_root": str(workspace_root),
                "state": "failed",
                "error": error,
            },
        )

    def _stop_running_server(self, server_name: str, running_server: _RunningLspServer) -> None:
        process = running_server.process
        if process.poll() is None:
            should_terminate = not running_server.initialized
            try:
                if running_server.initialized:
                    self._send_request(
                        running_server,
                        method="shutdown",
                        params={},
                        server_name=server_name,
                    )
                    self._send_notification(process, method="exit", params={})
            except Exception as exc:
                should_terminate = True
                logger.warning("failed to shut down LSP server %s cleanly: %s", server_name, exc)
            if should_terminate and process.poll() is None:
                process.terminate()
            self._wait_for_process_exit(process)

    def _release_server_lease(
        self,
        server_name: str,
        *,
        record_event: bool,
        reason: Literal["shutdown", "workspace_switch"],
    ) -> None:
        running_server = self._running_servers.pop(server_name, None)
        lease = self._leased_servers.pop(server_name, None)
        if running_server is None or lease is None:
            return

        released_server = _WORKSPACE_SCOPED_LSP_REGISTRY.release(lease=lease)
        if released_server is not None:
            self._stop_running_server(server_name, released_server)

        self._server_states[server_name] = LspServerState(
            name=server_name,
            status="stopped",
            available=False,
        )
        if record_event and released_server is not None:
            self._record_event(
                LspRuntimeEvent(
                    event_type="runtime.lsp_server_stopped",
                    payload={
                        "server": server_name,
                        "command": list(running_server.config.command),
                        "workspace_root": str(running_server.workspace_root),
                        "state": "stopped",
                        "reason": reason,
                    },
                )
            )

    def _workspace_root_for_request(
        self,
        *,
        request: LspRequest,
        server_config: ResolvedLspServerConfig,
        request_path: Path | None,
    ) -> Path:
        workspace_root = request.workspace.resolve()
        if request_path is None:
            return workspace_root
        return discover_workspace_root(
            file_path=request_path,
            workspace_root=workspace_root,
            root_markers=server_config.root_markers,
        )

    @classmethod
    def _request_file_path(cls, request: LspRequest) -> Path | None:
        for uri in cls._candidate_uris(request.params):
            candidate = cls._path_from_file_uri(uri)
            if candidate is not None:
                return candidate.resolve()
        return None

    @staticmethod
    def _candidate_uris(params: dict[str, object]) -> tuple[str, ...]:
        uris: list[str] = []
        for container_key in ("textDocument", "item"):
            raw_container = params.get(container_key)
            if not isinstance(raw_container, dict):
                continue
            container = cast(dict[str, object], raw_container)
            uri = container.get("uri")
            if isinstance(uri, str):
                uris.append(uri)
        return tuple(uris)

    @staticmethod
    def _path_from_file_uri(uri: str) -> Path | None:
        parsed = urlparse(uri)
        if parsed.scheme != "file":
            return None
        raw_path = parsed.path
        if parsed.netloc and parsed.netloc != "localhost":
            raw_path = f"//{parsed.netloc}{parsed.path}"
        return Path(url2pathname(raw_path))

    @staticmethod
    def _wait_for_process_exit(process: subprocess.Popen[bytes], *, timeout: float = 1.0) -> None:
        if process.poll() is not None:
            return
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=timeout)

    def _send_request(
        self,
        running_server: _RunningLspServer,
        *,
        method: str,
        params: dict[str, object],
        server_name: str,
    ) -> dict[str, object]:
        process = running_server.process
        with running_server.request_lock:
            request_id = running_server.next_request_id
            running_server.next_request_id += 1
            self._write_message(
                process=process,
                message={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
            )
            response = self._read_message(process=process)
            while response is not None and response.get("id") != request_id:
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
            process=process,
            message={"jsonrpc": "2.0", "method": method, "params": params},
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

        fd = process.stdout.fileno()
        deadline = time.monotonic() + timeout

        def _wait_for_ready(error_message: str) -> None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(error_message)
            if os.name == "nt":
                if not ManagedLspManager._wait_for_windows_pipe(fd=fd, timeout=remaining):
                    raise TimeoutError(error_message)
                return
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                raise TimeoutError(error_message)

        header = b""
        while b"\r\n\r\n" not in header:
            _wait_for_ready(f"LSP server did not respond within {timeout}s")
            chunk = os.read(fd, 1)
            if not chunk:
                return None
            header += chunk
            if len(header) > MAX_LSP_HEADER_BYTES:
                raise LspMessageBoundsError(f"LSP header exceeded {MAX_LSP_HEADER_BYTES} bytes")

        header_text = header.decode("ascii", errors="ignore")
        content_length = None
        for line in header_text.split("\r\n"):
            if line.lower().startswith("content-length:"):
                raw_length = line.split(":", 1)[1].strip()
                try:
                    content_length = int(raw_length)
                except ValueError as exc:
                    raise LspProtocolError(
                        f"invalid Content-Length from LSP server: {raw_length}"
                    ) from exc
                break
        if content_length is None:
            raise LspProtocolError("missing Content-Length in LSP response header")
        if content_length < 0:
            raise LspProtocolError("invalid negative Content-Length from LSP server")
        if content_length > MAX_LSP_MESSAGE_BYTES:
            raise LspMessageBoundsError(
                f"LSP Content-Length {content_length} exceeds {MAX_LSP_MESSAGE_BYTES} bytes"
            )

        body = b""
        while len(body) < content_length:
            _wait_for_ready(f"LSP server did not send body within {timeout}s")
            chunk = os.read(fd, content_length - len(body))
            if not chunk:
                return None
            body += chunk
        try:
            decoded_body = body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise LspProtocolError("invalid UTF-8 payload from LSP server") from exc
        try:
            payload = json.loads(decoded_body)
        except json.JSONDecodeError as exc:
            raise LspProtocolError("invalid JSON payload from LSP server") from exc
        if not isinstance(payload, dict):
            raise LspProtocolError("expected JSON-RPC object payload from LSP server")
        return cast(dict[str, object], payload)

    @staticmethod
    def _wait_for_windows_pipe(*, fd: int, timeout: float) -> bool:
        from importlib import import_module

        ctypes = import_module("ctypes")
        msvcrt = import_module("msvcrt")
        get_osfhandle = vars(msvcrt)["get_osfhandle"]
        handle = get_osfhandle(fd)
        bytes_available = ctypes.c_ulong()
        windll = vars(ctypes)["windll"]
        get_last_error = vars(ctypes)["get_last_error"]
        deadline = time.monotonic() + timeout
        while True:
            success = windll.kernel32.PeekNamedPipe(
                ctypes.c_void_p(handle),
                None,
                0,
                None,
                ctypes.byref(bytes_available),
                None,
            )
            if success == 0:
                last_error = get_last_error()
                if last_error == 109:
                    return True
                raise OSError(last_error, "PeekNamedPipe failed")
            if bytes_available.value > 0:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)


def build_lsp_manager(config: RuntimeLspConfig | None) -> LspManager:
    configuration = LspConfigState.from_runtime_config(config)
    if configuration.configured_enabled is not True or not configuration.servers:
        return DisabledLspManager(config)
    return ManagedLspManager(config or RuntimeLspConfig())
