"""Model facts choose thinking controls independently of model names."""

import pytest

from voidcode.capabilities.providers.native.anthropic_native import AnthropicMessagesProvider
from voidcode.capabilities.providers.native.model_catalog import ProviderModelMetadata
from voidcode.capabilities.providers.native.thinking_rules import thinking_rule_for
from voidcode.sdk.provider import ProviderTurnRequest


def test_new_google_model_uses_effort_levels_without_name_matching(monkeypatch):
    from voidcode.capabilities.providers.native import model_catalog

    monkeypatch.setattr(
        model_catalog, "static_catalog_metadata", lambda *_: ProviderModelMetadata(api="google-generative-ai", supports_reasoning_effort=True)
    )
    assert thinking_rule_for("google", "new-family-new-model").mode == "google-level"


def test_anthropic_effort_uses_adaptive_sdk_controls(monkeypatch):
    from voidcode.capabilities.providers.native import anthropic_native

    metadata = ProviderModelMetadata(supports_reasoning=True, supports_reasoning_effort=True, supported_effort_levels=("low", "high"))
    monkeypatch.setattr(anthropic_native, "static_catalog_metadata", lambda *_: metadata)
    provider = AnthropicMessagesProvider()
    request = ProviderTurnRequest(messages=(), model_name="new-model", reasoning_effort="high")
    payload = provider._payload(request, stream=False)
    assert payload["thinking"] == {"type": "adaptive"}
    assert payload["output_config"] == {"effort": "high"}
    assert "budget_tokens" not in payload["thinking"]


def test_explicit_off_disables_anthropic_thinking(monkeypatch):
    from voidcode.capabilities.providers.native import anthropic_native

    monkeypatch.setattr(anthropic_native, "static_catalog_metadata", lambda *_: ProviderModelMetadata(supports_reasoning=True))
    payload = AnthropicMessagesProvider()._payload(ProviderTurnRequest(messages=(), model_name="new-model", reasoning_effort="off"), stream=False)
    assert payload["thinking"] == {"type": "disabled"}


@pytest.mark.parametrize(
    ("effort", "supported", "expected"),
    [
        ("medium", ("max", "low"), "low"),
        ("minimal", ("high", "low"), "low"),
        ("max", ("high", "low"), "high"),
        ("off", ("low", "high"), "off"),
        ("high", None, "high"),
        ("high", (), "high"),
        ("high", ("vendor-specific",), "high"),
        ("medium", ("vendor-specific", "high"), "high"),
    ],
)
def test_effort_clamping_uses_canonical_order_and_preserves_unknown_capabilities(effort, supported, expected):
    from voidcode.capabilities.providers.native.reasoning_effort import clamp_effort_to_supported

    assert clamp_effort_to_supported(effort, supported) == expected


def test_anthropic_integer_budget_tokens(monkeypatch):
    from voidcode.capabilities.providers.native import anthropic_native

    metadata = ProviderModelMetadata(supports_reasoning=True)
    monkeypatch.setattr(anthropic_native, "static_catalog_metadata", lambda *_: metadata)
    provider = AnthropicMessagesProvider()
    request = ProviderTurnRequest(messages=(), model_name="claude-3-7-sonnet", reasoning_effort=2048)
    payload = provider._payload(request, stream=False)
    assert payload["thinking"] == {"type": "enabled", "budget_tokens": 2048}


def test_google_integer_thinking_budget():
    from voidcode.capabilities.providers.native.google_native import GoogleGenAIProvider

    provider = GoogleGenAIProvider()
    request = ProviderTurnRequest(messages=(), model_name="gemini-2.5-flash", reasoning_effort=1024)
    config = provider._thinking_config(request)
    assert config is not None
    assert config.thinking_budget == 1024
    assert config.include_thoughts is True

    request_zero = ProviderTurnRequest(messages=(), model_name="gemini-2.5-flash", reasoning_effort=0)
    config_zero = provider._thinking_config(request_zero)
    assert config_zero is not None
    assert config_zero.thinking_budget == 0
    assert config_zero.include_thoughts is False


def test_normalize_reasoning_effort():
    from voidcode.capabilities.providers.native.reasoning_effort import normalize_reasoning_effort

    assert normalize_reasoning_effort("none") == "off"
    assert normalize_reasoning_effort("off") == "off"
    assert normalize_reasoning_effort("high") == "high"
    assert normalize_reasoning_effort(1024) == 1024
    assert normalize_reasoning_effort(0) == 0
    assert normalize_reasoning_effort(None) is None
    with pytest.raises(ValueError):
        normalize_reasoning_effort("ultra")
    with pytest.raises(ValueError):
        normalize_reasoning_effort(-1)
