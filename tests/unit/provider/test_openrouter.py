from __future__ import annotations

from voidcode.provider.config import ProviderEndpointConfig
from voidcode.provider.openai_native import OpenAIChatCompletionsProvider
from voidcode.provider.openrouter import OpenRouterModelProvider


def test_openrouter_defaults_to_openai_compatible_gateway() -> None:
    provider = OpenRouterModelProvider()

    config = provider.provider_config()

    assert config.api_key_env_var == "OPENROUTER_API_KEY"
    assert config.base_url == "https://openrouter.ai/api/v1"
    assert config.discovery_base_url == "https://openrouter.ai/api/v1/models"
    assert config.model_map == {}
    turn_provider = provider.turn_provider()
    assert isinstance(turn_provider, OpenAIChatCompletionsProvider)
    assert turn_provider.name == "openrouter"


def test_openrouter_preserves_explicit_endpoint_and_credentials() -> None:
    provider = OpenRouterModelProvider(
        config=ProviderEndpointConfig(
            api_key="router-key",
            base_url="https://router.example.test/v1",
            model_map={"alias": "provider/model"},
        )
    )

    config = provider.provider_config()

    assert config.api_key == "router-key"
    assert config.api_key_env_var == "OPENROUTER_API_KEY"
    assert config.base_url == "https://router.example.test/v1"
    assert config.discovery_base_url is None
    assert config.model_map == {"alias": "provider/model"}


def test_openrouter_turn_provider_uses_resolved_endpoint_config() -> None:
    provider = OpenRouterModelProvider(config=ProviderEndpointConfig(api_key="router-key"))

    turn_provider = provider.turn_provider()

    assert isinstance(turn_provider, OpenAIChatCompletionsProvider)
    assert turn_provider.config == provider.provider_config()
