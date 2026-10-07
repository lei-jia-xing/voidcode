from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import cast

from .config import (
    PROVIDER_WIRES,
    AnthropicProviderConfig,
    CopilotProviderConfig,
    GoogleProviderConfig,
    OpenAICompatibleProviderConfig,
    OpenAIProviderConfig,
    ProviderConfigs,
    ProviderEndpointConfig,
    openai_compatible_endpoint_config,
)
from .model_catalog import ProviderModelCatalog, ProviderModelMetadata, discover_available_models, static_catalog_metadata
from .models import (
    BoundProviderChain,
    BoundProviderConfig,
    BoundProviderModel,
    ProviderDescriptor,
    ProviderModelSelection,
    ResolvedProviderConfig,
)
from .naming import UnknownProviderIdError, canonical_provider_id
from .protocol import ModelTurnProvider, TurnProvider
from .provider_config import anthropic_compatible_endpoint_config, resolved_provider_endpoint_config


@dataclass(frozen=True, slots=True)
class OpenAICompatibleModelProvider:
    """A named vendor whose wire is the OpenAI chat-completions protocol."""

    name: str
    config: OpenAICompatibleProviderConfig | None = None

    def provider_config(self) -> ProviderEndpointConfig:
        return openai_compatible_endpoint_config(self.name, self.config)

    def turn_provider(self) -> TurnProvider:
        from .openai_native import OpenAIChatCompletionsProvider

        return OpenAIChatCompletionsProvider(name=self.name, config=self.provider_config())


@dataclass(frozen=True, slots=True)
class AnthropicCompatibleModelProvider:
    """A named vendor whose wire is the Anthropic Messages protocol."""

    name: str
    config: AnthropicProviderConfig | None = None

    def provider_config(self) -> ProviderEndpointConfig:
        return anthropic_compatible_endpoint_config(self.name, self.config)

    def turn_provider(self) -> TurnProvider:
        from .anthropic_native import AnthropicMessagesProvider

        return AnthropicMessagesProvider(name=self.name, config=self.config)


def materialize_builtin_provider(descriptor: ProviderDescriptor) -> ModelTurnProvider:
    """Construct a genuine native adapter only when the admitted caller asks."""
    name, config = descriptor.provider_name, descriptor.configuration
    match name:
        case "opencode-zen":
            from .opencode import OpenCodeZenModelProvider

            return OpenCodeZenModelProvider(config=cast(ProviderEndpointConfig | None, config))
        case "openai":
            from .openai import OpenAIModelProvider

            return OpenAIModelProvider(config=cast(OpenAIProviderConfig | None, config))
        case "google":
            from .google import GoogleModelProvider

            return GoogleModelProvider(config=cast(GoogleProviderConfig | None, config))
        case "github-copilot":
            from .copilot import GithubCopilotModelProvider

            return GithubCopilotModelProvider(config=cast(CopilotProviderConfig | None, config))
        case "endpoint":
            from .endpoint import OpenAIEndpointProvider

            return OpenAIEndpointProvider(name=name, config=cast(ProviderEndpointConfig | None, config))
        case "openrouter":
            from .openrouter import OpenRouterModelProvider

            return OpenRouterModelProvider(config=cast(ProviderEndpointConfig | None, config))
        case "opencode-go":
            from .opencode_go import OpenCodeGoModelProvider

            return OpenCodeGoModelProvider(config=cast(OpenAICompatibleProviderConfig | None, config))
    wire = PROVIDER_WIRES.get(name)
    if wire is not None:
        if wire.shape == "anthropic":
            return AnthropicCompatibleModelProvider(name=name, config=cast(AnthropicProviderConfig | None, config))
        if wire.shape == "openai_compatible":
            return OpenAICompatibleModelProvider(name=name, config=cast(OpenAICompatibleProviderConfig | None, config))
    elif isinstance(config, ProviderEndpointConfig):
        from .endpoint import OpenAIEndpointProvider

        return OpenAIEndpointProvider(name=name, config=config)
    raise UnknownProviderIdError(name)


@dataclass(slots=True)
class ModelProviderRegistry:
    descriptors: Mapping[str, ProviderDescriptor]
    model_catalog: dict[str, ProviderModelCatalog] = field(default_factory=dict)
    _declarations: dict[str, ProviderDescriptor] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        declarations = self.descriptors
        self._declarations = {}
        self.descriptors = MappingProxyType(self._declarations)
        for name, descriptor in declarations.items():
            if canonical_provider_id(name) != descriptor.provider_name:
                raise ValueError("provider declaration key must match its canonical id")
            self.register(descriptor)

    def register(self, descriptor: ProviderDescriptor) -> None:
        """Register an actual static declaration; never overwrite another owner."""
        name = descriptor.provider_name
        if not name or "/" in name or canonical_provider_id(name) != name:
            raise ValueError("provider declaration must have a canonical nonempty id without '/'")
        if name in self._declarations:
            raise ValueError(f"provider id is already declared: {name}")
        if descriptor.catalog is not None and descriptor.catalog.provider != name:
            raise ValueError("provider catalog must belong to its declaring provider")
        self._declarations[name] = descriptor

    @classmethod
    def with_defaults(cls, *, provider_configs: ProviderConfigs | None = None) -> ModelProviderRegistry:
        configs = provider_configs or ProviderConfigs()
        descriptors: dict[str, ProviderDescriptor] = {}
        for name in PROVIDER_WIRES:
            config = configs.entry(name)
            descriptors[name] = ProviderDescriptor(
                provider_name=name,
                configuration=config,
                endpoint_config=resolved_provider_endpoint_config(name, config),
            )
        registry = cls(descriptors=descriptors)
        for name, config in configs.custom.items():
            registry.register(
                ProviderDescriptor(
                    provider_name=name,
                    configuration=config,
                    endpoint_config=resolved_provider_endpoint_config(name, config),
                )
            )
        return registry

    def resolve_static(self, provider_name: str) -> ProviderDescriptor:
        canonical_name = canonical_provider_id(provider_name)
        descriptor = self.descriptors.get(canonical_name)
        if descriptor is None:
            raise UnknownProviderIdError(canonical_name)
        return descriptor

    @staticmethod
    def bind(
        resolved_config: ResolvedProviderConfig,
        *,
        materialize: Callable[[ProviderDescriptor], ModelTurnProvider],
    ) -> BoundProviderConfig:
        """Bind captured declarations, never re-resolve against current config."""
        targets = resolved_config.target_chain.all_targets
        if not targets:
            if (
                resolved_config.model is not None
                or resolved_config.provider_fallback is not None
                or resolved_config.active_target.selection != ProviderModelSelection()
            ):
                raise ValueError("provider configuration has no selected target chain")
            return BoundProviderConfig()
        active_index: int | None = None
        declarations: dict[str, ProviderDescriptor] = {}
        for index, target in enumerate(targets):
            descriptor = target.descriptor
            selection = target.selection
            if descriptor is None or selection.provider != descriptor.provider_name or selection.raw_model is None or selection.model is None:
                raise ValueError("provider target is missing its static declaration")
            previous = declarations.setdefault(descriptor.provider_name, descriptor)
            if previous != descriptor:
                raise ValueError("one provider id cannot have conflicting selected declarations")
            if target == resolved_config.active_target:
                active_index = index
        if active_index is None:
            raise ValueError("active provider target must belong to its selected chain")
        providers: dict[str, ModelTurnProvider] = {}
        bound_targets: list[BoundProviderModel] = []
        for target in targets:
            descriptor = target.descriptor
            assert descriptor is not None
            provider = providers.get(descriptor.provider_name)
            if provider is None:
                provider = materialize(descriptor)
                if not isinstance(provider, ModelTurnProvider):
                    raise TypeError("provider materializer must return a genuine ModelTurnProvider")
                providers[descriptor.provider_name] = provider
            bound_targets.append(BoundProviderModel(selection=target.selection, provider=provider, metadata=target.metadata))
        bound_chain = tuple(bound_targets)
        return BoundProviderConfig(
            model=resolved_config.model,
            provider_fallback=resolved_config.provider_fallback,
            active_target=bound_chain[active_index],
            target_chain=BoundProviderChain(preferred=bound_chain[0], all_targets=bound_chain),
        )

    def provider_config(self, provider_name: str) -> ProviderEndpointConfig | None:
        return self.resolve_static(provider_name).endpoint_config

    def available_models(self, provider_name: str) -> tuple[str, ...]:
        catalog = self.provider_catalog(provider_name)
        return () if catalog is None else catalog.models

    def refresh_available_models(self, provider_name: str) -> tuple[str, ...]:
        descriptor = self.resolve_static(provider_name)
        if descriptor.endpoint_config is None:
            raise ValueError(f"provider {descriptor.provider_name!r} does not declare native endpoint model discovery")
        discovery = discover_available_models(descriptor.provider_name, descriptor.endpoint_config)
        self.model_catalog[descriptor.provider_name] = ProviderModelCatalog(
            provider=descriptor.provider_name,
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
        descriptor = self.resolve_static(provider_name)
        declared = descriptor.catalog.model_metadata.get(model_name) if descriptor.catalog is not None else None
        shipped = _merge_model_metadata(declared, static_catalog_metadata(descriptor.provider_name, model_name))
        catalog = self.model_catalog.get(descriptor.provider_name)
        discovered = catalog.model_metadata.get(model_name) if catalog is not None else None
        return _merge_model_metadata(discovered, shipped)

    def provider_catalog(self, provider_name: str) -> ProviderModelCatalog | None:
        descriptor = self.resolve_static(provider_name)
        return self.model_catalog.get(descriptor.provider_name, descriptor.catalog)


def _merge_model_metadata(
    primary: ProviderModelMetadata | None,
    fallback: ProviderModelMetadata | None,
) -> ProviderModelMetadata | None:
    if primary is None or fallback is None:
        return primary if primary is not None else fallback
    # The actual discovered row owns sizes/costs; fill only absent optional facts.
    return replace(
        primary,
        supports_reasoning_effort=(
            primary.supports_reasoning_effort if primary.supports_reasoning_effort is not None else fallback.supports_reasoning_effort
        ),
        default_reasoning_effort=(
            primary.default_reasoning_effort if primary.default_reasoning_effort is not None else fallback.default_reasoning_effort
        ),
        supported_effort_levels=primary.supported_effort_levels if primary.supported_effort_levels is not None else fallback.supported_effort_levels,
        api=primary.api if primary.api is not None else fallback.api,
        display_name=primary.display_name if primary.display_name is not None else fallback.display_name,
        modalities_output=primary.modalities_output if primary.modalities_output is not None else fallback.modalities_output,
    )
