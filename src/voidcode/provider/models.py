from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .config import ProviderFallbackConfig
from .model_catalog import ProviderModelMetadata
from .protocol import ModelTurnProvider

type ProviderResolutionSource = Literal["builtin", "custom", "default_litellm"]


@dataclass(frozen=True, slots=True)
class ProviderResolutionMetadata:
    source: ProviderResolutionSource | None = None
    configured: bool = False


@dataclass(frozen=True, slots=True)
class ProviderModelSelection:
    raw_model: str | None = None
    provider: str | None = None
    model: str | None = None


@dataclass(frozen=True, slots=True)
class ResolvedProviderModel:
    selection: ProviderModelSelection = ProviderModelSelection()
    provider: ModelTurnProvider | None = None
    resolution: ProviderResolutionMetadata = ProviderResolutionMetadata()
    metadata: ProviderModelMetadata | None = None


@dataclass(frozen=True, slots=True)
class ResolvedProviderChain:
    preferred: ResolvedProviderModel = ResolvedProviderModel()
    fallbacks: tuple[ResolvedProviderModel, ...] = ()
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
