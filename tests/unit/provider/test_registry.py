from __future__ import annotations

import pytest

from voidcode.provider.anthropic import AnthropicModelProvider
from voidcode.provider.anthropic_native import AnthropicMessagesProvider
from voidcode.provider.config import (
    AnthropicProviderConfig,
    CopilotProviderAuthConfig,
    CopilotProviderConfig,
    GoogleProviderAuthConfig,
    GoogleProviderConfig,
    OpenAICompatibleProviderConfig,
    OpenAIProviderConfig,
    ProviderConfigs,
    ProviderEndpointConfig,
    openai_compatible_endpoint_config,
)
from voidcode.provider.copilot import CopilotModelProvider
from voidcode.provider.deepseek import DeepSeekModelProvider
from voidcode.provider.endpoint import OpenAIEndpointProvider
from voidcode.provider.fireworks import FireworksModelProvider
from voidcode.provider.google import GoogleModelProvider
from voidcode.provider.grok import GrokModelProvider
from voidcode.provider.groq import GroqModelProvider
from voidcode.provider.kimi import KimiModelProvider
from voidcode.provider.minimax import MiniMaxModelProvider
from voidcode.provider.mistral import MistralModelProvider
from voidcode.provider.model_catalog import ProviderModelCatalog, ProviderModelMetadata
from voidcode.provider.openai import OpenAIModelProvider
from voidcode.provider.openai_native import OpenAIChatCompletionsProvider
from voidcode.provider.opencode import OpenCodeModelProvider
from voidcode.provider.opencode_go import OpenCodeGoModelProvider
from voidcode.provider.qwen import QwenModelProvider
from voidcode.provider.registry import ModelProviderRegistry
from voidcode.provider.resolution import resolve_provider_model
from voidcode.provider.together import TogetherModelProvider
from voidcode.provider.zai import ZAIModelProvider
from voidcode.provider.zhipuai import ZhipuAIModelProvider


def test_registry_registers_concrete_provider_adapters() -> None:
    registry = ModelProviderRegistry.with_defaults()

    assert isinstance(registry.resolve("openai"), OpenAIModelProvider)
    assert isinstance(registry.resolve("anthropic"), AnthropicModelProvider)
    assert isinstance(registry.resolve("google"), GoogleModelProvider)
    assert isinstance(registry.resolve("copilot"), CopilotModelProvider)
    assert isinstance(registry.resolve("endpoint"), OpenAIEndpointProvider)


def test_registry_resolves_unknown_provider_to_endpoint_adapter() -> None:
    registry = ModelProviderRegistry.with_defaults()

    resolved = registry.resolve("custom")

    assert isinstance(resolved, OpenAIEndpointProvider)
    assert resolved.name == "custom"


def test_registry_unknown_provider_reuses_default_endpoint_config() -> None:
    endpoint_config = ProviderEndpointConfig(
        api_key="token",
        base_url="http://localhost:4000",
    )
    registry = ModelProviderRegistry.with_defaults(provider_configs=ProviderConfigs(endpoint=endpoint_config))

    resolved = registry.resolve("custom")

    assert isinstance(resolved, OpenAIEndpointProvider)
    assert resolved.config == endpoint_config


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


def test_registry_endpoint_provider_config_preserves_ssl_verify() -> None:
    registry = ModelProviderRegistry.with_defaults(
        provider_configs=ProviderConfigs(
            endpoint=ProviderEndpointConfig(
                api_key="internal-litellm-key",
                base_url="https://litellm.example.test",
                ssl_verify=False,
            )
        )
    )

    config = registry.provider_config("endpoint")

    assert config is not None
    assert config.ssl_verify is False


def test_registry_openai_compatible_provider_config_preserves_ssl_verify() -> None:
    registry = ModelProviderRegistry.with_defaults(
        provider_configs=ProviderConfigs(
            opencode_go=OpenAICompatibleProviderConfig(
                api_key="opencode-go-key",
                ssl_verify=False,
            )
        )
    )

    config = registry.provider_config("opencode-go")

    assert config is not None
    assert config.ssl_verify is False


def test_registry_resolve_with_metadata_distinguishes_builtin_custom_and_default_sources() -> None:
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
    fallback = registry.resolve_with_metadata("typo-provider")

    assert builtin.source == "builtin"
    assert builtin.configured is True
    assert custom.source == "custom"
    assert custom.configured is True
    assert custom.provider.name == "llama-local"
    assert fallback.source == "default_endpoint"
    assert fallback.configured is True
    assert fallback.provider.name == "typo-provider"


def test_registry_registers_opencode_zen_provider_with_model_discovery() -> None:
    registry = ModelProviderRegistry.with_defaults()

    resolved = registry.resolve("opencode")

    assert isinstance(resolved, OpenCodeModelProvider)
    assert resolved.name == "opencode"


def test_registry_opencode_zen_custom_base_url_disables_default_discovery() -> None:
    registry = ModelProviderRegistry.with_defaults(
        provider_configs=ProviderConfigs(
            opencode=ProviderEndpointConfig(
                api_key="opencode-key",
                base_url="https://opencode-proxy.example.test/zen/v1",
            )
        )
    )

    config = registry.provider_config("opencode")

    assert config is not None
    assert config.base_url == "https://opencode-proxy.example.test/zen/v1"
    assert config.discovery_base_url is None


def test_registry_opencode_zen_explicit_discovery_base_url_wins_with_custom_base_url() -> None:
    registry = ModelProviderRegistry.with_defaults(
        provider_configs=ProviderConfigs(
            opencode=ProviderEndpointConfig(
                api_key="opencode-key",
                base_url="https://opencode-proxy.example.test/zen/v1",
                discovery_base_url="https://opencode-proxy.example.test/zen/v1/models",
            )
        )
    )

    config = registry.provider_config("opencode")

    assert config is not None
    assert config.base_url == "https://opencode-proxy.example.test/zen/v1"
    assert config.discovery_base_url == "https://opencode-proxy.example.test/zen/v1/models"


def test_registry_refresh_available_models_prefers_model_map_aliases() -> None:
    endpoint_config = ProviderEndpointConfig(
        base_url="http://127.0.0.1:65534",
        auth_scheme="none",
        model_map={
            "gpt-4o": "openrouter/openai/gpt-4o",
            "coder": "ollama/qwen2.5-coder:latest",
        },
    )
    registry = ModelProviderRegistry.with_defaults(provider_configs=ProviderConfigs(endpoint=endpoint_config))

    models = registry.refresh_available_models("endpoint")

    assert models[:2] == ("gpt-4o", "coder")
    assert "openrouter/openai/gpt-4o" in models
    assert "ollama/qwen2.5-coder:latest" in models


def test_registry_refresh_available_models_stores_model_metadata() -> None:
    endpoint_config = ProviderEndpointConfig(
        discovery_base_url="",
        model_map={"gpt-4o": "openrouter/openai/gpt-4o"},
    )
    registry = ModelProviderRegistry.with_defaults(provider_configs=ProviderConfigs(endpoint=endpoint_config))

    models = registry.refresh_available_models("endpoint")
    catalog = registry.provider_catalog("endpoint")

    assert "gpt-4o" in models
    assert catalog is not None
    # Without a discovery endpoint, the catalog lists aliases and targets but
    # attaches no metadata: the static catalog is keyed by first-party
    # provider + model, and "endpoint" is a generic passthrough provider.
    assert catalog.model_metadata == {}
    assert registry.available_models("endpoint") == models
    assert catalog.last_refresh_status == "skipped"
    assert catalog.discovery_mode == "disabled"


def test_registry_refresh_normalizes_raw_native_provider_configs() -> None:
    registry = ModelProviderRegistry(
        providers={
            "openai-native": OpenAIChatCompletionsProvider(
                name="openai-native",
                config=OpenAIProviderConfig(api_key="sk-native", discovery_base_url="", timeout_seconds=3.0),
            ),
            "anthropic-native": AnthropicMessagesProvider(
                name="anthropic-native",
                config=AnthropicProviderConfig(api_key="sk-native", discovery_base_url="", timeout_seconds=3.0),
            ),
        },
        model_catalog={},
    )

    for provider_name in ("openai-native", "anthropic-native"):
        config = registry.provider_config(provider_name)

        assert isinstance(config, ProviderEndpointConfig)
        assert config.api_key == "sk-native"
        assert config.discovery_base_url == ""
        assert config.timeout_seconds == 3.0

        assert registry.refresh_available_models(provider_name) == ()
        catalog = registry.provider_catalog(provider_name)
        assert catalog is not None
        assert catalog.discovery_mode == "disabled"
        assert catalog.last_refresh_status == "skipped"


def test_registry_shipped_provider_resolves_models_and_metadata_from_model_map() -> None:
    registry = ModelProviderRegistry.with_defaults(
        provider_configs=ProviderConfigs(
            zai=OpenAICompatibleProviderConfig(
                api_key="zai-key",
                discovery_base_url="",
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


def test_registry_shipped_providers_without_discovery_report_no_models() -> None:
    registry = ModelProviderRegistry.with_defaults(
        provider_configs=ProviderConfigs(
            copilot=CopilotProviderConfig(auth=CopilotProviderAuthConfig(method="token", token="copilot-token")),
            minimax=OpenAICompatibleProviderConfig(api_key="minimax-key"),
            fireworks=OpenAICompatibleProviderConfig(api_key="fireworks-key"),
            opencode_go=OpenAICompatibleProviderConfig(api_key="opencode-go-key"),
        )
    )

    discovery_modes = {
        # Copilot, MiniMax, Fireworks and OpenCode Go each resolve to a real
        # host with no public model listing, so discovery is disabled -- never
        # attempted unauthenticated.
        "copilot": "disabled",
        "minimax": "disabled",
        "fireworks": "disabled",
        "opencode-go": "disabled",
    }

    for provider_name, discovery_mode in discovery_modes.items():
        assert registry.refresh_available_models(provider_name) == ()

        catalog = registry.provider_catalog(provider_name)
        assert catalog is not None
        assert catalog.models == ()
        assert catalog.discovery_mode == discovery_mode
        assert catalog.last_refresh_status == "skipped"
        assert catalog.last_error is not None


def test_resolved_provider_model_carries_catalog_metadata_for_routing() -> None:
    registry = ModelProviderRegistry.with_defaults()
    registry.model_catalog = {
        "openai": ProviderModelCatalog(
            provider="openai",
            models=("gpt-4o",),
            refreshed=True,
            model_metadata={
                "gpt-4o": ProviderModelMetadata(
                    context_window=128_000,
                    supports_tools=True,
                    cost_per_input_token=0.000001,
                    model_status="active",
                )
            },
        )
    }

    resolved = resolve_provider_model("openai/gpt-4o", registry=registry)

    assert resolved.metadata is not None
    assert resolved.metadata.context_window == 128_000
    assert resolved.metadata.supports_tools is True
    assert resolved.metadata.cost_per_input_token is not None
    assert resolved.metadata.model_status == "active"


def test_registry_refresh_custom_provider_uses_custom_config() -> None:
    custom_config = ProviderEndpointConfig(
        base_url="http://127.0.0.1:65534",
        auth_scheme="none",
        model_map={"coder": "ollama/qwen2.5-coder:latest"},
    )
    registry = ModelProviderRegistry.with_defaults(provider_configs=ProviderConfigs(custom={"llama-local": custom_config}))

    models = registry.refresh_available_models("llama-local")

    assert models[0] == "coder"
    assert "ollama/qwen2.5-coder:latest" in models
    assert registry.available_models("llama-local") == models
    catalog = registry.provider_catalog("llama-local")
    assert catalog is not None
    assert catalog.provider == "llama-local"
    assert catalog.discovery_mode == "configured_base_url"


def test_registry_google_provider_config_uses_google_api_key_header_for_api_key_auth() -> None:
    registry = ModelProviderRegistry.with_defaults(
        provider_configs=ProviderConfigs(google=GoogleProviderConfig(auth=GoogleProviderAuthConfig(method="api_key", api_key="AIza-test")))
    )

    config = registry.provider_config("google")

    assert config == ProviderEndpointConfig(
        api_key="AIza-test",
        discovery_base_url="https://generativelanguage.googleapis.com",
        auth_header="x-goog-api-key",
        auth_scheme="token",
    )


def test_registry_openai_provider_config_sets_default_discovery_base_url() -> None:
    registry = ModelProviderRegistry.with_defaults(provider_configs=ProviderConfigs(openai=OpenAIProviderConfig(api_key="sk-openai")))

    config = registry.provider_config("openai")

    assert config is not None
    assert config.api_key == "sk-openai"
    assert config.discovery_base_url == "https://api.openai.com"


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
    assert config.discovery_base_url == ""

    assert registry.refresh_available_models("google") == ()
    catalog = registry.provider_catalog("google")
    assert catalog is not None
    assert catalog.discovery_mode == "disabled"
    assert catalog.last_refresh_status == "skipped"


def test_registry_openai_provider_config_with_custom_base_url_disables_default_discovery() -> None:
    registry = ModelProviderRegistry.with_defaults(
        provider_configs=ProviderConfigs(
            openai=OpenAIProviderConfig(
                api_key="sk-openai",
                base_url="https://proxy.example.com/v1",
            )
        )
    )

    config = registry.provider_config("openai")

    assert config is not None
    assert config.base_url == "https://proxy.example.com/v1"
    assert config.discovery_base_url is None


def test_registry_anthropic_provider_config_sets_default_discovery_base_url() -> None:
    registry = ModelProviderRegistry.with_defaults(provider_configs=ProviderConfigs(anthropic=AnthropicProviderConfig(api_key="sk-anthropic")))

    config = registry.provider_config("anthropic")

    assert config is not None
    assert config.api_key == "sk-anthropic"
    assert config.discovery_base_url == "https://api.anthropic.com"


def test_registry_endpoint_provider_config_sets_default_discovery_base_url() -> None:
    registry = ModelProviderRegistry.with_defaults(provider_configs=ProviderConfigs(endpoint=ProviderEndpointConfig(api_key="endpoint-key")))

    config = registry.provider_config("endpoint")

    assert config is not None
    assert config.api_key == "endpoint-key"
    assert config.discovery_base_url == "http://127.0.0.1:4000"


def test_registry_registers_zai_provider() -> None:
    registry = ModelProviderRegistry.with_defaults(provider_configs=ProviderConfigs(zai=OpenAICompatibleProviderConfig(api_key="zai-key")))

    resolved = registry.resolve("zai")

    assert isinstance(resolved, ZAIModelProvider)
    assert resolved.name == "zai"
    config = registry.provider_config("zai")
    assert config is not None
    assert config.api_key == "zai-key"
    assert config.base_url == "https://api.z.ai/api/paas/v4"
    assert config.discovery_base_url == "https://api.z.ai/api/paas/v4"


def test_registry_registers_zhipuai_provider() -> None:
    registry = ModelProviderRegistry.with_defaults(provider_configs=ProviderConfigs(zhipuai=OpenAICompatibleProviderConfig(api_key="zhipu-key")))

    resolved = registry.resolve("zhipuai")

    assert isinstance(resolved, ZhipuAIModelProvider)
    assert resolved.name == "zhipuai"
    config = registry.provider_config("zhipuai")
    assert config is not None
    assert config.api_key == "zhipu-key"
    assert config.base_url == "https://open.bigmodel.cn/api/paas/v4"
    assert config.discovery_base_url == "https://open.bigmodel.cn/api/paas/v4"


def test_registry_registers_deepseek_provider() -> None:
    registry = ModelProviderRegistry.with_defaults(provider_configs=ProviderConfigs(deepseek=OpenAICompatibleProviderConfig(api_key="deepseek-key")))

    resolved = registry.resolve("deepseek")

    assert isinstance(resolved, DeepSeekModelProvider)
    assert resolved.name == "deepseek"
    config = registry.provider_config("deepseek")
    assert config is not None
    assert config.api_key == "deepseek-key"
    assert config.base_url == "https://api.deepseek.com"
    assert config.discovery_base_url == "https://api.deepseek.com"


def test_registry_deepseek_custom_base_url_uses_configured_base_url_discovery() -> None:
    registry = ModelProviderRegistry.with_defaults(
        provider_configs=ProviderConfigs(
            deepseek=OpenAICompatibleProviderConfig(
                api_key="deepseek-key",
                base_url="https://deepseek-proxy.example.test/v1",
            )
        )
    )

    config = registry.provider_config("deepseek")

    assert config is not None
    assert config.base_url == "https://deepseek-proxy.example.test/v1"
    assert config.discovery_base_url is None


def test_registry_registers_grok_provider() -> None:
    registry = ModelProviderRegistry.with_defaults(provider_configs=ProviderConfigs(grok=OpenAICompatibleProviderConfig(api_key="grok-key")))

    resolved = registry.resolve("grok")

    assert isinstance(resolved, GrokModelProvider)
    assert resolved.name == "grok"
    config = registry.provider_config("grok")
    assert config is not None
    assert config.api_key == "grok-key"
    assert config.base_url == "https://api.x.ai"
    assert config.discovery_base_url == "https://api.x.ai"


def test_registry_registers_minimax_provider() -> None:
    registry = ModelProviderRegistry.with_defaults(provider_configs=ProviderConfigs(minimax=OpenAICompatibleProviderConfig(api_key="minimax-key")))

    resolved = registry.resolve("minimax")

    assert isinstance(resolved, MiniMaxModelProvider)
    assert resolved.name == "minimax"
    config = registry.provider_config("minimax")
    assert config is not None
    assert config.api_key == "minimax-key"
    assert config.base_url == "https://api.minimax.io"
    assert config.discovery_base_url == ""


def test_registry_registers_kimi_provider() -> None:
    registry = ModelProviderRegistry.with_defaults(provider_configs=ProviderConfigs(kimi=OpenAICompatibleProviderConfig(api_key="kimi-key")))

    resolved = registry.resolve("kimi")

    assert isinstance(resolved, KimiModelProvider)
    assert resolved.name == "kimi"
    config = registry.provider_config("kimi")
    assert config is not None
    assert config.api_key == "kimi-key"
    assert config.base_url == "https://api.moonshot.ai"
    assert config.discovery_base_url == "https://api.moonshot.ai/v1"


def test_registry_registers_opencode_go_provider() -> None:
    registry = ModelProviderRegistry.with_defaults(
        provider_configs=ProviderConfigs(opencode_go=OpenAICompatibleProviderConfig(api_key="opencode-go-key"))
    )

    resolved = registry.resolve("opencode-go")

    assert isinstance(resolved, OpenCodeGoModelProvider)
    assert resolved.name == "opencode-go"
    config = registry.provider_config("opencode-go")
    assert config is not None
    assert config.api_key == "opencode-go-key"
    assert config.base_url == "https://opencode.ai/zen/go"
    assert config.discovery_base_url == ""


def test_registry_opencode_go_custom_base_url_keeps_discovery_disabled() -> None:
    registry = ModelProviderRegistry.with_defaults(
        provider_configs=ProviderConfigs(
            opencode_go=OpenAICompatibleProviderConfig(
                api_key="opencode-go-key",
                base_url="https://opencode-go-proxy.example.test/zen/go",
            )
        )
    )

    config = registry.provider_config("opencode-go")

    assert config is not None
    assert config.base_url == "https://opencode-go-proxy.example.test/zen/go"
    assert config.discovery_base_url == ""


def test_registry_registers_qwen_provider() -> None:
    registry = ModelProviderRegistry.with_defaults(provider_configs=ProviderConfigs(qwen=OpenAICompatibleProviderConfig(api_key="qwen-key")))

    resolved = registry.resolve("qwen")

    assert isinstance(resolved, QwenModelProvider)
    assert resolved.name == "qwen"
    config = registry.provider_config("qwen")
    assert config is not None
    assert config.api_key == "qwen-key"
    assert config.base_url == "https://dashscope.aliyuncs.com/compatible-mode"
    assert config.discovery_base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"


def test_registry_zhipuai_provider_config_with_base_url_and_model_map() -> None:
    registry = ModelProviderRegistry.with_defaults(
        provider_configs=ProviderConfigs(
            zhipuai=OpenAICompatibleProviderConfig(
                api_key="zhipu-key",
                base_url="https://custom.zhipu.example",
                model_map={"glm4": "glm-4-flash"},
            )
        )
    )

    config = registry.provider_config("zhipuai")

    assert config == ProviderEndpointConfig(
        api_key="zhipu-key",
        base_url="https://custom.zhipu.example",
        discovery_base_url="https://open.bigmodel.cn/api/paas/v4",
        model_map={"glm4": "glm-4-flash"},
    )
    assert set(config.model_map) == {"glm4"}


def test_registry_openai_compatible_provider_uses_default_base_url_when_not_set() -> None:
    registry = ModelProviderRegistry.with_defaults(
        provider_configs=ProviderConfigs(
            deepseek=OpenAICompatibleProviderConfig(api_key="deepseek-key"),
            zai=OpenAICompatibleProviderConfig(api_key="zai-key"),
            zhipuai=OpenAICompatibleProviderConfig(api_key="zhipu-key"),
            grok=OpenAICompatibleProviderConfig(api_key="grok-key"),
            minimax=OpenAICompatibleProviderConfig(api_key="minimax-key"),
            kimi=OpenAICompatibleProviderConfig(api_key="kimi-key"),
            opencode_go=OpenAICompatibleProviderConfig(api_key="opencode-go-key"),
            qwen=OpenAICompatibleProviderConfig(api_key="qwen-key"),
        )
    )

    deepseek_config = registry.provider_config("deepseek")
    assert deepseek_config is not None
    assert deepseek_config.base_url == "https://api.deepseek.com"
    assert deepseek_config.discovery_base_url == "https://api.deepseek.com"

    zai_config = registry.provider_config("zai")
    assert zai_config is not None
    assert zai_config.base_url == "https://api.z.ai/api/paas/v4"
    assert zai_config.discovery_base_url == "https://api.z.ai/api/paas/v4"

    zhipuai_config = registry.provider_config("zhipuai")
    assert zhipuai_config is not None
    assert zhipuai_config.base_url == "https://open.bigmodel.cn/api/paas/v4"
    assert zhipuai_config.discovery_base_url == "https://open.bigmodel.cn/api/paas/v4"

    grok_config = registry.provider_config("grok")
    assert grok_config is not None
    assert grok_config.base_url == "https://api.x.ai"
    assert grok_config.discovery_base_url == "https://api.x.ai"

    minimax_config = registry.provider_config("minimax")
    assert minimax_config is not None
    assert minimax_config.base_url == "https://api.minimax.io"
    assert minimax_config.discovery_base_url == ""

    kimi_config = registry.provider_config("kimi")
    assert kimi_config is not None
    assert kimi_config.base_url == "https://api.moonshot.ai"
    assert kimi_config.discovery_base_url == "https://api.moonshot.ai/v1"

    opencode_go_config = registry.provider_config("opencode-go")
    assert opencode_go_config is not None
    assert opencode_go_config.base_url == "https://opencode.ai/zen/go"
    assert opencode_go_config.discovery_base_url == ""

    qwen_config = registry.provider_config("qwen")
    assert qwen_config is not None
    assert qwen_config.base_url == "https://dashscope.aliyuncs.com/compatible-mode"
    assert qwen_config.discovery_base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"


def test_registry_openai_compatible_provider_preserves_user_model_map() -> None:
    registry = ModelProviderRegistry.with_defaults(
        provider_configs=ProviderConfigs(
            zai=OpenAICompatibleProviderConfig(
                api_key="zai-key",
                model_map={"custom": "custom-model"},
            )
        )
    )

    config = registry.provider_config("zai")
    assert config is not None
    assert config.model_map == {"custom": "custom-model"}


def test_registry_openai_compatible_provider_user_base_url_overrides_default() -> None:
    registry = ModelProviderRegistry.with_defaults(
        provider_configs=ProviderConfigs(
            zai=OpenAICompatibleProviderConfig(
                api_key="zai-key",
                base_url="https://my-proxy.com/v1",
            )
        )
    )

    config = registry.provider_config("zai")
    assert config is not None
    assert config.base_url == "https://my-proxy.com/v1"


def test_registry_all_chinese_providers_resolve_correctly() -> None:
    registry = ModelProviderRegistry.with_defaults(
        provider_configs=ProviderConfigs(
            deepseek=OpenAICompatibleProviderConfig(api_key="deepseek-key"),
            zai=OpenAICompatibleProviderConfig(api_key="zai-key"),
            zhipuai=OpenAICompatibleProviderConfig(api_key="zhipu-key"),
            grok=OpenAICompatibleProviderConfig(api_key="grok-key"),
            minimax=OpenAICompatibleProviderConfig(api_key="minimax-key"),
            kimi=OpenAICompatibleProviderConfig(api_key="kimi-key"),
            opencode_go=OpenAICompatibleProviderConfig(api_key="opencode-go-key"),
            qwen=OpenAICompatibleProviderConfig(api_key="qwen-key"),
        )
    )

    assert isinstance(registry.resolve("deepseek"), DeepSeekModelProvider)
    assert isinstance(registry.resolve("zai"), ZAIModelProvider)
    assert isinstance(registry.resolve("zhipuai"), ZhipuAIModelProvider)
    assert isinstance(registry.resolve("minimax"), MiniMaxModelProvider)
    assert isinstance(registry.resolve("kimi"), KimiModelProvider)
    assert isinstance(registry.resolve("opencode-go"), OpenCodeGoModelProvider)
    assert isinstance(registry.resolve("qwen"), QwenModelProvider)


def test_registry_registers_groq_together_fireworks_and_mistral() -> None:
    registry = ModelProviderRegistry.with_defaults(
        provider_configs=ProviderConfigs(
            groq=OpenAICompatibleProviderConfig(api_key="groq-key"),
            together=OpenAICompatibleProviderConfig(api_key="together-key"),
            fireworks=OpenAICompatibleProviderConfig(api_key="fireworks-key"),
            mistral=OpenAICompatibleProviderConfig(api_key="mistral-key"),
        )
    )

    assert isinstance(registry.resolve("groq"), GroqModelProvider)
    assert isinstance(registry.resolve("together"), TogetherModelProvider)
    assert isinstance(registry.resolve("fireworks"), FireworksModelProvider)
    assert isinstance(registry.resolve("mistral"), MistralModelProvider)
    assert registry.provider_config("groq").base_url == "https://api.groq.com/openai/v1"
    assert registry.provider_config("together").base_url == "https://api.together.ai/v1"
    assert registry.provider_config("fireworks").discovery_base_url == ""
    assert registry.provider_config("mistral").discovery_base_url == "https://api.mistral.ai/v1"


def test_registry_openai_compatible_provider_without_a_config_block_keeps_its_own_host() -> None:
    """An unconfigured shipped provider resolves to its own vendor default.

    Resolution used to answer "no endpoint" for an absent config block, which
    left callers to fall back to whichever host their transport defaulted to.
    """
    registry = ModelProviderRegistry.with_defaults(provider_configs=ProviderConfigs())

    expected_base_urls = {
        "deepseek": "https://api.deepseek.com",
        "zai": "https://api.z.ai/api/paas/v4",
        "zhipuai": "https://open.bigmodel.cn/api/paas/v4",
        "grok": "https://api.x.ai",
        "minimax": "https://api.minimax.io",
        "kimi": "https://api.moonshot.ai",
        "opencode-go": "https://opencode.ai/zen/go",
        "qwen": "https://dashscope.aliyuncs.com/compatible-mode",
        "groq": "https://api.groq.com/openai/v1",
        "together": "https://api.together.ai/v1",
        "fireworks": "https://api.fireworks.ai/inference/v1",
        "mistral": "https://api.mistral.ai/v1",
    }

    for provider_name, expected_base_url in expected_base_urls.items():
        config = registry.provider_config(provider_name)

        assert config is not None, provider_name
        assert config.base_url == expected_base_url, provider_name
        assert config.api_key is None, provider_name
        # Nobody asked us to list this vendor's models, and an unauthenticated
        # listing must not be issued.
        assert config.discovery_base_url == "", provider_name


def test_registry_copilot_without_a_config_base_url_keeps_its_own_host() -> None:
    configured = ModelProviderRegistry.with_defaults(
        provider_configs=ProviderConfigs(copilot=CopilotProviderConfig(auth=CopilotProviderAuthConfig(method="token", token="copilot-token")))
    )
    unconfigured = ModelProviderRegistry.with_defaults(provider_configs=ProviderConfigs())

    for registry in (configured, unconfigured):
        config = registry.provider_config("copilot")

        assert config is not None
        # A Copilot token must never be sent to api.openai.com.
        assert config.base_url == "https://api.individual.githubcopilot.com"
        assert config.discovery_base_url == ""

    assert configured.provider_config("copilot").api_key == "copilot-token"
    assert unconfigured.provider_config("copilot").api_key is None


def test_registry_openai_compatible_endpoint_config_rejects_unknown_provider_names() -> None:
    with pytest.raises(ValueError, match="Unknown OpenAI-compatible provider"):
        openai_compatible_endpoint_config("acme-gateway", None)
    with pytest.raises(ValueError, match="Unknown OpenAI-compatible provider"):
        openai_compatible_endpoint_config("acme-gateway", OpenAICompatibleProviderConfig(api_key="key"))
