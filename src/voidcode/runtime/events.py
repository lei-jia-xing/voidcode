from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final, Literal

type EventSource = Literal["runtime", "graph", "tool"]

type ExistingEventType = Literal[
    "runtime.request_received",
    "runtime.skills_loaded",
    "graph.tool_request_created",
    "runtime.tool_lookup_succeeded",
    "runtime.permission_resolved",
    "runtime.tool_hook_pre",
    "runtime.tool_hook_post",
    "runtime.tool_completed",
    "graph.response_ready",
    "runtime.approval_requested",
    "runtime.approval_resolved",
    "runtime.failed",
]
type PrototypeAdditiveEventType = Literal[
    "graph.model_turn",
    "graph.loop_step",
    "runtime.memory_refreshed",
]
type KnownEventType = ExistingEventType | PrototypeAdditiveEventType

RUNTIME_REQUEST_RECEIVED: Final[ExistingEventType] = "runtime.request_received"
RUNTIME_SKILLS_LOADED: Final[ExistingEventType] = "runtime.skills_loaded"
GRAPH_TOOL_REQUEST_CREATED: Final[ExistingEventType] = "graph.tool_request_created"
RUNTIME_TOOL_LOOKUP_SUCCEEDED: Final[ExistingEventType] = "runtime.tool_lookup_succeeded"
RUNTIME_PERMISSION_RESOLVED: Final[ExistingEventType] = "runtime.permission_resolved"
RUNTIME_TOOL_HOOK_PRE: Final[ExistingEventType] = "runtime.tool_hook_pre"
RUNTIME_TOOL_HOOK_POST: Final[ExistingEventType] = "runtime.tool_hook_post"
RUNTIME_TOOL_COMPLETED: Final[ExistingEventType] = "runtime.tool_completed"
GRAPH_RESPONSE_READY: Final[ExistingEventType] = "graph.response_ready"
RUNTIME_APPROVAL_REQUESTED: Final[ExistingEventType] = "runtime.approval_requested"
RUNTIME_APPROVAL_RESOLVED: Final[ExistingEventType] = "runtime.approval_resolved"
RUNTIME_FAILED: Final[ExistingEventType] = "runtime.failed"

GRAPH_MODEL_TURN: Final[PrototypeAdditiveEventType] = "graph.model_turn"
GRAPH_LOOP_STEP: Final[PrototypeAdditiveEventType] = "graph.loop_step"
RUNTIME_MEMORY_REFRESHED: Final[PrototypeAdditiveEventType] = "runtime.memory_refreshed"

EMITTED_EVENT_TYPES: Final[tuple[ExistingEventType, ...]] = (
    RUNTIME_REQUEST_RECEIVED,
    RUNTIME_SKILLS_LOADED,
    GRAPH_TOOL_REQUEST_CREATED,
    RUNTIME_TOOL_LOOKUP_SUCCEEDED,
    RUNTIME_PERMISSION_RESOLVED,
    RUNTIME_TOOL_HOOK_PRE,
    RUNTIME_TOOL_HOOK_POST,
    RUNTIME_TOOL_COMPLETED,
    GRAPH_RESPONSE_READY,
    RUNTIME_APPROVAL_REQUESTED,
    RUNTIME_APPROVAL_RESOLVED,
    RUNTIME_FAILED,
)
PROTOTYPE_ADDITIVE_EVENT_TYPES: Final[tuple[PrototypeAdditiveEventType, ...]] = (
    GRAPH_MODEL_TURN,
    GRAPH_LOOP_STEP,
    RUNTIME_MEMORY_REFRESHED,
)
KNOWN_EVENT_TYPES: Final[tuple[KnownEventType, ...]] = (
    *EMITTED_EVENT_TYPES,
    *PROTOTYPE_ADDITIVE_EVENT_TYPES,
)


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    session_id: str
    sequence: int
    event_type: str
    source: EventSource
    payload: dict[str, object] = field(default_factory=dict)
