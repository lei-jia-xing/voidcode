from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .anthropic import AnthropicModelProvider
from .config import LiteLLMProviderConfig, ProviderConfigs
from .copilot import CopilotModelProvider
from .google import GoogleModelProvider
from .litellm import LiteLLMModelProvider
from .model_catalog import ProviderModelCatalog, discover_available_models
from .openai import OpenAIModelProvider
from .protocol import ModelProvider, SingleAgentProvider, StubSingleAgentProvider


@dataclass(frozen=True, slots=True)
class StaticModelProvider:
    name: str

    def single_agent_provider(self) -> SingleAgentProvider:
        return StubSingleAgentProvider(name=self.name)


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
