from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, TypeIs

from ..provider.model_catalog import ProviderModelCatalog
from ..provider.registry import ModelProviderRegistry
from .provider_metadata import catalog_metadata_from_payload

logger = logging.getLogger(__name__)


def is_provider_discovery_mode(value: object) -> TypeIs[Literal["configured_base_url", "disabled", "unavailable"]]:
    """Whether a persisted ``discovery_mode`` token names one of the catalog discovery modes."""
    return value in ("configured_base_url", "disabled", "unavailable")


class RuntimeProviderCatalogCache:
    """Persist and restore the runtime-owned provider model catalog cache."""

    def __init__(self, *, registry: ModelProviderRegistry, path: Path) -> None:
        self._registry = registry
        self._path = path

    def hydrate(self) -> None:
        catalog = self._registry.model_catalog
        if catalog is None or catalog:
            return
        try:
            raw_payload = json.loads(self._path.read_text(encoding="utf-8"))
        except OSError, json.JSONDecodeError, UnicodeDecodeError:
            return
        if not isinstance(raw_payload, dict):
            return
        payload: Mapping[str, object] = raw_payload
        raw_providers = payload.get("providers")
        if not isinstance(raw_providers, dict):
            return

        hydrated: dict[str, ProviderModelCatalog] = {}
        provider_entries: Mapping[str, object] = raw_providers
        for provider_name, raw_catalog in provider_entries.items():
            if not isinstance(provider_name, str) or not provider_name or "/" in provider_name:
                continue
            if not isinstance(raw_catalog, dict):
                continue
            catalog_payload: Mapping[str, object] = raw_catalog
            raw_models = catalog_payload.get("models", [])
            if not isinstance(raw_models, list):
                continue
            models = tuple(raw_model for raw_model in raw_models if isinstance(raw_model, str) and raw_model)
            raw_metadata = catalog_payload.get("model_metadata", {})
            metadata_payloads: Mapping[str, object] = raw_metadata if isinstance(raw_metadata, dict) else {}
            model_metadata = {
                model: catalog_metadata_from_payload(raw_entry)
                for model, raw_entry in metadata_payloads.items()
                if isinstance(model, str) and isinstance(raw_entry, dict)
            }
            raw_discovery_mode = catalog_payload.get("discovery_mode")
            discovery_mode = raw_discovery_mode if is_provider_discovery_mode(raw_discovery_mode) else "unavailable"
            raw_source = catalog_payload.get("source")
            raw_last_refresh_status = catalog_payload.get("last_refresh_status")
            raw_last_error = catalog_payload.get("last_error")
            hydrated[provider_name] = ProviderModelCatalog(
                provider=provider_name,
                models=models,
                refreshed=bool(catalog_payload.get("refreshed", False)),
                model_metadata=model_metadata,
                source=raw_source if isinstance(raw_source, str) else "unknown",
                last_refresh_status=raw_last_refresh_status if isinstance(raw_last_refresh_status, str) else "unavailable",
                last_error=raw_last_error if isinstance(raw_last_error, str) else None,
                discovery_mode=discovery_mode,
            )
        catalog.update(hydrated)

    def persist(self) -> None:
        catalog = self._registry.model_catalog
        if catalog is None:
            return
        payload = {
            "version": 1,
            "providers": {
                provider_name: {
                    "provider": entry.provider,
                    "models": list(entry.models),
                    "model_metadata": {model: metadata.payload() for model, metadata in entry.model_metadata.items()},
                    "refreshed": entry.refreshed,
                    "source": entry.source,
                    "last_refresh_status": entry.last_refresh_status,
                    "last_error": entry.last_error,
                    "discovery_mode": entry.discovery_mode,
                }
                for provider_name, entry in sorted(catalog.items())
            },
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        except OSError:
            logger.debug("failed to persist provider model catalog cache", exc_info=True)


__all__ = ["RuntimeProviderCatalogCache"]
