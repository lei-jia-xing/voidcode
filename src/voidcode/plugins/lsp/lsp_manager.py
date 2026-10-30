"""Plugin-owned LSP connection manager.

Holds session-scoped language servers over the host's supervised service
processes, plus document state for the ``lsp`` tool. The tool talks to this
manager only through the small requester boundary in ``lsp_types``; no runtime
module is imported. Every invocation passes its live lease services and abort
signal down to the transport; nothing here retains them across invocations.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from lsprotocol import converters as lsp_converters
from lsprotocol import types as lsp_types

from voidcode.config import CONFIG_FILE_NAME
from voidcode.sdk.provider import ProviderAbortSignal
from voidcode.sdk.services import PluginServices

from .contracts import ResolvedLspServerConfig
from .lsp_types import LspDocumentState, LspRequestError, RuntimeLspConfig
from .registry import (
    derive_workspace_lsp_defaults,
    match_lsp_servers_for_path,
    resolve_lsp_server_configs,
)
from .roots import discover_workspace_root

logger = logging.getLogger(__name__)

MAX_LSP_HEADER_BYTES = 8192
MAX_LSP_MESSAGE_BYTES = 1024 * 1024


class LspRuntimeError(LspRequestError):
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
        resolved_servers = resolve_lsp_server_configs(config.servers)
        return cls(
            configured_enabled=config.enabled,
            servers=resolved_servers,
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
    status: Literal["stopped", "starting", "running", "failed"] = "stopped"
    last_error: str | None = None


@dataclass(frozen=True, slots=True)
class LspManagerState:
    mode: Literal["disabled", "managed"] = "disabled"
    configuration: LspConfigState = field(default_factory=LspConfigState)
    servers: dict[str, LspServerState] = field(default_factory=dict)


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
    position_encoding: str = "utf-16"


@dataclass(frozen=True, slots=True)
class _OpenedLspDocument:
    version: int
    content_hash: str


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


@dataclass(slots=True)
class _RunningLspServer:
    config: ResolvedLspServerConfig
    process_id: str
    workspace_root: Path
    stdout_cursor: int = 0
    initialized: bool = False
    next_request_id: int = 1
    request_lock: threading.Lock = field(default_factory=threading.Lock)
    position_encoding: str = "utf-16"
    document_sync_enabled: bool = False
    documents: dict[str, _OpenedLspDocument] = field(default_factory=dict)


@dataclass(slots=True)
class _SharedLspServerEntry:
    server: _RunningLspServer
    generation: int
    ref_count: int = 0


@dataclass(frozen=True, slots=True)
class _LspRegistryKey:
    session_id: str
    server_name: str
    workspace_root: Path


@dataclass(frozen=True, slots=True)
class _LspServerLease:
    key: _LspRegistryKey
    generation: int


class _SessionScopedLspRegistry:
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
        start: Callable[[str], _RunningLspServer],
    ) -> tuple[_RunningLspServer, _LspServerLease, Literal["started", "reused"]]:
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                if entry.server.config != config:
                    raise ValueError(
                        "session-scoped LSP reuse rejected because the existing "
                        f"{key.server_name} server for {key.workspace_root} was started with "
                        "a different resolved configuration"
                    )
                entry.ref_count += 1
                return (
                    entry.server,
                    _LspServerLease(key=key, generation=entry.generation),
                    "reused",
                )

            server = start(f"lsp-{uuid.uuid4().hex[:12]}")
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

    def invalidate(self, *, key: _LspRegistryKey, process_id: str) -> _RunningLspServer | None:
        """Drop one entry only when it still holds this process id; never touches the host."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None or entry.server.process_id != process_id:
                return None
            self._entries.pop(key, None)
            return entry.server


_SESSION_SCOPED_LSP_REGISTRY = _SessionScopedLspRegistry()


class ManagedLspManager:
    def __init__(self, config: RuntimeLspConfig) -> None:
        self._configuration = LspConfigState.from_runtime_config(config)
        self._server_states: dict[str, LspServerState] = {name: LspServerState(name=name) for name in self._configuration.servers}
        self._lifecycle_lock = threading.RLock()
        self._running_servers: dict[tuple[str, str], _RunningLspServer] = {}
        self._leased_servers: dict[tuple[str, str], _LspServerLease] = {}
        self._converter = lsp_converters.get_converter()
        # An explicit plugin config owns the server set; without one the manager
        # derives workspace-safe defaults per workspace, like the runtime did.
        self._derive_servers = not config.servers
        self._derived_configurations: dict[Path, LspConfigState] = {}

    def configuration_for(self, workspace: Path) -> LspConfigState:
        """The server set one workspace resolves to (explicit config, else derived defaults)."""
        return self._configuration_for(workspace)

    def _configuration_for(self, workspace: Path) -> LspConfigState:
        if not self._derive_servers:
            return self._configuration
        resolved_workspace = workspace.resolve()
        derived = self._derived_configurations.get(resolved_workspace)
        if derived is None:
            derived = LspConfigState.from_runtime_config(RuntimeLspConfig(enabled=True, servers=derive_workspace_lsp_defaults(resolved_workspace)))
            self._derived_configurations[resolved_workspace] = derived
            for server_name in derived.servers:
                self._server_states.setdefault(server_name, LspServerState(name=server_name))
        return derived

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
        configuration = self._configuration_for(request.workspace)
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
        running_server, lease_key = self._ensure_running(
            request.services,
            server_name=server_name,
            server_config=server_config,
            workspace_root=workspace_root,
            session_id=request.session_id,
            abort_signal=request.abort_signal,
        )
        document_path = self._text_document_path(request.params)
        with running_server.request_lock:
            try:
                document_state = (
                    self._synchronize_document_locked(request.services, running_server, document_path) if document_path is not None else None
                )
                response = self._send_request_locked(
                    request.services,
                    running_server,
                    method=request.method,
                    params=request.params,
                    server_name=server_name,
                    abort_signal=request.abort_signal,
                )
            except (LspRuntimeError, TimeoutError) as exc:
                failed = (request.services, lease_key, running_server, exc)
            else:
                return LspRequestResult(
                    response=response,
                    document_state=document_state,
                    position_encoding=running_server.position_encoding,
                )
        failed_services, failed_key, failed_server, failure = failed
        stopped = self._cancel_own_server(failed_services, failed_key, failed_server, error=str(failure))
        if isinstance(failure, TimeoutError):
            stop_status = "the server was stopped" if stopped else "the server could not be confirmed stopped"
            raise ValueError(f"{failure}; {stop_status}") from failure
        raise failure

    def document_state(self, *, session_id: str, file_path: Path, workspace: Path) -> LspDocumentState | None:
        path = file_path.resolve()
        uri = path.as_uri()
        expected_workspace = workspace.resolve()
        matches: list[LspDocumentState] = []
        for key, running_server in tuple(self._running_servers.items()):
            if key[0] != session_id:
                continue
            with running_server.request_lock:
                if not running_server.config.matches_path(path):
                    continue
                resolved_root = discover_workspace_root(
                    file_path=path,
                    workspace_root=expected_workspace,
                    root_markers=running_server.config.root_markers,
                )
                if running_server.workspace_root != resolved_root:
                    continue
                document = running_server.documents.get(uri)
                if document is None:
                    continue
                try:
                    actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
                except OSError:
                    continue
                if actual_hash != document.content_hash:
                    continue
                matches.append(
                    LspDocumentState(
                        version=document.version,
                        content_hash=document.content_hash,
                        position_encoding=running_server.position_encoding,
                    )
                )
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _text_document_path(params: dict[str, object]) -> Path | None:
        document = params.get("textDocument")
        if not isinstance(document, dict) or not isinstance(document.get("uri"), str):
            return None
        return ManagedLspManager._path_from_file_uri(document["uri"])

    def _synchronize_document_locked(
        self,
        services: PluginServices,
        running_server: _RunningLspServer,
        file_path: Path,
    ) -> LspDocumentState | None:
        if not running_server.document_sync_enabled:
            return None
        path = file_path.resolve()
        if not path.is_relative_to(running_server.workspace_root) or not running_server.config.matches_path(path):
            raise ValueError(f"LSP document is outside the configured server workspace: {path}")
        try:
            content_bytes = path.read_bytes()
            content = content_bytes.decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ValueError(f"LSP document is unavailable as UTF-8 text: {path}") from exc

        content_hash = hashlib.sha256(content_bytes).hexdigest()
        uri = path.as_uri()
        opened = running_server.documents.get(uri)
        if opened is not None and opened.content_hash == content_hash:
            return LspDocumentState(
                version=opened.version,
                content_hash=opened.content_hash,
                position_encoding=running_server.position_encoding,
            )

        version = opened.version + 1 if opened is not None else 1
        if opened is None:
            language_id = running_server.config.languages[0] if running_server.config.languages else path.suffix.removeprefix(".") or "plaintext"
            self._send_notification(
                services,
                running_server,
                method="textDocument/didOpen",
                params={
                    "textDocument": {
                        "uri": uri,
                        "languageId": language_id,
                        "version": version,
                        "text": content,
                    }
                },
                abort_signal=None,
            )
        else:
            self._send_notification(
                services,
                running_server,
                method="textDocument/didChange",
                params={
                    "textDocument": {"uri": uri, "version": version},
                    "contentChanges": [{"text": content}],
                },
                abort_signal=None,
            )
        running_server.documents[uri] = _OpenedLspDocument(version, content_hash)
        return LspDocumentState(
            version=version,
            content_hash=content_hash,
            position_encoding=running_server.position_encoding,
        )

    def shutdown(self) -> None:
        with self._lifecycle_lock:
            # Host retirement already stopped every service process in this generation;
            # shutdown here only forgets local entries, never touches the host.
            for key in tuple(self._running_servers):
                self._running_servers.pop(key, None)
                lease = self._leased_servers.pop(key, None)
                if lease is not None:
                    _SESSION_SCOPED_LSP_REGISTRY.release(lease=lease)

    def _ensure_running(
        self,
        services: PluginServices,
        *,
        server_name: str,
        server_config: ResolvedLspServerConfig,
        workspace_root: Path,
        session_id: str,
        abort_signal: ProviderAbortSignal | None,
    ) -> tuple[_RunningLspServer, _LspRegistryKey]:
        # One local entry owns exactly one registry lease, including concurrent
        # first use. The registry lock alone cannot protect our local handoff.
        with self._lifecycle_lock:
            return self._ensure_running_locked(
                services,
                server_name=server_name,
                server_config=server_config,
                workspace_root=workspace_root,
                session_id=session_id,
                abort_signal=abort_signal,
            )

    def _ensure_running_locked(
        self,
        services: PluginServices,
        *,
        server_name: str,
        server_config: ResolvedLspServerConfig,
        workspace_root: Path,
        session_id: str,
        abort_signal: ProviderAbortSignal | None,
    ) -> tuple[_RunningLspServer, _LspRegistryKey]:
        lease_key = _LspRegistryKey(session_id=session_id, server_name=server_name, workspace_root=workspace_root)
        local_key = (session_id, server_name)
        running_server = self._running_servers.get(local_key)
        if running_server is not None and running_server.workspace_root == workspace_root:
            if self._service_answers(services, running_server):
                return running_server, lease_key
            self._forget_dead_entry(local_key, lease_key, running_server.process_id)
        elif running_server is not None:
            self._stop_abandoned_server(services, local_key, lease_key, running_server)

        self._server_states[server_name] = LspServerState(
            name=server_name,
            status="starting",
        )
        try:
            running_server, lease, outcome = _SESSION_SCOPED_LSP_REGISTRY.acquire(
                key=lease_key,
                config=server_config,
                start=lambda process_id: self._start_running_server(
                    services,
                    process_id=process_id,
                    server_name=server_name,
                    server_config=server_config,
                    workspace_root=workspace_root,
                    abort_signal=abort_signal,
                ),
            )
        except ValueError as exc:
            message = str(exc)
            self._mark_failed(server_name=server_name, error=message)
            raise

        self._running_servers[local_key] = running_server
        self._leased_servers[local_key] = lease
        if outcome == "reused" and not self._service_answers(services, running_server):
            self._forget_dead_entry(local_key, lease_key, running_server.process_id)
            return self._ensure_running(
                services,
                server_name=server_name,
                server_config=server_config,
                workspace_root=workspace_root,
                session_id=session_id,
                abort_signal=abort_signal,
            )
        self._server_states[server_name] = LspServerState(
            name=server_name,
            status="running",
        )
        return running_server, lease_key

    @staticmethod
    def _service_answers(services: PluginServices, running_server: _RunningLspServer) -> bool:
        try:
            services.read_process_output(running_server.process_id, stream="stdout", offset=running_server.stdout_cursor, limit=1)
        except ValueError:
            return False
        return True

    def _stop_abandoned_server(
        self,
        services: PluginServices,
        local_key: tuple[str, str],
        lease_key: _LspRegistryKey,
        running_server: _RunningLspServer,
    ) -> None:
        """Stop a workspace-switched server's tree instead of orphaning it behind a new entry."""
        process_id = running_server.process_id
        old_key = _LspRegistryKey(session_id=lease_key.session_id, server_name=lease_key.server_name, workspace_root=running_server.workspace_root)
        try:
            services.cancel_process(process_id)
        except ValueError:
            pass
        with running_server.request_lock:
            running_server.documents.clear()
            running_server.initialized = False
            running_server.stdout_cursor = 0
        self._running_servers.pop(local_key, None)
        lease = self._leased_servers.pop(local_key, None)
        _SESSION_SCOPED_LSP_REGISTRY.invalidate(key=old_key, process_id=process_id)
        if lease is not None:
            _SESSION_SCOPED_LSP_REGISTRY.release(lease=lease)

    def _forget_dead_entry(self, local_key: tuple[str, str], lease_key: _LspRegistryKey, process_id: str) -> None:
        """Drop a local entry whose service id the host no longer supervises for this session."""
        current = self._running_servers.get(local_key)
        if current is None or current.process_id != process_id:
            return
        self._running_servers.pop(local_key, None)
        lease = self._leased_servers.pop(local_key, None)
        _SESSION_SCOPED_LSP_REGISTRY.invalidate(key=lease_key, process_id=process_id)
        if lease is not None:
            _SESSION_SCOPED_LSP_REGISTRY.release(lease=lease)

    def _cancel_own_server(self, services: PluginServices, lease_key: _LspRegistryKey, running_server: _RunningLspServer, *, error: str) -> bool:
        with self._lifecycle_lock:
            return self._cancel_own_server_locked(services, lease_key, running_server, error=error)

    def _cancel_own_server_locked(
        self, services: PluginServices, lease_key: _LspRegistryKey, running_server: _RunningLspServer, *, error: str
    ) -> bool:
        """Stop a failing/cancelled server's tree and forget it; races surface as the next error."""
        process_id = running_server.process_id
        try:
            stopped = services.cancel_process(process_id)
        except ValueError:
            stopped = False
        with running_server.request_lock:
            running_server.documents.clear()
            running_server.initialized = False
            running_server.stdout_cursor = 0
        local_key = (lease_key.session_id, lease_key.server_name)
        if self._running_servers.get(local_key) is not running_server:
            return stopped
        self._forget_dead_entry(local_key, lease_key, process_id)
        self._server_states[lease_key.server_name] = LspServerState(
            name=lease_key.server_name,
            status="failed",
            last_error=error if stopped else f"{error}; LSP server {lease_key.server_name} could not be confirmed stopped",
        )
        return stopped

    def _start_running_server(
        self,
        services: PluginServices,
        *,
        process_id: str,
        server_name: str,
        server_config: ResolvedLspServerConfig,
        workspace_root: Path,
        abort_signal: ProviderAbortSignal | None,
    ) -> _RunningLspServer:
        try:
            services.spawn_service_process(
                list(server_config.command),
                cwd=workspace_root,
                process_id=process_id,
            )
        except (ValueError, PermissionError, OSError) as exc:
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

        running_server = _RunningLspServer(
            config=server_config,
            process_id=process_id,
            workspace_root=workspace_root,
        )
        try:
            self._initialize_server(services, running_server, server_name=server_name, abort_signal=abort_signal)
        except Exception:
            try:
                services.cancel_process(process_id)
            except ValueError:
                pass
            raise
        return running_server

    def _initialize_server(
        self,
        services: PluginServices,
        running_server: _RunningLspServer,
        *,
        server_name: str,
        abort_signal: ProviderAbortSignal | None,
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
            capabilities=lsp_types.ClientCapabilities(
                general=lsp_types.GeneralClientCapabilities(position_encodings=["utf-16", "utf-8", "utf-32"]),
            ),
            initialization_options=(dict(running_server.config.init_options) if running_server.config.init_options else None),
        )
        try:
            response = self._send_request(
                services,
                running_server,
                method="initialize",
                params=self._converter.unstructure(
                    init_params,
                    unstructure_as=lsp_types.InitializeParams,
                ),
                server_name=server_name,
                abort_signal=abort_signal,
            )
        except (ValueError, TimeoutError) as exc:
            message = f"LSP initialize failed for {server_name}: {exc}"
            self._mark_failed(server_name=server_name, error=message)
            raise

        init_error = response.get("error")
        if init_error is not None:
            message = f"LSP initialize failed for {server_name}: {init_error}"
            self._mark_failed(server_name=server_name, error=message)
            raise ValueError(message)
        init_result = response.get("result")
        server_capabilities = init_result.get("capabilities") if isinstance(init_result, dict) else None
        if not isinstance(server_capabilities, dict):
            raise LspProtocolError(f"LSP initialize response from {server_name} requires a capabilities object")
        position_encoding = server_capabilities.get("positionEncoding")
        if position_encoding is None:
            position_encoding = "utf-16"
        if not isinstance(position_encoding, str) or position_encoding not in ("utf-8", "utf-16", "utf-32"):
            raise ValueError(f"LSP server {server_name} selected unsupported position encoding: {position_encoding}")
        running_server.position_encoding = position_encoding

        text_sync = server_capabilities.get("textDocumentSync")
        if isinstance(text_sync, dict):
            change = text_sync.get("change")
            running_server.document_sync_enabled = (
                text_sync.get("openClose") is True and isinstance(change, int) and not isinstance(change, bool) and change in (1, 2)
            )
        else:
            running_server.document_sync_enabled = isinstance(text_sync, int) and not isinstance(text_sync, bool) and text_sync in (1, 2)
        self._send_notification(services, running_server, method="initialized", params={}, abort_signal=abort_signal)

        if running_server.config.settings:
            self._send_notification(
                services,
                running_server,
                method="workspace/didChangeConfiguration",
                params={"settings": dict(running_server.config.settings)},
                abort_signal=abort_signal,
            )
        running_server.initialized = True
        self._server_states[server_name] = LspServerState(
            name=server_name,
            status="running",
        )

    def _mark_failed(self, *, server_name: str, error: str) -> None:
        self._server_states[server_name] = LspServerState(
            name=server_name,
            status="failed",
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
        config_path = f"plugins.lsp.config.servers.{server_name}.command"
        return (
            f"failed to start LSP server {server_name}: {details}. "
            f"Ensure '{binary_name}' is installed and available on PATH, "
            f"or override '{config_path}' in {CONFIG_FILE_NAME} with a custom command."
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
            uri = raw_container.get("uri")
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
            # Python 3.14 rejects remote file authorities on POSIX. Preserve the
            # UNC-style path expected by LSP clients while still decoding escapes.
            if os.name != "nt":
                return Path(unquote(raw_path))
        return Path(url2pathname(raw_path))

    def _send_request(
        self,
        services: PluginServices,
        running_server: _RunningLspServer,
        *,
        method: str,
        params: dict[str, object],
        server_name: str,
        abort_signal: ProviderAbortSignal | None,
    ) -> dict[str, object]:
        with running_server.request_lock:
            return self._send_request_locked(
                services,
                running_server,
                method=method,
                params=params,
                server_name=server_name,
                abort_signal=abort_signal,
            )

    def _send_request_locked(
        self,
        services: PluginServices,
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
        while True:
            response = self._read_message(services, running_server, server_name=server_name, abort_signal=abort_signal)
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
        services: PluginServices,
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
    def _write_message(services: PluginServices, running_server: _RunningLspServer, *, message: dict[str, object]) -> None:
        body = json.dumps(message).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        try:
            services.write_process(running_server.process_id, header + body)
        except ValueError as exc:
            raise LspProtocolError(f"LSP server {running_server.process_id} is not reading its stdin: {exc}") from exc

    @staticmethod
    def _read_message(
        services: PluginServices,
        running_server: _RunningLspServer,
        *,
        server_name: str,
        abort_signal: ProviderAbortSignal | None,
        timeout: float = 30.0,
    ) -> dict[str, object] | None:
        process_id = running_server.process_id
        deadline = time.monotonic() + timeout
        buffer = bytearray()

        def _page(needed: int) -> bool:
            if abort_signal is not None and abort_signal.cancelled:
                raise TimeoutError(f"LSP request to {server_name} was cancelled")
            if time.monotonic() >= deadline:
                raise TimeoutError(f"LSP server did not respond within {timeout}s")
            page = services.read_process_output(
                process_id,
                stream="stdout",
                offset=running_server.stdout_cursor + len(buffer),
                limit=max(1, min(65536, needed)),
            )
            if page.data:
                buffer.extend(page.data)
                return True
            if not page.running:
                return False
            time.sleep(0.01)
            return True

        while bytes(buffer).find(b"\r\n\r\n") < 0:
            if len(buffer) > MAX_LSP_HEADER_BYTES:
                raise LspMessageBoundsError(f"LSP header exceeded {MAX_LSP_HEADER_BYTES} bytes")
            if not _page(MAX_LSP_HEADER_BYTES + 4 - len(buffer)):
                return None

        header_end = bytes(buffer).find(b"\r\n\r\n")
        header_bytes = bytes(buffer[:header_end])
        if len(header_bytes) > MAX_LSP_HEADER_BYTES:
            raise LspMessageBoundsError(f"LSP header exceeded {MAX_LSP_HEADER_BYTES} bytes")
        try:
            header_text = header_bytes.decode("ascii")
        except UnicodeDecodeError as exc:
            raise LspProtocolError("invalid ASCII header from LSP server") from exc
        content_length = None
        for line in header_text.split("\r\n"):
            if line.lower().startswith("content-length:"):
                raw_length = line.split(":", 1)[1].strip()
                try:
                    content_length = int(raw_length)
                except ValueError as exc:
                    raise LspProtocolError(f"invalid Content-Length from LSP server: {raw_length}") from exc
                break
        if content_length is None:
            raise LspProtocolError("missing Content-Length in LSP response header")
        if content_length < 0:
            raise LspProtocolError("invalid negative Content-Length from LSP server")
        if content_length > MAX_LSP_MESSAGE_BYTES:
            raise LspMessageBoundsError(f"LSP Content-Length {content_length} exceeds {MAX_LSP_MESSAGE_BYTES} bytes")

        body_start = header_end + 4
        while len(buffer) - body_start < content_length:
            if len(buffer) - body_start > MAX_LSP_MESSAGE_BYTES:
                raise LspMessageBoundsError(f"LSP Content-Length {content_length} exceeds {MAX_LSP_MESSAGE_BYTES} bytes")
            if not _page(content_length - (len(buffer) - body_start)):
                return None

        running_server.stdout_cursor += body_start + content_length
        body = bytes(buffer[body_start : body_start + content_length])
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
        return payload


def build_lsp_manager(config: RuntimeLspConfig | None) -> LspManager:
    configuration = LspConfigState.from_runtime_config(config)
    if configuration.configured_enabled is not True:
        return DisabledLspManager(config)
    # No configured servers is not a disable: the managed manager derives
    # workspace-safe defaults per workspace on first request and stays a no-op
    # (no process spawned) for a workspace that resolves to none.
    return ManagedLspManager(config or RuntimeLspConfig())
