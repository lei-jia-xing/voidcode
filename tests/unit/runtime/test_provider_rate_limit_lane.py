"""The lane a rate-limit answer takes: never the provider's own transient retry.

OMP excludes both usage/limit statuses from the provider retry lane
(`isProviderRetryableError` -> false for `isUsageLimit`, `error/retryable.ts:45-46`)
and hands them to credential rotation; voidcode has no rotation, so a 429 defers to
the runtime's own rate-limit lane where the runtime armed it (`background_rate_limit_retry`)
and otherwise moves on to fallback, while a 402 -- terminal for any non-408/429 4xx --
always moves on to fallback. None of them may be an inline provider retry.
"""

from __future__ import annotations

import pytest

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


@pytest.mark.parametrize(
    ("payload", "retryable"),
    [
        ({"status_code": 429, "message": "Too Many Requests", "headers": {"retry-after": "7"}}, None),
        ({"status_code": 402, "message": "Payment Required"}, False),
    ],
)
def test_a_usage_limit_answer_never_takes_the_transient_retry_lane(payload: dict[str, object], retryable: bool | None) -> None:
    error, decision = _decide(payload, background_rate_limit_retry=False)

    assert error.kind == "rate_limit"
    assert error.retryable is retryable
    assert not isinstance(decision, ProviderTransientRetryDecision)
    # The named lane for an unarmed runtime: the chain's next provider, not a repeat
    # of the rate-limited one.
    assert isinstance(decision, ProviderFallbackDecision)
    assert decision.reason == "rate_limit"
    assert decision.to_provider == "deepseek"


def test_a_429_defers_to_the_runtime_rate_limit_lane_when_the_runtime_armed_it() -> None:
    error, decision = _decide(
        {"status_code": 429, "message": "Too Many Requests", "headers": {"retry-after": "7"}},
        background_rate_limit_retry=True,
    )

    assert not isinstance(decision, ProviderTransientRetryDecision)
    assert isinstance(decision, ProviderTerminalDecision)
    assert decision.kind == _RETRY_LANE
    assert decision.payload["provider_error_kind"] == "rate_limit"
    # The hint still travels with the decision, so the deferred retry honours it.
    assert error.retry_after == 7.0


def test_a_402_is_terminal_for_the_retry_lane_and_for_the_deferral() -> None:
    """402 carries the explicit non-retryable answer a non-408/429 4xx gets, so it
    neither retries inline nor defers: the fallback chain takes the turn."""
    _, decision = _decide({"status_code": 402, "message": "Payment Required"}, background_rate_limit_retry=True)

    assert not isinstance(decision, ProviderTransientRetryDecision)
    assert isinstance(decision, ProviderFallbackDecision)


def test_a_transient_failure_still_retries_in_the_provider_lane() -> None:
    """The lane that the rate-limit branch must not steal is still reachable."""
    error, decision = _decide({"status_code": 503, "message": "upstream unavailable"}, background_rate_limit_retry=True)

    assert error.kind == "transient_failure"
    assert isinstance(decision, ProviderTransientRetryDecision)
    assert decision.reason == "transient_failure"
    assert decision.delay_ms >= 0
