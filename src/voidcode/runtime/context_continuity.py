from __future__ import annotations

from typing import cast

_RECOVERABLE_RUNTIME_CONTEXT_KEYS = frozenset({"context_projection", "context_projection_summary"})
_RECOVERABLE_TOP_LEVEL_CONTEXT_KEYS = frozenset({"context_window"})
# Runtime-owned interaction queue (steer / follow-up) lives in session metadata
# and is delivered at the next provider turn. It is not part of the
# integrity-checked context-continuity truth: a steer enqueued while a session
# waits on approval/question must not poison the approval resume (metadata
# mismatch) and must not be lost — it is preserved from the stored row and
# delivered on the next run after the resolution seals the session.
_INTERACTION_QUEUE_METADATA_KEYS = frozenset({"pending_messages"})


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
            key: value for key, value in cast(dict[str, object], runtime_state).items() if key not in _RECOVERABLE_RUNTIME_CONTEXT_KEYS
        }
        if runtime_payload:
            stripped["runtime_state"] = runtime_payload
        else:
            stripped.pop("runtime_state", None)
    return stripped
