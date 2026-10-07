from __future__ import annotations

from collections.abc import Callable, Iterable

from ...core.transcript import ContextSegment, output_text, project_report
from ...core.turns import ReportedCall
from ..composition import CompositionRef, FrozenComposition
from ..events import EventEnvelope
from ..session_metadata_helpers import parse_runtime_state_metadata

# Recoverable runtime context keys are a subset of the persisted runtime_state
# key-set (RUNTIME_STATE_METADATA_KEYS, contracts.py): context projection
# snapshots are checkpoint-authoritative while the stored row may drift, so
# they are exempted from the resume checkpoint equality check. The key-set
# relationship is documented here instead of being asserted mechanically
# because these keys are deliberately exempted from integrity checking while
# RUNTIME_STATE_METADATA_KEYS describes the full writable key space.
#
# ``reminders`` joins them for the opposite ownership reason: the per-call
# reminder counters are advanced inside the run loop after the iteration
# checkpoint was captured, so a checkpoint taken earlier in the same turn would
# otherwise mismatch the stored row and reject a legitimate resume. The stored
# row is authoritative for these counters (the checkpoint only carries whatever
# the session held when it was written).
_RECOVERABLE_RUNTIME_CONTEXT_KEYS = frozenset({"context_projection", "context_projection_summary", "reminders"})
# The native call prefix is checkpoint-authoritative; resume validates it against events.
_CHECKPOINT_PROGRESS_RUNTIME_STATE_KEYS = frozenset({"turn_batch", "pending_tool_intent"})
_RECOVERABLE_TOP_LEVEL_CONTEXT_KEYS = frozenset({"context_window"})
# Runtime-owned interaction queue (steer / follow-up) lives in session metadata
# and is delivered at the next provider turn. It is not part of the
# integrity-checked context-continuity truth: a steer enqueued while a session
# waits on approval/question must not poison the approval resume (metadata
# mismatch) and must not be lost — it is preserved from the stored row and
# delivered on the next run after the resolution seals the session.
_INTERACTION_QUEUE_METADATA_KEYS = frozenset({"pending_messages"})


def replayed_conversation_segments_from_events(
    events: tuple[EventEnvelope, ...],
    *,
    output: object | None,
    reported_call_from_event: Callable[[EventEnvelope], ReportedCall | None],
) -> tuple[ContextSegment, ...]:
    """Build replayable provider-context segments from bounded runtime events.

    This remains a pure projection of supplied runtime events. The callback
    parses each persisted event into its canonical report; this module only
    projects that report into the provider transcript.
    """
    segments: list[ContextSegment] = []
    for event in events:
        if event.event_type == "runtime.request_received":
            prompt = event.payload.get("prompt")
            if not isinstance(prompt, str) or not prompt.strip():
                continue
            segments.append(
                ContextSegment(
                    role="user",
                    content=prompt,
                    metadata={
                        "source": "replayed_conversation",
                        "tier": "recent",
                        "kind": "prior_user_prompt",
                        "sequence": event.sequence,
                    },
                )
            )
            continue
        if event.event_type != "runtime.tool_completed":
            continue
        report = reported_call_from_event(event)
        if report is None:
            continue
        tool_name = report.final_tool_name
        arguments = report.authorized_arguments
        # Keep replayed history aligned with the eligible rehydrated result
        # pool: only read-only inspection tools and shell commands are safe
        # to place back into provider context.
        if tool_name not in {"read", "grep", "glob", "ast_grep"}:
            if tool_name != "shell_exec":
                continue
            command = arguments.get("command")
            if not isinstance(command, str) or not command.strip():
                continue
        view = project_report(report)
        bounds = view.output.bounds
        reference = bounds.reference
        segments.append(
            ContextSegment(
                role="assistant",
                content=None,
                tool_call_id=report.tool_call_id,
                tool_name=tool_name,
                tool_arguments=dict(arguments),
                metadata={
                    "source": "replayed_conversation",
                    "tier": "recent",
                    "kind": "prior_tool_call",
                },
            )
        )
        segments.append(
            ContextSegment(
                role="tool",
                content=output_text(view.output) or "",
                tool_call_id=report.tool_call_id,
                tool_name=tool_name,
                metadata={
                    "source": "replayed_conversation",
                    "tier": "recent",
                    "kind": "prior_tool_result",
                    "status": view.status,
                    "error": view.error,
                    "truncated": bounds.truncated,
                    "partial": bounds.partial,
                    "reference": None if reference is None else reference.uri,
                },
            )
        )
    if isinstance(output, str) and output.strip():
        segments.append(
            ContextSegment(
                role="assistant",
                content=output,
                metadata={
                    "source": "replayed_conversation",
                    "tier": "recent",
                    "kind": "prior_assistant_output",
                },
            )
        )
    return tuple(segments)


def replayed_conversation_segments_from_segments(
    segments: Iterable[ContextSegment],
) -> tuple[ContextSegment, ...]:
    """Retain provider-context segments belonging to replayed conversation history.

    This is a pure projection of supplied context segments. Runtime request
    boundaries and orchestration remain outside this context module.
    """
    replayed: list[ContextSegment] = []
    for segment in segments:
        metadata = segment.metadata
        if not isinstance(metadata, dict):
            continue
        if metadata.get("source") != "replayed_conversation":
            continue
        content = segment.content
        if content is not None and not isinstance(content, str):
            continue
        role = segment.role
        if role not in {"system", "user", "assistant", "tool"}:
            continue
        replayed.append(
            ContextSegment(
                role=role,
                content=content,
                tool_call_id=segment.tool_call_id,
                tool_name=segment.tool_name,
                tool_arguments=segment.tool_arguments,
                metadata=dict(metadata),
            )
        )
    return tuple(replayed)


def verified_checkpoint_session_metadata(
    *,
    checkpoint_metadata: dict[str, object],
    stored_metadata: dict[str, object],
) -> dict[str, object] | None:
    """Verify a resume checkpoint's metadata against the stored session row.

    The checkpoint metadata is authoritative for resumable context snapshots
    and the atomically advanced native call prefix. Resume validates that prefix
    against the durable event path. Stored metadata may differ in these values,
    reminder counters, and the interaction queue (``pending_messages``); any
    other delta means session truth changed under the checkpoint and resume is
    rejected (returns ``None``). The queue is merged from the row so a pre-seal
    steer/follow-up survives resolution instead of poisoning the resume.
    """
    if "execution_composition" in checkpoint_metadata:
        return None
    raw_composition = stored_metadata.get("execution_composition")
    if not isinstance(raw_composition, dict):
        return None
    frozen = FrozenComposition.from_payload(raw_composition)
    checkpoint_snapshot = checkpoint_metadata.get("agent_capability_snapshot")
    stored_snapshot = stored_metadata.get("agent_capability_snapshot")
    if not isinstance(checkpoint_snapshot, dict) or not isinstance(stored_snapshot, dict):
        return None
    checkpoint_ref = checkpoint_snapshot.get("composition_ref")
    stored_ref = stored_snapshot.get("composition_ref")
    if not isinstance(checkpoint_ref, dict) or not isinstance(stored_ref, dict):
        return None
    canonical_ref = CompositionRef.model_validate(stored_ref)
    if CompositionRef.model_validate(checkpoint_ref) != canonical_ref:
        return None
    if canonical_ref.binding_id != frozen.binding.binding_id or canonical_ref.plan_id != frozen.plan.plan_id:
        return None
    stored_without_owner = {key: value for key, value in stored_metadata.items() if key != "execution_composition"}
    checkpoint_core = _without_recoverable_context(checkpoint_metadata)
    stored_core = _without_recoverable_context(stored_without_owner)
    checkpoint_core = {key: value for key, value in checkpoint_core.items() if key not in _INTERACTION_QUEUE_METADATA_KEYS}
    stored_core = {key: value for key, value in stored_core.items() if key not in _INTERACTION_QUEUE_METADATA_KEYS}
    if checkpoint_core != stored_core:
        return None
    merged = dict(checkpoint_metadata)
    raw_messages = stored_metadata.get("pending_messages")
    if isinstance(raw_messages, list):
        merged["pending_messages"] = raw_messages
    return merged


def _without_recoverable_context(metadata: dict[str, object]) -> dict[str, object]:
    stripped = {key: value for key, value in metadata.items() if key not in _RECOVERABLE_TOP_LEVEL_CONTEXT_KEYS}
    runtime_state = stripped.get("runtime_state")
    if isinstance(runtime_state, dict):
        runtime_payload = {
            key: value
            for key, value in parse_runtime_state_metadata(runtime_state).items()
            if key not in _RECOVERABLE_RUNTIME_CONTEXT_KEYS | _CHECKPOINT_PROGRESS_RUNTIME_STATE_KEYS
        }
        if runtime_payload:
            stripped["runtime_state"] = runtime_payload
        else:
            stripped.pop("runtime_state", None)
    runtime_policy = stripped.get("runtime_policy")
    if isinstance(runtime_policy, dict):
        prompt_activation = runtime_policy.get("prompt_activation")
        if isinstance(prompt_activation, dict) and "activated_this_turn" in prompt_activation:
            stripped["runtime_policy"] = {
                **runtime_policy,
                "prompt_activation": {**prompt_activation, "activated_this_turn": False},
            }
    return stripped
