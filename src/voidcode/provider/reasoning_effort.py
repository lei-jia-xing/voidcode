from __future__ import annotations

"""Canonical reasoning-effort values and provider-specific mapping helpers.

This module is intentionally dependency-free and leaf: it is importable from
`runtime/` without pulling in LiteLLM or any provider adapter, so effort
normalization, clamping, and provider mapping can be shared across the
runtime control plane and provider backends.
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


def map_effort_for_provider(*, provider_name: str, effort: str) -> dict[str, object]:
    """Map a canonical effort to the request kwargs a provider expects.

    The GLM provider takes a binary `extra_body.thinking.type` of
    "enabled"/"disabled" (keyed on the exact provider name). Everything else
    receives a `reasoning_effort` kwarg, with "off" mapped to "none" and
    "max" collapsed to the highest level the provider actually accepts.
    """
    if provider_name == "glm":
        thinking_type = "disabled" if effort == REASONING_EFFORT_OFF else "enabled"
        return {"extra_body": {"thinking": {"type": thinking_type}}}

    if effort == REASONING_EFFORT_OFF:
        mapped = "none"
    elif effort == REASONING_EFFORT_MAX:
        if provider_name == "anthropic":
            mapped = REASONING_EFFORT_MAX
        elif provider_name == "google":
            mapped = REASONING_EFFORT_HIGH
        else:
            mapped = REASONING_EFFORT_XHIGH
    else:
        mapped = effort
    return {"reasoning_effort": mapped}
