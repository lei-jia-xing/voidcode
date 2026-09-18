from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace

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
    static_catalog_metadata,
)
from .models import ProviderResolutionSource
from .naming import UnknownProviderIdError, canonical_provider_id
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
            custom_provider_configs=configs.custom,
            model_catalog={},
        )

    def resolve_with_metadata(self, provider_name: str) -> ProviderResolution:
        """Resolve one provider id, rejecting ids nothing declares.

        The id is canonicalised first, so ``MiniMax`` resolves exactly like
        ``minimax``. An id that is neither a built-in nor declared under
        ``providers.custom`` raises instead of borrowing the generic endpoint
        provider: an undeclared prefix must not silently reach a host the user
        did not name.
        """
        canonical_name = canonical_provider_id(provider_name)
        provider = self.providers.get(canonical_name)
        if provider is not None:
            return ProviderResolution(
                provider_name=canonical_name,
                provider=provider,
                source="builtin",
                configured=True,
            )
        if self.custom_provider_configs is not None:
            custom_config = self.custom_provider_configs.get(canonical_name)
            if custom_config is not None:
                return ProviderResolution(
                    provider_name=canonical_name,
                    provider=OpenAIEndpointProvider(name=canonical_name, config=custom_config),
                    source="custom",
                    configured=True,
                )
        raise UnknownProviderIdError(canonical_name)

    def resolve(self, provider_name: str) -> ModelTurnProvider:
        return self.resolve_with_metadata(provider_name).provider

    def provider_config(self, provider_name: str) -> ProviderEndpointConfig | None:
        canonical_name = canonical_provider_id(provider_name)
        provider = self.providers.get(canonical_name)
        if provider is not None:
            provider_config = getattr(provider, "provider_config", None)
            if callable(provider_config):
                return _discovery_config(provider_config())
        if self.custom_provider_configs is not None:
            return self.custom_provider_configs.get(canonical_name)
        return None

    def available_models(self, provider_name: str) -> tuple[str, ...]:
        if self.model_catalog is None:
            return ()
        entry = self.model_catalog.get(canonical_provider_id(provider_name))
        if entry is None:
            return ()
        return entry.models

    def refresh_available_models(self, provider_name: str) -> tuple[str, ...]:
        canonical_name = canonical_provider_id(provider_name)
        discovery = discover_available_models(canonical_name, self.provider_config(canonical_name))
        if self.model_catalog is not None:
            self.model_catalog[canonical_name] = ProviderModelCatalog(
                provider=canonical_name,
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
        canonical_name = canonical_provider_id(provider_name)
        catalog = self.model_catalog.get(canonical_name) if self.model_catalog is not None else None
        discovered = catalog.model_metadata.get(model_name) if catalog is not None else None
        if discovered is not None and (discovered.supports_reasoning_effort is not None or discovered.supported_effort_levels is not None):
            return discovered
        shipped = static_catalog_metadata(canonical_name, model_name)
        if shipped is None or discovered is None:
            return shipped if discovered is None else discovered
        # A discovered entry - including one hydrated from a catalog cache written by
        # an older build - owns model sizes and costs, but it may predate the shipped
        # reasoning-effort facts. Fill only the effort fields it leaves unset instead
        # of letting a stale cache hide the model's own capability, which is what the
        # runtime gate and the effort clamp read.
        return replace(
            discovered,
            supports_reasoning_effort=(
                discovered.supports_reasoning_effort if discovered.supports_reasoning_effort is not None else shipped.supports_reasoning_effort
            ),
            default_reasoning_effort=(
                discovered.default_reasoning_effort if discovered.default_reasoning_effort is not None else shipped.default_reasoning_effort
            ),
            supported_effort_levels=(
                discovered.supported_effort_levels if discovered.supported_effort_levels is not None else shipped.supported_effort_levels
            ),
        )

    def provider_catalog(self, provider_name: str) -> ProviderModelCatalog | None:
        if self.model_catalog is None:
            return None
        return self.model_catalog.get(canonical_provider_id(provider_name))
