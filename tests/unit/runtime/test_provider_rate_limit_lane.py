"""Rate-limit handling is selected only from the rate-limit status/code fact.

429 is handled by the runtime's rate-limit lane rather than inline provider retry.
A generic 402 is a terminal client error with no inferred cause.
"""

from __future__ import annotations

from voidcode.provider.config import DEFAULT_PROVIDER_TRANSIENT_RETRY_CONFIG
from voidcode.provider.errors import provider_execution_error_from_api_payload
from voidcode.runtime.execution.provider_fallback import (
    ProviderFallbackDecision,
    ProviderTerminalDecision,
    ProviderTransientRetryDecision,
    decide_provider_error_policy,
)

_RETRY_LANE = "background_rate_limit_retry"


def _decide(payload: dict[str, object], *, background_rate_limit_retry: bool):
    error = provider_execution_error_from_api_payload(
        payload=payload,
        provider_name="opencode-go",
        model_name="deepseek-v4-pro",
    )
    return error, decide_provider_error_policy(
        error=error,
        current_provider_attempt=0,
        provider_retry_attempt=0,
        transient_retry_config=DEFAULT_PROVIDER_TRANSIENT_RETRY_CONFIG,
        fallback_target_provider="deepseek",
        fallback_target_model="deepseek-v4-pro",
        background_rate_limit_retry=background_rate_limit_retry,
    )


def test_a_429_uses_the_rate_limit_lane_not_provider_retry() -> None:
    payload = {"status_code": 429, "message": "Too Many Requests", "headers": {"retry-after": "7"}}
    error, decision = _decide(payload, background_rate_limit_retry=False)

    assert error.kind == "rate_limit"
    assert error.retryable is None
    assert not isinstance(decision, ProviderTransientRetryDecision)
    assert isinstance(decision, ProviderFallbackDecision)
    assert decision.reason == "rate_limit"
    assert decision.to_provider == "deepseek"


def test_a_402_does_not_enter_the_rate_limit_lane() -> None:
    error, decision = _decide({"status_code": 402, "message": "Payment Required"}, background_rate_limit_retry=False)

    assert error.kind == "provider_error"
    assert error.retryable is False
    assert isinstance(decision, ProviderFallbackDecision)
    assert decision.reason == "provider_error"


def test_a_429_defers_to_the_runtime_rate_limit_lane_when_the_runtime_armed_it() -> None:
    error, decision = _decide(
        {"status_code": 429, "message": "Too Many Requests", "headers": {"retry-after": "7"}},
        background_rate_limit_retry=True,
    )

    assert not isinstance(decision, ProviderTransientRetryDecision)
    assert isinstance(decision, ProviderTerminalDecision)
    assert decision.kind == _RETRY_LANE
    assert decision.payload["provider_error_kind"] == "rate_limit"
    assert error.retry_after == 7.0


def test_a_402_is_not_deferred_as_a_rate_limit() -> None:
    _, decision = _decide({"status_code": 402, "message": "Payment Required"}, background_rate_limit_retry=True)

    assert not isinstance(decision, ProviderTransientRetryDecision)
    assert isinstance(decision, ProviderFallbackDecision)


def test_a_5xx_retries_by_status_policy_without_a_transient_error_kind() -> None:
    error, decision = _decide({"status_code": 503, "message": "upstream unavailable"}, background_rate_limit_retry=True)

    assert error.kind == "provider_error"
    assert isinstance(decision, ProviderTransientRetryDecision)
    assert decision.reason == "provider_error"
    assert decision.delay_ms >= 0
