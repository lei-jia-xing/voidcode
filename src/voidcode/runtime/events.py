from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final, Literal, cast

type EventSource = Literal["runtime", "graph", "tool"]

type ExistingEventType = Literal[
    "runtime.request_received",
    "runtime.skills_loaded",
    "runtime.skills_applied",
    "runtime.provider_fallback",
    "runtime.category_model_diagnostic",
    "runtime.acp_connected",
    "runtime.acp_disconnected",
    "runtime.acp_failed",
    "runtime.acp_delegated_lifecycle",
    "runtime.lsp_server_started",
    "runtime.lsp_server_reused",
    "runtime.lsp_server_startup_rejected",
    "runtime.lsp_server_stopped",
    "runtime.lsp_server_failed",
    "runtime.mcp_server_started",
    "runtime.mcp_server_reused",
    "runtime.mcp_server_acquired",
    "runtime.mcp_server_released",
    "runtime.mcp_server_stopped",
    "runtime.mcp_server_idle_cleaned",
    "runtime.mcp_server_failed",
    "graph.loop_step",
    "graph.model_turn",
    "graph.tool_request_created",
    "runtime.tool_lookup_succeeded",
    "runtime.tool_started",
    "runtime.permission_resolved",
    "runtime.tool_hook_pre",
    "runtime.tool_hook_post",
    "runtime.tool_completed",
    "graph.response_ready",
    "runtime.approval_requested",
    "runtime.approval_resolved",
    "runtime.question_requested",
    "runtime.question_answered",
    "runtime.failed",
]
type PrototypeAdditiveEventType = Literal[
    "runtime.memory_refreshed",
    "runtime.context_pressure",
    "runtime.session_started",
    "runtime.session_ended",
    "runtime.session_idle",
    "runtime.skills_binding_mismatch",
    "runtime.background_task_registered",
    "runtime.background_task_started",
    "runtime.background_task_progress",
    "runtime.background_task_waiting_approval",
    "runtime.background_task_completed",
    "runtime.background_task_failed",
    "runtime.background_task_cancelled",
    "runtime.background_task_notification_enqueued",
    "runtime.background_task_result_read",
    "runtime.delegated_result_available",
    "runtime.skill_loaded",
    "runtime.todo_updated",
    "runtime.reasoning_part",
    "runtime.reasoning_diagnostic",
]
type DelegatedBackgroundTaskEventType = Literal[
    "runtime.background_task_waiting_approval",
    "runtime.background_task_completed",
    "runtime.background_task_failed",
    "runtime.background_task_cancelled",
    "runtime.delegated_result_available",
]
type DelegatedLifecycleStatus = Literal[
    "queued",
    "running",
    "waiting_approval",
    "completed",
    "failed",
    "cancelled",
    "interrupted",
]
type KnownEventType = ExistingEventType | PrototypeAdditiveEventType

RUNTIME_REQUEST_RECEIVED: Final[ExistingEventType] = "runtime.request_received"
RUNTIME_SKILLS_LOADED: Final[ExistingEventType] = "runtime.skills_loaded"
RUNTIME_SKILLS_APPLIED: Final[ExistingEventType] = "runtime.skills_applied"
RUNTIME_PROVIDER_FALLBACK: Final[ExistingEventType] = "runtime.provider_fallback"
RUNTIME_CATEGORY_MODEL_DIAGNOSTIC: Final[ExistingEventType] = "runtime.category_model_diagnostic"
RUNTIME_ACP_CONNECTED: Final[ExistingEventType] = "runtime.acp_connected"
RUNTIME_ACP_DISCONNECTED: Final[ExistingEventType] = "runtime.acp_disconnected"
RUNTIME_ACP_FAILED: Final[ExistingEventType] = "runtime.acp_failed"
RUNTIME_ACP_DELEGATED_LIFECYCLE: Final[ExistingEventType] = "runtime.acp_delegated_lifecycle"
RUNTIME_LSP_SERVER_STARTED: Final[ExistingEventType] = "runtime.lsp_server_started"
RUNTIME_LSP_SERVER_REUSED: Final[ExistingEventType] = "runtime.lsp_server_reused"
RUNTIME_LSP_SERVER_STARTUP_REJECTED: Final[ExistingEventType] = (
    "runtime.lsp_server_startup_rejected"
)
RUNTIME_LSP_SERVER_STOPPED: Final[ExistingEventType] = "runtime.lsp_server_stopped"
RUNTIME_LSP_SERVER_FAILED: Final[ExistingEventType] = "runtime.lsp_server_failed"
RUNTIME_MCP_SERVER_STARTED: Final[ExistingEventType] = "runtime.mcp_server_started"
RUNTIME_MCP_SERVER_REUSED: Final[ExistingEventType] = "runtime.mcp_server_reused"
RUNTIME_MCP_SERVER_ACQUIRED: Final[ExistingEventType] = "runtime.mcp_server_acquired"
RUNTIME_MCP_SERVER_RELEASED: Final[ExistingEventType] = "runtime.mcp_server_released"
RUNTIME_MCP_SERVER_STOPPED: Final[ExistingEventType] = "runtime.mcp_server_stopped"
RUNTIME_MCP_SERVER_IDLE_CLEANED: Final[ExistingEventType] = "runtime.mcp_server_idle_cleaned"
RUNTIME_MCP_SERVER_FAILED: Final[ExistingEventType] = "runtime.mcp_server_failed"
GRAPH_LOOP_STEP: Final[ExistingEventType] = "graph.loop_step"
GRAPH_MODEL_TURN: Final[ExistingEventType] = "graph.model_turn"
GRAPH_TOOL_REQUEST_CREATED: Final[ExistingEventType] = "graph.tool_request_created"
RUNTIME_TOOL_LOOKUP_SUCCEEDED: Final[ExistingEventType] = "runtime.tool_lookup_succeeded"
RUNTIME_TOOL_STARTED: Final[ExistingEventType] = "runtime.tool_started"
RUNTIME_PERMISSION_RESOLVED: Final[ExistingEventType] = "runtime.permission_resolved"
RUNTIME_TOOL_HOOK_PRE: Final[ExistingEventType] = "runtime.tool_hook_pre"
RUNTIME_TOOL_HOOK_POST: Final[ExistingEventType] = "runtime.tool_hook_post"
RUNTIME_TOOL_COMPLETED: Final[ExistingEventType] = "runtime.tool_completed"
GRAPH_RESPONSE_READY: Final[ExistingEventType] = "graph.response_ready"
RUNTIME_APPROVAL_REQUESTED: Final[ExistingEventType] = "runtime.approval_requested"
RUNTIME_APPROVAL_RESOLVED: Final[ExistingEventType] = "runtime.approval_resolved"
RUNTIME_QUESTION_REQUESTED: Final[ExistingEventType] = "runtime.question_requested"
RUNTIME_QUESTION_ANSWERED: Final[ExistingEventType] = "runtime.question_answered"
RUNTIME_FAILED: Final[ExistingEventType] = "runtime.failed"

RUNTIME_MEMORY_REFRESHED: Final[PrototypeAdditiveEventType] = "runtime.memory_refreshed"
RUNTIME_CONTEXT_PRESSURE: Final[PrototypeAdditiveEventType] = "runtime.context_pressure"
RUNTIME_SESSION_STARTED: Final[PrototypeAdditiveEventType] = "runtime.session_started"
RUNTIME_SESSION_ENDED: Final[PrototypeAdditiveEventType] = "runtime.session_ended"
RUNTIME_SESSION_IDLE: Final[PrototypeAdditiveEventType] = "runtime.session_idle"
RUNTIME_SKILLS_BINDING_MISMATCH: Final[PrototypeAdditiveEventType] = (
    "runtime.skills_binding_mismatch"
)
RUNTIME_BACKGROUND_TASK_REGISTERED: Final[PrototypeAdditiveEventType] = (
    "runtime.background_task_registered"
)
RUNTIME_BACKGROUND_TASK_STARTED: Final[PrototypeAdditiveEventType] = (
    "runtime.background_task_started"
)
RUNTIME_BACKGROUND_TASK_PROGRESS: Final[PrototypeAdditiveEventType] = (
    "runtime.background_task_progress"
)
RUNTIME_BACKGROUND_TASK_WAITING_APPROVAL: Final[PrototypeAdditiveEventType] = (
    "runtime.background_task_waiting_approval"
)
RUNTIME_BACKGROUND_TASK_COMPLETED: Final[PrototypeAdditiveEventType] = (
    "runtime.background_task_completed"
)
RUNTIME_BACKGROUND_TASK_FAILED: Final[PrototypeAdditiveEventType] = "runtime.background_task_failed"
RUNTIME_BACKGROUND_TASK_CANCELLED: Final[PrototypeAdditiveEventType] = (
    "runtime.background_task_cancelled"
)
RUNTIME_BACKGROUND_TASK_NOTIFICATION_ENQUEUED: Final[PrototypeAdditiveEventType] = (
    "runtime.background_task_notification_enqueued"
)
RUNTIME_BACKGROUND_TASK_RESULT_READ: Final[PrototypeAdditiveEventType] = (
    "runtime.background_task_result_read"
)
RUNTIME_DELEGATED_RESULT_AVAILABLE: Final[PrototypeAdditiveEventType] = (
    "runtime.delegated_result_available"
)
RUNTIME_SKILL_LOADED: Final[PrototypeAdditiveEventType] = "runtime.skill_loaded"
RUNTIME_TODO_UPDATED: Final[PrototypeAdditiveEventType] = "runtime.todo_updated"
RUNTIME_REASONING_PART: Final[PrototypeAdditiveEventType] = "runtime.reasoning_part"
RUNTIME_REASONING_DIAGNOSTIC: Final[PrototypeAdditiveEventType] = "runtime.reasoning_diagnostic"

REASONING_TEXT_LIMIT_CHARS: Final[int] = 4000
REASONING_PREVIEW_LIMIT_CHARS: Final[int] = 240
REASONING_SESSION_TEXT_LIMIT_CHARS: Final[int] = 16_000
REASONING_SESSION_PART_LIMIT: Final[int] = 32
_SAFE_PROVIDER_REASONING_METADATA_KEYS: Final[frozenset[str]] = frozenset({"source"})

EMITTED_EVENT_TYPES: Final[tuple[ExistingEventType, ...]] = (
    RUNTIME_REQUEST_RECEIVED,
    RUNTIME_SKILLS_LOADED,
    RUNTIME_SKILLS_APPLIED,
    RUNTIME_PROVIDER_FALLBACK,
    RUNTIME_CATEGORY_MODEL_DIAGNOSTIC,
    RUNTIME_ACP_CONNECTED,
    RUNTIME_ACP_DISCONNECTED,
    RUNTIME_ACP_FAILED,
    RUNTIME_ACP_DELEGATED_LIFECYCLE,
    RUNTIME_LSP_SERVER_STARTED,
    RUNTIME_LSP_SERVER_REUSED,
    RUNTIME_LSP_SERVER_STARTUP_REJECTED,
    RUNTIME_LSP_SERVER_STOPPED,
    RUNTIME_LSP_SERVER_FAILED,
    RUNTIME_MCP_SERVER_STARTED,
    RUNTIME_MCP_SERVER_REUSED,
    RUNTIME_MCP_SERVER_ACQUIRED,
    RUNTIME_MCP_SERVER_RELEASED,
    RUNTIME_MCP_SERVER_STOPPED,
    RUNTIME_MCP_SERVER_IDLE_CLEANED,
    RUNTIME_MCP_SERVER_FAILED,
    GRAPH_LOOP_STEP,
    GRAPH_MODEL_TURN,
    GRAPH_TOOL_REQUEST_CREATED,
    RUNTIME_TOOL_LOOKUP_SUCCEEDED,
    RUNTIME_TOOL_STARTED,
    RUNTIME_PERMISSION_RESOLVED,
    RUNTIME_TOOL_HOOK_PRE,
    RUNTIME_TOOL_HOOK_POST,
    RUNTIME_TOOL_COMPLETED,
    GRAPH_RESPONSE_READY,
    RUNTIME_APPROVAL_REQUESTED,
    RUNTIME_APPROVAL_RESOLVED,
    RUNTIME_QUESTION_REQUESTED,
    RUNTIME_QUESTION_ANSWERED,
    RUNTIME_FAILED,
)
PROTOTYPE_ADDITIVE_EVENT_TYPES: Final[tuple[PrototypeAdditiveEventType, ...]] = (
    RUNTIME_MEMORY_REFRESHED,
    RUNTIME_CONTEXT_PRESSURE,
    RUNTIME_SESSION_STARTED,
    RUNTIME_SESSION_ENDED,
    RUNTIME_SESSION_IDLE,
    RUNTIME_SKILLS_BINDING_MISMATCH,
    RUNTIME_BACKGROUND_TASK_REGISTERED,
    RUNTIME_BACKGROUND_TASK_STARTED,
    RUNTIME_BACKGROUND_TASK_PROGRESS,
    RUNTIME_BACKGROUND_TASK_WAITING_APPROVAL,
    RUNTIME_BACKGROUND_TASK_COMPLETED,
    RUNTIME_BACKGROUND_TASK_FAILED,
    RUNTIME_BACKGROUND_TASK_CANCELLED,
    RUNTIME_BACKGROUND_TASK_NOTIFICATION_ENQUEUED,
    RUNTIME_BACKGROUND_TASK_RESULT_READ,
    RUNTIME_DELEGATED_RESULT_AVAILABLE,
    RUNTIME_SKILL_LOADED,
    RUNTIME_TODO_UPDATED,
    RUNTIME_REASONING_PART,
    RUNTIME_REASONING_DIAGNOSTIC,
)


def runtime_reasoning_part_payload(
    *,
    text: str,
    source: str = "provider_stream",
    provider_metadata: Mapping[str, object] | None = None,
    started_at: str | None = None,
    ended_at: str | None = None,
) -> dict[str, object]:
    """Build a bounded, runtime-owned reasoning part payload."""

    captured_at = datetime.now(UTC).isoformat()
    text_char_count = len(text)
    truncated = text_char_count > REASONING_TEXT_LIMIT_CHARS
    bounded_text = text[:REASONING_TEXT_LIMIT_CHARS]
    preview = bounded_text[:REASONING_PREVIEW_LIMIT_CHARS]
    payload: dict[str, object] = {
        "type": "reasoning",
        "text": bounded_text,
        "text_char_count": text_char_count,
        "truncated": truncated,
        "source": source,
        "visibility": "showable",
        "time": {
            "start": started_at or captured_at,
            "end": ended_at or captured_at,
        },
        "preview": preview,
    }
    if provider_metadata:
        payload["provider_metadata"] = dict(provider_metadata)
    return payload


def runtime_reasoning_part_from_provider_stream(
    payload: Mapping[str, object],
) -> dict[str, object] | None:
    if payload.get("channel") != "reasoning":
        return None
    kind = payload.get("kind")
    if kind not in {"delta", "content"}:
        return None
    text = payload.get("text")
    if not isinstance(text, str) or not text:
        return None
    provider_metadata: dict[str, object] = {
        "stream_kind": kind,
        "stream_channel": "reasoning",
    }
    raw_metadata = payload.get("metadata")
    if isinstance(raw_metadata, Mapping):
        for key, value in cast(Mapping[str, object], raw_metadata).items():
            if key not in _SAFE_PROVIDER_REASONING_METADATA_KEYS:
                continue
            if isinstance(value, str) and value:
                provider_metadata[key] = value[:REASONING_PREVIEW_LIMIT_CHARS]
    return runtime_reasoning_part_payload(
        text=text,
        source="provider_stream",
        provider_metadata=provider_metadata,
    )


def is_reasoning_payload(event_type: str, payload: Mapping[str, object]) -> bool:
    if event_type == RUNTIME_REASONING_PART:
        return True
    return event_type == "graph.provider_stream" and payload.get("channel") == "reasoning"


def redact_reasoning_payload(
    event_type: str,
    payload: Mapping[str, object],
    *,
    show_thinking: bool = False,
) -> dict[str, object]:
    redacted = dict(payload)
    if show_thinking or not is_reasoning_payload(event_type, payload):
        return redacted
    if "text" in redacted:
        redacted.pop("text", None)
        redacted["text_omitted"] = True
    if "preview" in redacted:
        redacted.pop("preview", None)
        redacted["preview_omitted"] = True
    if event_type == RUNTIME_REASONING_PART:
        redacted["visibility"] = "hidden"
    return redacted


KNOWN_EVENT_TYPES: Final[tuple[KnownEventType, ...]] = (
    *EMITTED_EVENT_TYPES,
    *PROTOTYPE_ADDITIVE_EVENT_TYPES,
)
DELEGATED_BACKGROUND_TASK_EVENT_TYPES: Final[tuple[DelegatedBackgroundTaskEventType, ...]] = (
    RUNTIME_BACKGROUND_TASK_WAITING_APPROVAL,
    RUNTIME_BACKGROUND_TASK_COMPLETED,
    RUNTIME_BACKGROUND_TASK_FAILED,
    RUNTIME_BACKGROUND_TASK_CANCELLED,
    RUNTIME_DELEGATED_RESULT_AVAILABLE,
)
DELEGATED_BACKGROUND_TASK_CORRELATION_FIELDS: Final[tuple[str, ...]] = (
    "task_id",
    "parent_session_id",
    "requested_child_session_id",
    "child_session_id",
    "approval_request_id",
    "question_request_id",
)
DELEGATED_BACKGROUND_TASK_ROUTING_FIELDS: Final[tuple[str, ...]] = (
    "routing_mode",
    "routing_category",
    "routing_subagent_type",
    "routing_description",
    "routing_command",
)
DELEGATED_BACKGROUND_TASK_DURABILITY_FIELDS: Final[tuple[str, ...]] = (
    *DELEGATED_BACKGROUND_TASK_CORRELATION_FIELDS,
    *DELEGATED_BACKGROUND_TASK_ROUTING_FIELDS,
    "status",
    "approval_blocked",
    "result_available",
    "cancellation_cause",
)
ACP_DELEGATED_EXECUTION_FIELDS: Final[tuple[str, ...]] = (
    *DELEGATED_BACKGROUND_TASK_DURABILITY_FIELDS,
    "selected_preset",
    "selected_execution_engine",
    "lifecycle_status",
)

_DELEGATED_EVENT_STATUS_BY_TYPE: Final[
    dict[DelegatedBackgroundTaskEventType | ExistingEventType, DelegatedLifecycleStatus]
] = {
    RUNTIME_BACKGROUND_TASK_WAITING_APPROVAL: "waiting_approval",
    RUNTIME_BACKGROUND_TASK_COMPLETED: "completed",
    RUNTIME_BACKGROUND_TASK_FAILED: "failed",
    RUNTIME_BACKGROUND_TASK_CANCELLED: "cancelled",
    RUNTIME_DELEGATED_RESULT_AVAILABLE: "completed",
    RUNTIME_ACP_DELEGATED_LIFECYCLE: "running",
}
_DELEGATED_LIFECYCLE_STATUSES: Final[frozenset[DelegatedLifecycleStatus]] = frozenset(
    {
        "queued",
        "running",
        "waiting_approval",
        "completed",
        "failed",
        "cancelled",
        "interrupted",
    }
)


def _string_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _bool_or_default(value: object, *, default: bool = False) -> bool:
    return value if isinstance(value, bool) else default


def _mapping_or_none(value: object) -> Mapping[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    mapping = cast(Mapping[object, object], value)
    if not all(isinstance(key, str) for key in mapping):
        return None
    return cast(Mapping[str, object], value)


def _delegated_lifecycle_message_payload(
    payload: Mapping[str, object],
) -> Mapping[str, object] | None:
    message_payload = _mapping_or_none(payload.get("message"))
    if message_payload is not None:
        return message_payload
    fallback_payload = {
        key: payload[key]
        for key in ("summary_output", "error", "approval_blocked", "result_available")
        if key in payload
    }
    return fallback_payload or None


@dataclass(frozen=True, slots=True)
class DelegatedRoutingPayload:
    mode: Literal["sync", "background"] | None = None
    category: str | None = None
    subagent_type: str | None = None
    description: str | None = None
    command: str | None = None

    def as_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {}
        if self.mode is not None:
            payload["mode"] = self.mode
        if self.category is not None:
            payload["category"] = self.category
        if self.subagent_type is not None:
            payload["subagent_type"] = self.subagent_type
        if self.description is not None:
            payload["description"] = self.description
        if self.command is not None:
            payload["command"] = self.command
        return payload

    @classmethod
    def from_payload(cls, payload: Mapping[str, object] | None) -> DelegatedRoutingPayload | None:
        if payload is None:
            return None
        raw_mode = payload.get("mode")
        mode = raw_mode if raw_mode in ("sync", "background") else None
        routing = cls(
            mode=mode,
            category=_string_or_none(payload.get("category")),
            subagent_type=_string_or_none(payload.get("subagent_type")),
            description=_string_or_none(payload.get("description")),
            command=_string_or_none(payload.get("command")),
        )
        return routing if routing.as_payload() else None


@dataclass(frozen=True, slots=True)
class DelegatedExecutionPayload:
    parent_session_id: str | None = None
    requested_child_session_id: str | None = None
    child_session_id: str | None = None
    delegated_task_id: str | None = None
    approval_request_id: str | None = None
    question_request_id: str | None = None
    routing: DelegatedRoutingPayload | None = None
    selected_preset: str | None = None
    selected_execution_engine: str | None = None
    lifecycle_status: DelegatedLifecycleStatus | None = None
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
            "routing": self.routing.as_payload() if self.routing is not None else None,
            "selected_preset": self.selected_preset,
            "selected_execution_engine": self.selected_execution_engine,
            "lifecycle_status": self.lifecycle_status,
            "approval_blocked": self.approval_blocked,
            "result_available": self.result_available,
            "cancellation_cause": self.cancellation_cause,
        }

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, object],
        *,
        lifecycle_status: DelegatedLifecycleStatus | None = None,
    ) -> DelegatedExecutionPayload:
        nested_routing = DelegatedRoutingPayload.from_payload(
            _mapping_or_none(payload.get("routing"))
        )
        raw_status = payload.get("lifecycle_status")
        parsed_status = raw_status if raw_status in _DELEGATED_LIFECYCLE_STATUSES else None
        return cls(
            parent_session_id=_string_or_none(payload.get("parent_session_id")),
            requested_child_session_id=_string_or_none(payload.get("requested_child_session_id")),
            child_session_id=_string_or_none(payload.get("child_session_id")),
            delegated_task_id=_string_or_none(payload.get("delegated_task_id")),
            approval_request_id=_string_or_none(payload.get("approval_request_id")),
            question_request_id=_string_or_none(payload.get("question_request_id")),
            routing=nested_routing,
            selected_preset=_string_or_none(payload.get("selected_preset")),
            selected_execution_engine=_string_or_none(payload.get("selected_execution_engine")),
            lifecycle_status=parsed_status or lifecycle_status,
            approval_blocked=_bool_or_default(payload.get("approval_blocked")),
            result_available=_bool_or_default(payload.get("result_available")),
            cancellation_cause=_string_or_none(payload.get("cancellation_cause")),
        )


@dataclass(frozen=True, slots=True)
class DelegatedLifecycleMessage:
    kind: Literal["delegated_lifecycle"] = "delegated_lifecycle"
    status: DelegatedLifecycleStatus | None = None
    summary_output: str | None = None
    error: str | None = None
    approval_blocked: bool = False
    result_available: bool = False

    def as_payload(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "status": self.status,
            "summary_output": self.summary_output,
            "error": self.error,
            "approval_blocked": self.approval_blocked,
            "result_available": self.result_available,
        }

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, object] | None,
        *,
        default_status: DelegatedLifecycleStatus | None = None,
    ) -> DelegatedLifecycleMessage:
        if payload is None:
            return cls(status=default_status)
        raw_status = payload.get("status")
        status = raw_status if raw_status in _DELEGATED_LIFECYCLE_STATUSES else default_status
        return cls(
            status=status,
            summary_output=_string_or_none(payload.get("summary_output")),
            error=_string_or_none(payload.get("error")),
            approval_blocked=_bool_or_default(payload.get("approval_blocked")),
            result_available=_bool_or_default(payload.get("result_available")),
        )


@dataclass(frozen=True, slots=True)
class DelegatedLifecycleEventPayload:
    session_id: str | None = None
    parent_session_id: str | None = None
    delegation: DelegatedExecutionPayload = field(default_factory=DelegatedExecutionPayload)
    message: DelegatedLifecycleMessage = field(default_factory=DelegatedLifecycleMessage)

    def as_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "delegation": self.delegation.as_payload(),
            "message": self.message.as_payload(),
        }
        if self.session_id is not None:
            payload["session_id"] = self.session_id
        if self.parent_session_id is not None:
            payload["parent_session_id"] = self.parent_session_id
        return payload

    @classmethod
    def from_event(cls, event: EventEnvelope) -> DelegatedLifecycleEventPayload | None:
        if event.event_type not in (
            *DELEGATED_BACKGROUND_TASK_EVENT_TYPES,
            RUNTIME_ACP_DELEGATED_LIFECYCLE,
        ):
            return None
        default_status = _DELEGATED_EVENT_STATUS_BY_TYPE.get(
            cast(DelegatedBackgroundTaskEventType | ExistingEventType, event.event_type)
        )
        payload = event.payload
        delegation_payload = _mapping_or_none(payload.get("delegation")) or {}
        message_payload = _delegated_lifecycle_message_payload(payload)
        delegation = DelegatedExecutionPayload.from_payload(
            delegation_payload,
            lifecycle_status=default_status,
        )
        message = DelegatedLifecycleMessage.from_payload(
            message_payload,
            default_status=delegation.lifecycle_status or default_status,
        )
        return cls(
            session_id=_string_or_none(payload.get("session_id")) or event.session_id,
            parent_session_id=(
                _string_or_none(payload.get("parent_session_id")) or delegation.parent_session_id
            ),
            delegation=delegation,
            message=message,
        )


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    session_id: str
    sequence: int
    event_type: str
    source: EventSource
    payload: dict[str, object] = field(default_factory=dict)

    @property
    def delegated_lifecycle(self) -> DelegatedLifecycleEventPayload | None:
        return DelegatedLifecycleEventPayload.from_event(self)
