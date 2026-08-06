from __future__ import annotations

from ..provider.model_catalog import infer_model_metadata, merge_model_metadata
from ..provider.registry import ModelProviderRegistry
from .contracts import ProviderModelMetadata, ProviderModelsResult
from .provider_metadata import contract_metadata_from_catalog


class RuntimeProviderCatalogQuery:
    """Read-only projections over the runtime provider model catalog."""

    def __init__(self, *, registry: ModelProviderRegistry) -> None:
        self._registry = registry

    def models(self, provider_name: str) -> tuple[str, ...]:
        self._validate_provider_name(provider_name)
        return self._registry.available_models(provider_name)

    def catalog_payload(self, provider_name: str) -> dict[str, object] | None:
        self._validate_provider_name(provider_name)
        catalog = self._registry.provider_catalog(provider_name)
        if catalog is None:
            return None
        return {
            "provider": catalog.provider,
            "models": list(catalog.models),
            "model_metadata": {model: metadata.payload() for model, metadata in catalog.model_metadata.items()},
            "refreshed": catalog.refreshed,
            "source": catalog.source,
            "last_refresh_status": catalog.last_refresh_status,
            "last_error": catalog.last_error,
            "discovery_mode": catalog.discovery_mode,
        }

    def metadata_for_model(
        self,
        provider_name: str,
        model_name: str,
    ) -> ProviderModelMetadata | None:
        self._validate_provider_name(provider_name)
        catalog = self._registry.provider_catalog(provider_name)
        if catalog is not None:
            catalog_metadata = catalog.model_metadata.get(model_name)
            if catalog_metadata is not None:
                merged = merge_model_metadata(
                    inferred=infer_model_metadata(provider_name, model_name),
                    override=catalog_metadata,
                )
                if merged is not None:
                    return contract_metadata_from_catalog(merged)
        inferred = infer_model_metadata(provider_name, model_name)
        if inferred is None:
            return None
        return contract_metadata_from_catalog(inferred)

    def models_result(
        self,
        provider_name: str,
        *,
        configured: bool,
    ) -> ProviderModelsResult:
        self._validate_provider_name(provider_name)
        catalog = self._registry.provider_catalog(provider_name)
        if catalog is None:
            return ProviderModelsResult(
                provider=provider_name,
                configured=configured,
                models=(),
            )
        return ProviderModelsResult(
            provider=provider_name,
            configured=configured,
            models=catalog.models,
            model_metadata={
                model: metadata
                for model in catalog.model_metadata
                for metadata in (self.metadata_for_model(provider_name, model),)
                if metadata is not None
            },
            source=catalog.source,
            last_refresh_status=catalog.last_refresh_status,
            last_error=catalog.last_error,
            discovery_mode=catalog.discovery_mode,
        )

    @staticmethod
    def _validate_provider_name(provider_name: str) -> None:
        if not provider_name or "/" in provider_name:
            raise ValueError("provider_name must be a non-empty provider id without '/'")


__all__ = ["RuntimeProviderCatalogQuery"]
