from __future__ import annotations

"""Canonical reasoning-effort values and provider-specific mapping helpers.

This module is intentionally dependency-free and leaf: it is importable from
`runtime/` without pulling in a provider SDK or adapter, so effort
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


# Providers whose compatible endpoint takes a binary `thinking.type` switch
# instead of a graded `reasoning_effort` value: Z.AI and ZhipuAI (both endpoints
# are read through `extra_body`, because the upstream APIs read these fields from
# the request body rather than as top-level OpenAI SDK parameters).
_BINARY_THINKING_PROVIDERS = frozenset({"zai", "zhipuai"})

# DeepSeek takes graded levels through the request body and turns thinking off
# with the same binary switch.
_EFFORT_IN_REQUEST_BODY_PROVIDERS = frozenset({"deepseek"})


def lowest_supported_effort(supported: tuple[str, ...] | None) -> str | None:
    """Return the lowest canonical level in `supported`, or None if it lists none."""
    if not supported:
        return None
    canonical_by_name = {level: index for index, level in enumerate(CANONICAL_EFFORTS)}
    levels = [level for level in supported if level in canonical_by_name]
    if not levels:
        return None
    return min(levels, key=canonical_by_name.__getitem__)


def map_effort_for_provider(
    *,
    provider_name: str,
    effort: str,
    supported_levels: tuple[str, ...] | None = None,
) -> dict[str, object]:
    """Map an already-clamped canonical effort to the request kwargs a provider expects.

    The *level* is decided by `clamp_effort_to_supported` from the model's own
    metadata; this function only chooses which request field carries it and which
    known binary thinking switch applies:

    - Z.AI / ZhipuAI: binary `extra_body.thinking.type` of "enabled"/"disabled".
    - DeepSeek: graded level through `extra_body.reasoning_effort`, disabled
      through `extra_body.thinking.type = "disabled"`.
    - Everything else: a top-level `reasoning_effort` kwarg.

    `off` is resolved by `explicit_off_kwargs`, which never sends a bare "none" to
    a model that does not accept it.
    """
    if effort == REASONING_EFFORT_OFF:
        return explicit_off_kwargs(provider_name=provider_name, supported_levels=supported_levels)
    if provider_name in _BINARY_THINKING_PROVIDERS:
        return {"extra_body": {"thinking": {"type": "enabled"}}}
    if provider_name in _EFFORT_IN_REQUEST_BODY_PROVIDERS:
        return {"extra_body": {"reasoning_effort": effort}}
    return {"reasoning_effort": effort}


def explicit_off_kwargs(
    *,
    provider_name: str,
    supported_levels: tuple[str, ...] | None,
) -> dict[str, object]:
    """Resolve the request kwargs for an explicit `off` (do not reason).

    The known binary disable forms win: Z.AI/ZhipuAI and DeepSeek turn thinking
    off with `thinking.type = "disabled"`. For every other provider the hint means
    "reason as little as this model can", so the lowest level the model actually
    supports is sent. That follows the dominant convention in Oh My Pi's catalog:
    107 of the 259 models VoidCode ships declare a `lowest-effort` disable mode and
    only 4 accept a literal `"none"`; OMP never sends "none" to the former
    (`packages/ai/.../tBt` writes the vendor switch, and `reasoningDisableMode:
    "lowest-effort"` substitutes the lowest supported effort). A model that lists
    no supported level gets no effort parameter at all.
    """
    if provider_name in _BINARY_THINKING_PROVIDERS or provider_name in _EFFORT_IN_REQUEST_BODY_PROVIDERS:
        return {"extra_body": {"thinking": {"type": "disabled"}}}
    lowest = lowest_supported_effort(supported_levels)
    if lowest is None:
        return {}
    return {"reasoning_effort": lowest}


_PROVIDERS_WITHOUT_REASONING_EFFORT = frozenset({"qwen", "kimi", "minimax"})


def provider_supports_reasoning_effort(provider_name: str, model_name: str) -> bool | None:
    """Provider-level fallback for reasoning-effort capability.

    Model metadata is the authority: `validate_reasoning_effort_capability` /
    `resolve_reasoning_effort_capability` consult the resolved model's own
    `supports_reasoning_effort` first and only fall back to this provider-level
    answer when the model metadata is silent.

    Returns False for single-upstream providers whose API does not take the
    OpenAI-style `reasoning_effort` field the OpenAI-compatible adapter sends
    (Qwen/Kimi/MiniMax are explicitly unsupported here).
    Z.AI and ZhipuAI are binary (thinking.type), not reasoning_effort: True only for reasoning GLM models.
    Returns None (unknown → passthrough, reported as `forwarded_unverified`)
    otherwise - including every `opencode-go` model, because that gateway forwards
    to whichever upstream serves the model, so a model the shipped catalog does not
    describe must not be judged by its provider name. The shipped catalog answers
    for the opencode-go models it lists (35 of the gateway's 38).
    """
    provider = provider_name.strip().lower()
    if provider in _PROVIDERS_WITHOUT_REASONING_EFFORT:
        return False
    if provider in {"zai", "zhipuai"}:
        model = model_name.strip().lower()
        return True if model.startswith(("glm-5", "glm-z1")) else False
    return None
