from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, cast

from ..acp import (
    AcpConfigState,
    AcpDelegatedExecution,
    AcpEventEnvelope,
    AcpRequestEnvelope,
    AcpRequestHandler,
    AcpResponseEnvelope,
)
from .config import RuntimeAcpConfig
from .contracts import RuntimeStreamChunk, UnknownSessionError
from .event_envelopes import envelopes_for_acp_events
from .events import RUNTIME_ACP_DELEGATED_LIFECYCLE
from .session import SessionState
from .session_metadata_helpers import session_with_current_acp_metadata
from .storage import SessionEventAppender, SessionStore

if TYPE_CHECKING:
    from .background_tasks import BackgroundTaskState

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _MemoryAcpTransport:
    connected: bool = False

    def open(self) -> _MemoryAcpTransport:
        return _MemoryAcpTransport(connected=True)

    def close(self) -> _MemoryAcpTransport:
        return _MemoryAcpTransport(connected=False)

    def request(self, envelope: AcpRequestEnvelope) -> AcpResponseEnvelope:
        if not self.connected:
            return AcpResponseEnvelope(
                status="error",
                request_type=envelope.request_type,
                request_id=envelope.request_id,
                session_id=envelope.session_id,
                parent_session_id=envelope.parent_session_id,
                delegation=envelope.delegation,
                error="ACP transport is not connected",
                payload={"request_type": envelope.request_type},
            )
        if envelope.request_type == "handshake_fail":
            return AcpResponseEnvelope(
                status="error",
                request_type=envelope.request_type,
                request_id=envelope.request_id,
                session_id=envelope.session_id,
                parent_session_id=envelope.parent_session_id,
                delegation=envelope.delegation,
                error="ACP handshake rejected by memory transport",
                payload={"request_type": envelope.request_type},
            )
        payload: dict[str, object] = {
            "request_type": envelope.request_type,
            "accepted": True,
            **envelope.payload,
        }
        if envelope.delegation is not None:
            payload["delegation"] = envelope.delegation.as_payload()
        return AcpResponseEnvelope(
            status="ok",
            request_type=envelope.request_type,
            request_id=envelope.request_id,
            session_id=envelope.session_id,
            parent_session_id=envelope.parent_session_id,
            delegation=envelope.delegation,
            payload=payload,
        )

    def publish(self, envelope: AcpEventEnvelope) -> AcpResponseEnvelope:
        if not self.connected:
            return AcpResponseEnvelope(
                status="error",
                session_id=envelope.session_id,
                parent_session_id=envelope.parent_session_id,
                delegation=envelope.delegation,
                error="ACP transport is not connected",
                payload={"event_type": envelope.event_type},
            )
        payload: dict[str, object] = {
            "event_type": envelope.event_type,
            "accepted": True,
            **envelope.payload,
        }
        if envelope.delegation is not None:
            payload["delegation"] = envelope.delegation.as_payload()
        return AcpResponseEnvelope(
            status="ok",
            session_id=envelope.session_id,
            parent_session_id=envelope.parent_session_id,
            delegation=envelope.delegation,
            payload=payload,
        )


def _config_state_from_runtime_config(config: RuntimeAcpConfig | None) -> AcpConfigState:
    return AcpConfigState.from_enabled(config.enabled if config is not None else None)


@dataclass(frozen=True, slots=True)
class AcpRuntimeEvent:
    event_type: str
    payload: dict[str, object]
    session_id: str | None = None
    parent_session_id: str | None = None
    delegation: AcpDelegatedExecution | None = None


@dataclass(frozen=True, slots=True)
class AcpAdapterState:
    mode: Literal["disabled", "managed"] = "disabled"
    configuration: AcpConfigState = field(default_factory=AcpConfigState)
    configured: bool = False
    available: bool = False
    status: Literal["disconnected", "connected", "failed"] = "disconnected"
    last_error: str | None = None
    last_request_type: str | None = None
    last_request_id: str | None = None
    last_event_type: str | None = None
    last_delegation: AcpDelegatedExecution | None = None


class AcpAdapter(AcpRequestHandler, Protocol):
    @property
    def configuration(self) -> AcpConfigState: ...

    def current_state(self) -> AcpAdapterState: ...

    def connect(self) -> tuple[AcpRuntimeEvent, ...]: ...

    def disconnect(self) -> tuple[AcpRuntimeEvent, ...]: ...

    def fail(self, message: str) -> tuple[AcpRuntimeEvent, ...]: ...

    def publish(self, envelope: AcpEventEnvelope) -> AcpResponseEnvelope: ...

    def drain_events(self) -> tuple[AcpRuntimeEvent, ...]: ...


class DisabledAcpAdapter:
    def __init__(self, config: RuntimeAcpConfig | None = None) -> None:
        self._configuration = _config_state_from_runtime_config(config)

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

    def publish(self, envelope: AcpEventEnvelope) -> AcpResponseEnvelope:
        _ = envelope
        raise ValueError("ACP runtime support is disabled")

    def drain_events(self) -> tuple[AcpRuntimeEvent, ...]:
        return ()


class ManagedAcpAdapter:
    def __init__(self, config: RuntimeAcpConfig) -> None:
        self._runtime_config = config
        self._configuration = _config_state_from_runtime_config(config)
        self._state = AcpAdapterState(
            mode="managed",
            configuration=self._configuration,
            configured=self._configuration.configured_enabled,
        )
        self._pending_events: list[AcpRuntimeEvent] = []
        self._transport = _MemoryAcpTransport()

    @property
    def configuration(self) -> AcpConfigState:
        return self._configuration

    def current_state(self) -> AcpAdapterState:
        return self._state

    def connect(self) -> tuple[AcpRuntimeEvent, ...]:
        if self._state.status == "connected":
            return ()
        try:
            self._transport = self._transport.open()
            handshake_response = self._transport.request(
                AcpRequestEnvelope(
                    request_type=self._runtime_config.handshake_request_type,
                    payload=dict(self._runtime_config.handshake_payload),
                )
            )
            if handshake_response.status != "ok":
                error = handshake_response.error or "ACP handshake failed"
                self._fail(error)
                raise RuntimeError(error)
        except Exception as exc:
            if self._state.status != "failed" or self._state.last_error != str(exc):
                self._fail(str(exc))
            raise
        self._state = AcpAdapterState(
            mode="managed",
            configuration=self._configuration,
            configured=True,
            available=True,
            status="connected",
            last_request_type=handshake_response.request_type,
            last_request_id=handshake_response.request_id,
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
        self._transport = self._transport.close()
        last_delegation = self._state.last_delegation
        self._state = AcpAdapterState(
            mode="managed",
            configuration=self._configuration,
            configured=True,
            available=False,
            status="disconnected",
            last_request_type=self._state.last_request_type,
            last_request_id=self._state.last_request_id,
            last_event_type=self._state.last_event_type,
            last_delegation=last_delegation,
        )
        self._record_event(
            AcpRuntimeEvent(
                event_type="runtime.acp_disconnected",
                payload={"status": "disconnected", "available": False},
                delegation=last_delegation,
            )
        )
        return self.drain_events()

    def request(self, envelope: AcpRequestEnvelope) -> AcpResponseEnvelope:
        if self._state.status != "connected":
            return AcpResponseEnvelope(
                status="error",
                request_type=envelope.request_type,
                request_id=envelope.request_id,
                session_id=envelope.session_id,
                parent_session_id=envelope.parent_session_id,
                delegation=envelope.delegation,
                error="ACP adapter is not connected",
                payload={"request_type": envelope.request_type},
            )
        response = self._transport.request(envelope)
        self._state = AcpAdapterState(
            mode="managed",
            configuration=self._configuration,
            configured=True,
            available=True,
            status=self._state.status,
            last_error=self._state.last_error,
            last_request_type=envelope.request_type,
            last_request_id=envelope.request_id,
            last_event_type=self._state.last_event_type,
            last_delegation=envelope.delegation,
        )
        return response

    def publish(self, envelope: AcpEventEnvelope) -> AcpResponseEnvelope:
        if self._state.status != "connected":
            return AcpResponseEnvelope(
                status="error",
                session_id=envelope.session_id,
                parent_session_id=envelope.parent_session_id,
                delegation=envelope.delegation,
                error="ACP adapter is not connected",
                payload={"event_type": envelope.event_type},
            )
        response = self._transport.publish(envelope)
        self._state = AcpAdapterState(
            mode="managed",
            configuration=self._configuration,
            configured=True,
            available=True,
            status=self._state.status,
            last_error=self._state.last_error,
            last_request_type=self._state.last_request_type,
            last_request_id=self._state.last_request_id,
            last_event_type=envelope.event_type,
            last_delegation=envelope.delegation,
        )
        return response

    def drain_events(self) -> tuple[AcpRuntimeEvent, ...]:
        events = tuple(self._pending_events)
        self._pending_events.clear()
        return events

    def fail(self, message: str) -> tuple[AcpRuntimeEvent, ...]:
        self._fail(message)
        return self.drain_events()

    def _fail(self, message: str) -> None:
        self._transport = self._transport.close()
        self._state = AcpAdapterState(
            mode="managed",
            configuration=self._configuration,
            configured=True,
            available=False,
            status="failed",
            last_error=message,
            last_request_type=self._state.last_request_type,
            last_request_id=self._state.last_request_id,
            last_event_type=self._state.last_event_type,
            last_delegation=self._state.last_delegation,
        )
        self._record_event(
            AcpRuntimeEvent(
                event_type="runtime.acp_failed",
                payload={"status": "failed", "available": False, "error": message},
                delegation=self._state.last_delegation,
            )
        )

    def _record_event(self, event: AcpRuntimeEvent) -> None:
        self._pending_events.append(event)


def build_acp_adapter(config: RuntimeAcpConfig | None) -> AcpAdapter:
    configuration = _config_state_from_runtime_config(config)
    if configuration.configured_enabled is not True:
        return DisabledAcpAdapter(config)
    return ManagedAcpAdapter(config or RuntimeAcpConfig())


def delegated_execution_for_task(
    *,
    task: BackgroundTaskState,
    lifecycle_status: str,
    approval_blocked: bool | None = None,
    result_available: bool | None = None,
) -> AcpDelegatedExecution:
    try:
        routing = task.routing_identity
    except ValueError:
        routing = None
    delegation_metadata = task.request.metadata.get("delegation")
    delegation_dict = cast(dict[str, object], delegation_metadata) if isinstance(delegation_metadata, dict) else {}
    return AcpDelegatedExecution(
        parent_session_id=task.parent_session_id,
        requested_child_session_id=task.request.session_id,
        child_session_id=task.session_id,
        delegated_task_id=task.task.id,
        approval_request_id=task.approval_request_id,
        question_request_id=task.question_request_id,
        routing_mode=routing.mode if routing is not None else None,
        routing_subagent_type=routing.subagent_type if routing is not None else None,
        routing_description=routing.description if routing is not None else None,
        routing_command=routing.command if routing is not None else None,
        selected_preset=(cast(str, delegation_dict["selected_preset"]) if isinstance(delegation_dict.get("selected_preset"), str) else None),
        selected_execution_engine=(
            cast(str, delegation_dict["selected_execution_engine"]) if isinstance(delegation_dict.get("selected_execution_engine"), str) else None
        ),
        lifecycle_status=cast(
            Literal[
                "queued",
                "running",
                "idle",
                "waiting_approval",
                "completed",
                "failed",
                "cancelled",
            ],
            lifecycle_status,
        ),
        approval_blocked=(approval_blocked if approval_blocked is not None else task.status == "running"),
        result_available=(result_available if result_available is not None else task.result_available),
        cancellation_cause=task.cancellation_cause,
    )


def publish_delegated_acp_event(
    adapter: AcpAdapter,
    *,
    task: BackgroundTaskState,
    lifecycle_status: str,
    payload: dict[str, object],
    approval_blocked: bool | None = None,
    result_available: bool | None = None,
) -> None:
    if adapter.current_state().status != "connected":
        return
    delegation = delegated_execution_for_task(
        task=task,
        lifecycle_status=lifecycle_status,
        approval_blocked=approval_blocked,
        result_available=result_available,
    )
    response = adapter.publish(
        AcpEventEnvelope(
            event_type=RUNTIME_ACP_DELEGATED_LIFECYCLE,
            session_id=task.session_id,
            parent_session_id=task.parent_session_id,
            delegation=delegation,
            payload=payload,
        )
    )
    if response.status != "ok":
        logger.debug("skipping ACP delegated lifecycle event: %s", response.error)


def append_parent_acp_delegated_lifecycle_event(
    appender: SessionStore,
    *,
    workspace: Path,
    task: BackgroundTaskState,
    lifecycle_status: str,
    payload: dict[str, object],
    approval_blocked: bool | None = None,
    result_available: bool | None = None,
) -> None:
    parent_session_id = task.parent_session_id
    if parent_session_id is None:
        return
    if not isinstance(appender, SessionEventAppender):
        return
    delegation = delegated_execution_for_task(
        task=task,
        lifecycle_status=lifecycle_status,
        approval_blocked=approval_blocked,
        result_available=result_available,
    )
    correlation_id = task.approval_request_id or task.question_request_id or task.session_id or "none"
    try:
        _ = appender.append_session_event(
            workspace=workspace,
            session_id=parent_session_id,
            event_type=RUNTIME_ACP_DELEGATED_LIFECYCLE,
            source="runtime",
            payload={
                "session_id": task.session_id,
                "parent_session_id": parent_session_id,
                "delegation": delegation.as_payload(),
                **payload,
            },
            dedupe_key=(f"{RUNTIME_ACP_DELEGATED_LIFECYCLE}:{task.task.id}:{lifecycle_status}:{correlation_id}"),
        )
    except (AttributeError, UnknownSessionError):
        logger.debug(
            "skipping ACP delegated lifecycle event for unavailable parent session: %s",
            parent_session_id,
        )


def disconnect_acp_for_session_state(adapter: AcpAdapter, session: SessionState) -> SessionState:
    _ = adapter.disconnect()
    return session_with_current_acp_metadata(session, adapter.current_state())


def emit_acp_events(
    adapter: AcpAdapter,
    session: SessionState,
    start_sequence: int,
    acp_events: tuple[object, ...],
) -> tuple[tuple[RuntimeStreamChunk, ...], SessionState, int]:
    emitted: list[RuntimeStreamChunk] = []
    current_session = session
    sequence = start_sequence - 1
    for acp_event in envelopes_for_acp_events(
        session_id=session.session.id,
        start_sequence=start_sequence,
        acp_events=acp_events,
    ):
        sequence = acp_event.sequence
        current_session = session_with_current_acp_metadata(current_session, adapter.current_state())
        emitted.append(RuntimeStreamChunk(kind="event", session=current_session, event=acp_event))
    return tuple(emitted), current_session, sequence


def emit_current_acp_drain(
    adapter: AcpAdapter,
    session: SessionState,
    start_sequence: int,
) -> tuple[tuple[RuntimeStreamChunk, ...], SessionState, int]:
    return emit_acp_events(
        adapter,
        session,
        start_sequence=start_sequence,
        acp_events=adapter.drain_events(),
    )


def finalize_run_acp(
    adapter: AcpAdapter,
    session: SessionState,
    sequence: int,
) -> tuple[tuple[RuntimeStreamChunk, ...], SessionState, int]:
    if adapter.current_state().configuration.configured_enabled is not True:
        return (), session, sequence
    emitted, updated_session, last_sequence = emit_acp_events(
        adapter,
        session,
        start_sequence=sequence + 1,
        acp_events=adapter.disconnect(),
    )
    if not emitted:
        updated_session = session_with_current_acp_metadata(updated_session, adapter.current_state())
    return emitted, updated_session, last_sequence or sequence
