from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol


@dataclass(frozen=True, slots=True)
class AcpConfigState:
    configured_enabled: bool = False

    @classmethod
    def from_enabled(cls, enabled: bool | None) -> AcpConfigState:
        return cls(configured_enabled=bool(enabled))


@dataclass(frozen=True, slots=True)
class AcpRequestEnvelope:
    request_type: str
    payload: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AcpResponseEnvelope:
    status: Literal["ok", "error"]
    payload: dict[str, object] = field(default_factory=dict)
    error: str | None = None


class AcpRequestHandler(Protocol):
    def request(self, envelope: AcpRequestEnvelope) -> AcpResponseEnvelope: ...
