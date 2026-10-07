from __future__ import annotations

from dataclasses import dataclass
from typing import cast
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
    ProviderFallbackConfig,
    openai_compatible_endpoint_config,
)
from voidcode.provider.model_catalog import ProviderModelCatalog, ProviderModelMetadata
from voidcode.provider.models import ProviderDescriptor
from voidcode.provider.naming import UnknownProviderIdError
from voidcode.provider.provider_config import anthropic_compatible_endpoint_config
from voidcode.provider.registry import (
    ModelProviderRegistry,
    materialize_builtin_provider,
)
from voidcode.provider.resolution import resolve_provider_config, resolve_provider_model


def test_anthropic_wire_turn_resolves_the_vendor_default_host() -> None:
    """``turn_provider`` hands the adapter the raw vendor config while
    ``provider_config()`` returns the normalized endpoint config. The asymmetry is
    benign for a turn: the adapter resolves the vendor's own host from the wire
    table itself, so an unconfigured vendor reaches its own endpoint -- never
    Anthropic's -- and carries no ambient credential."""
    for provider_name, base_url in (
        ("minimax-cn", "https://api.minimaxi.com/anthropic"),
        ("kimi-code", "https://api.kimi.com/coding"),
    ):
        descriptor = ModelProviderRegistry.with_defaults().resolve_static(provider_name)
        turn = cast(AnthropicMessagesProvider, materialize_builtin_provider(descriptor).turn_provider())

        transport = turn._transport()

        assert transport.base_url == base_url
        assert transport.api_key is None


def test_anthropic_wire_endpoint_config_rejects_unknown_vendor_names() -> None:
    with pytest.raises(ValueError, match="Unknown Anthropic-wire provider"):
        anthropic_compatible_endpoint_config("unknown-vendor", None)


@pytest.mark.parametrize("removed_id", ["grok", "kimi", "opencode", "copilot", "kimi-coding"])
def test_registry_rejects_the_ids_the_rename_removed(removed_id: str) -> None:
    # The rename is a hard cutover: an old id must fail loudly instead of
    # resolving to the provider that replaced it.
    registry = ModelProviderRegistry.with_defaults()

    with pytest.raises(UnknownProviderIdError):
        registry.resolve_static(removed_id)


def test_registry_canonicalises_provider_id_case() -> None:
    registry = ModelProviderRegistry.with_defaults()

    descriptor = registry.resolve_static("MiniMax")

    assert descriptor.provider_name == "minimax"


def test_registry_rejects_undeclared_provider_even_with_endpoint_config() -> None:
    # A declared `providers.endpoint` is reached through the `endpoint` id; it does
    # not make an unknown prefix resolve. This is the fallthrough that used to send
    # an undeclared provider's traffic to a host the user never named.
    endpoint_config = ProviderEndpointConfig(
        api_key="token",
        base_url="http://localhost:4000",
    )
    registry = ModelProviderRegistry.with_defaults(provider_configs=ProviderConfigs(endpoint=endpoint_config))

    with pytest.raises(UnknownProviderIdError):
        _ = registry.resolve_static("custom")

    config = registry.provider_config("endpoint")
    assert config is not None
    assert config.base_url == "http://localhost:4000"
    assert config.api_key == "token"


def test_registry_unknown_provider_prefers_custom_provider_config() -> None:
    default_config = ProviderEndpointConfig(api_key="default", base_url="http://localhost:4000")
    custom_config = ProviderEndpointConfig(api_key="custom", base_url="http://localhost:11434/v1")
    registry = ModelProviderRegistry.with_defaults(
        provider_configs=ProviderConfigs(
            endpoint=default_config,
            custom={"llama-local": custom_config},
        )
    )

    config = registry.provider_config("llama-local")

    assert config is not None
    assert config.base_url == "http://localhost:11434/v1"
    assert config.api_key == "custom"


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


@dataclass(frozen=True, slots=True)
class _PackageConfig:
    mode: str


def test_installed_declaration_owns_config_and_catalog_without_builtin_endpoint_shape() -> None:
    catalog = ProviderModelCatalog(
        provider="package",
        models=("VendorCase",),
        refreshed=False,
        model_metadata={"VendorCase": ProviderModelMetadata(context_window=4096, supports_tools=False)},
        source="declared",
    )
    registry = ModelProviderRegistry(descriptors={"package": ProviderDescriptor("package", _PackageConfig("local"), catalog=catalog)})
    target = resolve_provider_model("Package/VendorCase", registry=registry)

    assert target.selection.provider == "package"
    assert target.selection.model == "VendorCase"
    assert target.metadata is not None
    assert target.metadata.context_window == 4096
    assert target.metadata.supports_tools is False
    assert registry.available_models("package") == ("VendorCase",)
    assert registry.provider_config("package") is None
    with pytest.raises(ValueError):
        registry.refresh_available_models("package")
    assert registry.available_models("package") == ("VendorCase",)
    with pytest.raises(ValueError):
        registry.register(ProviderDescriptor("package", _PackageConfig("replacement")))


def test_discovered_metadata_precedes_declared_facts_but_fills_absent_optional_fields() -> None:
    declared = ProviderModelMetadata(context_window=8192, supports_reasoning_effort=True, api="openai-chat-completions")
    discovered = ProviderModelMetadata(context_window=1024, supports_reasoning_effort=False)
    registry = ModelProviderRegistry(
        descriptors={
            "package": ProviderDescriptor(
                "package",
                _PackageConfig("local"),
                catalog=ProviderModelCatalog(provider="package", models=("Case",), refreshed=False, model_metadata={"Case": declared}),
            )
        },
        model_catalog={"package": ProviderModelCatalog(provider="package", models=("Case",), refreshed=True, model_metadata={"Case": discovered})},
    )

    metadata = resolve_provider_model("package/Case", registry=registry).metadata
    assert metadata is not None
    assert metadata.context_window == 1024
    assert metadata.supports_reasoning_effort is False
    assert metadata.api == "openai-chat-completions"


def test_binding_materializes_each_provider_once_and_preserves_model_specific_chain() -> None:
    registry = ModelProviderRegistry.with_defaults()
    first = ProviderModelMetadata(context_window=1024)
    second = ProviderModelMetadata(context_window=8192)
    registry.model_catalog["openai"] = ProviderModelCatalog(
        provider="openai",
        models=("FirstCase", "SecondCase"),
        refreshed=True,
        model_metadata={"FirstCase": first, "SecondCase": second},
    )
    resolved = resolve_provider_config(
        None,
        ProviderFallbackConfig(preferred_model="OpenAI/FirstCase", fallback_models=("openai/SecondCase", "anthropic/ThirdCase")),
        registry=registry,
    )
    constructed: list[str] = []

    def materialize(descriptor: ProviderDescriptor):
        constructed.append(descriptor.provider_name)
        return materialize_builtin_provider(descriptor)

    bound = ModelProviderRegistry(descriptors={}).bind(resolved, materialize=materialize)
    assert constructed == ["openai", "anthropic"]
    assert tuple(target.selection.model for target in bound.target_chain.all_targets) == ("FirstCase", "SecondCase", "ThirdCase")
    assert bound.target_chain.all_targets[0].provider is bound.target_chain.all_targets[1].provider
    assert tuple(target.metadata for target in bound.target_chain.all_targets[:2]) == (first, second)
    assert bound.active_target is bound.target_chain.all_targets[0]


def test_empty_selection_stays_absent_without_materialization() -> None:
    registry = ModelProviderRegistry.with_defaults()
    resolved = resolve_provider_config(None, None, registry=registry)

    def materialize(descriptor: ProviderDescriptor):
        raise AssertionError(f"absence cannot construct {descriptor.provider_name}")

    bound = registry.bind(resolved, materialize=materialize)
    assert bound.active_target is None
    assert bound.target_chain.preferred is None
    assert bound.target_chain.all_targets == ()


def test_fallback_selection_preserves_override_order_and_refuses_canonical_duplicates() -> None:
    registry = ModelProviderRegistry.with_defaults()
    fallback = ProviderFallbackConfig(preferred_model="openai/Old", fallback_models=("anthropic/Keep", "openai/New"))
    resolved = resolve_provider_config("openai/New", fallback, registry=registry)
    assert tuple(target.selection.raw_model for target in resolved.target_chain.all_targets) == ("openai/New", "anthropic/Keep")
    with pytest.raises(ValueError):
        resolve_provider_config(
            None,
            ProviderFallbackConfig(preferred_model="OpenAI/Case", fallback_models=("openai/case",)),
            registry=registry,
        )
