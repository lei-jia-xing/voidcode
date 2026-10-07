from __future__ import annotations

from dataclasses import dataclass

from .config import ProviderEndpointConfig, ProviderFallbackConfig
from .model_catalog import ProviderModelCatalog, ProviderModelMetadata
from .protocol import ModelTurnProvider


@dataclass(frozen=True, slots=True)
class ProviderDescriptor:
    """Pure declaration with its owner's actual typed configuration and catalog."""

    provider_name: str
    configuration: object = None
    endpoint_config: ProviderEndpointConfig | None = None
    catalog: ProviderModelCatalog | None = None


@dataclass(frozen=True, slots=True)
class ProviderModelSelection:
    raw_model: str | None = None
    provider: str | None = None
    model: str | None = None


@dataclass(frozen=True, slots=True)
class ResolvedProviderModel:
    selection: ProviderModelSelection = ProviderModelSelection()
    descriptor: ProviderDescriptor | None = None
    metadata: ProviderModelMetadata | None = None


@dataclass(frozen=True, slots=True)
class ResolvedProviderChain:
    preferred: ResolvedProviderModel = ResolvedProviderModel()
    all_targets: tuple[ResolvedProviderModel, ...] = ()

    def target_at(self, index: int) -> ResolvedProviderModel | None:
        if index < 0 or index >= len(self.all_targets):
            return None
        return self.all_targets[index]


@dataclass(frozen=True, slots=True)
class ResolvedProviderConfig:
    model: str | None = None
    provider_fallback: ProviderFallbackConfig | None = None
    active_target: ResolvedProviderModel = ResolvedProviderModel()
    target_chain: ResolvedProviderChain = ResolvedProviderChain()


@dataclass(frozen=True, slots=True)
class BoundProviderModel:
    selection: ProviderModelSelection
    provider: ModelTurnProvider
    metadata: ProviderModelMetadata | None = None


@dataclass(frozen=True, slots=True)
class BoundProviderChain:
    preferred: BoundProviderModel | None = None
    all_targets: tuple[BoundProviderModel, ...] = ()

    def target_at(self, index: int) -> BoundProviderModel | None:
        if index < 0 or index >= len(self.all_targets):
            return None
        return self.all_targets[index]


@dataclass(frozen=True, slots=True)
class BoundProviderConfig:
    model: str | None = None
    provider_fallback: ProviderFallbackConfig | None = None
    active_target: BoundProviderModel | None = None
    target_chain: BoundProviderChain = BoundProviderChain()
