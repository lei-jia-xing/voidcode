import hashlib
from enum import StrEnum


class EventType(StrEnum):
    """Core event type constants."""

    USER_MESSAGE = "core.user_message"
    MODEL_TURN = "core.model_turn"
    PROVIDER_INTENT = "core.provider_intent"
    PROVIDER_INTERRUPTED = "core.provider_interrupted"
    TOOL_INTENT = "core.tool_intent"
    TOOL_COMPLETED = "core.tool_completed"
    TOOL_INTERRUPTED = "core.tool_interrupted"
    TOOL_DEFERRED = "core.tool_deferred"
    TOOL_PROGRESS = "core.tool_progress"
    RUN_RESUMED = "core.run_resumed"
    RUN_COMPLETED = "core.run_completed"
    RUN_FAILED = "core.run_failed"
    RUN_INTERRUPTED = "core.run_interrupted"
    APPROVAL_REQUESTED = "core.approval_requested"
    APPROVAL_DECIDED = "core.approval_decided"
    CONFIRMATION_REQUESTED = "core.confirmation_requested"
    CONFIRMATION_DECIDED = "core.confirmation_decided"
    COMMAND_COMPLETED = "core.command_completed"
    CONTINUATION_STARTED = "core.continuation_started"
    WAKEUP_FIRED = "core.wakeup_fired"
    CHILD_SPAWNED = "core.child_spawned"
    CHILD_FINISHED = "core.child_finished"
    WINDOW_PROJECTED = "core.window_projected"
    ACTIVATION_REQUESTED = "core.activation_requested"
    ACTIVATION_COMMITTED = "core.activation_committed"
    ACTIVATION_FAILED = "core.activation_failed"
    OBSERVER_ERROR = "core.observer_error"
    MODEL_QUERY = "core.model_query"


CHILD_MESSAGE_OWNER = "@core/child"


def child_completion_notice_id(parent_session_id: str, child_session_id: str, run_id: str) -> str:
    """The durable identity of one run's completion notice."""
    return hashlib.sha256("\0".join((CHILD_MESSAGE_OWNER, parent_session_id, child_session_id, run_id)).encode("utf-8")).hexdigest()
