from __future__ import annotations

from typing import cast

_RECOVERABLE_RUNTIME_CONTEXT_KEYS = frozenset({"continuity", "continuity_summary"})
_RECOVERABLE_TOP_LEVEL_CONTEXT_KEYS = frozenset({"context_window"})


def verified_checkpoint_session_metadata(
    *,
    checkpoint_metadata: dict[str, object],
    stored_metadata: dict[str, object],
) -> dict[str, object] | None:
    if checkpoint_metadata == stored_metadata:
        return checkpoint_metadata
    if _without_recoverable_context(checkpoint_metadata) != _without_recoverable_context(
        stored_metadata
    ):
        return None
    if not _checkpoint_has_recoverable_context(checkpoint_metadata):
        return None
    return checkpoint_metadata


def _checkpoint_has_recoverable_context(metadata: dict[str, object]) -> bool:
    if any(key in metadata for key in _RECOVERABLE_TOP_LEVEL_CONTEXT_KEYS):
        return True
    runtime_state = metadata.get("runtime_state")
    if not isinstance(runtime_state, dict):
        return False
    runtime_payload = cast(dict[str, object], runtime_state)
    return any(key in runtime_payload for key in _RECOVERABLE_RUNTIME_CONTEXT_KEYS)


def _without_recoverable_context(metadata: dict[str, object]) -> dict[str, object]:
    stripped = {
        key: value
        for key, value in metadata.items()
        if key not in _RECOVERABLE_TOP_LEVEL_CONTEXT_KEYS
    }
    runtime_state = stripped.get("runtime_state")
    if isinstance(runtime_state, dict):
        runtime_payload = {
            key: value
            for key, value in cast(dict[str, object], runtime_state).items()
            if key not in _RECOVERABLE_RUNTIME_CONTEXT_KEYS
        }
        if runtime_payload:
            stripped["runtime_state"] = runtime_payload
        else:
            stripped.pop("runtime_state", None)
    return stripped
