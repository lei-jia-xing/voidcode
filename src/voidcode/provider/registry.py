from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

from .anthropic import AnthropicModelProvider
from .config import LiteLLMProviderConfig, ProviderConfigs
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
    merge_model_metadata,
)
from .models import ProviderResolutionSource
from .openai import OpenAIModelProvider
from .opencode_go import OpenCodeGoModelProvider
from .protocol import ModelTurnProvider, StubTurnProvider, TurnProvider
from .qwen import QwenModelProvider


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
        provider = self.providers.get(provider_name)
        if provider is not None:
            provider_config = getattr(provider, "provider_config", None)
            if callable(provider_config):
                return cast(LiteLLMProviderConfig | None, provider_config())
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
                    return merge_model_metadata(
                        inferred=infer_model_metadata(provider_name, model_name),
                        override=metadata,
                    )
        return infer_model_metadata(provider_name, model_name)

    def provider_catalog(self, provider_name: str) -> ProviderModelCatalog | None:
        if self.model_catalog is None:
            return None
        return self.model_catalog.get(provider_name)
