from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import cast

from ..events import EventEnvelope
from ..session_metadata_helpers import parse_runtime_state_metadata
from .window import RuntimeContextSegment

# Recoverable runtime context keys are a subset of the persisted runtime_state
# key-set (RUNTIME_STATE_METADATA_KEYS, contracts.py): context projection
# snapshots are checkpoint-authoritative while the stored row may drift, so
# they are exempted from the resume checkpoint equality check. The key-set
# relationship is documented here instead of being asserted mechanically
# because these keys are deliberately exempted from integrity checking while
# RUNTIME_STATE_METADATA_KEYS describes the full writable key space.
_RECOVERABLE_RUNTIME_CONTEXT_KEYS = frozenset({"context_projection", "context_projection_summary"})
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
    provider_visible_tool_result_data: Callable[[dict[str, object]], dict[str, object]],
) -> tuple[RuntimeContextSegment, ...]:
    """Build replayable provider-context segments from bounded runtime events.

    This is deliberately a pure projection of the supplied event log and
    output. Session loading, parent ownership, current-prompt boundaries, and
    provider-visible result sanitization remain runtime-owned; the latter is
    supplied as a callback so this leaf module does not depend on
    ``runtime_debug``.
    """
    segments: list[RuntimeContextSegment] = []
    tool_index = 0
    for event in events:
        if event.event_type == "runtime.request_received":
            prompt = event.payload.get("prompt")
            if not isinstance(prompt, str) or not prompt.strip():
                continue
            segments.append(
                RuntimeContextSegment(
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
        payload = event.payload
        tool_name = payload.get("tool")
        if not isinstance(tool_name, str) or not tool_name:
            continue
        # Keep replayed history aligned with the eligible rehydrated result
        # pool: only read-only inspection tools and shell commands with a
        # persisted command are safe to place back into provider context.
        if tool_name not in {"read", "grep", "glob", "ast_grep"}:
            if tool_name != "shell_exec":
                continue
            command = payload.get("command")
            if not isinstance(command, str) or not command.strip():
                continue
        tool_index += 1
        raw_tool_call_id = payload.get("tool_call_id")
        tool_call_id = raw_tool_call_id if isinstance(raw_tool_call_id, str) and raw_tool_call_id.strip() else f"voidcode_replayed_tool_{tool_index}"
        raw_arguments = payload.get("arguments")
        tool_arguments = cast(dict[str, object], raw_arguments) if isinstance(raw_arguments, dict) else {}
        error_value = payload.get("error")
        is_error = error_value is not None
        raw_content = payload.get("content")
        content = str(raw_content) if raw_content is not None and not is_error else ""
        segments.append(
            RuntimeContextSegment(
                role="assistant",
                content=None,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                tool_arguments=tool_arguments,
                metadata={
                    "source": "replayed_conversation",
                    "tier": "recent",
                    "kind": "prior_tool_call",
                },
            )
        )
        segments.append(
            RuntimeContextSegment(
                role="tool",
                content=content,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                metadata={
                    "source": "replayed_conversation",
                    "tier": "recent",
                    "kind": "prior_tool_result",
                    "status": "error" if is_error else "ok",
                    "error": str(error_value) if is_error else None,
                    "data": provider_visible_tool_result_data(payload),
                    "truncated": payload.get("truncated") is True,
                    "partial": payload.get("partial") is True,
                    "reference": (payload.get("reference") if isinstance(payload.get("reference"), str) else None),
                },
            )
        )
    if isinstance(output, str) and output.strip():
        segments.append(
            RuntimeContextSegment(
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
    segments: Iterable[object],
) -> tuple[RuntimeContextSegment, ...]:
    """Retain provider-context segments belonging to replayed conversation history.

    This is a pure projection of supplied context segments. Runtime request
    boundaries and orchestration remain outside this context module.
    """
    replayed: list[RuntimeContextSegment] = []
    for segment in segments:
        metadata = getattr(segment, "metadata", None)
        if not isinstance(metadata, dict):
            continue
        if metadata.get("source") != "replayed_conversation":
            continue
        content = getattr(segment, "content", None)
        if content is not None and not isinstance(content, str):
            continue
        role = getattr(segment, "role", None)
        if role not in {"system", "user", "assistant", "tool"}:
            continue
        replayed.append(
            RuntimeContextSegment(
                role=role,
                content=content,
                tool_call_id=getattr(segment, "tool_call_id", None),
                tool_name=getattr(segment, "tool_name", None),
                tool_arguments=getattr(segment, "tool_arguments", None),
                metadata=dict(cast(dict[str, object], metadata)),
            )
        )
    return tuple(replayed)


def verified_checkpoint_session_metadata(
    *,
    checkpoint_metadata: dict[str, object],
    stored_metadata: dict[str, object],
) -> dict[str, object] | None:
    """Verify a resume checkpoint's metadata against the stored session row.

    The checkpoint metadata is authoritative for the resumable context
    (``context_window`` / ``context_projection`` snapshots). The stored row may
    legitimately differ only in those recoverable context keys and in the
    runtime-owned interaction queue (``pending_messages``). Any other delta
    means the session truth changed under the checkpoint and resume must be
    rejected (returns ``None``). The interaction queue is merged back from the
    stored row so a pre-seal steer/follow-up survives the resolution instead of
    being dropped or poisoning the resume.
    """
    if checkpoint_metadata == stored_metadata:
        return checkpoint_metadata
    checkpoint_core = _without_recoverable_context(checkpoint_metadata)
    stored_core = _without_recoverable_context(stored_metadata)
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
            key: value for key, value in parse_runtime_state_metadata(runtime_state).items() if key not in _RECOVERABLE_RUNTIME_CONTEXT_KEYS
        }
        if runtime_payload:
            stripped["runtime_state"] = runtime_payload
        else:
            stripped.pop("runtime_state", None)
    return stripped
