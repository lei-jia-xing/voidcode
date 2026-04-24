from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

type AcpDelegatedLifecycleStatus = Literal[
    "queued",
    "running",
    "waiting_approval",
    "completed",
    "failed",
    "cancelled",
]


@dataclass(frozen=True, slots=True)
class AcpConfigState:
    configured_enabled: bool = False

    @classmethod
    def from_enabled(cls, enabled: bool | None) -> AcpConfigState:
        return cls(configured_enabled=bool(enabled))


@dataclass(frozen=True, slots=True)
class AcpDelegatedExecution:
    parent_session_id: str | None = None
    requested_child_session_id: str | None = None
    child_session_id: str | None = None
    delegated_task_id: str | None = None
    approval_request_id: str | None = None
    question_request_id: str | None = None
    routing_mode: Literal["sync", "background"] | None = None
    routing_category: str | None = None
    routing_subagent_type: str | None = None
    routing_description: str | None = None
    routing_command: str | None = None
    selected_preset: str | None = None
    selected_execution_engine: str | None = None
    lifecycle_status: AcpDelegatedLifecycleStatus | None = None
    approval_blocked: bool = False
    result_available: bool = False
    cancellation_cause: str | None = None

    def as_payload(self) -> dict[str, object]:
        return {
            "parent_session_id": self.parent_session_id,
            "requested_child_session_id": self.requested_child_session_id,
            "child_session_id": self.child_session_id,
            "delegated_task_id": self.delegated_task_id,
            "approval_request_id": self.approval_request_id,
            "question_request_id": self.question_request_id,
            "routing_mode": self.routing_mode,
            "routing_category": self.routing_category,
            "routing_subagent_type": self.routing_subagent_type,
            "routing_description": self.routing_description,
            "routing_command": self.routing_command,
            "selected_preset": self.selected_preset,
            "selected_execution_engine": self.selected_execution_engine,
            "lifecycle_status": self.lifecycle_status,
            "approval_blocked": self.approval_blocked,
            "result_available": self.result_available,
            "cancellation_cause": self.cancellation_cause,
        }


@dataclass(frozen=True, slots=True)
class AcpRequestEnvelope:
    request_type: str
    request_id: str | None = None
    session_id: str | None = None
    parent_session_id: str | None = None
    delegation: AcpDelegatedExecution | None = None
    payload: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AcpResponseEnvelope:
    status: Literal["ok", "error"]
    request_type: str | None = None
    request_id: str | None = None
    session_id: str | None = None
    parent_session_id: str | None = None
    delegation: AcpDelegatedExecution | None = None
    payload: dict[str, object] = field(default_factory=dict)
    error: str | None = None


@dataclass(frozen=True, slots=True)
class AcpEventEnvelope:
    event_type: str
    session_id: str | None = None
    parent_session_id: str | None = None
    delegation: AcpDelegatedExecution | None = None
    payload: dict[str, object] = field(default_factory=dict)


class AcpRequestHandler(Protocol):
    def request(self, envelope: AcpRequestEnvelope) -> AcpResponseEnvelope: ...


class AcpEventPublisher(Protocol):
    def publish(self, envelope: AcpEventEnvelope) -> AcpResponseEnvelope: ...
