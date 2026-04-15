from __future__ import annotations

from voidcode.provider.anthropic import AnthropicModelProvider
from voidcode.provider.config import (
    GoogleProviderAuthConfig,
    GoogleProviderConfig,
    LiteLLMProviderConfig,
    ProviderConfigs,
)
from voidcode.provider.copilot import CopilotModelProvider
from voidcode.provider.google import GoogleModelProvider
from voidcode.provider.litellm import LiteLLMModelProvider
from voidcode.provider.openai import OpenAIModelProvider
from voidcode.provider.registry import ModelProviderRegistry, StaticModelProvider


def test_registry_registers_concrete_provider_adapters() -> None:
    registry = ModelProviderRegistry.with_defaults()

    assert isinstance(registry.resolve("openai"), OpenAIModelProvider)
    assert isinstance(registry.resolve("anthropic"), AnthropicModelProvider)
    assert isinstance(registry.resolve("google"), GoogleModelProvider)
    assert isinstance(registry.resolve("copilot"), CopilotModelProvider)
    assert isinstance(registry.resolve("litellm"), LiteLLMModelProvider)


def test_registry_resolves_unknown_provider_to_litellm_adapter() -> None:
    registry = ModelProviderRegistry.with_defaults()

    resolved = registry.resolve("custom")

    assert isinstance(resolved, LiteLLMModelProvider)
    assert resolved.name == "custom"


def test_registry_unknown_provider_reuses_default_litellm_config() -> None:
    litellm_config = LiteLLMProviderConfig(
        api_key="token",
        base_url="http://localhost:4000",
    )
    registry = ModelProviderRegistry.with_defaults(
        provider_configs=ProviderConfigs(litellm=litellm_config)
    )

    resolved = registry.resolve("custom")

    assert isinstance(resolved, LiteLLMModelProvider)
    assert resolved.config == litellm_config


def test_registry_unknown_provider_prefers_custom_provider_config() -> None:
    default_config = LiteLLMProviderConfig(api_key="default", base_url="http://localhost:4000")
    custom_config = LiteLLMProviderConfig(api_key="custom", base_url="http://localhost:11434/v1")
    registry = ModelProviderRegistry.with_defaults(
        provider_configs=ProviderConfigs(
            litellm=default_config,
            custom={"llama-local": custom_config},
        )
    )

    resolved = registry.resolve("llama-local")

    assert isinstance(resolved, LiteLLMModelProvider)
    assert resolved.name == "llama-local"
    assert resolved.config == custom_config


def test_registry_keeps_existing_opencode_static_provider_behavior() -> None:
    registry = ModelProviderRegistry.with_defaults()

    resolved = registry.resolve("opencode")

    assert isinstance(resolved, StaticModelProvider)
    assert resolved.name == "opencode"


def test_registry_refresh_available_models_prefers_model_map_aliases() -> None:
    litellm_config = LiteLLMProviderConfig(
        base_url="http://127.0.0.1:65534",
        auth_scheme="none",
        model_map={
            "gpt-4o": "openrouter/openai/gpt-4o",
            "coder": "ollama/qwen2.5-coder:latest",
        },
    )
    registry = ModelProviderRegistry.with_defaults(
        provider_configs=ProviderConfigs(litellm=litellm_config)
    )

    models = registry.refresh_available_models("litellm")

    assert models[:2] == ("gpt-4o", "coder")
    assert "openrouter/openai/gpt-4o" in models
    assert "ollama/qwen2.5-coder:latest" in models
    assert registry.available_models("litellm") == models
    catalog = registry.provider_catalog("litellm")
    assert catalog is not None
    assert catalog.last_refresh_status in {"ok", "failed", "skipped"}


def test_registry_refresh_custom_provider_uses_custom_config() -> None:
    custom_config = LiteLLMProviderConfig(
        base_url="http://127.0.0.1:65534",
        auth_scheme="none",
        model_map={"coder": "ollama/qwen2.5-coder:latest"},
    )
    registry = ModelProviderRegistry.with_defaults(
        provider_configs=ProviderConfigs(custom={"llama-local": custom_config})
    )

    models = registry.refresh_available_models("llama-local")

    assert models[0] == "coder"
    assert "ollama/qwen2.5-coder:latest" in models
    assert registry.available_models("llama-local") == models
    catalog = registry.provider_catalog("llama-local")
    assert catalog is not None
    assert catalog.provider == "llama-local"


def test_registry_google_provider_config_uses_google_api_key_header_for_api_key_auth() -> None:
    registry = ModelProviderRegistry.with_defaults(
        provider_configs=ProviderConfigs(
            google=GoogleProviderConfig(
                auth=GoogleProviderAuthConfig(method="api_key", api_key="AIza-test")
            )
        )
    )

    config = registry.provider_config("google")

    assert config == LiteLLMProviderConfig(
        api_key="AIza-test",
        auth_header="x-goog-api-key",
        auth_scheme="token",
    )
