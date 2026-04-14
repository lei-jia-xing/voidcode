from __future__ import annotations

from voidcode.provider.anthropic import AnthropicModelProvider
from voidcode.provider.copilot import CopilotModelProvider
from voidcode.provider.google import GoogleModelProvider
from voidcode.provider.litellm import LiteLLMModelProvider
from voidcode.provider.openai import OpenAIModelProvider
from voidcode.provider.registry import ModelProviderRegistry, StaticModelProvider


def test_model_provider_registry_with_defaults_registers_concrete_provider_adapters() -> None:
    registry = ModelProviderRegistry.with_defaults()

    assert isinstance(registry.resolve("openai"), OpenAIModelProvider)
    assert isinstance(registry.resolve("anthropic"), AnthropicModelProvider)
    assert isinstance(registry.resolve("google"), GoogleModelProvider)
    assert isinstance(registry.resolve("copilot"), CopilotModelProvider)
    assert isinstance(registry.resolve("litellm"), LiteLLMModelProvider)


def test_model_provider_registry_with_defaults_preserves_static_fallback_for_unknown_provider() -> (
    None
):
    registry = ModelProviderRegistry.with_defaults()

    resolved = registry.resolve("custom")

    assert isinstance(resolved, StaticModelProvider)
    assert resolved.name == "custom"


def test_model_provider_registry_keeps_existing_opencode_static_provider_behavior() -> None:
    registry = ModelProviderRegistry.with_defaults()

    resolved = registry.resolve("opencode")

    assert isinstance(resolved, StaticModelProvider)
    assert resolved.name == "opencode"
