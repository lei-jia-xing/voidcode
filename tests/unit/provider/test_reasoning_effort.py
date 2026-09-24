from __future__ import annotations

import pytest

from voidcode.provider.model_catalog import static_catalog_metadata
from voidcode.provider.reasoning_effort import (
    ALL_EFFORTS,
    CANONICAL_EFFORTS,
    REASONING_EFFORT_HIGH,
    REASONING_EFFORT_LOW,
    REASONING_EFFORT_MAX,
    REASONING_EFFORT_MEDIUM,
    REASONING_EFFORT_MINIMAL,
    REASONING_EFFORT_OFF,
    REASONING_EFFORT_XHIGH,
    clamp_effort_to_supported,
    disabled_reasoning_kwargs,
    lowest_supported_effort,
    normalize_reasoning_effort,
    reasoning_kwargs,
)
from voidcode.provider.thinking_rules import thinking_rule_for
from voidcode.runtime.provider_metadata import resolve_reasoning_effort_capability


def test_constants_match_spec() -> None:
    assert REASONING_EFFORT_OFF == "off"
    assert REASONING_EFFORT_MINIMAL == "minimal"
    assert REASONING_EFFORT_LOW == "low"
    assert REASONING_EFFORT_MEDIUM == "medium"
    assert REASONING_EFFORT_HIGH == "high"
    assert REASONING_EFFORT_XHIGH == "xhigh"
    assert REASONING_EFFORT_MAX == "max"
    assert CANONICAL_EFFORTS == (
        REASONING_EFFORT_MINIMAL,
        REASONING_EFFORT_LOW,
        REASONING_EFFORT_MEDIUM,
        REASONING_EFFORT_HIGH,
        REASONING_EFFORT_XHIGH,
        REASONING_EFFORT_MAX,
    )
    assert REASONING_EFFORT_OFF not in CANONICAL_EFFORTS
    assert ALL_EFFORTS == (REASONING_EFFORT_OFF, *CANONICAL_EFFORTS)


@pytest.mark.parametrize(
    "value",
    [REASONING_EFFORT_OFF, *CANONICAL_EFFORTS],
)
def test_normalize_reasoning_effort_accepts_all_efforts(value: str) -> None:
    assert normalize_reasoning_effort(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "none",
        "None",
        "banana",
        "High",
        "high ",
        "",
        1,
        None,
        True,
    ],
)
def test_normalize_reasoning_effort_rejects_invalid_values(value: object) -> None:
    with pytest.raises(ValueError, match="reasoning_effort must be one of:"):
        normalize_reasoning_effort(value)


def test_clamp_effort_to_supported_returns_unchanged_when_supported_is_none() -> None:
    assert clamp_effort_to_supported("high", None) == "high"


def test_clamp_effort_to_supported_snaps_down_to_nearest_supported() -> None:
    assert clamp_effort_to_supported("high", ("minimal", "low", "medium")) == "medium"


def test_clamp_effort_to_supported_snaps_up_when_below_all_supported() -> None:
    assert clamp_effort_to_supported("low", ("high", "xhigh", "max")) == "high"


def test_lowest_supported_effort_picks_the_cheapest_canonical_level() -> None:
    assert lowest_supported_effort(("medium", "low", "max")) == "low"


@pytest.mark.parametrize("supported", [None, (), ("banana",)])
def test_lowest_supported_effort_reports_no_level(supported: tuple[str, ...] | None) -> None:
    assert lowest_supported_effort(supported) is None


def test_off_resolves_through_the_rows_disable_spelling() -> None:
    """`off` is the "reasoning disabled" request state, not a ladder member: each
    row's spelling decides the wire shape (OMP's encodeChatCompletionsDisabledReasoning)."""
    # zai's format is a binary body switch.
    assert disabled_reasoning_kwargs(
        rule=thinking_rule_for("zai", "glm-5"),
        supported_levels=("minimal", "low", "high"),
    ) == {"extra_body": {"thinking": {"type": "disabled"}}}
    # The openai gpt-5.6 revisions take the literal "none".
    assert disabled_reasoning_kwargs(
        rule=thinking_rule_for("openai", "gpt-5.6-luna"),
        supported_levels=("low", "high"),
    ) == {"reasoning_effort": "none"}
    # A row with no spelling of its own asks for the least the model supports.
    assert disabled_reasoning_kwargs(
        rule=thinking_rule_for("deepseek", "deepseek-v4-pro"),
        supported_levels=("low", "high", "max"),
    ) == {"reasoning_effort": "low"}
    # openrouter's format disables with a body flag.
    assert disabled_reasoning_kwargs(rule=thinking_rule_for("openrouter", "any-model"), supported_levels=None) == {
        "extra_body": {"reasoning": {"enabled": False}},
    }
    # A model that lists no level at all gets no effort parameter.
    assert disabled_reasoning_kwargs(rule=thinking_rule_for("groq", "any-model"), supported_levels=None) == {}


def test_a_model_that_always_reasons_is_clamped_instead_of_disabled() -> None:
    rule = thinking_rule_for("xai", "grok-4.20-0309-reasoning")

    assert rule.requires_effort is True
    assert disabled_reasoning_kwargs(rule=rule, supported_levels=("low", "high")) == {"reasoning_effort": "low"}
    assert reasoning_kwargs(rule=rule, effort="off", supported_levels=("low", "high")) == {"reasoning_effort": "low"}


def test_enabled_effort_uses_the_rows_field_and_effort_map() -> None:
    # fireworks remaps minimal -> none (thinking-effort-map).
    assert reasoning_kwargs(
        rule=thinking_rule_for("fireworks", "accounts/fireworks/models/glm-5p3"),
        effort="minimal",
        supported_levels=None,
    ) == {"reasoning_effort": "none"}
    # xai remaps minimal/xhigh/max; unmapped levels pass through.
    xai = thinking_rule_for("xai", "grok-4.5")
    assert reasoning_kwargs(rule=xai, effort="max", supported_levels=None) == {"reasoning_effort": "high"}
    assert reasoning_kwargs(rule=xai, effort="medium", supported_levels=None) == {"reasoning_effort": "medium"}
    # zai's format is the binary switch, never reasoning_effort.
    assert reasoning_kwargs(rule=thinking_rule_for("zai", "glm-5"), effort="high", supported_levels=None) == {
        "extra_body": {"thinking": {"type": "enabled"}},
    }
    # A row with neither a map nor a format sends the clamped level verbatim.
    assert reasoning_kwargs(rule=thinking_rule_for("deepseek", "deepseek-v4-pro"), effort="high", supported_levels=None) == {
        "reasoning_effort": "high",
    }


def test_clamp_then_map_snaps_to_the_models_own_levels_for_a_shipped_model() -> None:
    # Real catalog data: openai/gpt-5.5 tops out at xhigh, openai/gpt-5.6 offers max.
    gpt_5_5 = static_catalog_metadata("openai", "gpt-5.5")
    assert gpt_5_5 is not None
    assert gpt_5_5.supported_effort_levels == ("low", "medium", "high", "xhigh")
    assert reasoning_kwargs(
        rule=thinking_rule_for("openai", "gpt-5.5"),
        effort=clamp_effort_to_supported(REASONING_EFFORT_MAX, gpt_5_5.supported_effort_levels),
        supported_levels=gpt_5_5.supported_effort_levels,
    ) == {"reasoning_effort": "xhigh"}

    gpt_5_6 = static_catalog_metadata("openai", "gpt-5.6")
    assert gpt_5_6 is not None
    assert reasoning_kwargs(
        rule=thinking_rule_for("openai", "gpt-5.6"),
        effort=clamp_effort_to_supported(REASONING_EFFORT_MAX, gpt_5_6.supported_effort_levels),
        supported_levels=gpt_5_6.supported_effort_levels,
    ) == {"reasoning_effort": "max"}


def test_capability_is_the_models_own_and_has_no_provider_level_fallback() -> None:
    """A provider name never decides capability any more: a model the catalog
    describes is judged by its row, and one it does not is simply unknown."""
    unknown = resolve_reasoning_effort_capability(
        provider_name="qwen",
        model_name="not-in-any-catalog",
        model_metadata=None,
    )
    assert (unknown.supported, unknown.source) == (None, "unknown")

    described = static_catalog_metadata("zai", "glm-5.3")
    assert described is not None
    verdict = resolve_reasoning_effort_capability(
        provider_name="zai",
        model_name="glm-5.3",
        model_metadata=described,
    )
    assert verdict.source == "model_metadata"
    assert verdict.supported == described.supports_reasoning_effort
