from __future__ import annotations

from urllib.error import URLError
from urllib.request import Request

import pytest

from voidcode.provider import model_catalog
from voidcode.provider.anthropic_native import AnthropicMessagesProvider
from voidcode.provider.config import (
    GoogleProviderAuthConfig,
    GoogleProviderConfig,
    OpenAICompatibleProviderConfig,
    OpenAIProviderConfig,
    ProviderConfigs,
    ProviderEndpointConfig,
    openai_compatible_endpoint_config,
)
from voidcode.provider.copilot import GithubCopilotModelProvider
from voidcode.provider.endpoint import OpenAIEndpointProvider
from voidcode.provider.google import GoogleModelProvider
from voidcode.provider.naming import UnknownProviderIdError
from voidcode.provider.openai import OpenAIModelProvider
from voidcode.provider.provider_config import anthropic_compatible_endpoint_config
from voidcode.provider.registry import (
    AnthropicCompatibleModelProvider,
    ModelProviderRegistry,
    OpenAICompatibleModelProvider,
)
from voidcode.provider.resolution import resolve_provider_model


def test_registry_anthropic_wire_vendors_share_one_adapter() -> None:
    registry = ModelProviderRegistry.with_defaults()

    for provider_id, base_url in (
        ("kimi-code", "https://api.kimi.com/coding"),
        ("minimax-cn", "https://api.minimaxi.com/anthropic"),
    ):
        provider = registry.resolve(provider_id)

        assert isinstance(provider, AnthropicCompatibleModelProvider)
        assert provider.name == provider_id
        config = registry.provider_config(provider_id)
        assert config is not None
        assert config.base_url == base_url
        assert config.wire == "anthropic-messages"
        assert isinstance(provider.turn_provider(), AnthropicMessagesProvider)


def test_anthropic_wire_endpoint_config_rejects_unknown_vendor_names() -> None:
    with pytest.raises(ValueError, match="Unknown Anthropic-wire provider"):
        anthropic_compatible_endpoint_config("unknown-vendor", None)


def test_registry_registers_concrete_provider_adapters() -> None:
    registry = ModelProviderRegistry.with_defaults()

    assert isinstance(registry.resolve("openai"), OpenAIModelProvider)
    assert isinstance(registry.resolve("anthropic"), AnthropicCompatibleModelProvider)
    assert isinstance(registry.resolve("google"), GoogleModelProvider)
    assert isinstance(registry.resolve("github-copilot"), GithubCopilotModelProvider)
    assert isinstance(registry.resolve("endpoint"), OpenAIEndpointProvider)


@pytest.mark.parametrize("removed_id", ["grok", "kimi", "opencode", "copilot", "kimi-coding"])
def test_registry_rejects_the_ids_the_rename_removed(removed_id: str) -> None:
    # The rename is a hard cutover: an old id must fail loudly instead of
    # resolving to the provider that replaced it.
    registry = ModelProviderRegistry.with_defaults()

    with pytest.raises(UnknownProviderIdError):
        registry.resolve(removed_id)


def test_registry_canonicalises_provider_id_case() -> None:
    registry = ModelProviderRegistry.with_defaults()

    resolved = registry.resolve_with_metadata("MiniMax")

    assert isinstance(resolved.provider, OpenAICompatibleModelProvider)
    assert resolved.provider.name == "minimax"
    assert resolved.provider_name == "minimax"
    assert resolved.source == "builtin"


def test_registry_declared_custom_provider_resolves_to_endpoint_adapter() -> None:
    custom_config = ProviderEndpointConfig(
        api_key="token",
        base_url="http://localhost:11434/v1",
    )
    registry = ModelProviderRegistry.with_defaults(provider_configs=ProviderConfigs(custom={"llama-local": custom_config}))

    resolved = registry.resolve("llama-local")

    assert isinstance(resolved, OpenAIEndpointProvider)
    assert resolved.name == "llama-local"
    assert resolved.config == custom_config


def test_registry_rejects_undeclared_provider_even_with_endpoint_config() -> None:
    # A declared `providers.endpoint` is reached through the `endpoint` id; it does
    # not make an unknown prefix resolve. This is the fallthrough that used to send
    # an undeclared provider's traffic to a host the user never named.
    endpoint_config = ProviderEndpointConfig(
        api_key="token",
        base_url="http://localhost:4000",
    )
    registry = ModelProviderRegistry.with_defaults(provider_configs=ProviderConfigs(endpoint=endpoint_config))

    with pytest.raises(UnknownProviderIdError) as excinfo:
        _ = registry.resolve("custom")

    assert "unknown provider id 'custom'" in str(excinfo.value)
    assert "providers.custom.custom" in str(excinfo.value)
    assert isinstance(registry.resolve("endpoint"), OpenAIEndpointProvider)


def test_registry_unknown_provider_prefers_custom_provider_config() -> None:
    default_config = ProviderEndpointConfig(api_key="default", base_url="http://localhost:4000")
    custom_config = ProviderEndpointConfig(api_key="custom", base_url="http://localhost:11434/v1")
    registry = ModelProviderRegistry.with_defaults(
        provider_configs=ProviderConfigs(
            endpoint=default_config,
            custom={"llama-local": custom_config},
        )
    )

    resolved = registry.resolve("llama-local")

    assert isinstance(resolved, OpenAIEndpointProvider)
    assert resolved.name == "llama-local"
    assert resolved.config == custom_config


def test_registry_resolve_with_metadata_distinguishes_builtin_and_custom_sources() -> None:
    default_config = ProviderEndpointConfig(api_key="default")
    custom_config = ProviderEndpointConfig(api_key="custom", base_url="http://localhost:11434/v1")
    registry = ModelProviderRegistry.with_defaults(
        provider_configs=ProviderConfigs(
            endpoint=default_config,
            custom={"llama-local": custom_config},
        )
    )

    builtin = registry.resolve_with_metadata("openai")
    custom = registry.resolve_with_metadata("llama-local")

    assert builtin.source == "builtin"
    assert builtin.configured is True
    assert custom.source == "custom"
    assert custom.configured is True
    assert custom.provider.name == "llama-local"

    with pytest.raises(UnknownProviderIdError):
        _ = registry.resolve_with_metadata("typo-provider")


@pytest.mark.parametrize(
    ("provider_id", "provider_configs", "base_url"),
    (
        pytest.param(
            "opencode-zen",
            ProviderConfigs(
                opencode_zen=ProviderEndpointConfig(
                    api_key="opencode-key",
                    base_url="https://opencode-proxy.example.test/zen/v1",
                )
            ),
            "https://opencode-proxy.example.test/zen/v1",
            id="opencode-zen",
        ),
        pytest.param(
            "openai",
            ProviderConfigs(
                openai=OpenAIProviderConfig(
                    api_key="sk-openai",
                    base_url="https://proxy.example.com/v1",
                )
            ),
            "https://proxy.example.com/v1",
            id="openai",
        ),
        pytest.param(
            "deepseek",
            ProviderConfigs(
                deepseek=OpenAICompatibleProviderConfig(
                    api_key="deepseek-key",
                    base_url="https://deepseek-proxy.example.test/v1",
                )
            ),
            "https://deepseek-proxy.example.test/v1",
            id="deepseek",
        ),
        pytest.param(
            "opencode-go",
            ProviderConfigs(
                opencode_go=OpenAICompatibleProviderConfig(
                    api_key="opencode-go-key",
                    base_url="https://opencode-go-proxy.example.test/zen/go",
                )
            ),
            "https://opencode-go-proxy.example.test/zen/go",
            id="opencode-go",
        ),
    ),
)
def test_registry_custom_base_url_wins_over_vendor_default(
    provider_id: str,
    provider_configs: ProviderConfigs,
    base_url: str,
) -> None:
    registry = ModelProviderRegistry.with_defaults(provider_configs=provider_configs)

    config = registry.provider_config(provider_id)

    assert config is not None
    assert config.base_url == base_url


def test_registry_shipped_provider_resolves_models_and_metadata_from_model_map(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Discovery is derived, so this vendor would be probed for real: keep the
    # unit test offline and let the refresh fall back to the configured map.
    def _offline(_request: Request, timeout: float) -> object:
        del timeout
        raise URLError("offline")

    monkeypatch.setattr(model_catalog, "urlopen", _offline)
    registry = ModelProviderRegistry.with_defaults(
        provider_configs=ProviderConfigs(
            zai=OpenAICompatibleProviderConfig(
                api_key="zai-key",
                model_map={"glm": "glm-4.5"},
            )
        )
    )

    models = registry.refresh_available_models("zai")

    assert models == ("glm", "glm-4.5")
    assert registry.available_models("zai") == models
    assert registry.model_metadata_for_model("zai", "glm") is None

    metadata = registry.model_metadata_for_model("zai", "glm-4.5")
    assert metadata is not None
    assert metadata.supports_tools is True

    resolved = resolve_provider_model("zai/glm-4.5", registry=registry)

    assert resolved.resolution.source == "builtin"
    assert resolved.resolution.configured is True
    assert resolved.metadata is not None
    assert resolved.metadata.supports_tools is True


def test_registry_google_service_account_auth_disables_discovery() -> None:
    """Service-account auth carries no key the discovery path could send."""
    registry = ModelProviderRegistry.with_defaults(
        provider_configs=ProviderConfigs(
            google=GoogleProviderConfig(auth=GoogleProviderAuthConfig(method="service_account", service_account_json_path="/tmp/sa.json"))
        )
    )

    config = registry.provider_config("google")

    assert config is not None
    assert config.api_key is None
    assert config.auth_header is None

    assert registry.refresh_available_models("google") == ()
    catalog = registry.provider_catalog("google")
    assert catalog is not None
    assert catalog.discovery_mode == "disabled"
    assert catalog.last_refresh_status == "skipped"


def test_registry_openai_compatible_endpoint_config_rejects_unknown_provider_names() -> None:
    with pytest.raises(ValueError, match="Unknown OpenAI-compatible provider"):
        openai_compatible_endpoint_config("acme-gateway", None)
    with pytest.raises(ValueError, match="Unknown OpenAI-compatible provider"):
        openai_compatible_endpoint_config("acme-gateway", OpenAICompatibleProviderConfig(api_key="key"))
