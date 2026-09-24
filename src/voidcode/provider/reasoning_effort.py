from __future__ import annotations

from .thinking_rules import DisableMode, ThinkingRule

"""Canonical reasoning-effort values and the data-driven thinking-request mapping.

This module is intentionally dependency-free and leaf: it is importable from
`runtime/` without pulling in a provider SDK or adapter, so effort
normalization, clamping, and provider mapping can be shared across the
runtime control plane and provider backends. The provider-specific mapping is
*data*: which field carries the effort, which vendor spelling turns reasoning
off and which budgets apply come from `thinking_rules.json`.
"""
REASONING_EFFORT_OFF = "off"
REASONING_EFFORT_MINIMAL = "minimal"
REASONING_EFFORT_LOW = "low"
REASONING_EFFORT_MEDIUM = "medium"
REASONING_EFFORT_HIGH = "high"
REASONING_EFFORT_XHIGH = "xhigh"
REASONING_EFFORT_MAX = "max"

# Ordered clamping ladder (off is NOT in it).
CANONICAL_EFFORTS: tuple[str, ...] = (
    REASONING_EFFORT_MINIMAL,
    REASONING_EFFORT_LOW,
    REASONING_EFFORT_MEDIUM,
    REASONING_EFFORT_HIGH,
    REASONING_EFFORT_XHIGH,
    REASONING_EFFORT_MAX,
)

ALL_EFFORTS: tuple[str, ...] = (REASONING_EFFORT_OFF, *CANONICAL_EFFORTS)


def normalize_reasoning_effort(value: object) -> str:
    """Validate and normalize a reasoning-effort value to its canonical form.

    Case-sensitive, no aliases, no trimming: only the exact members of
    `ALL_EFFORTS` are accepted.
    """
    if not isinstance(value, str) or not value or value not in ALL_EFFORTS:
        raise ValueError(f"reasoning_effort must be one of: {', '.join(ALL_EFFORTS)}; got {value!r}")
    return value


def clamp_effort_to_supported(effort: str, supported: tuple[str, ...] | None) -> str:
    """Clamp `effort` to the nearest level the provider/model actually supports.

    `supported` is an ordered tuple of canonical efforts (excluding "off").
    Returns `effort` unchanged when `supported` is None, when `effort` is
    "off", or when `effort` is already supported. Otherwise snaps DOWN to the
    nearest supported level at or below `effort`; if `effort` is below every
    supported level, snaps UP to the lowest supported level.
    """
    if supported is None:
        return effort
    if effort == REASONING_EFFORT_OFF:
        return effort
    if effort in supported:
        return effort

    supported_canonical = tuple(level for level in supported if level in CANONICAL_EFFORTS)
    if not supported_canonical:
        return effort

    canonical_by_name = {level: index for index, level in enumerate(CANONICAL_EFFORTS)}
    effort_index = canonical_by_name.get(effort)
    if effort_index is None:
        return effort

    candidates = [level for level in supported_canonical if canonical_by_name[level] <= effort_index]
    if candidates:
        return max(candidates, key=canonical_by_name.__getitem__)
    return min(supported_canonical, key=canonical_by_name.__getitem__)


def lowest_supported_effort(supported: tuple[str, ...] | None) -> str | None:
    """Return the lowest canonical level in `supported`, or None if it lists none."""
    if not supported:
        return None
    canonical_by_name = {level: index for index, level in enumerate(CANONICAL_EFFORTS)}
    levels = [level for level in supported if level in canonical_by_name]
    if not levels:
        return None
    return min(levels, key=canonical_by_name.__getitem__)


def reasoning_kwargs(
    *,
    rule: ThinkingRule,
    effort: str,
    supported_levels: tuple[str, ...] | None,
) -> dict[str, object]:
    """Map a canonical effort to the OpenAI-wire request kwargs the row's format uses.

    ``effort`` is already clamped to the model's own ladder by
    `clamp_effort_to_supported`; this function only chooses the field and the
    vendor spelling. ``off`` is the "reasoning disabled" request state, never a
    ladder member: it resolves through the row's ``disable_mode`` (OMP's
    ``encodeChatCompletionsDisabledReasoning``), and a model whose row says it
    always reasons (``requires_effort``) is clamped to its lowest level instead.
    """
    if effort == REASONING_EFFORT_OFF:
        return disabled_reasoning_kwargs(rule=rule, supported_levels=supported_levels)
    if rule.mode == "binary":
        # The vendor reads its thinking switch from the request body, so the
        # ladder level never reaches the wire as an effort value.
        return _binary_thinking_kwargs(rule.disable_mode, enabled=True)
    return {"reasoning_effort": rule.mapped_effort(effort)}


def disabled_reasoning_kwargs(
    *,
    rule: ThinkingRule,
    supported_levels: tuple[str, ...] | None,
) -> dict[str, object]:
    """Request kwargs for an explicit ``off`` (do not reason), per the row's spelling."""
    if rule.requires_effort:
        # The model always reasons: ask for as little as it can rather than
        # disabling, which its API does not accept (``thinking.requiresEffort``).
        lowest = lowest_supported_effort(supported_levels)
        return {"reasoning_effort": rule.mapped_effort(lowest)} if lowest is not None else {}
    match rule.disable_mode:
        case "lowest-effort":
            lowest = lowest_supported_effort(supported_levels)
            return {"reasoning_effort": rule.mapped_effort(lowest)} if lowest is not None else {}
        case "none-effort":
            return {"reasoning_effort": "none"}
        case "openrouter-enabled-false":
            return {"extra_body": {"reasoning": {"enabled": False}}}
        case _:
            return _binary_thinking_kwargs(rule.disable_mode, enabled=False)


def _binary_thinking_kwargs(disable_mode: DisableMode, *, enabled: bool) -> dict[str, object]:
    """The body shape one binary thinking format uses, for either state."""
    match disable_mode:
        case "zai-thinking-disabled":
            return {"extra_body": {"thinking": {"type": "enabled" if enabled else "disabled"}}}
        case "qwen-enable-thinking-false":
            return {} if enabled else {"extra_body": {"enable_thinking": False}}
        case _:
            return {}


__all__ = [
    "ALL_EFFORTS",
    "CANONICAL_EFFORTS",
    "REASONING_EFFORT_HIGH",
    "REASONING_EFFORT_MAX",
    "REASONING_EFFORT_MEDIUM",
    "REASONING_EFFORT_MINIMAL",
    "REASONING_EFFORT_OFF",
    "REASONING_EFFORT_XHIGH",
    "clamp_effort_to_supported",
    "disabled_reasoning_kwargs",
    "lowest_supported_effort",
    "normalize_reasoning_effort",
    "reasoning_kwargs",
]
