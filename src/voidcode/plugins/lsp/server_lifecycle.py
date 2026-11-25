"""Language-server acquisition, initialization, failure handling, and retirement."""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Protocol

from lsprotocol import converters as lsp_converters
from lsprotocol import types as lsp_types

from voidcode.capabilities.workspace_queries import PositionEncoding, is_position_encoding
from voidcode.sdk.provider import ProviderAbortSignal
from voidcode.sdk.services import ProcessServices

from .contracts import ResolvedLspServerConfig
from .manager_state import LspServerState
from .protocol import LspProtocolError
from .server_registry import (
    LocalServerKey,
    LspRegistryKey,
    LspServerLease,
    RunningLspServer,
    SessionScopedLspRegistry,
)

logger = logging.getLogger(__name__)


class StartServer(Protocol):
    def __call__(
        self,
        services: ProcessServices,
        *,
        process_id: str,
        server_name: str,
        server_config: ResolvedLspServerConfig,
        workspace_root: Path,
        abort_signal: ProviderAbortSignal | None,
    ) -> RunningLspServer: ...


class InitializeServer(Protocol):
    def __call__(
        self,
        services: ProcessServices,
        running_server: RunningLspServer,
        *,
        server_name: str,
        abort_signal: ProviderAbortSignal | None,
    ) -> None: ...


class ServiceAnswers(Protocol):
    def __call__(self, services: ProcessServices, running_server: RunningLspServer) -> bool: ...


class SendRequest(Protocol):
    def __call__(
        self,
        services: ProcessServices,
        running_server: RunningLspServer,
        *,
        method: str,
        params: dict[str, object],
        server_name: str,
        abort_signal: ProviderAbortSignal | None,
    ) -> dict[str, object]: ...


class SendNotification(Protocol):
    def __call__(
        self,
        services: ProcessServices,
        running_server: RunningLspServer,
        *,
        method: str,
        params: dict[str, object],
        abort_signal: ProviderAbortSignal | None,
    ) -> None: ...


class ServerLifecycle:
    """Owns local server/lease maps and the process lifecycle around them."""

    def __init__(self, registry: SessionScopedLspRegistry, server_names: Iterable[str]) -> None:
        self._registry = registry
        self.server_states: dict[str, LspServerState] = {name: LspServerState(name=name) for name in server_names}
        self.running_servers: dict[LocalServerKey, RunningLspServer] = {}
        self.leased_servers: dict[LocalServerKey, LspServerLease] = {}
        self._converter = lsp_converters.get_converter()

    def register_server(self, server_name: str) -> None:
        self.server_states.setdefault(server_name, LspServerState(name=server_name))

    def ensure_running_owned(
        self,
        services: ProcessServices,
        *,
        server_name: str,
        server_config: ResolvedLspServerConfig,
        workspace_root: Path,
        session_id: str,
        abort_signal: ProviderAbortSignal | None,
        start: StartServer,
        answers: ServiceAnswers,
    ) -> tuple[RunningLspServer, LspRegistryKey]:
        lease_key = LspRegistryKey(session_id=session_id, server_name=server_name, workspace_root=workspace_root)
        local_key = (session_id, server_name, workspace_root)
        running_server = self.running_servers.get(local_key)
        if running_server is not None:
            if running_server.workspace_root != workspace_root:
                if not self.stop_abandoned_server(services, local_key, lease_key, running_server):
                    raise ValueError(
                        f"LSP server {server_name} for {running_server.workspace_root} could not be confirmed stopped; not starting another"
                    )
            elif answers(services, running_server):
                return running_server, lease_key
            else:
                self.forget_dead_entry(local_key, lease_key, running_server.process_id)

        self.server_states[server_name] = LspServerState(
            name=server_name,
            status="starting",
        )
        try:
            running_server, lease, outcome = self._registry.acquire(
                key=lease_key,
                config=server_config,
                start=lambda process_id: start(
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
            self.mark_failed(server_name=server_name, error=message)
            raise

        self.running_servers[local_key] = running_server
        self.leased_servers[local_key] = lease
        if outcome == "reused" and not answers(services, running_server):
            self.forget_dead_entry(local_key, lease_key, running_server.process_id)
            return self.ensure_running_owned(
                services,
                server_name=server_name,
                server_config=server_config,
                workspace_root=workspace_root,
                session_id=session_id,
                abort_signal=abort_signal,
                start=start,
                answers=answers,
            )
        self.server_states[server_name] = LspServerState(
            name=server_name,
            status="running",
        )
        return running_server, lease_key

    @staticmethod
    def service_answers(services: ProcessServices, running_server: RunningLspServer) -> bool:
        try:
            page = services.read_process_output(running_server.process_id, stream="stdout", offset=running_server.stdout_cursor, limit=1)
        except ValueError:
            return False
        return page.running

    def stop_abandoned_server(
        self,
        services: ProcessServices,
        local_key: LocalServerKey,
        lease_key: LspRegistryKey,
        running_server: RunningLspServer,
    ) -> bool:
        """Stop a mismatched-root server and report an unconfirmed stop instead of dropping it."""
        process_id = running_server.process_id
        old_key = LspRegistryKey(session_id=lease_key.session_id, server_name=lease_key.server_name, workspace_root=running_server.workspace_root)
        error: str | None = None
        try:
            stopped = services.cancel_process(process_id)
        except ValueError as exc:
            stopped = False
            error = str(exc)
        running_server.documents.clear()
        running_server.initialized = False
        running_server.stdout_cursor = 0
        self.running_servers.pop(local_key, None)
        lease = self.leased_servers.pop(local_key, None)
        self._registry.invalidate(key=old_key, process_id=process_id)
        if lease is not None:
            self._registry.release(lease=lease)
        if stopped:
            self.server_states[lease_key.server_name] = LspServerState(name=lease_key.server_name, status="stopped")
        else:
            detail = f": {error}" if error else ""
            self.server_states[lease_key.server_name] = LspServerState(
                name=lease_key.server_name,
                status="failed",
                last_error=f"LSP server {lease_key.server_name} could not be confirmed stopped{detail}",
            )
        return stopped

    def forget_dead_entry(self, local_key: LocalServerKey, lease_key: LspRegistryKey, process_id: str) -> None:
        """Drop a local entry whose service id the host no longer supervises for this session."""
        current = self.running_servers.get(local_key)
        if current is None or current.process_id != process_id:
            return
        self.running_servers.pop(local_key, None)
        lease = self.leased_servers.pop(local_key, None)
        self._registry.invalidate(key=lease_key, process_id=process_id)
        if lease is not None:
            self._registry.release(lease=lease)

    def cancel_own_server_owned(
        self,
        services: ProcessServices,
        lease_key: LspRegistryKey,
        running_server: RunningLspServer,
        *,
        error: str,
    ) -> bool:
        """Stop a failing/cancelled server's tree and forget it; races surface as the next error."""
        process_id = running_server.process_id
        try:
            stopped = services.cancel_process(process_id)
        except ValueError:
            stopped = False
        running_server.documents.clear()
        running_server.initialized = False
        running_server.stdout_cursor = 0
        local_key = (lease_key.session_id, lease_key.server_name, lease_key.workspace_root)
        if self.running_servers.get(local_key) is not running_server:
            return stopped
        self.forget_dead_entry(local_key, lease_key, process_id)
        self.server_states[lease_key.server_name] = LspServerState(
            name=lease_key.server_name,
            status="failed",
            last_error=error if stopped else f"{error}; LSP server {lease_key.server_name} could not be confirmed stopped",
        )
        return stopped

    def shutdown_owned(self) -> None:
        # Host retirement already stopped every service process in this generation;
        # shutdown here only forgets local entries, never touches the host.
        for key in tuple(self.running_servers):
            self.running_servers.pop(key, None)
            lease = self.leased_servers.pop(key, None)
            if lease is not None:
                self._registry.release(lease=lease)

    def start_running_server(
        self,
        services: ProcessServices,
        *,
        process_id: str,
        server_name: str,
        server_config: ResolvedLspServerConfig,
        workspace_root: Path,
        abort_signal: ProviderAbortSignal | None,
        initialize: InitializeServer,
    ) -> RunningLspServer:
        try:
            services.spawn_service_process(
                list(server_config.command),
                cwd=workspace_root,
                process_id=process_id,
            )
        except (ValueError, PermissionError, OSError) as exc:
            message = self.startup_failure_message(
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

        running_server = RunningLspServer(
            config=server_config,
            process_id=process_id,
            workspace_root=workspace_root,
        )
        try:
            initialize(services, running_server, server_name=server_name, abort_signal=abort_signal)
        except Exception:
            try:
                stopped = services.cancel_process(process_id)
            except ValueError:
                stopped = False
            if not stopped:
                current = self.server_states.get(server_name)
                detail = current.last_error if current is not None and current.last_error else f"LSP initialize failed for {server_name}"
                self.mark_failed(
                    server_name=server_name,
                    error=f"{detail}; LSP server {server_name} could not be confirmed stopped",
                )
            raise
        return running_server

    def initialize_server(
        self,
        services: ProcessServices,
        running_server: RunningLspServer,
        *,
        server_name: str,
        abort_signal: ProviderAbortSignal | None,
        send_request: SendRequest,
        send_notification: SendNotification,
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
            response = send_request(
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
            self.mark_failed(server_name=server_name, error=message)
            raise

        init_error = response.get("error")
        if init_error is not None:
            message = f"LSP initialize failed for {server_name}: {init_error}"
            self.mark_failed(server_name=server_name, error=message)
            raise ValueError(message)
        init_result = response.get("result")
        server_capabilities = init_result.get("capabilities") if isinstance(init_result, dict) else None
        if not isinstance(server_capabilities, dict):
            raise LspProtocolError(f"LSP initialize response from {server_name} requires a capabilities object")
        raw_position_encoding = server_capabilities.get("positionEncoding")
        if raw_position_encoding is None:
            position_encoding: PositionEncoding = "utf-16"
        elif is_position_encoding(raw_position_encoding):
            position_encoding = raw_position_encoding
        else:
            raise ValueError(f"LSP server {server_name} selected unsupported position encoding: {raw_position_encoding}")
        running_server.position_encoding = position_encoding

        text_sync = server_capabilities.get("textDocumentSync")
        if isinstance(text_sync, dict):
            change = text_sync.get("change")
            running_server.document_sync_enabled = (
                text_sync.get("openClose") is True and isinstance(change, int) and not isinstance(change, bool) and change in (1, 2)
            )
        else:
            running_server.document_sync_enabled = isinstance(text_sync, int) and not isinstance(text_sync, bool) and text_sync in (1, 2)
        send_notification(services, running_server, method="initialized", params={}, abort_signal=abort_signal)

        if running_server.config.settings:
            send_notification(
                services,
                running_server,
                method="workspace/didChangeConfiguration",
                params={"settings": dict(running_server.config.settings)},
                abort_signal=abort_signal,
            )
        running_server.initialized = True
        self.server_states[server_name] = LspServerState(
            name=server_name,
            status="running",
        )

    def mark_failed(self, *, server_name: str, error: str) -> None:
        self.server_states[server_name] = LspServerState(
            name=server_name,
            status="failed",
            last_error=error,
        )

    @staticmethod
    def startup_failure_message(
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
            f"or override '{config_path}' in the product configuration with a custom command."
        )
