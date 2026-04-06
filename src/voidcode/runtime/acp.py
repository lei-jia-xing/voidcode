from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from .config import RuntimeAcpConfig


@dataclass(frozen=True, slots=True)
class AcpConfigState:
    configured_enabled: bool = False

    @classmethod
    def from_runtime_config(cls, config: RuntimeAcpConfig | None) -> AcpConfigState:
        if config is None:
            return cls()

        return cls(configured_enabled=bool(config.enabled))


@dataclass(frozen=True, slots=True)
class AcpAdapterState:
    mode: Literal["disabled"] = "disabled"
    configuration: AcpConfigState = field(default_factory=AcpConfigState)
    configured: bool = False
    available: bool = False


class DisabledAcpAdapter:
    def __init__(self, config: RuntimeAcpConfig | None = None) -> None:
        self._configuration = AcpConfigState.from_runtime_config(config)

    @property
    def configuration(self) -> AcpConfigState:
        return self._configuration

    def current_state(self) -> AcpAdapterState:
        return AcpAdapterState(
            configuration=self._configuration,
            configured=self._configuration.configured_enabled,
        )
