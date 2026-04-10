from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

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
class AcpRequestEnvelope:
    request_type: str
    payload: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AcpResponseEnvelope:
    status: Literal["ok", "error"]
    payload: dict[str, object] = field(default_factory=dict)
    error: str | None = None


@dataclass(frozen=True, slots=True)
class AcpRuntimeEvent:
    event_type: str
    payload: dict[str, object]


@dataclass(frozen=True, slots=True)
class AcpAdapterState:
    mode: Literal["disabled", "managed"] = "disabled"
    configuration: AcpConfigState = field(default_factory=AcpConfigState)
    configured: bool = False
    available: bool = False
    status: Literal["disconnected", "connected", "failed"] = "disconnected"
    last_error: str | None = None


class AcpAdapter(Protocol):
    @property
    def configuration(self) -> AcpConfigState: ...

    def current_state(self) -> AcpAdapterState: ...

    def connect(self) -> tuple[AcpRuntimeEvent, ...]: ...

    def disconnect(self) -> tuple[AcpRuntimeEvent, ...]: ...

    def request(self, envelope: AcpRequestEnvelope) -> AcpResponseEnvelope: ...

    def fail(self, message: str) -> tuple[AcpRuntimeEvent, ...]: ...

    def drain_events(self) -> tuple[AcpRuntimeEvent, ...]: ...


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

    def connect(self) -> tuple[AcpRuntimeEvent, ...]:
        raise ValueError("ACP runtime support is disabled")

    def disconnect(self) -> tuple[AcpRuntimeEvent, ...]:
        return ()

    def request(self, envelope: AcpRequestEnvelope) -> AcpResponseEnvelope:
        _ = envelope
        raise ValueError("ACP runtime support is disabled")

    def fail(self, message: str) -> tuple[AcpRuntimeEvent, ...]:
        _ = message
        raise ValueError("ACP runtime support is disabled")

    def drain_events(self) -> tuple[AcpRuntimeEvent, ...]:
        return ()


class ManagedAcpAdapter:
    def __init__(self, config: RuntimeAcpConfig) -> None:
        self._configuration = AcpConfigState.from_runtime_config(config)
        self._state = AcpAdapterState(
            mode="managed",
            configuration=self._configuration,
            configured=self._configuration.configured_enabled,
        )
        self._pending_events: list[AcpRuntimeEvent] = []

    @property
    def configuration(self) -> AcpConfigState:
        return self._configuration

    def current_state(self) -> AcpAdapterState:
        return self._state

    def connect(self) -> tuple[AcpRuntimeEvent, ...]:
        if self._state.status == "connected":
            return ()
        self._state = AcpAdapterState(
            mode="managed",
            configuration=self._configuration,
            configured=True,
            available=True,
            status="connected",
        )
        self._record_event(
            AcpRuntimeEvent(
                event_type="runtime.acp_connected",
                payload={"status": "connected", "available": True},
            )
        )
        return self.drain_events()

    def disconnect(self) -> tuple[AcpRuntimeEvent, ...]:
        if self._state.status != "connected":
            return ()
        self._state = AcpAdapterState(
            mode="managed",
            configuration=self._configuration,
            configured=True,
            available=False,
            status="disconnected",
        )
        self._record_event(
            AcpRuntimeEvent(
                event_type="runtime.acp_disconnected",
                payload={"status": "disconnected", "available": False},
            )
        )
        return self.drain_events()

    def request(self, envelope: AcpRequestEnvelope) -> AcpResponseEnvelope:
        if self._state.status != "connected":
            return AcpResponseEnvelope(
                status="error",
                error="ACP adapter is not connected",
                payload={"request_type": envelope.request_type},
            )
        return AcpResponseEnvelope(
            status="ok",
            payload={
                "request_type": envelope.request_type,
                "accepted": True,
                **envelope.payload,
            },
        )

    def drain_events(self) -> tuple[AcpRuntimeEvent, ...]:
        events = tuple(self._pending_events)
        self._pending_events.clear()
        return events

    def fail(self, message: str) -> tuple[AcpRuntimeEvent, ...]:
        self._fail(message)
        return self.drain_events()

    def _fail(self, message: str) -> None:
        self._state = AcpAdapterState(
            mode="managed",
            configuration=self._configuration,
            configured=True,
            available=False,
            status="failed",
            last_error=message,
        )
        self._record_event(
            AcpRuntimeEvent(
                event_type="runtime.acp_failed",
                payload={"status": "failed", "available": False, "error": message},
            )
        )

    def _record_event(self, event: AcpRuntimeEvent) -> None:
        self._pending_events.append(event)


def build_acp_adapter(config: RuntimeAcpConfig | None) -> AcpAdapter:
    configuration = AcpConfigState.from_runtime_config(config)
    if configuration.configured_enabled is not True:
        return DisabledAcpAdapter(config)
    return ManagedAcpAdapter(config or RuntimeAcpConfig())
