from __future__ import annotations

from ...provider.models import ResolvedProviderConfig
from ..config import RuntimeContextWindowConfig
from .window import ContextWindowPolicy


def context_window_config_from_policy(
    policy: ContextWindowPolicy | None,
) -> RuntimeContextWindowConfig | None:
    if policy is None:
        return None
    return RuntimeContextWindowConfig(
        default_tool_result_chars=policy.default_tool_result_chars,
        per_tool_result_chars=dict(policy.per_tool_result_chars),
        summary_strategy=policy.summary_strategy,
    )


def context_window_policy_from_config(
    config: RuntimeContextWindowConfig | None,
    *,
    resolved_provider: ResolvedProviderConfig | None,
    provider_attempt: int = 0,
) -> ContextWindowPolicy:
    _ = resolved_provider, provider_attempt
    if config is None:
        return ContextWindowPolicy()
    return ContextWindowPolicy(
        default_tool_result_chars=config.default_tool_result_chars,
        per_tool_result_chars=dict(config.per_tool_result_chars),
        summary_strategy=config.summary_strategy,
    )


__all__ = [
    "context_window_config_from_policy",
    "context_window_policy_from_config",
]
