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
    TOOL_DEFERRED_CANCELLED = "core.tool_deferred_cancelled"
    TOOL_PROGRESS = "core.tool_progress"
    PROVIDER_PROGRESS = "core.provider_progress"
    RUN_RESUMED = "core.run_resumed"
    RUN_COMPLETED = "core.run_completed"
    RUN_FAILED = "core.run_failed"
    RUN_INTERRUPTED = "core.run_interrupted"
    APPROVAL_REQUESTED = "core.approval_requested"
    APPROVAL_DECIDED = "core.approval_decided"
    ASK_REQUESTED = "core.ask_requested"
    ASK_ANSWERED = "core.ask_answered"
    CONFIRMATION_REQUESTED = "core.confirmation_requested"
    CONFIRMATION_DECIDED = "core.confirmation_decided"
    COMMAND_COMPLETED = "core.command_completed"
    CONTINUATION_STARTED = "core.continuation_started"
    FOLLOWUP_QUEUED = "core.followup_queued"
    FOLLOWUP_WITHDRAWN = "core.followup_withdrawn"
    WAKEUP_FIRED = "core.wakeup_fired"
    CHILD_SPAWNED = "core.child_spawned"
    #: A keyed child's atomic birth record on its own session (sequence 1): core
    #: bookkeeping, never a public spawn fact, transcript entry, or observer notice.
    CHILD_RESERVED = "core.child_reserved"
    CHILD_FINISHED = "core.child_finished"
    WINDOW_PROJECTED = "core.window_projected"
    ACTIVATION_REQUESTED = "core.activation_requested"
    ACTIVATION_COMMITTED = "core.activation_committed"
    ACTIVATION_FAILED = "core.activation_failed"
    #: One lifecycle hook invocation and its bounded note/failure, or one mutating
    #: pre-tool gate skipped by the effective read-only stance.
    HOOK_INVOKED = "core.hook_invoked"
    HOOK_SKIPPED = "core.hook_skipped"
    OBSERVER_ERROR = "core.observer_error"
    MODEL_QUERY = "core.model_query"


CHILD_MESSAGE_OWNER = "@core/child"

CHILD_OUTCOME_RECEIPT_OWNER = "@core/child-outcome"


def child_completion_notice_id(parent_session_id: str, child_session_id: str, run_id: str) -> str:
    """The durable identity of one run's completion notice."""
    return hashlib.sha256("\0".join((CHILD_MESSAGE_OWNER, parent_session_id, child_session_id, run_id)).encode("utf-8")).hexdigest()
