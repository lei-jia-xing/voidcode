from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .anthropic import AnthropicModelProvider
from .config import (
    AnthropicProviderConfig,
    CopilotProviderConfig,
    GoogleProviderConfig,
    OpenAIProviderConfig,
    ProviderConfigs,
    ProviderEndpointConfig,
)
from .copilot import CopilotModelProvider
from .deepseek import DeepSeekModelProvider
from .endpoint import OpenAIEndpointProvider
from .fireworks import FireworksModelProvider
from .google import GoogleModelProvider
from .grok import GrokModelProvider
from .groq import GroqModelProvider
from .kimi import KimiModelProvider
from .minimax import MiniMaxModelProvider
from .mistral import MistralModelProvider
from .model_catalog import (
    ProviderModelCatalog,
    ProviderModelMetadata,
    discover_available_models,
)
from .models import ProviderResolutionSource
from .openai import OpenAIModelProvider
from .opencode import OpenCodeModelProvider
from .opencode_go import OpenCodeGoModelProvider
from .openrouter import OpenRouterModelProvider
from .protocol import ModelTurnProvider, StubTurnProvider, TurnProvider
from .provider_config import (
    anthropic_provider_config,
    copilot_provider_config,
    google_provider_config,
    openai_provider_config,
)
from .qwen import QwenModelProvider
from .together import TogetherModelProvider
from .zai import ZAIModelProvider
from .zhipuai import ZhipuAIModelProvider


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


def _discovery_config(config: object) -> ProviderEndpointConfig | None:
    """Normalize any config shape a registry entry exposes into the single shape model discovery consumes."""
    if config is None or isinstance(config, ProviderEndpointConfig):
        return config
    if isinstance(config, OpenAIProviderConfig):
        return openai_provider_config(config)
    if isinstance(config, AnthropicProviderConfig):
        return anthropic_provider_config(config)
    if isinstance(config, GoogleProviderConfig):
        return google_provider_config(config)
    if isinstance(config, CopilotProviderConfig):
        return copilot_provider_config(config)
    return None


@dataclass(slots=True)
class ModelProviderRegistry:
    providers: dict[str, ModelTurnProvider]
    default_endpoint_config: ProviderEndpointConfig | None = None
    custom_provider_configs: Mapping[str, ProviderEndpointConfig] | None = None
    model_catalog: dict[str, ProviderModelCatalog] | None = None

    @classmethod
    def with_defaults(cls, *, provider_configs: ProviderConfigs | None = None) -> ModelProviderRegistry:
        configs = provider_configs or ProviderConfigs()
        return cls(
            providers={
                "opencode": OpenCodeModelProvider(config=configs.opencode),
                "openai": OpenAIModelProvider(config=configs.openai),
                "anthropic": AnthropicModelProvider(config=configs.anthropic),
                "google": GoogleModelProvider(config=configs.google),
                "copilot": CopilotModelProvider(config=configs.copilot),
                "endpoint": OpenAIEndpointProvider(name="endpoint", config=configs.endpoint),
                "deepseek": DeepSeekModelProvider(config=configs.deepseek),
                "openrouter": OpenRouterModelProvider(config=configs.openrouter),
                "zai": ZAIModelProvider(config=configs.zai),
                "zhipuai": ZhipuAIModelProvider(config=configs.zhipuai),
                "grok": GrokModelProvider(config=configs.grok),
                "minimax": MiniMaxModelProvider(config=configs.minimax),
                "kimi": KimiModelProvider(config=configs.kimi),
                "opencode-go": OpenCodeGoModelProvider(config=configs.opencode_go),
                "qwen": QwenModelProvider(config=configs.qwen),
                "groq": GroqModelProvider(config=configs.groq),
                "together": TogetherModelProvider(config=configs.together),
                "fireworks": FireworksModelProvider(config=configs.fireworks),
                "mistral": MistralModelProvider(config=configs.mistral),
            },
            default_endpoint_config=configs.endpoint,
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
                    provider=OpenAIEndpointProvider(name=provider_name, config=custom_config),
                    source="custom",
                    configured=True,
                )
        return ProviderResolution(
            provider_name=provider_name,
            provider=OpenAIEndpointProvider(name=provider_name, config=self.default_endpoint_config),
            source="default_endpoint",
            configured=self.default_endpoint_config is not None,
        )

    def resolve(self, provider_name: str) -> ModelTurnProvider:
        return self.resolve_with_metadata(provider_name).provider

    def provider_config(self, provider_name: str) -> ProviderEndpointConfig | None:
        provider = self.providers.get(provider_name)
        if provider is not None:
            provider_config = getattr(provider, "provider_config", None)
            if callable(provider_config):
                return _discovery_config(provider_config())
        if self.custom_provider_configs is not None and provider_name in self.custom_provider_configs:
            return self.custom_provider_configs[provider_name]
        return self.default_endpoint_config

    def available_models(self, provider_name: str) -> tuple[str, ...]:
        if self.model_catalog is None:
            return ()
        entry = self.model_catalog.get(provider_name)
        if entry is None:
            return ()
        return entry.models

    def refresh_available_models(self, provider_name: str) -> tuple[str, ...]:
        discovery = discover_available_models(provider_name, self.provider_config(provider_name))
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

    def model_metadata_for_model(self, provider_name: str, model_name: str) -> ProviderModelMetadata | None:
        if self.model_catalog is None:
            return None
        catalog = self.model_catalog.get(provider_name)
        if catalog is None:
            return None
        return catalog.model_metadata.get(model_name)

    def provider_catalog(self, provider_name: str) -> ProviderModelCatalog | None:
        if self.model_catalog is None:
            return None
        return self.model_catalog.get(provider_name)
