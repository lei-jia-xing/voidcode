from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Literal

from ..provider.config import ProviderTransientRetryConfig
from ..provider.errors import ProviderExecutionError

PROVIDER_TRANSIENT_RETRYABLE_KINDS = frozenset({"rate_limit", "transient_failure"})
PROVIDER_FALLBACK_ELIGIBLE_KINDS = frozenset(
    {
        "missing_auth",
        "rate_limit",
        "invalid_model",
        "transient_failure",
        "unsupported_feature",
        "stream_tool_feedback_shape",
    }
)


@dataclass(frozen=True, slots=True)
class ProviderTransientRetryDecision:
    reason: str
    provider: str
    model: str
    retry_attempt: int
    max_retries: int
    delay_ms: int
    provider_error_details: dict[str, object] | None

    def event_payload(self) -> dict[str, object]:
        return {
            "reason": self.reason,
            "provider": self.provider,
            "model": self.model,
            "retry_attempt": self.retry_attempt,
            "max_retries": self.max_retries,
            "delay_ms": self.delay_ms,
            **(
                {"provider_error_details": self.provider_error_details}
                if self.provider_error_details is not None
                else {}
            ),
        }


@dataclass(frozen=True, slots=True)
class ProviderFallbackDecision:
    reason: str
    from_provider: str
    from_model: str
    to_provider: str
    to_model: str
    attempt: int
    provider_error_details: dict[str, object] | None

    def event_payload(self) -> dict[str, object]:
        return {
            "reason": self.reason,
            "from_provider": self.from_provider,
            "from_model": self.from_model,
            "to_provider": self.to_provider,
            "to_model": self.to_model,
            "attempt": self.attempt,
            **(
                {"provider_error_details": self.provider_error_details}
                if self.provider_error_details is not None
                else {}
            ),
        }


@dataclass(frozen=True, slots=True)
class ProviderTerminalDecision:
    kind: Literal[
        "cancelled",
        "background_rate_limit_retry",
        "fallback_exhausted",
        "provider_error",
    ]
    payload: dict[str, object]


type ProviderFallbackPolicyDecision = (
    ProviderTransientRetryDecision | ProviderFallbackDecision | ProviderTerminalDecision
)


def provider_transient_retry_delay_ms(
    *,
    retry_attempt: int,
    base_delay_ms: float,
    max_delay_ms: float,
    jitter: bool,
) -> int:
    capped_delay = min(base_delay_ms * (2 ** max(retry_attempt - 1, 0)), max_delay_ms)
    if jitter and capped_delay > 0:
        capped_delay = random.uniform(0, capped_delay)
    return max(0, int(round(capped_delay)))


def decide_provider_error_policy(
    *,
    error: ProviderExecutionError,
    current_provider_attempt: int,
    provider_retry_attempt: int,
    transient_retry_config: ProviderTransientRetryConfig,
    fallback_target_provider: str | None,
    fallback_target_model: str | None,
    background_rate_limit_retry: bool,
) -> ProviderFallbackPolicyDecision:
    if error.kind == "cancelled":
        return ProviderTerminalDecision(
            kind="cancelled",
            payload={
                "provider_error_kind": error.kind,
                "provider": error.provider_name,
                "model": error.model_name,
                "cancelled": True,
            },
        )
    if error.kind == "rate_limit" and background_rate_limit_retry:
        return ProviderTerminalDecision(
            kind="background_rate_limit_retry",
            payload={
                "provider_error_kind": error.kind,
                "provider": error.provider_name,
                "model": error.model_name,
                "background_retry_deferred_fallback": True,
                **({"provider_error_details": error.details} if error.details is not None else {}),
            },
        )
    if (
        error.kind in PROVIDER_TRANSIENT_RETRYABLE_KINDS
        and provider_retry_attempt < transient_retry_config.max_retries
    ):
        retry_attempt = provider_retry_attempt + 1
        return ProviderTransientRetryDecision(
            reason=error.kind,
            provider=error.provider_name,
            model=error.model_name,
            retry_attempt=retry_attempt,
            max_retries=transient_retry_config.max_retries,
            delay_ms=provider_transient_retry_delay_ms(
                retry_attempt=retry_attempt,
                base_delay_ms=transient_retry_config.base_delay_ms,
                max_delay_ms=transient_retry_config.max_delay_ms,
                jitter=transient_retry_config.jitter,
            ),
            provider_error_details=error.details,
        )
    if (
        error.kind in PROVIDER_FALLBACK_ELIGIBLE_KINDS
        and fallback_target_provider is not None
        and fallback_target_model is not None
    ):
        return ProviderFallbackDecision(
            reason=error.kind,
            from_provider=error.provider_name,
            from_model=error.model_name,
            to_provider=fallback_target_provider,
            to_model=fallback_target_model,
            attempt=current_provider_attempt + 1,
            provider_error_details=error.details,
        )
    if error.kind in PROVIDER_FALLBACK_ELIGIBLE_KINDS:
        return ProviderTerminalDecision(
            kind="fallback_exhausted",
            payload={
                "provider_error_kind": error.kind,
                "provider": error.provider_name,
                "model": error.model_name,
                "fallback_exhausted": True,
                **(
                    {
                        "provider_retry_exhausted": True,
                        "provider_retry_attempts": provider_retry_attempt,
                    }
                    if error.kind in PROVIDER_TRANSIENT_RETRYABLE_KINDS
                    else {}
                ),
                **({"provider_error_details": error.details} if error.details is not None else {}),
            },
        )
    return ProviderTerminalDecision(
        kind="provider_error",
        payload={
            "provider_error_kind": error.kind,
            "provider": error.provider_name,
            "model": error.model_name,
            **({"provider_error_details": error.details} if error.details is not None else {}),
        },
    )


__all__ = [
    "PROVIDER_FALLBACK_ELIGIBLE_KINDS",
    "PROVIDER_TRANSIENT_RETRYABLE_KINDS",
    "ProviderFallbackDecision",
    "ProviderFallbackPolicyDecision",
    "ProviderTerminalDecision",
    "ProviderTransientRetryDecision",
    "decide_provider_error_policy",
    "provider_transient_retry_delay_ms",
]
