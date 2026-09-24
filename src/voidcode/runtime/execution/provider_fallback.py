from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Literal

from ...provider.config import (
    DEFAULT_PROVIDER_TRANSIENT_RETRY_CONFIG,
    ProviderConfigEntry,
    ProviderTransientRetryConfig,
)
from ...provider.errors import ProviderExecutionError
from ...provider.naming import canonical_provider_id
from ..config import RuntimeProvidersConfig

#: The kinds the provider's own transient-retry lane owns. ``rate_limit`` is
#: deliberately absent: a usage/limit answer is decided by the rate-limit branch in
#: ``decide_provider_error_policy`` (defer to the runtime's rate-limit lane, else
#: fallback), never by an inline provider retry.
PROVIDER_TRANSIENT_RETRYABLE_KINDS = frozenset({"transient_failure"})
PROVIDER_FALLBACK_ALLOWED_KINDS = frozenset(
    {
        "missing_auth",
        "not_configured",
        "rate_limit",
        "invalid_model",
        "transient_failure",
        "unsupported_feature",
        "stream_tool_feedback_shape",
    }
)


def fallback_allowed(error: ProviderExecutionError) -> bool:
    if error.fallback_allowed is not None:
        return error.fallback_allowed
    return error.kind in PROVIDER_FALLBACK_ALLOWED_KINDS


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
            **({"provider_error_details": self.provider_error_details} if self.provider_error_details is not None else {}),
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
            **({"provider_error_details": self.provider_error_details} if self.provider_error_details is not None else {}),
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


type ProviderFallbackPolicyDecision = ProviderTransientRetryDecision | ProviderFallbackDecision | ProviderTerminalDecision


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
    default_retryable = error.kind in PROVIDER_TRANSIENT_RETRYABLE_KINDS
    retryable = default_retryable if error.retryable is None else error.retryable
    fallback_permitted = fallback_allowed(error)
    if error.kind == "rate_limit":
        # A usage/limit answer never takes the provider's own transient-retry lane:
        # OMP's `isProviderRetryableError` returns false for `isUsageLimit` and hands
        # both 402 and 429 to credential rotation (`error/retryable.ts:45-46`). voidcode
        # has no rotation, so the rate-limited turn either defers to the runtime's own
        # rate-limit lane (armed for background tasks) or moves on to fallback -- the
        # generic transient retry below is unreachable for this kind whatever the error
        # carries.
        if background_rate_limit_retry and error.retryable is not False and error.fallback_allowed is not False:
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
        retryable = False
    if retryable and provider_retry_attempt < transient_retry_config.max_retries:
        retry_attempt = provider_retry_attempt + 1
        delay_ms = provider_transient_retry_delay_ms(
            retry_attempt=retry_attempt,
            base_delay_ms=transient_retry_config.base_delay_ms,
            max_delay_ms=transient_retry_config.max_delay_ms,
            jitter=transient_retry_config.jitter,
        )
        if error.retry_after is not None:
            delay_ms = min(int(round(error.retry_after * 1000)), int(transient_retry_config.max_delay_ms))
        return ProviderTransientRetryDecision(
            reason=error.kind,
            provider=error.provider_name,
            model=error.model_name,
            retry_attempt=retry_attempt,
            max_retries=transient_retry_config.max_retries,
            delay_ms=max(0, delay_ms),
            provider_error_details=error.details,
        )
    if fallback_permitted and fallback_target_provider is not None and fallback_target_model is not None:
        return ProviderFallbackDecision(
            reason=error.kind,
            from_provider=error.provider_name,
            from_model=error.model_name,
            to_provider=fallback_target_provider,
            to_model=fallback_target_model,
            attempt=current_provider_attempt + 1,
            provider_error_details=error.details,
        )
    if fallback_permitted:
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
                    if retryable
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
    "PROVIDER_FALLBACK_ALLOWED_KINDS",
    "PROVIDER_TRANSIENT_RETRYABLE_KINDS",
    "ProviderFallbackDecision",
    "ProviderFallbackPolicyDecision",
    "ProviderTerminalDecision",
    "ProviderTransientRetryDecision",
    "decide_provider_error_policy",
    "fallback_allowed",
    "provider_transient_retry_config",
    "provider_transient_retry_delay_ms",
]


def provider_transient_retry_config(
    providers: RuntimeProvidersConfig | None,
    provider_name: str,
) -> ProviderTransientRetryConfig:
    if providers is None:
        return DEFAULT_PROVIDER_TRANSIENT_RETRY_CONFIG
    # Built-in ids come from the ProviderConfigs field the id names; anything
    # else is a custom endpoint id. A ``custom`` lookup must not resolve to the
    # custom mapping itself, which is why the id goes through the same lookup.
    provider_config: ProviderConfigEntry | None = providers.entry(provider_name)
    if provider_config is None:
        provider_config = providers.custom.get(canonical_provider_id(provider_name))
    if provider_config is None or provider_config.transient_retry is None:
        return DEFAULT_PROVIDER_TRANSIENT_RETRY_CONFIG
    return provider_config.transient_retry
