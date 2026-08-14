from __future__ import annotations

import pytest

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
    map_effort_for_provider,
    normalize_reasoning_effort,
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


def test_map_effort_for_provider_maps_openai_off_to_none() -> None:
    assert map_effort_for_provider(provider_name="openai", model_name="gpt-5", effort="off") == {
        "reasoning_effort": "none",
    }


def test_map_effort_for_provider_maps_openai_max_to_xhigh() -> None:
    assert map_effort_for_provider(provider_name="openai", model_name="gpt-5", effort="max") == {
        "reasoning_effort": "xhigh",
    }


def test_map_effort_for_provider_maps_anthropic_max_to_max() -> None:
    assert map_effort_for_provider(provider_name="anthropic", model_name="claude-sonnet", effort="max") == {
        "reasoning_effort": "max",
    }


def test_map_effort_for_provider_maps_google_max_to_high() -> None:
    assert map_effort_for_provider(provider_name="google", model_name="gemini-3", effort="max") == {
        "reasoning_effort": "high",
    }


def test_map_effort_for_provider_passes_other_values_through() -> None:
    assert map_effort_for_provider(provider_name="openai", model_name="gpt-5", effort="medium") == {
        "reasoning_effort": "medium",
    }


def test_map_effort_for_provider_uses_glm_binary_for_glm_provider_off() -> None:
    assert map_effort_for_provider(provider_name="glm", model_name="glm-5", effort="off") == {
        "extra_body": {"thinking": {"type": "disabled"}},
    }


def test_map_effort_for_provider_uses_glm_binary_for_glm_provider_high() -> None:
    assert map_effort_for_provider(provider_name="glm", model_name="glm-5", effort="high") == {
        "extra_body": {"thinking": {"type": "enabled"}},
    }


def test_map_effort_for_provider_detects_glm_via_model_prefix() -> None:
    assert map_effort_for_provider(provider_name="custom", model_name="glm-z1", effort="low") == {
        "extra_body": {"thinking": {"type": "enabled"}},
    }


def test_map_effort_for_provider_detects_glm_via_glm_5_prefix() -> None:
    assert map_effort_for_provider(provider_name="custom", model_name="glm-5-turbo", effort="off") == {
        "extra_body": {"thinking": {"type": "disabled"}},
    }


def test_map_effort_for_provider_uses_reasoning_effort_kwarg_for_non_glm() -> None:
    assert map_effort_for_provider(provider_name="custom", model_name="some-model", effort="high") == {
        "reasoning_effort": "high",
    }
