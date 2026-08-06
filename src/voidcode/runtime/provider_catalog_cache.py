from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Literal, cast

from ..provider.model_catalog import ProviderModelCatalog
from ..provider.registry import ModelProviderRegistry
from .provider_metadata import catalog_metadata_from_payload

logger = logging.getLogger(__name__)


class RuntimeProviderCatalogCache:
    """Persist and restore the runtime-owned provider model catalog cache."""

    def __init__(self, *, registry: ModelProviderRegistry, path: Path) -> None:
        self._registry = registry
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def hydrate(self) -> None:
        catalog = self._registry.model_catalog
        if catalog is None or catalog:
            return
        try:
            raw_payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return
        if not isinstance(raw_payload, dict):
            return
        raw_providers = cast(dict[str, object], raw_payload).get("providers")
        if not isinstance(raw_providers, dict):
            return

        hydrated: dict[str, ProviderModelCatalog] = {}
        for provider_name, raw_catalog in cast(dict[object, object], raw_providers).items():
            if not isinstance(provider_name, str) or not provider_name or "/" in provider_name:
                continue
            if not isinstance(raw_catalog, dict):
                continue
            catalog_payload = cast(dict[str, object], raw_catalog)
            raw_models = catalog_payload.get("models", [])
            if not isinstance(raw_models, list):
                continue
            models = tuple(raw_model for raw_model in cast(list[object], raw_models) if isinstance(raw_model, str) and raw_model)
            raw_metadata = catalog_payload.get("model_metadata", {})
            metadata_payloads: dict[object, object] = cast(dict[object, object], raw_metadata) if isinstance(raw_metadata, dict) else {}
            model_metadata = {
                model: catalog_metadata_from_payload(payload)
                for model, raw_payload in metadata_payloads.items()
                if isinstance(model, str) and isinstance(raw_payload, dict)
                for payload in (cast(dict[str, object], raw_payload),)
            }
            raw_discovery_mode = catalog_payload.get("discovery_mode")
            discovery_mode = (
                cast(
                    Literal[
                        "configured_endpoint",
                        "configured_base_url",
                        "disabled",
                        "unavailable",
                    ],
                    raw_discovery_mode,
                )
                if raw_discovery_mode in {"configured_endpoint", "configured_base_url", "disabled", "unavailable"}
                else "unavailable"
            )
            hydrated[provider_name] = ProviderModelCatalog(
                provider=provider_name,
                models=models,
                refreshed=bool(catalog_payload.get("refreshed", False)),
                model_metadata=model_metadata,
                source=(cast(str, catalog_payload["source"]) if isinstance(catalog_payload.get("source"), str) else "remote"),
                last_refresh_status=(
                    cast(str, catalog_payload["last_refresh_status"]) if isinstance(catalog_payload.get("last_refresh_status"), str) else "ok"
                ),
                last_error=(cast(str, catalog_payload["last_error"]) if isinstance(catalog_payload.get("last_error"), str) else None),
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
