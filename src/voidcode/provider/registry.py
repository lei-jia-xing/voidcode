from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .anthropic import AnthropicModelProvider
from .config import LiteLLMProviderConfig, ProviderConfigs, SimplifiedProviderConfig
from .copilot import CopilotModelProvider
from .glm import GLMModelProvider
from .google import GoogleModelProvider
from .kimi import KimiModelProvider
from .litellm import LiteLLMModelProvider
from .minimax import MiniMaxModelProvider
from .model_catalog import ProviderModelCatalog, discover_available_models
from .openai import OpenAIModelProvider
from .opencode_go import OpenCodeGoModelProvider
from .protocol import ModelProvider, SingleAgentProvider, StubSingleAgentProvider
from .qwen import QwenModelProvider


@dataclass(frozen=True, slots=True)
class StaticModelProvider:
    name: str

    def single_agent_provider(self) -> SingleAgentProvider:
        return StubSingleAgentProvider(name=self.name)


# Defaults for simplified providers: (default_base_url, default_model_map)
_SIMPLIFIED_DEFAULTS: dict[str, tuple[str, dict[str, str]]] = {
    "glm": (
        "https://open.bigmodel.cn/api/paas/v4",
        {
            "glm-4-flash": "glm-4-flash",
            "glm-4-plus": "glm-4-plus",
            "glm-4": "glm-4-flash",
            "glm-5": "glm-5",
            "glm-5-turbo": "glm-5-turbo",
        },
    ),
    "minimax": (
        "https://api.minimax.io/v1",
        {
            "minimax-m2.7": "MiniMax-M2.7",
            "minimax-m2.5": "MiniMax-M2.5",
            "minimax-m2.1": "MiniMax-M2.1",
            "minimax-m2": "MiniMax-M2",
        },
    ),
    "kimi": (
        "https://api.moonshot.ai/v1",
        {
            "kimi-k2.5": "kimi-k2.5",
            "kimi-k2": "kimi-k2",
            "kimi-k2-turbo": "kimi-k2-turbo-preview",
            "kimi-k2-thinking": "kimi-k2-thinking",
        },
    ),
    "opencode-go": (
        "https://opencode.ai/zen/go/v1",
        {
            "kimi-k2.5": "kimi-k2.5",
            "minimax-m2.7": "minimax-m2.7",
            "minimax-m2.5": "minimax-m2.5",
            "glm-5": "glm-5",
            "glm-5.1": "glm-5.1",
            "mimo-v2-pro": "mimo-v2-pro",
            "mimo-v2-omni": "mimo-v2-omni",
            "qwen3.5-plus": "qwen3.5-plus",
            "qwen3.6-plus": "qwen3.6-plus",
        },
    ),
    "qwen": (
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        {
            "qwen-plus": "qwen-plus",
            "qwen-max": "qwen-max",
            "qwen-flash": "qwen-flash",
            "qwen3.5-plus": "qwen3.5-plus",
            "qwen3.5-flash": "qwen3.5-flash",
            "qwq-plus": "qwq-plus",
        },
    ),
}


@dataclass(slots=True)
class ModelProviderRegistry:
    providers: dict[str, ModelProvider]
    default_litellm_config: LiteLLMProviderConfig | None = None
    custom_provider_configs: Mapping[str, LiteLLMProviderConfig] | None = None
    model_catalog: dict[str, ProviderModelCatalog] | None = None

    @classmethod
    def with_defaults(
        cls, *, provider_configs: ProviderConfigs | None = None
    ) -> ModelProviderRegistry:
        configs = provider_configs or ProviderConfigs()
        return cls(
            providers={
                "opencode": StaticModelProvider(name="opencode"),
                "openai": OpenAIModelProvider(config=configs.openai),
                "anthropic": AnthropicModelProvider(config=configs.anthropic),
                "google": GoogleModelProvider(config=configs.google),
                "copilot": CopilotModelProvider(config=configs.copilot),
                "litellm": LiteLLMModelProvider(config=configs.litellm),
                "glm": GLMModelProvider(config=configs.glm),
                "minimax": MiniMaxModelProvider(config=configs.minimax),
                "kimi": KimiModelProvider(config=configs.kimi),
                "opencode-go": OpenCodeGoModelProvider(config=configs.opencode_go),
                "qwen": QwenModelProvider(config=configs.qwen),
            },
            default_litellm_config=configs.litellm,
            custom_provider_configs=configs.custom,
            model_catalog={},
        )

    def resolve(self, provider_name: str) -> ModelProvider:
        provider = self.providers.get(provider_name)
        if provider is not None:
            return provider
        if self.custom_provider_configs is not None:
            custom_config = self.custom_provider_configs.get(provider_name)
            if custom_config is not None:
                return LiteLLMModelProvider(name=provider_name, config=custom_config)
        return LiteLLMModelProvider(name=provider_name, config=self.default_litellm_config)

    def provider_config(self, provider_name: str) -> LiteLLMProviderConfig | None:
        if provider_name == "openai":
            provider = self.providers.get("openai")
            if isinstance(provider, OpenAIModelProvider):
                return LiteLLMProviderConfig(
                    api_key=None if provider.config is None else provider.config.api_key,
                    base_url=None if provider.config is None else provider.config.base_url,
                    timeout_seconds=None
                    if provider.config is None
                    else provider.config.timeout_seconds,
                )
        if provider_name == "anthropic":
            provider = self.providers.get("anthropic")
            if isinstance(provider, AnthropicModelProvider):
                return LiteLLMProviderConfig(
                    api_key=None if provider.config is None else provider.config.api_key,
                    base_url=None if provider.config is None else provider.config.base_url,
                    timeout_seconds=None
                    if provider.config is None
                    else provider.config.timeout_seconds,
                )
        if provider_name == "google":
            provider = self.providers.get("google")
            if isinstance(provider, GoogleModelProvider):
                api_key = None
                auth_header = None
                auth_scheme = "bearer"
                if provider.config is not None and provider.config.auth is not None:
                    if provider.config.auth.method == "api_key":
                        api_key = provider.config.auth.api_key
                        auth_header = "x-goog-api-key"
                        auth_scheme = "token"
                    elif provider.config.auth.method == "oauth":
                        api_key = provider.config.auth.access_token
                return LiteLLMProviderConfig(
                    api_key=api_key,
                    base_url=None if provider.config is None else provider.config.base_url,
                    auth_header=auth_header,
                    auth_scheme=auth_scheme,
                    timeout_seconds=None
                    if provider.config is None
                    else provider.config.timeout_seconds,
                )
        if provider_name == "copilot":
            provider = self.providers.get("copilot")
            if isinstance(provider, CopilotModelProvider):
                token = None
                if provider.config is not None and provider.config.auth is not None:
                    token = provider.config.auth.token
                return LiteLLMProviderConfig(
                    api_key=token,
                    base_url=None if provider.config is None else provider.config.base_url,
                    timeout_seconds=None
                    if provider.config is None
                    else provider.config.timeout_seconds,
                )
        if provider_name == "litellm":
            provider = self.providers.get("litellm")
            if isinstance(provider, LiteLLMModelProvider):
                return provider.config
        if provider_name == "glm":
            provider = self.providers.get("glm")
            if isinstance(provider, GLMModelProvider):
                return self._simplified_config_to_litellm("glm", provider.config)
        if provider_name == "minimax":
            provider = self.providers.get("minimax")
            if isinstance(provider, MiniMaxModelProvider):
                return self._simplified_config_to_litellm("minimax", provider.config)
        if provider_name == "kimi":
            provider = self.providers.get("kimi")
            if isinstance(provider, KimiModelProvider):
                return self._simplified_config_to_litellm("kimi", provider.config)
        if provider_name == "opencode-go":
            provider = self.providers.get("opencode-go")
            if isinstance(provider, OpenCodeGoModelProvider):
                return self._simplified_config_to_litellm("opencode-go", provider.config)
        if provider_name == "qwen":
            provider = self.providers.get("qwen")
            if isinstance(provider, QwenModelProvider):
                return self._simplified_config_to_litellm("qwen", provider.config)
        if (
            self.custom_provider_configs is not None
            and provider_name in self.custom_provider_configs
        ):
            return self.custom_provider_configs[provider_name]
        return self.default_litellm_config

    def available_models(self, provider_name: str) -> tuple[str, ...]:
        if self.model_catalog is None:
            return ()
        entry = self.model_catalog.get(provider_name)
        if entry is None:
            return ()
        return entry.models

    def refresh_available_models(self, provider_name: str) -> tuple[str, ...]:
        config = self.provider_config(provider_name)
        discovery = discover_available_models(provider_name, config)
        if self.model_catalog is not None:
            self.model_catalog[provider_name] = ProviderModelCatalog(
                provider=provider_name,
                models=discovery.models,
                refreshed=True,
                source=discovery.source,
                last_refresh_status=discovery.last_refresh_status,
                last_error=discovery.last_error,
            )
        return discovery.models

    def provider_catalog(self, provider_name: str) -> ProviderModelCatalog | None:
        if self.model_catalog is None:
            return None
        return self.model_catalog.get(provider_name)

    @staticmethod
    def _simplified_config_to_litellm(
        provider_name: str,
        config: SimplifiedProviderConfig | None,
    ) -> LiteLLMProviderConfig | None:
        if config is None:
            return None
        default = _SIMPLIFIED_DEFAULTS.get(provider_name, ("", {}))
        default_base_url, default_model_map = default
        return LiteLLMProviderConfig(
            api_key=config.api_key,
            base_url=config.base_url if config.base_url else default_base_url,
            timeout_seconds=config.timeout_seconds,
            model_map=config.model_map if config.model_map else default_model_map,
        )
