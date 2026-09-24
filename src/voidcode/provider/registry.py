from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace

from .anthropic_native import AnthropicMessagesProvider
from .auth import ANTHROPIC_COMPATIBLE_BUILTIN_FIELDS, OPENAI_COMPATIBLE_BUILTIN_FIELDS
from .config import (
    AnthropicProviderConfig,
    OpenAICompatibleProviderConfig,
    ProviderConfigs,
    ProviderEndpointConfig,
    openai_compatible_endpoint_config,
)
from .copilot import GithubCopilotModelProvider
from .endpoint import OpenAIEndpointProvider
from .google import GoogleModelProvider
from .model_catalog import (
    ProviderModelCatalog,
    ProviderModelMetadata,
    discover_available_models,
    static_catalog_metadata,
)
from .models import ProviderResolutionSource
from .naming import UnknownProviderIdError, canonical_provider_id
from .openai import OpenAIModelProvider
from .openai_native import OpenAIChatCompletionsProvider
from .opencode import OpenCodeZenModelProvider
from .opencode_go import OpenCodeGoModelProvider
from .openrouter import OpenRouterModelProvider
from .protocol import ModelTurnProvider, TurnProvider
from .provider_config import anthropic_compatible_endpoint_config


@dataclass(frozen=True, slots=True)
class OpenAICompatibleModelProvider:
    """A named vendor whose wire is the OpenAI chat-completions protocol.

    A vendor is a table entry, not a module: ``name`` selects its endpoint
    defaults -- base URL, discovery URL and credential environment variable --
    inside ``openai_compatible_endpoint_config``, which owns the vendor table and
    rejects a name that is not in it.
    """

    name: str
    config: OpenAICompatibleProviderConfig | None = None

    def provider_config(self) -> ProviderEndpointConfig:
        return openai_compatible_endpoint_config(self.name, self.config)

    def turn_provider(self) -> TurnProvider:
        return OpenAIChatCompletionsProvider(name=self.name, config=self.provider_config())


@dataclass(frozen=True, slots=True)
class AnthropicCompatibleModelProvider:
    """A named vendor whose wire is the Anthropic Messages protocol.

    Same contract as the OpenAI-compatible adapter: ``name`` selects the vendor's
    endpoint defaults inside ``anthropic_compatible_endpoint_config``, which owns
    the Anthropic-wire vendor table and rejects a name that is not in it.
    """

    name: str
    config: AnthropicProviderConfig | None = None

    def provider_config(self) -> ProviderEndpointConfig:
        return anthropic_compatible_endpoint_config(self.name, self.config)

    def turn_provider(self) -> TurnProvider:
        return AnthropicMessagesProvider(name=self.name, config=self.config)


@dataclass(frozen=True, slots=True)
class ProviderResolution:
    provider_name: str
    provider: ModelTurnProvider
    source: ProviderResolutionSource
    configured: bool


@dataclass(slots=True)
class ModelProviderRegistry:
    providers: dict[str, ModelTurnProvider]
    custom_provider_configs: Mapping[str, ProviderEndpointConfig] | None = None
    model_catalog: dict[str, ProviderModelCatalog] | None = None

    @classmethod
    def with_defaults(cls, *, provider_configs: ProviderConfigs | None = None) -> ModelProviderRegistry:
        configs = provider_configs or ProviderConfigs()
        # Adapters that are not shared per-wire ones: a gateway that routes per
        # model, a credential flow of its own, or a wire of its own.
        providers: dict[str, ModelTurnProvider] = {
            "opencode-zen": OpenCodeZenModelProvider(config=configs.opencode_zen),
            "openai": OpenAIModelProvider(config=configs.openai),
            "google": GoogleModelProvider(config=configs.google),
            "github-copilot": GithubCopilotModelProvider(config=configs.github_copilot),
            "endpoint": OpenAIEndpointProvider(name="endpoint", config=configs.endpoint),
            "openrouter": OpenRouterModelProvider(config=configs.openrouter),
            "opencode-go": OpenCodeGoModelProvider(config=configs.opencode_go),
        }
        # Every remaining built-in id is a vendor on one shared wire adapter. The
        # ids and their ``ProviderConfigs`` fields both come from the payload
        # schema, so a new vendor is a table entry plus a config field, never a
        # module; an id already registered above keeps its own adapter.
        shared_wire_vendors = (
            (AnthropicCompatibleModelProvider, ANTHROPIC_COMPATIBLE_BUILTIN_FIELDS),
            (OpenAICompatibleModelProvider, OPENAI_COMPATIBLE_BUILTIN_FIELDS),
        )
        for adapter, vendor_fields in shared_wire_vendors:
            for provider_name, field_name in vendor_fields.items():
                if provider_name in providers:
                    continue
                providers[provider_name] = adapter(name=provider_name, config=getattr(configs, field_name))
        return cls(
            providers=providers,
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
            # Optional capability, not a contract field: only the wire adapters expose a
            # config, so one without it falls through to the registry's own map below.
            provider_config = getattr(provider, "provider_config", None)
            if callable(provider_config):
                return provider_config()
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
        shipped = static_catalog_metadata(canonical_name, model_name)
        if discovered is None or shipped is None:
            return discovered if discovered is not None else shipped
        # A discovered entry - including one hydrated from a catalog cache written
        # by an older build - owns model sizes and costs, but it may predate the
        # shipped reasoning-effort, wire and display facts. Fill only the fields it
        # leaves unset instead of letting a stale cache hide them: the runtime gate
        # and the effort clamp read the effort fields, and wire dispatch reads
        # ``api``.
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
            api=discovered.api if discovered.api is not None else shipped.api,
            display_name=discovered.display_name if discovered.display_name is not None else shipped.display_name,
            modalities_output=(discovered.modalities_output if discovered.modalities_output is not None else shipped.modalities_output),
        )

    def provider_catalog(self, provider_name: str) -> ProviderModelCatalog | None:
        if self.model_catalog is None:
            return None
        return self.model_catalog.get(canonical_provider_id(provider_name))
