from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import cast
from urllib.error import URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .config import LiteLLMProviderConfig


@dataclass(frozen=True, slots=True)
class DiscoveryRequest:
    provider: str
    base_url: str
    headers: dict[str, str]
    timeout_seconds: float
    api_key: str | None


type ModelCatalogFetcher = Callable[[DiscoveryRequest], tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class ProviderModelCatalog:
    provider: str
    models: tuple[str, ...]
    refreshed: bool
    source: str = "remote"
    last_refresh_status: str = "ok"
    last_error: str | None = None


@dataclass(frozen=True, slots=True)
class ModelDiscoveryResult:
    models: tuple[str, ...]
    source: str
    last_refresh_status: str
    last_error: str | None


def discover_available_models(
    provider_name: str,
    config: LiteLLMProviderConfig | None,
    *,
    fetcher: ModelCatalogFetcher | None = None,
) -> ModelDiscoveryResult:
    discovered: tuple[str, ...] = ()
    error_message: str | None = None
    source = "remote"
    refresh_status = "ok"
    request = _build_discovery_request(provider_name=provider_name, config=config)
    if request is not None:
        active_fetcher = _fetch_models if fetcher is None else fetcher
        try:
            discovered = active_fetcher(request)
        except (ValueError, OSError, TimeoutError, URLError):
            discovered = ()
            source = "fallback"
            refresh_status = "failed"
            error_message = "remote model discovery failed"
    else:
        source = "fallback"
        refresh_status = "skipped"
        error_message = "provider has no model discovery endpoint"

    mapped_aliases = tuple(config.model_map.keys()) if config is not None else ()
    mapped_targets = tuple(config.model_map.values()) if config is not None else ()
    ordered = (*mapped_aliases, *discovered, *mapped_targets)
    deduped: list[str] = []
    seen: set[str] = set()
    for model in ordered:
        if not model or model in seen:
            continue
        seen.add(model)
        deduped.append(model)
    if source == "remote" and not discovered and deduped:
        source = "mixed"

    return ModelDiscoveryResult(
        models=tuple(deduped),
        source=source,
        last_refresh_status=refresh_status,
        last_error=error_message,
    )


def _base_url_for_discovery(
    *, provider_name: str, config: LiteLLMProviderConfig | None
) -> str | None:
    if config is not None and config.base_url:
        return config.base_url.rstrip("/")
    if provider_name == "openai":
        return "https://api.openai.com"
    if provider_name == "anthropic":
        return "https://api.anthropic.com"
    if provider_name == "google":
        return "https://generativelanguage.googleapis.com"
    if provider_name == "litellm":
        return "http://127.0.0.1:4000"
    return None


def _timeout_for_discovery(config: LiteLLMProviderConfig | None) -> float:
    if config is None or config.timeout_seconds is None:
        return 10.0
    return max(1.0, float(config.timeout_seconds))


def _headers_for_discovery(config: LiteLLMProviderConfig | None) -> dict[str, str]:
    if config is None or config.api_key is None or config.auth_scheme == "none":
        return {}
    if config.auth_scheme == "token":
        return {config.auth_header or "Authorization": config.api_key}
    header_name = config.auth_header or "Authorization"
    if header_name == "Authorization":
        return {header_name: f"Bearer {config.api_key}"}
    return {header_name: f"Bearer {config.api_key}"}


def _build_discovery_request(
    *, provider_name: str, config: LiteLLMProviderConfig | None
) -> DiscoveryRequest | None:
    base_url = _base_url_for_discovery(provider_name=provider_name, config=config)
    if base_url is None:
        return None

    provider = provider_name.strip().lower()
    timeout_seconds = _timeout_for_discovery(config)
    headers = _headers_for_discovery(config)
    if provider == "anthropic":
        api_key = None if config is None else config.api_key
        anthropic_headers: dict[str, str] = {"anthropic-version": "2023-06-01"}
        if api_key is not None:
            anthropic_headers["x-api-key"] = api_key
        headers = anthropic_headers

    return DiscoveryRequest(
        provider=provider,
        base_url=base_url,
        headers=headers,
        timeout_seconds=timeout_seconds,
        api_key=None if config is None else config.api_key,
    )


def _fetch_models(request: DiscoveryRequest) -> tuple[str, ...]:
    if request.provider == "google":
        return _fetch_google_models(request)
    if request.provider == "anthropic":
        return _fetch_anthropic_models(request)
    return _fetch_openai_compatible_models(request)


def _fetch_openai_compatible_models(
    request: DiscoveryRequest,
) -> tuple[str, ...]:
    base_url = request.base_url.rstrip("/")
    if base_url.endswith("/v1/models"):
        models_url = base_url
    elif base_url.endswith("/v1"):
        models_url = f"{base_url}/models"
    else:
        models_url = f"{base_url}/v1/models"

    http_request = Request(url=models_url, headers=request.headers, method="GET")
    with urlopen(http_request, timeout=request.timeout_seconds) as response:  # noqa: S310
        payload = json.loads(response.read().decode("utf-8"))

    if not isinstance(payload, dict):
        raise ValueError("provider model discovery response must be an object")
    payload_dict = cast(dict[str, object], payload)
    raw_data = payload_dict.get("data")
    if not isinstance(raw_data, list):
        return ()
    raw_items = cast(list[object], raw_data)

    model_ids: list[str] = []
    for item in raw_items:
        if isinstance(item, str) and item:
            model_ids.append(item)
            continue
        if isinstance(item, dict):
            raw_id = cast(dict[str, object], item).get("id")
            if isinstance(raw_id, str) and raw_id:
                model_ids.append(raw_id)
    return tuple(model_ids)


def _fetch_anthropic_models(request: DiscoveryRequest) -> tuple[str, ...]:
    base_url = request.base_url.rstrip("/")
    models_url = f"{base_url}/models" if base_url.endswith("/v1") else f"{base_url}/v1/models"
    http_request = Request(url=models_url, headers=request.headers, method="GET")
    with urlopen(http_request, timeout=request.timeout_seconds) as response:  # noqa: S310
        payload = json.loads(response.read().decode("utf-8"))

    if not isinstance(payload, dict):
        return ()
    payload_dict = cast(dict[str, object], payload)
    raw_data = payload_dict.get("data")
    if not isinstance(raw_data, list):
        return ()

    model_ids: list[str] = []
    for item in cast(list[object], raw_data):
        if not isinstance(item, dict):
            continue
        raw_id = cast(dict[str, object], item).get("id")
        if isinstance(raw_id, str) and raw_id:
            model_ids.append(raw_id)
    return tuple(model_ids)


def _fetch_google_models(request: DiscoveryRequest) -> tuple[str, ...]:
    base_url = request.base_url.rstrip("/")
    if base_url.endswith("/v1beta/models"):
        models_url = base_url
    elif base_url.endswith("/v1beta"):
        models_url = f"{base_url}/models"
    else:
        models_url = f"{base_url}/v1beta/models"

    if request.api_key is not None:
        models_url = f"{models_url}?key={quote(request.api_key, safe='')}"

    http_request = Request(url=models_url, headers=request.headers, method="GET")
    with urlopen(http_request, timeout=request.timeout_seconds) as response:  # noqa: S310
        payload = json.loads(response.read().decode("utf-8"))

    if not isinstance(payload, dict):
        return ()
    payload_dict = cast(dict[str, object], payload)
    raw_models = payload_dict.get("models")
    if not isinstance(raw_models, list):
        return ()

    model_ids: list[str] = []
    for item in cast(list[object], raw_models):
        if not isinstance(item, dict):
            continue
        raw_name = cast(dict[str, object], item).get("name")
        if isinstance(raw_name, str) and raw_name:
            normalized = raw_name[len("models/") :] if raw_name.startswith("models/") else raw_name
            model_ids.append(normalized)
    return tuple(model_ids)
