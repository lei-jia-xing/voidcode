from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from .config import RuntimeLspConfig


@dataclass(frozen=True, slots=True)
class LspServerConfig:
    name: str
    definition: object


@dataclass(frozen=True, slots=True)
class LspConfigState:
    configured_enabled: bool = False
    servers: dict[str, LspServerConfig] = field(default_factory=dict)

    @classmethod
    def from_runtime_config(cls, config: RuntimeLspConfig | None) -> LspConfigState:
        if config is None:
            return cls()

        servers: dict[str, LspServerConfig] = {
            name: LspServerConfig(name=name, definition=definition)
            for name, definition in (config.servers or {}).items()
        }
        return cls(configured_enabled=bool(config.enabled), servers=servers)

    def resolve(self, server_name: str) -> LspServerConfig | None:
        return self.servers.get(server_name)


@dataclass(frozen=True, slots=True)
class LspServerState:
    name: str
    configured: bool = True
    available: bool = False


@dataclass(frozen=True, slots=True)
class LspManagerState:
    mode: Literal["disabled"] = "disabled"
    configuration: LspConfigState = field(default_factory=LspConfigState)
    servers: dict[str, LspServerState] = field(default_factory=dict)


class DisabledLspManager:
    def __init__(self, config: RuntimeLspConfig | None = None) -> None:
        self._configuration = LspConfigState.from_runtime_config(config)

    @property
    def configuration(self) -> LspConfigState:
        return self._configuration

    def resolve(self, server_name: str) -> LspServerConfig | None:
        return self._configuration.resolve(server_name)

    def current_state(self) -> LspManagerState:
        servers: dict[str, LspServerState] = {
            name: LspServerState(name=name) for name in self._configuration.servers
        }
        return LspManagerState(configuration=self._configuration, servers=servers)
