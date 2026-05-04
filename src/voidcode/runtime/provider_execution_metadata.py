from __future__ import annotations

from typing import cast

from ..provider.protocol import ProviderTokenUsage
from .session import SessionState


def provider_attempt_from_metadata(metadata: dict[str, object]) -> int:
    raw_provider_attempt = metadata.get("provider_attempt", 0)
    return raw_provider_attempt if isinstance(raw_provider_attempt, int) else 0


def provider_retry_attempt_from_metadata(metadata: dict[str, object]) -> int:
    raw_provider_retry_attempt = metadata.get("provider_retry_attempt", 0)
    return raw_provider_retry_attempt if isinstance(raw_provider_retry_attempt, int) else 0


def run_id_from_session_metadata(metadata: dict[str, object]) -> str | None:
    runtime_state = metadata.get("runtime_state")
    if not isinstance(runtime_state, dict):
        return None
    runtime_state_dict = cast(dict[str, object], runtime_state)
    run_id = runtime_state_dict.get("run_id")
    return run_id if isinstance(run_id, str) and run_id else None


def session_with_provider_usage_metadata(
    session: SessionState,
    usage: ProviderTokenUsage | None,
) -> SessionState:
    if usage is None:
        return session
    usage_payload = usage.metadata_payload()
    raw_provider_usage = session.metadata.get("provider_usage")
    provider_usage = (
        dict(cast(dict[str, object], raw_provider_usage))
        if isinstance(raw_provider_usage, dict)
        else {}
    )
    raw_cumulative = provider_usage.get("cumulative")
    cumulative = (
        dict(cast(dict[str, object], raw_cumulative)) if isinstance(raw_cumulative, dict) else {}
    )

    def _int_value(key: str) -> int:
        raw_value = cumulative.get(key, 0)
        if isinstance(raw_value, int) and not isinstance(raw_value, bool):
            return raw_value
        return 0

    cumulative_payload = {key: _int_value(key) + value for key, value in usage_payload.items()}
    raw_turn_count = provider_usage.get("turn_count", 0)
    turn_count = 0
    if isinstance(raw_turn_count, int) and not isinstance(raw_turn_count, bool):
        turn_count = raw_turn_count
    current_run_id = run_id_from_session_metadata(session.metadata)
    current_provider_attempt = provider_attempt_from_metadata(session.metadata)
    return SessionState(
        session=session.session,
        status=session.status,
        turn=session.turn,
        metadata={
            **session.metadata,
            "provider_usage": {
                "latest": usage_payload,
                "latest_run_id": current_run_id,
                "latest_provider_attempt": current_provider_attempt,
                "cumulative": cumulative_payload,
                "turn_count": turn_count + 1,
            },
        },
    )


__all__ = [
    "provider_attempt_from_metadata",
    "provider_retry_attempt_from_metadata",
    "run_id_from_session_metadata",
    "session_with_provider_usage_metadata",
]
