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


# DeepSeek graded reasoning-effort levels (canonical -> DeepSeek upstream value).
# Routed via `extra_body` because litellm's deepseek adapter binary-collapses a
# top-level `reasoning_effort` kwarg into `thinking: {type: enabled}`, dropping
# the level. `off` maps to the binary `thinking` disable toggle instead.
_DEEPSEEK_GRADED_EFFORT: dict[str, str] = {
    REASONING_EFFORT_MINIMAL: "low",
    REASONING_EFFORT_LOW: "low",
    REASONING_EFFORT_MEDIUM: "high",
    REASONING_EFFORT_HIGH: "high",
    REASONING_EFFORT_XHIGH: "high",
    REASONING_EFFORT_MAX: "max",
}

# OpenCodeGo minimax reasoning-capable model names (stage-1 routing: m2.5 goes
# through the Anthropic adapter, m2.7 through the OpenAI adapter). Anything else
# on opencode-go (glm/kimi/qwen/mimo/deepseek-v4-*) is gated out upstream.
_OPENCODE_GO_REASONING_MODELS = frozenset({"minimax-m2.5", "minimax-m2.7"})

# OpenCodeGo `minimax-m2.7` (OpenAI route) ladder: canonical -> upstream
# `reasoning_effort`. Upstream has no "max"; xhigh/max clamp to "high". Sent
# via `extra_body` because litellm's openai adapter otherwise rejects or
# collapses a top-level `reasoning_effort` for this model name.
_OPENCODE_GO_M2_7_EFFORT: dict[str, str] = {
    REASONING_EFFORT_OFF: REASONING_EFFORT_LOW,
    REASONING_EFFORT_MINIMAL: REASONING_EFFORT_LOW,
    REASONING_EFFORT_LOW: REASONING_EFFORT_LOW,
    REASONING_EFFORT_MEDIUM: REASONING_EFFORT_MEDIUM,
    REASONING_EFFORT_HIGH: REASONING_EFFORT_HIGH,
    REASONING_EFFORT_XHIGH: REASONING_EFFORT_HIGH,
    REASONING_EFFORT_MAX: REASONING_EFFORT_HIGH,
}


def map_effort_for_provider(*, provider_name: str, model_name: str = "", effort: str) -> dict[str, object]:
    """Map a canonical effort to the request kwargs a provider expects.

    The GLM provider takes a binary `extra_body.thinking.type` of
    "enabled"/"disabled" (keyed on the exact provider name). The DeepSeek
    provider takes graded levels via `extra_body.reasoning_effort` (litellm's
    adapter collapses a top-level `reasoning_effort` kwarg into binary
    `thinking`, so it must ride in `extra_body`); "off" maps to
    `extra_body.thinking.type = disabled`.

    OpenCodeGo maps per-model, and both reasoning-capable models ride in
    `extra_body` because litellm's openai/anthropic adapters otherwise reject
    or collapse a top-level `reasoning_effort` for these model names:
    - `minimax-m2.5` (Anthropic route) always sends
      `extra_body.thinking.type = "adaptive"`: reasoning is mandatory, so
      every effort — including "off" — maps to adaptive and can never be
      disabled.
    - `minimax-m2.7` (OpenAI route) maps the canonical ladder to
      `extra_body.reasoning_effort` of low/medium/high; "off"/"minimal"/"low"
      all map to "low" and "xhigh"/"max" clamp to "high" (no upstream "max").
    - Any other opencode-go model name falls through to the generic
      `reasoning_effort` kwarg handling below (they are gated out upstream by
      `provider_supports_reasoning_effort`, but map defensively).

    Everything else receives a `reasoning_effort` kwarg, with "off" mapped to
    "none" and "max" collapsed to the highest level the provider actually
    accepts.
    """
    if provider_name == "glm":
        thinking_type = "disabled" if effort == REASONING_EFFORT_OFF else "enabled"
        return {"extra_body": {"thinking": {"type": thinking_type}}}

    if provider_name == "deepseek":
        if effort == REASONING_EFFORT_OFF:
            return {"extra_body": {"thinking": {"type": "disabled"}}}
        return {"extra_body": {"reasoning_effort": _DEEPSEEK_GRADED_EFFORT[effort]}}

    if provider_name == "opencode-go":
        model = model_name.strip().lower()
        if model == "minimax-m2.5":
            return {"extra_body": {"thinking": {"type": "adaptive"}}}
        if model == "minimax-m2.7":
            return {"extra_body": {"reasoning_effort": _OPENCODE_GO_M2_7_EFFORT[effort]}}

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

_PROVIDERS_WITHOUT_REASONING_EFFORT = frozenset({"qwen", "kimi", "minimax"})


def provider_supports_reasoning_effort(provider_name: str, model_name: str) -> bool | None:
    """Provider-derived reasoning-effort capability gate (NOT model metadata).

    Returns False for providers whose LiteLLM adapters reject the OpenAI-style
    `reasoning_effort` kwarg (litellm raises `UnsupportedParamsError`) or otherwise
    do not forward it (fail-fast per docs/reasoning-effort-decision.md: Qwen/Kimi/MiniMax).
    GLM is binary (thinking.type), not reasoning_effort: True only for reasoning GLM models.
    OpenCodeGo is per-model: only `minimax-m2.5` (Anthropic adaptive thinking) and
    `minimax-m2.7` (OpenAI reasoning_effort ladder) are reasoning-capable; every other
    opencode-go model (glm/kimi/qwen/mimo/deepseek-v4-*) fails fast.
    Returns None (unknown → passthrough) otherwise.
    """
    provider = provider_name.strip().lower()
    if provider in _PROVIDERS_WITHOUT_REASONING_EFFORT:
        return False
    if provider == "glm":
        model = model_name.strip().lower()
        return True if model.startswith(("glm-5", "glm-z1")) else False
    if provider == "opencode-go":
        return model_name.strip().lower() in _OPENCODE_GO_REASONING_MODELS
    return None
