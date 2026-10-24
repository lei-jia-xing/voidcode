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
