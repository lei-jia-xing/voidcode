from __future__ import annotations

from types import SimpleNamespace

from voidcode.provider.config import LiteLLMProviderConfig
from voidcode.provider.openrouter import (
    OpenRouterModelProvider,
    OpenRouterSingleAgentProvider,
)


def test_openrouter_defaults_to_openai_compatible_gateway() -> None:
    provider = OpenRouterModelProvider()

    config = provider.provider_config()

    assert config.api_key_env_var == "OPENROUTER_API_KEY"
    assert config.base_url == "https://openrouter.ai/api/v1"
    assert config.discovery_base_url == "https://openrouter.ai/api/v1/models"
    assert config.model_map == {}
    assert isinstance(provider.turn_provider(), OpenRouterSingleAgentProvider)


def test_openrouter_preserves_explicit_endpoint_and_credentials() -> None:
    provider = OpenRouterModelProvider(
        config=LiteLLMProviderConfig(
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


def test_openrouter_completion_forces_openai_litellm_adapter() -> None:
    provider = OpenRouterSingleAgentProvider(name="openrouter", config=LiteLLMProviderConfig())

    # The request is only used by this hook to calculate optional reasoning
    # kwargs; a request without reasoning leaves the base kwargs unchanged.
    kwargs = provider._completion_kwargs_for_request(SimpleNamespace(reasoning_effort=None))  # type: ignore[arg-type]

    assert kwargs == {"custom_llm_provider": "openai"}
