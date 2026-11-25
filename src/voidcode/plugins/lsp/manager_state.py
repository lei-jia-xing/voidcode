"""Immutable manager/configuration state exposed by the LSP facade."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .contracts import ResolvedLspServerConfig
from .lsp_types import LspManagerMode, LspServerStatus, RuntimeLspConfig
from .registry import match_lsp_servers_for_path, resolve_lsp_server_configs


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
    status: LspServerStatus = "stopped"
    last_error: str | None = None


@dataclass(frozen=True, slots=True)
class LspManagerState:
    mode: LspManagerMode = "disabled"
    configuration: LspConfigState = field(default_factory=LspConfigState)
    servers: dict[str, LspServerState] = field(default_factory=dict)
