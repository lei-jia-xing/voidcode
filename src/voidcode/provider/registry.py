from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

from .anthropic import AnthropicModelProvider
from .config import (
    LiteLLMProviderConfig,
    ProviderConfigs,
    SimplifiedProviderConfig,
    simplified_config_to_litellm,
)
from .copilot import CopilotModelProvider
from .deepseek import DeepSeekModelProvider
from .glm import GLMModelProvider
from .google import GoogleModelProvider
from .grok import GrokModelProvider
from .kimi import KimiModelProvider
from .litellm import LiteLLMModelProvider
from .minimax import MiniMaxModelProvider
from .model_catalog import (
    ProviderModelCatalog,
    ProviderModelMetadata,
    discover_available_models,
    infer_model_metadata,
)
from .models import ProviderResolutionSource
from .openai import OpenAIModelProvider
from .opencode_go import OpenCodeGoModelProvider
from .protocol import ModelTurnProvider, StubTurnProvider, TurnProvider
from .qwen import QwenModelProvider

_DEFAULT_DISCOVERY_BASE_URLS: dict[str, str] = {
    "openai": "https://api.openai.com",
    "anthropic": "https://api.anthropic.com",
    "google": "https://generativelanguage.googleapis.com",
    "litellm": "http://127.0.0.1:4000",
}

_DEFAULT_MODEL_MAPS: dict[str, dict[str, str]] = {
    "openai": {
        "gpt-5.5": "gpt-5.5",
        "gpt-5.4": "gpt-5.4",
        "gpt-5.4-mini": "gpt-5.4-mini",
        "gpt-5.4-nano": "gpt-5.4-nano",
        "gpt-5.2": "gpt-5.2",
        "gpt-5.2-pro": "gpt-5.2-pro",
        "gpt-5.2-codex": "gpt-5.2-codex",
        "gpt-5.1": "gpt-5.1",
        "gpt-5.1-codex": "gpt-5.1-codex",
        "gpt-5.1-codex-max": "gpt-5.1-codex-max",
        "gpt-5": "gpt-5",
        "gpt-5-mini": "gpt-5-mini",
        "gpt-5-nano": "gpt-5-nano",
        "gpt-4.1": "gpt-4.1",
        "gpt-4.1-mini": "gpt-4.1-mini",
        "gpt-4.1-nano": "gpt-4.1-nano",
    },
    "anthropic": {
        "claude-opus-4-7": "claude-opus-4-7",
        "claude-sonnet-4-6": "claude-sonnet-4-6",
        "claude-haiku-4-5": "claude-haiku-4-5",
        "claude-haiku-4-5-20251001": "claude-haiku-4-5-20251001",
    },
    "google": {
        "gemini-3-pro-preview": "gemini-3-pro-preview",
        "gemini-3-flash-preview": "gemini-3-flash-preview",
        "gemini-2.5-pro": "gemini-2.5-pro",
        "gemini-2.5-flash": "gemini-2.5-flash",
        "gemini-2.5-flash-lite": "gemini-2.5-flash-lite",
        "gemini-3.1-flash-live-preview": "gemini-3.1-flash-live-preview",
        "gemini-3.1-flash-tts-preview": "gemini-3.1-flash-tts-preview",
    },
}


def _default_discovery_base_url(
    provider_name: str,
    *,
    configured_base_url: str | None,
    configured_discovery_base_url: str | None,
) -> str | None:
    if configured_discovery_base_url is not None:
        return configured_discovery_base_url
    if configured_base_url is not None:
        return None
    return _DEFAULT_DISCOVERY_BASE_URLS.get(provider_name)


_SIMPLIFIED_PROVIDER_MAP: dict[str, type] = {
    "deepseek": DeepSeekModelProvider,
    "glm": GLMModelProvider,
    "grok": GrokModelProvider,
    "minimax": MiniMaxModelProvider,
    "kimi": KimiModelProvider,
    "opencode-go": OpenCodeGoModelProvider,
    "qwen": QwenModelProvider,
}


@dataclass(frozen=True, slots=True)
class StaticModelProvider:
    name: str

    def turn_provider(self) -> TurnProvider:
        return StubTurnProvider(name=self.name)


@dataclass(frozen=True, slots=True)
class ProviderResolution:
    provider_name: str
    provider: ModelTurnProvider
    source: ProviderResolutionSource
    configured: bool


@dataclass(slots=True)
class ModelProviderRegistry:
    providers: dict[str, ModelTurnProvider]
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
                "deepseek": DeepSeekModelProvider(config=configs.deepseek),
                "glm": GLMModelProvider(config=configs.glm),
                "grok": GrokModelProvider(config=configs.grok),
                "minimax": MiniMaxModelProvider(config=configs.minimax),
                "kimi": KimiModelProvider(config=configs.kimi),
                "opencode-go": OpenCodeGoModelProvider(config=configs.opencode_go),
                "qwen": QwenModelProvider(config=configs.qwen),
            },
            default_litellm_config=configs.litellm,
            custom_provider_configs=configs.custom,
            model_catalog={},
        )

    def resolve_with_metadata(self, provider_name: str) -> ProviderResolution:
        provider = self.providers.get(provider_name)
        if provider is not None:
            return ProviderResolution(
                provider_name=provider_name,
                provider=provider,
                source="builtin",
                configured=True,
            )
        if self.custom_provider_configs is not None:
            custom_config = self.custom_provider_configs.get(provider_name)
            if custom_config is not None:
                return ProviderResolution(
                    provider_name=provider_name,
                    provider=LiteLLMModelProvider(name=provider_name, config=custom_config),
                    source="custom",
                    configured=True,
                )
        return ProviderResolution(
            provider_name=provider_name,
            provider=LiteLLMModelProvider(name=provider_name, config=self.default_litellm_config),
            source="default_litellm",
            configured=self.default_litellm_config is not None,
        )

    def resolve(self, provider_name: str) -> ModelTurnProvider:
        return self.resolve_with_metadata(provider_name).provider

    def provider_config(self, provider_name: str) -> LiteLLMProviderConfig | None:
        if provider_name == "openai":
            provider = self.providers.get("openai")
            if isinstance(provider, OpenAIModelProvider):
                return LiteLLMProviderConfig(
                    api_key=None if provider.config is None else provider.config.api_key,
                    base_url=None if provider.config is None else provider.config.base_url,
                    discovery_base_url=(
                        _default_discovery_base_url(
                            "openai",
                            configured_base_url=(
                                None if provider.config is None else provider.config.base_url
                            ),
                            configured_discovery_base_url=(
                                None
                                if provider.config is None
                                else provider.config.discovery_base_url
                            ),
                        )
                    ),
                    timeout_seconds=None
                    if provider.config is None
                    else provider.config.timeout_seconds,
                    model_map=dict(_DEFAULT_MODEL_MAPS["openai"]),
                )
        if provider_name == "anthropic":
            provider = self.providers.get("anthropic")
            if isinstance(provider, AnthropicModelProvider):
                return LiteLLMProviderConfig(
                    api_key=None if provider.config is None else provider.config.api_key,
                    base_url=None if provider.config is None else provider.config.base_url,
                    discovery_base_url=(
                        _default_discovery_base_url(
                            "anthropic",
                            configured_base_url=(
                                None if provider.config is None else provider.config.base_url
                            ),
                            configured_discovery_base_url=(
                                None
                                if provider.config is None
                                else provider.config.discovery_base_url
                            ),
                        )
                    ),
                    timeout_seconds=None
                    if provider.config is None
                    else provider.config.timeout_seconds,
                    model_map=dict(_DEFAULT_MODEL_MAPS["anthropic"]),
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
                    discovery_base_url=(
                        _default_discovery_base_url(
                            "google",
                            configured_base_url=(
                                None if provider.config is None else provider.config.base_url
                            ),
                            configured_discovery_base_url=(
                                None
                                if provider.config is None
                                else provider.config.discovery_base_url
                            ),
                        )
                    ),
                    auth_header=auth_header,
                    auth_scheme=auth_scheme,
                    timeout_seconds=None
                    if provider.config is None
                    else provider.config.timeout_seconds,
                    model_map=dict(_DEFAULT_MODEL_MAPS["google"]),
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
                if provider.config is None:
                    return LiteLLMProviderConfig(
                        discovery_base_url=_DEFAULT_DISCOVERY_BASE_URLS["litellm"]
                    )
                return LiteLLMProviderConfig(
                    api_key=provider.config.api_key,
                    api_key_env_var=provider.config.api_key_env_var,
                    base_url=provider.config.base_url,
                    discovery_base_url=_default_discovery_base_url(
                        "litellm",
                        configured_base_url=provider.config.base_url,
                        configured_discovery_base_url=provider.config.discovery_base_url,
                    ),
                    auth_header=provider.config.auth_header,
                    auth_scheme=provider.config.auth_scheme,
                    timeout_seconds=provider.config.timeout_seconds,
                    model_map=dict(provider.config.model_map),
                )
        provider_cls = _SIMPLIFIED_PROVIDER_MAP.get(provider_name)
        if provider_cls is not None:
            provider = self.providers.get(provider_name)
            if isinstance(provider, provider_cls):
                return simplified_config_to_litellm(
                    provider_name, cast(SimplifiedProviderConfig | None, cast(Any, provider).config)
                )
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
                model_metadata=discovery.model_metadata,
                source=discovery.source,
                last_refresh_status=discovery.last_refresh_status,
                last_error=discovery.last_error,
                discovery_mode=discovery.discovery_mode,
            )
        return discovery.models

    def model_metadata_for_model(
        self, provider_name: str, model_name: str
    ) -> ProviderModelMetadata | None:
        if self.model_catalog is not None:
            catalog = self.model_catalog.get(provider_name)
            if catalog is not None:
                metadata = catalog.model_metadata.get(model_name)
                if metadata is not None:
                    return metadata
        return infer_model_metadata(provider_name, model_name)

    def provider_catalog(self, provider_name: str) -> ProviderModelCatalog | None:
        if self.model_catalog is None:
            return None
        return self.model_catalog.get(provider_name)
