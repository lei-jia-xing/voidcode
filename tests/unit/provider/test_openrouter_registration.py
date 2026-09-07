from __future__ import annotations

from voidcode.provider.config import (
    LiteLLMProviderConfig,
    ProviderConfigs,
    parse_provider_configs_payload,
    provider_configs_from_env,
    serialize_provider_configs,
)
from voidcode.provider.openrouter import OpenRouterModelProvider
from voidcode.provider.registry import ModelProviderRegistry
from voidcode.provider.resolution import resolve_provider_model


def test_registry_resolves_openrouter_with_slash_model_id() -> None:
    registry = ModelProviderRegistry.with_defaults(
        provider_configs=ProviderConfigs(
            openrouter=LiteLLMProviderConfig(api_key="router-key"),
        )
    )

    resolved = resolve_provider_model(
        "openrouter/anthropic/claude-3.7-sonnet:free",
        registry=registry,
    )

    assert isinstance(registry.resolve("openrouter"), OpenRouterModelProvider)
    assert resolved.selection.provider == "openrouter"
    assert resolved.selection.model == "anthropic/claude-3.7-sonnet:free"
    assert resolved.resolution.source == "builtin"
    config = registry.provider_config("openrouter")
    assert config is not None
    assert config.base_url == "https://openrouter.ai/api/v1"


def test_registry_resolves_openrouter_free_router_without_rewriting() -> None:
    registry = ModelProviderRegistry.with_defaults()

    resolved = resolve_provider_model("openrouter/free", registry=registry)

    assert resolved.selection.raw_model == "openrouter/free"
    assert resolved.selection.provider == "openrouter"
    assert resolved.selection.model == "free"

def test_openrouter_config_reads_env_and_round_trips_without_secret() -> None:
    parsed = parse_provider_configs_payload(
        {"openrouter": {}},
        source="providers",
        env={"OPENROUTER_API_KEY": "router-secret"},
    )

    assert parsed == ProviderConfigs(openrouter=LiteLLMProviderConfig(api_key="router-secret"))
    assert serialize_provider_configs(parsed) == {
        "openrouter": {
            "auth_scheme": "bearer",
        }
    }
    assert provider_configs_from_env({"OPENROUTER_API_KEY": "router-secret"}) == ProviderConfigs(
        openrouter=LiteLLMProviderConfig(api_key="router-secret")
    )


def test_openrouter_auth_uses_litellm_bearer_material() -> None:
    from voidcode.provider.auth import ProviderAuthAuthorizeRequest, ProviderAuthResolver

    resolver = ProviderAuthResolver(providers=ProviderConfigs(openrouter=LiteLLMProviderConfig(api_key="router-secret")))

    methods = resolver.methods("openrouter")
    result = resolver.authorize(ProviderAuthAuthorizeRequest(provider="openrouter"))

    assert methods.default_method == "api_key"
    assert result.status == "authorized"
    assert result.material is not None
    assert result.material.headers == {"Authorization": "Bearer router-secret"}
