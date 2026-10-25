"""Canonical effort vocabulary and protocol-level request mapping.

Unknown model capability metadata leaves requested effort unchanged. Explicit
levels clamp down through the canonical ladder, or up to its lowest supported level.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .thinking_rules import DisableMode, ThinkingRule

CANONICAL_EFFORTS: tuple[str, ...] = ("minimal", "low", "medium", "high", "xhigh", "max")
ALL_EFFORTS: tuple[str, ...] = ("off", *CANONICAL_EFFORTS)


def normalize_reasoning_effort(value: str | int | None) -> str | int | None:
    """Validate and normalize a reasoning-effort value to its canonical form."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"reasoning_effort must be one of: {', '.join(ALL_EFFORTS)}, a non-negative integer budget, or None; got {value!r}")
    if isinstance(value, int):
        if value < 0:
            raise ValueError(f"reasoning_effort budget must be >= 0; got {value!r}")
        return value
    if value == "none":
        return "off"
    if not isinstance(value, str) or value not in ALL_EFFORTS:
        raise ValueError(f"reasoning_effort must be one of: {', '.join(ALL_EFFORTS)}; got {value!r}")
    return value


def clamp_effort_to_supported(effort: str, supported: tuple[str, ...] | None) -> str:
    """Clamp `effort` to the nearest level the provider/model actually supports.

    `supported` lists advertised canonical efforts (excluding "off").
    Returns `effort` unchanged when `supported` is None, when `effort` is
    "off", or when `effort` is already supported. Otherwise snaps DOWN to the
    nearest supported level at or below `effort`; if `effort` is below every
    supported level, snaps UP to the lowest supported level.
    """
    if supported is None or effort == "off" or effort in supported or effort not in CANONICAL_EFFORTS:
        return effort
    candidates = [level for level in CANONICAL_EFFORTS[: CANONICAL_EFFORTS.index(effort) + 1] if level in supported]
    return candidates[-1] if candidates else lowest_supported_effort(supported) or effort


def lowest_supported_effort(supported: tuple[str, ...] | None) -> str | None:
    """Return the lowest canonical supported level, preserving absence as None."""
    return next((level for level in CANONICAL_EFFORTS if level in (supported or ())), None)


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
    ladder member: it resolves through the protocol default, overridden when
    upstream explicitly declares a none effort.
    """
    if effort == "off":
        return disabled_reasoning_kwargs(rule=rule, supported_levels=supported_levels)
    if rule.mode == "binary":
        # The vendor reads its thinking switch from the request body, so the
        # ladder level never reaches the wire as an effort value.
        return _binary_thinking_kwargs(rule.disable_mode, enabled=True)
    return {"reasoning_effort": effort}


def disabled_reasoning_kwargs(
    *,
    rule: ThinkingRule,
    supported_levels: tuple[str, ...] | None,
) -> dict[str, object]:
    """Request kwargs for an explicit ``off`` (do not reason), per the row's spelling."""
    match rule.disable_mode:
        case "lowest-effort":
            lowest = lowest_supported_effort(supported_levels)
            return {"reasoning_effort": lowest} if lowest is not None else {}
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
    "clamp_effort_to_supported",
    "disabled_reasoning_kwargs",
    "lowest_supported_effort",
    "normalize_reasoning_effort",
    "reasoning_kwargs",
]
