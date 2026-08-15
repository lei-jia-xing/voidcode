from __future__ import annotations

from ..provider.models import ResolvedProviderConfig
from .config import RuntimeContextWindowConfig
from .context_window import ContextWindowPolicy


def context_window_config_from_policy(
    policy: ContextWindowPolicy | None,
) -> RuntimeContextWindowConfig | None:
    if policy is None:
        return None
    return RuntimeContextWindowConfig(
        auto_compaction=policy.auto_compaction,
        model_context_window_tokens=policy.model_context_window_tokens,
        reserved_output_tokens=policy.reserved_output_tokens,
        default_tool_result_tokens=policy.default_tool_result_tokens,
        per_tool_result_tokens=dict(policy.per_tool_result_tokens),
        tokenizer_model=policy.tokenizer_model,
        summary_strategy=policy.summary_strategy,
    )


def context_window_policy_from_config(
    config: RuntimeContextWindowConfig | None,
    *,
    resolved_provider: ResolvedProviderConfig | None,
    provider_attempt: int = 0,
) -> ContextWindowPolicy:
    if config is None:
        return ContextWindowPolicy()
    model_context_window_tokens = config.model_context_window_tokens
    if model_context_window_tokens is None and resolved_provider is not None:
        provider_target = resolved_provider.target_chain.target_at(provider_attempt)
        if provider_target is None:
            provider_target = resolved_provider.active_target
        metadata = provider_target.metadata
        if metadata is not None and metadata.context_window is not None:
            model_context_window_tokens = metadata.context_window
    return ContextWindowPolicy(
        auto_compaction=config.auto_compaction,
        model_context_window_tokens=model_context_window_tokens,
        reserved_output_tokens=config.reserved_output_tokens,
        default_tool_result_tokens=config.default_tool_result_tokens,
        per_tool_result_tokens=dict(config.per_tool_result_tokens),
        tokenizer_model=config.tokenizer_model,
        summary_strategy=config.summary_strategy,
    )


__all__ = [
    "context_window_config_from_policy",
    "context_window_policy_from_config",
]
