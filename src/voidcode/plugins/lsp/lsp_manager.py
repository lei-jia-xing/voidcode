"""LSP manager facade: disabled/managed selection and the request boundary.

Protocol framing lives in ``protocol.py``, server sharing in ``server_registry.py``,
document synchronization in ``documents.py``, and process lifecycle in
``server_lifecycle.py``. This module keeps the stable manager API and the
single-owner request routing that callers and tests address.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

from voidcode.capabilities.workspace_queries import PositionEncoding
from voidcode.sdk.provider import ProviderAbortSignal
from voidcode.sdk.services import PluginServices, ProcessServices

from . import documents as _documents
from . import owner as _owner
from . import protocol as _protocol
from . import server_lifecycle as _server_lifecycle
from .contracts import ResolvedLspServerConfig
from .lsp_types import LspDocumentState, RuntimeLspConfig
from .manager_state import LspConfigState, LspManagerState, LspServerState
from .registry import derive_workspace_lsp_defaults
from .server_registry import (
    LocalServerKey,
    LspRegistryKey,
    LspServerLease,
    OpenedLspDocument,
    RunningLspServer,
    SessionScopedLspRegistry,
)

# Stable names tests and callers import from the facade.
MAX_LSP_HEADER_BYTES = _protocol.MAX_LSP_HEADER_BYTES
MAX_LSP_MESSAGE_BYTES = _protocol.MAX_LSP_MESSAGE_BYTES
LspMessageBoundsError = _protocol.LspMessageBoundsError
LspProtocolError = _protocol.LspProtocolError
LspRuntimeError = _protocol.LspRuntimeError
_encode_lsp_message = _protocol.encode_lsp_message
_read_lsp_frame = _protocol.read_lsp_frame
_language_id_for_path = _documents.language_id_for_path
_LspOwner = _owner.LspOwner
_LspRegistryKey = LspRegistryKey
_LspServerLease = LspServerLease
_OpenedLspDocument = OpenedLspDocument
_RunningLspServer = RunningLspServer
_SessionScopedLspRegistry = SessionScopedLspRegistry

__all__ = [
    "DisabledLspManager",
    "LSP_REQUEST_TIMEOUT_SECONDS",
    "LspConfigState",
    "LspManager",
    "LspManagerState",
    "LspMessageBoundsError",
    "LspProtocolError",
    "LspRequest",
    "LspRequestResult",
    "LspRuntimeError",
    "LspServerState",
    "ManagedLspManager",
    "MAX_LSP_HEADER_BYTES",
    "MAX_LSP_MESSAGE_BYTES",
    "build_lsp_manager",
]

LSP_REQUEST_TIMEOUT_SECONDS: Final[float] = 30.0


@dataclass(frozen=True, slots=True)
class LspRequest:
    server_name: str | None
    method: str
    params: dict[str, object]
    workspace: Path
    session_id: str
    services: PluginServices
    abort_signal: ProviderAbortSignal | None = None


@dataclass(frozen=True, slots=True)
class LspRequestResult:
    response: dict[str, object]
    document_state: LspDocumentState | None = None
    position_encoding: PositionEncoding = "utf-16"


class LspManager(Protocol):
    @property
    def configuration(self) -> LspConfigState: ...

    def configuration_for(self, workspace: Path) -> LspConfigState: ...

    def current_state(self) -> LspManagerState: ...

    def request(self, request: LspRequest) -> LspRequestResult: ...

    def document_state(self, *, session_id: str, file_path: Path, workspace: Path) -> LspDocumentState | None: ...

    def shutdown(self) -> None: ...


class DisabledLspManager:
    def __init__(self, config: RuntimeLspConfig | None = None) -> None:
        self._configuration = LspConfigState.from_runtime_config(config)

    @property
    def configuration(self) -> LspConfigState:
        return self._configuration

    def configuration_for(self, workspace: Path) -> LspConfigState:
        _ = workspace
        return self._configuration

    def resolve(self, server_name: str) -> ResolvedLspServerConfig | None:
        return self._configuration.resolve(server_name)

    def current_state(self) -> LspManagerState:
        servers: dict[str, LspServerState] = {name: LspServerState(name=name) for name in self._configuration.servers}
        return LspManagerState(configuration=self._configuration, servers=servers)

    def request(self, request: LspRequest) -> LspRequestResult:
        _ = request
        raise ValueError("LSP runtime support is disabled")

    def document_state(self, *, session_id: str, file_path: Path, workspace: Path) -> LspDocumentState | None:
        _ = session_id, file_path, workspace
        return None

    def shutdown(self) -> None:
        return None


_SESSION_SCOPED_LSP_REGISTRY = _SessionScopedLspRegistry()


class ManagedLspManager:
    """Plugin-owned LSP servers; a handle whose work all runs on the LSP owner thread."""

    def __init__(self, config: RuntimeLspConfig) -> None:
        self._configuration = LspConfigState.from_runtime_config(config)
        self._lifecycle = _server_lifecycle.ServerLifecycle(_SESSION_SCOPED_LSP_REGISTRY, self._configuration.servers)
        self._server_states = self._lifecycle.server_states
        self._running_servers = self._lifecycle.running_servers
        self._leased_servers = self._lifecycle.leased_servers
        self._owner = _owner.LspOwner()
        self._shutdown_lock = threading.Lock()
        self._shutdown = False
        # An explicit plugin config owns the server set; without one the manager
        # derives workspace-safe defaults per workspace, like the runtime did.
        self._derive_servers = not config.servers
        self._derived_configurations: dict[Path, LspConfigState] = {}

    def configuration_for(self, workspace: Path) -> LspConfigState:
        """The server set one workspace resolves to (explicit config, else derived defaults)."""
        return self._owner.call(self._configuration_for_owned, workspace)

    def _configuration_for_owned(self, workspace: Path) -> LspConfigState:
        if not self._derive_servers:
            return self._configuration
        resolved_workspace = workspace.resolve()
        derived = self._derived_configurations.get(resolved_workspace)
        if derived is None:
            derived = LspConfigState.from_runtime_config(RuntimeLspConfig(enabled=True, servers=derive_workspace_lsp_defaults(resolved_workspace)))
            self._derived_configurations[resolved_workspace] = derived
            for server_name in derived.servers:
                self._lifecycle.register_server(server_name)
        return derived

    @property
    def configuration(self) -> LspConfigState:
        return self._configuration

    def current_state(self) -> LspManagerState:
        return self._owner.call(self._current_state_owned)

    def _current_state_owned(self) -> LspManagerState:
        return LspManagerState(
            mode="managed",
            configuration=self._configuration,
            servers=dict(self._server_states),
        )

    def request(self, request: LspRequest) -> LspRequestResult:
        return self._owner.call(self._request_owned, request)

    def _request_owned(self, request: LspRequest) -> LspRequestResult:
        configuration = self._configuration_for_owned(request.workspace)
        request_path = self._request_file_path(request)
        server_name = request.server_name or configuration.default_server_name(request_path)
        if server_name is None:
            raise ValueError("no LSP server is configured")

        server_config = configuration.resolve(server_name)
        if server_config is None:
            raise ValueError(f"unknown LSP server: {server_name}")

        workspace_root = self._workspace_root_for_request(
            request=request,
            server_config=server_config,
            request_path=request_path,
        )
        running_server, lease_key = self._ensure_running_owned(
            request.services,
            server_name=server_name,
            server_config=server_config,
            workspace_root=workspace_root,
            session_id=request.session_id,
            abort_signal=request.abort_signal,
        )
        document_path = self._text_document_path(request.params)
        try:
            document_state = self._synchronize_document_owned(request.services, running_server, document_path) if document_path is not None else None
            response = self._send_request_owned(
                request.services,
                running_server,
                method=request.method,
                params=request.params,
                server_name=server_name,
                abort_signal=request.abort_signal,
            )
        except (LspRuntimeError, TimeoutError) as failure:
            stopped = self._cancel_own_server_owned(request.services, lease_key, running_server, error=str(failure))
            if isinstance(failure, TimeoutError):
                stop_status = "the server was stopped" if stopped else "the server could not be confirmed stopped"
                raise ValueError(f"{failure}; {stop_status}") from failure
            raise
        return LspRequestResult(
            response=response,
            document_state=document_state,
            position_encoding=running_server.position_encoding,
        )

    def document_state(self, *, session_id: str, file_path: Path, workspace: Path) -> LspDocumentState | None:
        return self._owner.call(self._document_state_owned, session_id=session_id, file_path=file_path, workspace=workspace)

    def _document_state_owned(self, *, session_id: str, file_path: Path, workspace: Path) -> LspDocumentState | None:
        return _documents.document_state(self._running_servers, session_id=session_id, file_path=file_path, workspace=workspace)

    @staticmethod
    def _text_document_path(params: dict[str, object]) -> Path | None:
        return _documents.text_document_path(params)

    def _synchronize_document_owned(
        self,
        services: ProcessServices,
        running_server: _RunningLspServer,
        file_path: Path,
    ) -> LspDocumentState | None:
        return _documents.synchronize_document(services, running_server, file_path, notify=self._send_notification)

    def shutdown(self) -> None:
        with self._shutdown_lock:
            if self._shutdown:
                return
            self._shutdown = True
        try:
            self._owner.call(self._shutdown_owned)
        finally:
            self._owner.close()

    def _shutdown_owned(self) -> None:
        self._lifecycle.shutdown_owned()

    def _ensure_running(
        self,
        services: ProcessServices,
        *,
        server_name: str,
        server_config: ResolvedLspServerConfig,
        workspace_root: Path,
        session_id: str,
        abort_signal: ProviderAbortSignal | None,
    ) -> tuple[_RunningLspServer, _LspRegistryKey]:
        # One local entry owns exactly one registry lease, including concurrent
        # first use. Owner-thread serialization covers the local handoff.
        return self._owner.call(
            self._ensure_running_owned,
            services,
            server_name=server_name,
            server_config=server_config,
            workspace_root=workspace_root,
            session_id=session_id,
            abort_signal=abort_signal,
        )

    def _ensure_running_owned(
        self,
        services: ProcessServices,
        *,
        server_name: str,
        server_config: ResolvedLspServerConfig,
        workspace_root: Path,
        session_id: str,
        abort_signal: ProviderAbortSignal | None,
    ) -> tuple[_RunningLspServer, _LspRegistryKey]:
        return self._lifecycle.ensure_running_owned(
            services,
            server_name=server_name,
            server_config=server_config,
            workspace_root=workspace_root,
            session_id=session_id,
            abort_signal=abort_signal,
            start=self._start_running_server,
            answers=self._service_answers,
        )

    @staticmethod
    def _service_answers(services: ProcessServices, running_server: _RunningLspServer) -> bool:
        return _server_lifecycle.ServerLifecycle.service_answers(services, running_server)

    def _stop_abandoned_server(
        self,
        services: ProcessServices,
        local_key: LocalServerKey,
        lease_key: _LspRegistryKey,
        running_server: _RunningLspServer,
    ) -> bool:
        return self._lifecycle.stop_abandoned_server(services, local_key, lease_key, running_server)

    def _forget_dead_entry(self, local_key: LocalServerKey, lease_key: _LspRegistryKey, process_id: str) -> None:
        self._lifecycle.forget_dead_entry(local_key, lease_key, process_id)

    def _cancel_own_server(self, services: ProcessServices, lease_key: _LspRegistryKey, running_server: _RunningLspServer, *, error: str) -> bool:
        return self._owner.call(self._cancel_own_server_owned, services, lease_key, running_server, error=error)

    def _cancel_own_server_owned(
        self, services: ProcessServices, lease_key: _LspRegistryKey, running_server: _RunningLspServer, *, error: str
    ) -> bool:
        return self._lifecycle.cancel_own_server_owned(services, lease_key, running_server, error=error)

    def _start_running_server(
        self,
        services: ProcessServices,
        *,
        process_id: str,
        server_name: str,
        server_config: ResolvedLspServerConfig,
        workspace_root: Path,
        abort_signal: ProviderAbortSignal | None,
    ) -> _RunningLspServer:
        return self._lifecycle.start_running_server(
            services,
            process_id=process_id,
            server_name=server_name,
            server_config=server_config,
            workspace_root=workspace_root,
            abort_signal=abort_signal,
            initialize=self._initialize_server,
        )

    def _initialize_server(
        self,
        services: ProcessServices,
        running_server: _RunningLspServer,
        *,
        server_name: str,
        abort_signal: ProviderAbortSignal | None,
    ) -> None:
        self._lifecycle.initialize_server(
            services,
            running_server,
            server_name=server_name,
            abort_signal=abort_signal,
            send_request=self._send_request_owned,
            send_notification=self._send_notification,
        )

    def _mark_failed(self, *, server_name: str, error: str) -> None:
        self._lifecycle.mark_failed(server_name=server_name, error=error)

    @staticmethod
    def _startup_failure_message(
        *,
        server_name: str,
        server_config: ResolvedLspServerConfig,
        details: str,
    ) -> str:
        return _server_lifecycle.ServerLifecycle.startup_failure_message(server_name=server_name, server_config=server_config, details=details)

    def _workspace_root_for_request(
        self,
        *,
        request: LspRequest,
        server_config: ResolvedLspServerConfig,
        request_path: Path | None,
    ) -> Path:
        return _documents.workspace_root_for_request(workspace=request.workspace, request_path=request_path, server_config=server_config)

    @classmethod
    def _request_file_path(cls, request: LspRequest) -> Path | None:
        _ = cls
        return _documents.request_file_path(request.params)

    @staticmethod
    def _candidate_uris(params: dict[str, object]) -> tuple[str, ...]:
        return _documents.candidate_uris(params)

    @staticmethod
    def _path_from_file_uri(uri: str) -> Path | None:
        return _documents.path_from_file_uri(uri)

    def _send_request_owned(
        self,
        services: ProcessServices,
        running_server: _RunningLspServer,
        *,
        method: str,
        params: dict[str, object],
        server_name: str,
        abort_signal: ProviderAbortSignal | None,
    ) -> dict[str, object]:
        request_id = running_server.next_request_id
        running_server.next_request_id += 1
        self._write_message(
            services,
            running_server,
            message={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
        )
        deadline = time.monotonic() + LSP_REQUEST_TIMEOUT_SECONDS
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"LSP server {server_name} did not respond within {LSP_REQUEST_TIMEOUT_SECONDS}s")
            response = self._read_message(services, running_server, server_name=server_name, abort_signal=abort_signal, timeout=remaining)
            if response is None:
                raise LspProtocolError(f"No response from LSP server {server_name} for {method}")
            if "method" in response:
                if not isinstance(response["method"], str):
                    raise LspProtocolError("LSP method must be a string")
                if "id" in response:
                    if type(response["id"]) not in (str, int):
                        raise LspProtocolError("LSP request id must be an integer or string")
                    # No server-request capabilities are advertised. Reject unsupported
                    # requests explicitly; notifications must never receive a response.
                    self._write_message(
                        services,
                        running_server,
                        message={"jsonrpc": "2.0", "id": response["id"], "error": {"code": -32601, "message": "Method not found"}},
                    )
                continue
            if type(response.get("id")) is int and response["id"] == request_id:
                if ("result" in response) == ("error" in response):
                    raise LspProtocolError("LSP response requires exactly one of result or error")
                return response

    def _send_notification(
        self,
        services: ProcessServices,
        running_server: _RunningLspServer,
        *,
        method: str,
        params: dict[str, object],
        abort_signal: ProviderAbortSignal | None,
    ) -> None:
        _ = abort_signal
        self._write_message(
            services,
            running_server,
            message={"jsonrpc": "2.0", "method": method, "params": params},
        )

    @staticmethod
    def _write_message(services: ProcessServices, running_server: _RunningLspServer, *, message: dict[str, object]) -> None:
        payload = _encode_lsp_message(message)
        try:
            written = services.write_process(running_server.process_id, payload)
        except ValueError as exc:
            raise LspProtocolError(f"LSP server {running_server.process_id} is not reading its stdin: {exc}") from exc
        if written < len(payload):
            raise LspProtocolError(
                f"LSP server {running_server.process_id} accepted only {written} of {len(payload)} stdin bytes; the JSON-RPC frame is truncated"
            )

    @staticmethod
    def _read_message(
        services: ProcessServices,
        running_server: _RunningLspServer,
        *,
        server_name: str,
        abort_signal: ProviderAbortSignal | None,
        timeout: float = 30.0,
    ) -> dict[str, object] | None:
        return _read_lsp_frame(
            services,
            running_server,
            server_name=server_name,
            abort_signal=abort_signal,
            timeout=timeout,
        )


def build_lsp_manager(config: RuntimeLspConfig | None) -> LspManager:
    configuration = LspConfigState.from_runtime_config(config)
    if configuration.configured_enabled is not True:
        return DisabledLspManager(config)
    # No configured servers is not a disable: the managed manager derives
    # workspace-safe defaults per workspace on first request and stays a no-op
    # (no process spawned) for a workspace that resolves to none.
    return ManagedLspManager(config or RuntimeLspConfig())
