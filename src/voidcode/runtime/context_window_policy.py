from __future__ import annotations

from ..provider.model_catalog import infer_model_metadata
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
        max_tool_results=policy.max_tool_results,
        max_tool_result_tokens=policy.max_tool_result_tokens,
        max_context_ratio=policy.max_context_ratio,
        model_context_window_tokens=policy.model_context_window_tokens,
        reserved_output_tokens=policy.reserved_output_tokens,
        minimum_retained_tool_results=policy.minimum_retained_tool_results,
        recent_tool_result_count=policy.recent_tool_result_count,
        recent_tool_result_tokens=policy.recent_tool_result_tokens,
        default_tool_result_tokens=policy.default_tool_result_tokens,
        per_tool_result_tokens=dict(policy.per_tool_result_tokens),
        tokenizer_model=policy.tokenizer_model,
        continuity_preview_items=policy.continuity_preview_items,
        continuity_preview_chars=policy.continuity_preview_chars,
        context_pressure_threshold=policy.context_pressure_threshold,
        context_pressure_cooldown_steps=policy.context_pressure_cooldown_steps,
        continuity_distillation_enabled=policy.continuity_distillation_enabled,
        continuity_distillation_max_input_items=policy.continuity_distillation_max_input_items,
        continuity_distillation_max_input_chars=policy.continuity_distillation_max_input_chars,
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
        provider = provider_target.selection.provider
        model = provider_target.selection.model
        if provider is not None and model is not None:
            metadata = infer_model_metadata(provider, model)
            if metadata is not None:
                model_context_window_tokens = metadata.context_window
    return ContextWindowPolicy(
        auto_compaction=config.auto_compaction,
        max_tool_results=config.max_tool_results,
        max_tool_result_tokens=config.max_tool_result_tokens,
        max_context_ratio=config.max_context_ratio,
        model_context_window_tokens=model_context_window_tokens,
        reserved_output_tokens=config.reserved_output_tokens,
        minimum_retained_tool_results=config.minimum_retained_tool_results,
        recent_tool_result_count=config.recent_tool_result_count,
        recent_tool_result_tokens=config.recent_tool_result_tokens,
        default_tool_result_tokens=config.default_tool_result_tokens,
        per_tool_result_tokens=dict(config.per_tool_result_tokens),
        tokenizer_model=config.tokenizer_model,
        continuity_preview_items=config.continuity_preview_items,
        continuity_preview_chars=config.continuity_preview_chars,
        context_pressure_threshold=config.context_pressure_threshold,
        context_pressure_cooldown_steps=config.context_pressure_cooldown_steps,
        continuity_distillation_enabled=config.continuity_distillation_enabled,
        continuity_distillation_max_input_items=config.continuity_distillation_max_input_items,
        continuity_distillation_max_input_chars=config.continuity_distillation_max_input_chars,
    )


__all__ = [
    "context_window_config_from_policy",
    "context_window_policy_from_config",
]
