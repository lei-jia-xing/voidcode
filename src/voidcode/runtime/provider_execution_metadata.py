from __future__ import annotations

from typing import cast

from ..provider.protocol import ProviderTokenUsage
from .session import SessionState
from .session_metadata_helpers import runtime_state_run_id


def provider_attempt_from_metadata(metadata: dict[str, object]) -> int:
    if "provider_attempt" not in metadata:
        return 0
    raw_provider_attempt = metadata["provider_attempt"]
    if not isinstance(raw_provider_attempt, int) or isinstance(raw_provider_attempt, bool):
        raise ValueError("persisted provider_attempt must be an integer")
    return raw_provider_attempt


def provider_retry_attempt_from_metadata(metadata: dict[str, object]) -> int:
    if "provider_retry_attempt" not in metadata:
        return 0
    raw_provider_retry_attempt = metadata["provider_retry_attempt"]
    if not isinstance(raw_provider_retry_attempt, int) or isinstance(raw_provider_retry_attempt, bool):
        raise ValueError("persisted provider_retry_attempt must be an integer")
    return raw_provider_retry_attempt


def run_id_from_session_metadata(metadata: dict[str, object]) -> str | None:
    """Persisted ``runtime_state.run_id``（非空 str 语义）。

    薄转发：解析统一走 ``session_metadata_helpers.runtime_state_run_id``，
    本函数保留函数名以不动既有 3 处调用点，只保留非空过滤。
    """
    run_id = runtime_state_run_id(metadata)
    return run_id if run_id else None


def _cache_hit_rate(cache_read_tokens: int, uncached_input_tokens: int) -> float | None:
    denominator = cache_read_tokens + uncached_input_tokens
    if denominator <= 0:
        return None
    return cache_read_tokens / denominator


def session_with_provider_usage_metadata(
    session: SessionState,
    usage: ProviderTokenUsage | None,
) -> SessionState:
    if usage is None:
        return session
    usage_payload = usage.metadata_payload()
    raw_provider_usage = session.metadata.get("provider_usage")
    if raw_provider_usage is not None and not isinstance(raw_provider_usage, dict):
        raise ValueError("persisted provider_usage must be an object")
    provider_usage = dict(cast(dict[str, object], raw_provider_usage or {}))
    raw_cumulative = provider_usage.get("cumulative")
    if raw_cumulative is not None and not isinstance(raw_cumulative, dict):
        raise ValueError("persisted provider_usage.cumulative must be an object")
    cumulative = dict(cast(dict[str, object], raw_cumulative or {}))

    def _int_value(key: str) -> int:
        raw_value = cumulative.get(key, 0)
        if isinstance(raw_value, int) and not isinstance(raw_value, bool):
            return raw_value
        raise ValueError(f"persisted provider_usage.cumulative.{key} must be an integer")

    cumulative_payload = {key: _int_value(key) + value for key, value in usage_payload.items()}
    latest_payload = {
        **usage_payload,
        "cache_hit_rate": usage.cache_hit_rate,
    }
    cumulative_payload_with_rate = {
        **cumulative_payload,
        "cache_hit_rate": _cache_hit_rate(cumulative_payload["cache_read_tokens"], cumulative_payload["uncached_input_tokens"]),
    }
    raw_turn_count = provider_usage.get("turn_count", 0)
    turn_count = 0
    if isinstance(raw_turn_count, int) and not isinstance(raw_turn_count, bool):
        turn_count = raw_turn_count
    else:
        raise ValueError("persisted provider_usage.turn_count must be an integer")
    current_run_id = run_id_from_session_metadata(session.metadata)
    current_provider_attempt = provider_attempt_from_metadata(session.metadata)
    return SessionState(
        session=session.session,
        status=session.status,
        turn=session.turn,
        metadata={
            **session.metadata,
            "provider_usage": {
                "latest": latest_payload,
                "latest_run_id": current_run_id,
                "latest_provider_attempt": current_provider_attempt,
                "cumulative": cumulative_payload_with_rate,
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
