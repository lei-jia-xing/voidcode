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
    lowest_supported_effort,
    map_effort_for_provider,
    normalize_reasoning_effort,
    provider_supports_reasoning_effort,
)


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


def test_clamp_effort_to_supported_returns_off_unchanged() -> None:
    assert clamp_effort_to_supported("off", ("low", "high")) == "off"


def test_clamp_effort_to_supported_returns_unchanged_when_effort_supported() -> None:
    assert clamp_effort_to_supported("medium", ("minimal", "medium", "max")) == "medium"


def test_clamp_effort_to_supported_snaps_down_to_nearest_supported() -> None:
    assert clamp_effort_to_supported("high", ("minimal", "low", "medium")) == "medium"


def test_clamp_effort_to_supported_snaps_up_when_below_all_supported() -> None:
    assert clamp_effort_to_supported("low", ("high", "xhigh", "max")) == "high"


def test_lowest_supported_effort_picks_the_cheapest_canonical_level() -> None:
    assert lowest_supported_effort(("medium", "low", "max")) == "low"


@pytest.mark.parametrize("supported", [None, (), ("banana",)])
def test_lowest_supported_effort_reports_no_level(supported: tuple[str, ...] | None) -> None:
    assert lowest_supported_effort(supported) is None


def test_map_effort_for_provider_sends_lowest_supported_level_for_off() -> None:
    # New promise: an explicit "off" no longer sends the literal "none"; the model
    # gets the least reasoning it actually supports. `"none"` is accepted by only a
    # handful of upstreams, while the lowest supported level is the common
    # convention (107 of the 259 catalog models VoidCode ships disable that way).
    assert map_effort_for_provider(
        provider_name="openai",
        effort=REASONING_EFFORT_OFF,
        supported_levels=("low", "medium", "high"),
    ) == {"reasoning_effort": "low"}


def test_map_effort_for_provider_omits_effort_for_off_without_supported_levels() -> None:
    assert map_effort_for_provider(provider_name="openai", effort=REASONING_EFFORT_OFF) == {}


def test_map_effort_for_provider_does_not_translate_levels_by_provider_name() -> None:
    # Old promise: the mapping table rewrote levels per provider name ("max" became
    # "xhigh" except for anthropic). New promise: levels are decided by the model's
    # clamp; the mapping only chooses the request field, so providers agree.
    for provider_name in ("openai", "anthropic", "custom"):
        assert map_effort_for_provider(provider_name=provider_name, effort=REASONING_EFFORT_MAX) == {
            "reasoning_effort": REASONING_EFFORT_MAX,
        }


def test_map_effort_for_provider_passes_other_values_through() -> None:
    assert map_effort_for_provider(provider_name="openai", effort="medium") == {
        "reasoning_effort": "medium",
    }


def test_clamp_then_map_snaps_to_the_models_own_levels_for_a_shipped_model() -> None:
    # Real catalog data: openai/gpt-5.5 tops out at xhigh, openai/gpt-5.6 offers max.
    gpt_5_5 = static_catalog_metadata("openai", "gpt-5.5")
    assert gpt_5_5 is not None
    assert gpt_5_5.supported_effort_levels == ("low", "medium", "high", "xhigh")
    assert map_effort_for_provider(
        provider_name="openai",
        effort=clamp_effort_to_supported(REASONING_EFFORT_MAX, gpt_5_5.supported_effort_levels),
        supported_levels=gpt_5_5.supported_effort_levels,
    ) == {"reasoning_effort": "xhigh"}

    gpt_5_6 = static_catalog_metadata("openai", "gpt-5.6")
    assert gpt_5_6 is not None
    assert map_effort_for_provider(
        provider_name="openai",
        effort=clamp_effort_to_supported(REASONING_EFFORT_MAX, gpt_5_6.supported_effort_levels),
        supported_levels=gpt_5_6.supported_effort_levels,
    ) == {"reasoning_effort": "max"}


@pytest.mark.parametrize("provider_name", ["zai", "zhipuai"])
def test_map_effort_for_provider_uses_named_provider_binary_for_off(provider_name: str) -> None:
    assert map_effort_for_provider(provider_name=provider_name, effort="off") == {
        "extra_body": {"thinking": {"type": "disabled"}},
    }


@pytest.mark.parametrize("provider_name", ["zai", "zhipuai"])
def test_map_effort_for_provider_uses_named_provider_binary_for_high(provider_name: str) -> None:
    assert map_effort_for_provider(provider_name=provider_name, effort="high") == {
        "extra_body": {"thinking": {"type": "enabled"}},
    }


@pytest.mark.parametrize("effort", CANONICAL_EFFORTS)
def test_map_effort_for_provider_routes_deepseek_levels_through_extra_body(effort: str) -> None:
    # Old promise: DeepSeek levels were rewritten by a hardcoded table
    # (medium -> high). New promise: the field carries whatever the model's clamp
    # decided, so DeepSeek is not special-cased on the level.
    assert map_effort_for_provider(provider_name="deepseek", effort=effort) == {
        "extra_body": {"reasoning_effort": effort},
    }


def test_map_effort_for_provider_routes_deepseek_off_through_the_binary_switch() -> None:
    assert map_effort_for_provider(provider_name="deepseek", effort=REASONING_EFFORT_OFF) == {
        "extra_body": {"thinking": {"type": "disabled"}},
    }


@pytest.mark.parametrize(
    ("effort", "expected"),
    [
        (REASONING_EFFORT_OFF, "low"),
        (REASONING_EFFORT_MINIMAL, "low"),
        (REASONING_EFFORT_LOW, "low"),
        (REASONING_EFFORT_MEDIUM, "medium"),
        (REASONING_EFFORT_HIGH, "high"),
        (REASONING_EFFORT_XHIGH, "high"),
        (REASONING_EFFORT_MAX, "high"),
    ],
)
def test_opencode_go_minimax_m2_7_keeps_its_previous_wire_values(effort: str, expected: str) -> None:
    # The name-keyed ladder is gone, but for the shipped metadata the clamp
    # reproduces exactly the values the removed table produced.
    metadata = static_catalog_metadata("opencode-go", "minimax-m2.7")
    assert metadata is not None
    assert metadata.supported_effort_levels == ("low", "medium", "high")
    assert map_effort_for_provider(
        provider_name="opencode-go",
        effort=clamp_effort_to_supported(effort, metadata.supported_effort_levels),
        supported_levels=metadata.supported_effort_levels,
    ) == {"reasoning_effort": expected}


@pytest.mark.parametrize(
    ("provider_name", "effort", "supported_levels", "expected"),
    [
        ("custom", "low", None, {"reasoning_effort": "low"}),
        ("custom", "max", None, {"reasoning_effort": "max"}),
        ("custom", "off", ("minimal", "high"), {"reasoning_effort": "minimal"}),
        ("opencode-go", "high", ("low", "medium", "high"), {"reasoning_effort": "high"}),
    ],
)
def test_map_effort_for_provider_uses_reasoning_effort_kwarg_for_generic_provider(
    provider_name: str,
    effort: str,
    supported_levels: tuple[str, ...] | None,
    expected: dict[str, object],
) -> None:
    assert (
        map_effort_for_provider(
            provider_name=provider_name,
            effort=effort,
            supported_levels=supported_levels,
        )
        == expected
    )


@pytest.mark.parametrize(
    ("provider_name", "model_name", "expected"),
    [
        # Old promise: the gateway answered per model through a two-name allowlist
        # (`minimax-m2.7`/`minimax-m3` -> True, every other opencode-go model ->
        # False). New promise: a multi-upstream gateway has no provider-level
        # verdict, so it answers None and the shipped catalog decides per model.
        ("opencode-go", "minimax-m2.5", None),
        ("opencode-go", "minimax-m2.7", None),
        ("opencode-go", "minimax-m3", None),
        ("opencode-go", "glm-5", None),
        ("opencode-go", "not-in-any-catalog", None),
        ("deepseek", "deepseek-chat", None),
        ("zai", "glm-4-flash", False),
        ("zai", "glm-5", True),
        ("zhipuai", "glm-4-flash", False),
        ("zhipuai", "glm-5", True),
        ("openai", "gpt-5", None),
        # The surviving denies: single-upstream hosts whose API does not take the
        # field the OpenAI-compatible adapter sends.
        ("qwen", "not-in-any-catalog", False),
        ("kimi", "not-in-any-catalog", False),
        ("minimax", "not-in-any-catalog", False),
    ],
)
def test_provider_supports_reasoning_effort_reports_provider_level_capability(
    provider_name: str,
    model_name: str,
    expected: bool | None,
) -> None:
    assert provider_supports_reasoning_effort(provider_name, model_name) is expected
