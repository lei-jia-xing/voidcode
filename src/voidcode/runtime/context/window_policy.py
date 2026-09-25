from __future__ import annotations

from ..config import RuntimeCompactionConfig, RuntimeContextWindowConfig
from .window import CompactionBudget, ContextWindowPolicy


def context_window_config_from_policy(
    policy: ContextWindowPolicy | None,
) -> RuntimeContextWindowConfig | None:
    if policy is None:
        return None
    return RuntimeContextWindowConfig(
        default_tool_result_chars=policy.default_tool_result_chars,
        per_tool_result_chars=dict(policy.per_tool_result_chars),
        summary_strategy=policy.summary_strategy,
        compaction=RuntimeCompactionConfig(
            enabled=policy.compaction_enabled,
            keep_recent_tool_tokens=policy.keep_recent_tool_tokens,
        ),
    )


def context_window_policy_from_config(
    config: RuntimeContextWindowConfig | None,
) -> ContextWindowPolicy:
    if config is None:
        return ContextWindowPolicy()
    return ContextWindowPolicy(
        default_tool_result_chars=config.default_tool_result_chars,
        per_tool_result_chars=dict(config.per_tool_result_chars),
        summary_strategy=config.summary_strategy,
        compaction_enabled=config.compaction.enabled,
        keep_recent_tool_tokens=config.compaction.keep_recent_tool_tokens,
    )


def compaction_budget_from_config(
    config: RuntimeContextWindowConfig | None,
    *,
    context_window: int | None,
    anchor_tokens: int | None = None,
) -> CompactionBudget:
    """Resolve the pruning budget inputs for one provider call.

    ``context_window`` is the runtime-resolved catalog window (``None`` when the
    catalog does not describe the model); the caller passes whatever it resolved
    so the unsized case still produces an explicit ``compaction_reason`` rather
    than a silent unbounded view. ``anchor_tokens`` is the last provider-reported
    context size (``provider_usage.latest``), when one is usable. The knobs
    themselves live in the policy.
    """
    compaction = config.compaction if config is not None else RuntimeCompactionConfig()
    return CompactionBudget(
        context_window=context_window,
        threshold_tokens=compaction.threshold_tokens,
        reserve_tokens=compaction.reserve_tokens,
        anchor_tokens=anchor_tokens,
    )


__all__ = [
    "compaction_budget_from_config",
    "context_window_config_from_policy",
    "context_window_policy_from_config",
]
