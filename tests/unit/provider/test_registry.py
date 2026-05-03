from __future__ import annotations

from voidcode.provider.anthropic import AnthropicModelProvider
from voidcode.provider.config import (
    AnthropicProviderConfig,
    GoogleProviderAuthConfig,
    GoogleProviderConfig,
    LiteLLMProviderConfig,
    OpenAIProviderConfig,
    ProviderConfigs,
    SimplifiedProviderConfig,
)
from voidcode.provider.copilot import CopilotModelProvider
from voidcode.provider.deepseek import DeepSeekModelProvider
from voidcode.provider.glm import GLMModelProvider
from voidcode.provider.google import GoogleModelProvider
from voidcode.provider.grok import GrokModelProvider
from voidcode.provider.kimi import KimiModelProvider
from voidcode.provider.litellm import LiteLLMModelProvider
from voidcode.provider.minimax import MiniMaxModelProvider
from voidcode.provider.openai import OpenAIModelProvider
from voidcode.provider.opencode_go import OpenCodeGoModelProvider
from voidcode.provider.qwen import QwenModelProvider
from voidcode.provider.registry import ModelProviderRegistry, StaticModelProvider
from voidcode.provider.resolution import resolve_provider_model


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


def test_registry_litellm_provider_config_preserves_ssl_verify() -> None:
    registry = ModelProviderRegistry.with_defaults(
        provider_configs=ProviderConfigs(
            litellm=LiteLLMProviderConfig(
                api_key="internal-litellm-key",
                base_url="https://litellm.example.test",
                ssl_verify=False,
            )
        )
    )

    config = registry.provider_config("litellm")

    assert config is not None
    assert config.ssl_verify is False


def test_registry_simplified_provider_config_preserves_ssl_verify() -> None:
    registry = ModelProviderRegistry.with_defaults(
        provider_configs=ProviderConfigs(
            opencode_go=SimplifiedProviderConfig(
                api_key="opencode-go-key",
                ssl_verify=False,
            )
        )
    )

    config = registry.provider_config("opencode-go")

    assert config is not None
    assert config.ssl_verify is False


def test_registry_resolve_with_metadata_distinguishes_builtin_custom_and_default_sources() -> None:
    default_config = LiteLLMProviderConfig(api_key="default")
    custom_config = LiteLLMProviderConfig(api_key="custom", base_url="http://localhost:11434/v1")
    registry = ModelProviderRegistry.with_defaults(
        provider_configs=ProviderConfigs(
            litellm=default_config,
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
    assert fallback.source == "default_litellm"
    assert fallback.configured is True
    assert fallback.provider.name == "typo-provider"


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


def test_registry_refresh_available_models_stores_model_metadata() -> None:
    litellm_config = LiteLLMProviderConfig(
        discovery_base_url="",
        model_map={"gpt-4o": "openrouter/openai/gpt-4o"},
    )
    registry = ModelProviderRegistry.with_defaults(
        provider_configs=ProviderConfigs(litellm=litellm_config)
    )

    models = registry.refresh_available_models("litellm")
    catalog = registry.provider_catalog("litellm")

    assert "gpt-4o" in models
    assert catalog is not None
    assert catalog.model_metadata["gpt-4o"].context_window == 128_000
    assert catalog.model_metadata["gpt-4o"].cost_per_output_token is not None
    assert registry.available_models("litellm") == models
    catalog = registry.provider_catalog("litellm")
    assert catalog is not None
    assert catalog.last_refresh_status in {"ok", "failed", "skipped"}
    assert catalog.discovery_mode in {"configured_endpoint", "configured_base_url", "disabled"}


def test_resolved_provider_model_carries_catalog_metadata_for_routing() -> None:
    registry = ModelProviderRegistry.with_defaults()
    registry.refresh_available_models("openai")

    resolved = resolve_provider_model("openai/gpt-4o", registry=registry)

    assert resolved.metadata is not None
    assert resolved.metadata.context_window == 128_000
    assert resolved.metadata.supports_tools is True
    assert resolved.metadata.cost_per_input_token is not None
    assert resolved.metadata.model_status == "active"


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
    assert catalog.discovery_mode == "configured_base_url"


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
        discovery_base_url="https://generativelanguage.googleapis.com",
        auth_header="x-goog-api-key",
        auth_scheme="token",
        model_map={
            "gemini-3-pro-preview": "gemini-3-pro-preview",
            "gemini-3-flash-preview": "gemini-3-flash-preview",
            "gemini-2.5-pro": "gemini-2.5-pro",
            "gemini-2.5-flash": "gemini-2.5-flash",
            "gemini-2.5-flash-lite": "gemini-2.5-flash-lite",
            "gemini-3.1-flash-live-preview": "gemini-3.1-flash-live-preview",
            "gemini-3.1-flash-tts-preview": "gemini-3.1-flash-tts-preview",
        },
    )


def test_registry_openai_provider_config_sets_default_discovery_base_url() -> None:
    registry = ModelProviderRegistry.with_defaults(
        provider_configs=ProviderConfigs(openai=OpenAIProviderConfig(api_key="sk-openai"))
    )

    config = registry.provider_config("openai")

    assert config is not None
    assert config.api_key == "sk-openai"
    assert config.discovery_base_url == "https://api.openai.com"
    assert config.model_map["gpt-5.5"] == "gpt-5.5"


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
    assert config.model_map["gpt-5.5"] == "gpt-5.5"


def test_registry_anthropic_provider_config_sets_default_discovery_base_url() -> None:
    registry = ModelProviderRegistry.with_defaults(
        provider_configs=ProviderConfigs(anthropic=AnthropicProviderConfig(api_key="sk-anthropic"))
    )

    config = registry.provider_config("anthropic")

    assert config is not None
    assert config.api_key == "sk-anthropic"
    assert config.discovery_base_url == "https://api.anthropic.com"
    assert config.model_map["claude-opus-4-7"] == "claude-opus-4-7"


def test_registry_litellm_provider_config_sets_default_discovery_base_url() -> None:
    registry = ModelProviderRegistry.with_defaults(
        provider_configs=ProviderConfigs(litellm=LiteLLMProviderConfig(api_key="litellm-key"))
    )

    config = registry.provider_config("litellm")

    assert config is not None
    assert config.api_key == "litellm-key"
    assert config.discovery_base_url == "http://127.0.0.1:4000"


def test_registry_registers_glm_provider() -> None:
    registry = ModelProviderRegistry.with_defaults(
        provider_configs=ProviderConfigs(glm=SimplifiedProviderConfig(api_key="glm-key"))
    )

    resolved = registry.resolve("glm")

    assert isinstance(resolved, GLMModelProvider)
    assert resolved.name == "glm"
    config = registry.provider_config("glm")
    assert config is not None
    assert config.api_key == "glm-key"
    assert config.base_url == "https://open.bigmodel.cn/api/paas/v4"
    assert config.discovery_base_url == "https://open.bigmodel.cn/api/paas/v4"
    assert "glm-4-flash" in config.model_map


def test_registry_registers_deepseek_provider() -> None:
    registry = ModelProviderRegistry.with_defaults(
        provider_configs=ProviderConfigs(deepseek=SimplifiedProviderConfig(api_key="deepseek-key"))
    )

    resolved = registry.resolve("deepseek")

    assert isinstance(resolved, DeepSeekModelProvider)
    assert resolved.name == "deepseek"
    config = registry.provider_config("deepseek")
    assert config is not None
    assert config.api_key == "deepseek-key"
    assert config.base_url == "https://api.deepseek.com"
    assert config.discovery_base_url == "https://api.deepseek.com"
    assert "deepseek-v4-pro" in config.model_map.values()


def test_registry_deepseek_custom_base_url_uses_configured_base_url_discovery() -> None:
    registry = ModelProviderRegistry.with_defaults(
        provider_configs=ProviderConfigs(
            deepseek=SimplifiedProviderConfig(
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
    registry = ModelProviderRegistry.with_defaults(
        provider_configs=ProviderConfigs(grok=SimplifiedProviderConfig(api_key="grok-key"))
    )

    resolved = registry.resolve("grok")

    assert isinstance(resolved, GrokModelProvider)
    assert resolved.name == "grok"
    config = registry.provider_config("grok")
    assert config is not None
    assert config.api_key == "grok-key"
    assert config.base_url == "https://api.x.ai"
    assert config.discovery_base_url == "https://api.x.ai"
    assert "grok-4-1-fast-reasoning" in config.model_map.values()


def test_registry_registers_minimax_provider() -> None:
    registry = ModelProviderRegistry.with_defaults(
        provider_configs=ProviderConfigs(minimax=SimplifiedProviderConfig(api_key="minimax-key"))
    )

    resolved = registry.resolve("minimax")

    assert isinstance(resolved, MiniMaxModelProvider)
    assert resolved.name == "minimax"
    config = registry.provider_config("minimax")
    assert config is not None
    assert config.api_key == "minimax-key"
    assert config.base_url == "https://api.minimax.io"
    assert config.discovery_base_url == ""
    assert "MiniMax-M2.7" in config.model_map.values()


def test_registry_registers_kimi_provider() -> None:
    registry = ModelProviderRegistry.with_defaults(
        provider_configs=ProviderConfigs(kimi=SimplifiedProviderConfig(api_key="kimi-key"))
    )

    resolved = registry.resolve("kimi")

    assert isinstance(resolved, KimiModelProvider)
    assert resolved.name == "kimi"
    config = registry.provider_config("kimi")
    assert config is not None
    assert config.api_key == "kimi-key"
    assert config.base_url == "https://api.moonshot.ai"
    assert config.discovery_base_url == "https://api.moonshot.ai/v1"
    assert "kimi-k2.5" in config.model_map.values()


def test_registry_registers_opencode_go_provider() -> None:
    registry = ModelProviderRegistry.with_defaults(
        provider_configs=ProviderConfigs(
            opencode_go=SimplifiedProviderConfig(api_key="opencode-go-key")
        )
    )

    resolved = registry.resolve("opencode-go")

    assert isinstance(resolved, OpenCodeGoModelProvider)
    assert resolved.name == "opencode-go"
    config = registry.provider_config("opencode-go")
    assert config is not None
    assert config.api_key == "opencode-go-key"
    assert config.base_url == "https://opencode.ai/zen/go"
    assert config.discovery_base_url == ""
    assert "kimi-k2.5" in config.model_map.values()
    assert "kimi-k2.6" in config.model_map.values()
    assert "mimo-v2.5" in config.model_map.values()
    assert "mimo-v2.5-pro" in config.model_map.values()
    assert "qwen-plus" not in config.model_map
    assert "qwen-max" not in config.model_map
    assert "qwen-flash" not in config.model_map
    assert "qwen3.5-flash" in config.model_map
    assert "qwen3.6-plus" in config.model_map


def test_registry_opencode_go_custom_base_url_keeps_discovery_disabled() -> None:
    registry = ModelProviderRegistry.with_defaults(
        provider_configs=ProviderConfigs(
            opencode_go=SimplifiedProviderConfig(
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
    registry = ModelProviderRegistry.with_defaults(
        provider_configs=ProviderConfigs(qwen=SimplifiedProviderConfig(api_key="qwen-key"))
    )

    resolved = registry.resolve("qwen")

    assert isinstance(resolved, QwenModelProvider)
    assert resolved.name == "qwen"
    config = registry.provider_config("qwen")
    assert config is not None
    assert config.api_key == "qwen-key"
    assert config.base_url == "https://dashscope.aliyuncs.com/compatible-mode"
    assert config.discovery_base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert "qwen-plus" in config.model_map.values()


def test_registry_glm_provider_config_with_base_url_and_model_map() -> None:
    registry = ModelProviderRegistry.with_defaults(
        provider_configs=ProviderConfigs(
            glm=SimplifiedProviderConfig(
                api_key="glm-key",
                base_url="https://custom.glm.cn",
                model_map={"glm4": "glm-4-flash"},
            )
        )
    )

    config = registry.provider_config("glm")

    assert config == LiteLLMProviderConfig(
        api_key="glm-key",
        base_url="https://custom.glm.cn",
        discovery_base_url="https://open.bigmodel.cn/api/paas/v4",
        model_map={"glm4": "glm-4-flash"},
    )


def test_registry_simplified_provider_uses_default_base_url_when_not_set() -> None:
    registry = ModelProviderRegistry.with_defaults(
        provider_configs=ProviderConfigs(
            deepseek=SimplifiedProviderConfig(api_key="deepseek-key"),
            glm=SimplifiedProviderConfig(api_key="glm-key"),
            grok=SimplifiedProviderConfig(api_key="grok-key"),
            minimax=SimplifiedProviderConfig(api_key="minimax-key"),
            kimi=SimplifiedProviderConfig(api_key="kimi-key"),
            opencode_go=SimplifiedProviderConfig(api_key="opencode-go-key"),
            qwen=SimplifiedProviderConfig(api_key="qwen-key"),
        )
    )

    deepseek_config = registry.provider_config("deepseek")
    assert deepseek_config is not None
    assert deepseek_config.base_url == "https://api.deepseek.com"
    assert deepseek_config.discovery_base_url == "https://api.deepseek.com"
    assert deepseek_config.model_map.get("deepseek-v4-flash") == "deepseek-v4-flash"

    glm_config = registry.provider_config("glm")
    assert glm_config is not None
    assert glm_config.base_url == "https://open.bigmodel.cn/api/paas/v4"
    assert glm_config.discovery_base_url == "https://open.bigmodel.cn/api/paas/v4"
    assert glm_config.model_map.get("glm-4-flash") == "glm-4-flash"

    grok_config = registry.provider_config("grok")
    assert grok_config is not None
    assert grok_config.base_url == "https://api.x.ai"
    assert grok_config.discovery_base_url == "https://api.x.ai"
    assert grok_config.model_map.get("grok-4-1-fast-reasoning") == ("grok-4-1-fast-reasoning")

    minimax_config = registry.provider_config("minimax")
    assert minimax_config is not None
    assert minimax_config.base_url == "https://api.minimax.io"
    assert minimax_config.discovery_base_url == ""
    assert minimax_config.model_map.get("minimax-m2.7") == "MiniMax-M2.7"

    kimi_config = registry.provider_config("kimi")
    assert kimi_config is not None
    assert kimi_config.base_url == "https://api.moonshot.ai"
    assert kimi_config.discovery_base_url == "https://api.moonshot.ai/v1"
    assert kimi_config.model_map.get("kimi-k2.5") == "kimi-k2.5"

    opencode_go_config = registry.provider_config("opencode-go")
    assert opencode_go_config is not None
    assert opencode_go_config.base_url == "https://opencode.ai/zen/go"
    assert opencode_go_config.discovery_base_url == ""
    assert opencode_go_config.model_map.get("kimi-k2.5") == "kimi-k2.5"
    assert opencode_go_config.model_map.get("kimi-k2.6") == "kimi-k2.6"
    assert opencode_go_config.model_map.get("glm-5") == "glm-5"
    assert opencode_go_config.model_map.get("glm-5.1") == "glm-5.1"
    assert opencode_go_config.model_map.get("mimo-v2.5") == "mimo-v2.5"

    qwen_config = registry.provider_config("qwen")
    assert qwen_config is not None
    assert qwen_config.base_url == "https://dashscope.aliyuncs.com/compatible-mode"
    assert qwen_config.discovery_base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert qwen_config.model_map.get("qwen-plus") == "qwen-plus"


def test_registry_simplified_provider_user_model_map_overrides_default() -> None:
    registry = ModelProviderRegistry.with_defaults(
        provider_configs=ProviderConfigs(
            glm=SimplifiedProviderConfig(
                api_key="glm-key",
                model_map={"custom": "custom-model"},
            )
        )
    )

    config = registry.provider_config("glm")
    assert config is not None
    assert config.model_map == {"custom": "custom-model"}
    assert "glm-4-flash" not in config.model_map


def test_registry_simplified_provider_user_base_url_overrides_default() -> None:
    registry = ModelProviderRegistry.with_defaults(
        provider_configs=ProviderConfigs(
            glm=SimplifiedProviderConfig(
                api_key="glm-key",
                base_url="https://my-proxy.com/v1",
            )
        )
    )

    config = registry.provider_config("glm")
    assert config is not None
    assert config.base_url == "https://my-proxy.com/v1"


def test_registry_all_chinese_providers_resolve_correctly() -> None:
    registry = ModelProviderRegistry.with_defaults(
        provider_configs=ProviderConfigs(
            deepseek=SimplifiedProviderConfig(api_key="deepseek-key"),
            glm=SimplifiedProviderConfig(api_key="glm-key"),
            grok=SimplifiedProviderConfig(api_key="grok-key"),
            minimax=SimplifiedProviderConfig(api_key="minimax-key"),
            kimi=SimplifiedProviderConfig(api_key="kimi-key"),
            opencode_go=SimplifiedProviderConfig(api_key="opencode-go-key"),
            qwen=SimplifiedProviderConfig(api_key="qwen-key"),
        )
    )

    assert isinstance(registry.resolve("deepseek"), DeepSeekModelProvider)
    assert isinstance(registry.resolve("glm"), GLMModelProvider)
    assert isinstance(registry.resolve("grok"), GrokModelProvider)
    assert isinstance(registry.resolve("minimax"), MiniMaxModelProvider)
    assert isinstance(registry.resolve("kimi"), KimiModelProvider)
    assert isinstance(registry.resolve("opencode-go"), OpenCodeGoModelProvider)
    assert isinstance(registry.resolve("qwen"), QwenModelProvider)
