from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal
from urllib.error import URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict, ValidationError

from .config import LiteLLMProviderConfig


@dataclass(frozen=True, slots=True)
class ProviderModelMetadata:
    context_window: int | None = None
    max_output_tokens: int | None = None

    def payload(self) -> dict[str, int]:
        payload: dict[str, int] = {}
        if self.context_window is not None:
            payload["context_window"] = self.context_window
        if self.max_output_tokens is not None:
            payload["max_output_tokens"] = self.max_output_tokens
        return payload


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
    model_metadata: dict[str, ProviderModelMetadata] = field(default_factory=dict)
    source: str = "remote"
    last_refresh_status: str = "ok"
    last_error: str | None = None
    discovery_mode: Literal[
        "configured_endpoint",
        "configured_base_url",
        "disabled",
        "unavailable",
    ] = "unavailable"


@dataclass(frozen=True, slots=True)
class ModelDiscoveryResult:
    models: tuple[str, ...]
    model_metadata: dict[str, ProviderModelMetadata]
    source: str
    last_refresh_status: str
    last_error: str | None
    discovery_mode: Literal[
        "configured_endpoint",
        "configured_base_url",
        "disabled",
        "unavailable",
    ]


@dataclass(frozen=True, slots=True)
class ModelDiscoveryPlan:
    discovery_mode: Literal[
        "configured_endpoint",
        "configured_base_url",
        "disabled",
        "unavailable",
    ]
    request: DiscoveryRequest | None
    skip_reason: str | None = None


class _DiscoveryPayloadModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class _OpenAICompatibleModelItem(_DiscoveryPayloadModel):
    id: str | None = None


class _OpenAICompatibleDiscoveryPayload(_DiscoveryPayloadModel):
    data: list[str | _OpenAICompatibleModelItem] | None = None


class _AnthropicDiscoveryPayload(_DiscoveryPayloadModel):
    data: list[_OpenAICompatibleModelItem] | None = None


class _GoogleModelItem(_DiscoveryPayloadModel):
    name: str | None = None


class _GoogleDiscoveryPayload(_DiscoveryPayloadModel):
    models: list[_GoogleModelItem] | None = None


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
    discovery_plan = _build_discovery_plan(provider_name=provider_name, config=config)
    if discovery_plan.request is not None:
        active_fetcher = _fetch_models if fetcher is None else fetcher
        try:
            discovered = active_fetcher(discovery_plan.request)
        except (ValueError, OSError, TimeoutError, URLError):
            discovered = ()
            source = "fallback"
            refresh_status = "failed"
            error_message = "remote model discovery failed"
    else:
        source = "fallback"
        refresh_status = "skipped"
        error_message = discovery_plan.skip_reason

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

    model_metadata = {
        model: metadata
        for model in deduped
        if (metadata := infer_model_metadata(provider_name, model)) is not None
    }

    return ModelDiscoveryResult(
        models=tuple(deduped),
        model_metadata=model_metadata,
        source=source,
        last_refresh_status=refresh_status,
        last_error=error_message,
        discovery_mode=discovery_plan.discovery_mode,
    )


def infer_model_metadata(provider_name: str, model_name: str) -> ProviderModelMetadata | None:
    provider = provider_name.strip().lower()
    model = model_name.strip().lower()
    if not model:
        return None
    if provider == "openai" or model.startswith(("gpt-4.1", "gpt-4o", "o1", "o3", "o4")):
        if model.startswith("gpt-4o-mini"):
            return ProviderModelMetadata(context_window=128_000, max_output_tokens=16_384)
        if model.startswith("gpt-4o"):
            return ProviderModelMetadata(context_window=128_000, max_output_tokens=16_384)
        if model.startswith("gpt-4.1"):
            return ProviderModelMetadata(context_window=1_047_576, max_output_tokens=32_768)
        if model.startswith(("o1", "o3", "o4")):
            return ProviderModelMetadata(context_window=200_000, max_output_tokens=100_000)
    if provider == "anthropic" or model.startswith("claude-"):
        return ProviderModelMetadata(context_window=200_000, max_output_tokens=8_192)
    if provider == "google" or model.startswith("gemini-"):
        if model.startswith("gemini-2.5"):
            return ProviderModelMetadata(context_window=1_000_000, max_output_tokens=65_536)
        return ProviderModelMetadata(context_window=1_000_000, max_output_tokens=8_192)
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


def _build_discovery_plan(
    *, provider_name: str, config: LiteLLMProviderConfig | None
) -> ModelDiscoveryPlan:
    if config is not None and config.discovery_base_url is not None:
        candidate = config.discovery_base_url.strip()
        if not candidate:
            return ModelDiscoveryPlan(
                discovery_mode="disabled",
                request=None,
                skip_reason="provider model discovery disabled by config",
            )
        return _discovery_plan_from_base_url(
            provider_name=provider_name,
            config=config,
            base_url=candidate.rstrip("/"),
            discovery_mode="configured_endpoint",
        )
    if config is not None and config.base_url:
        return _discovery_plan_from_base_url(
            provider_name=provider_name,
            config=config,
            base_url=config.base_url.rstrip("/"),
            discovery_mode="configured_base_url",
        )
    return ModelDiscoveryPlan(
        discovery_mode="unavailable",
        request=None,
        skip_reason="provider has no model discovery endpoint",
    )


def _discovery_plan_from_base_url(
    *,
    provider_name: str,
    config: LiteLLMProviderConfig | None,
    base_url: str,
    discovery_mode: Literal["configured_endpoint", "configured_base_url"],
) -> ModelDiscoveryPlan:

    provider = provider_name.strip().lower()
    timeout_seconds = _timeout_for_discovery(config)
    headers = _headers_for_discovery(config)
    if (
        provider == "google"
        and config is not None
        and config.api_key is not None
        and config.auth_scheme == "bearer"
        and config.auth_header is None
    ):
        headers = {"x-goog-api-key": config.api_key}
    if provider == "anthropic":
        api_key = None if config is None else config.api_key
        anthropic_headers: dict[str, str] = {"anthropic-version": "2023-06-01"}
        if api_key is not None:
            anthropic_headers["x-api-key"] = api_key
        headers = anthropic_headers

    return ModelDiscoveryPlan(
        discovery_mode=discovery_mode,
        request=DiscoveryRequest(
            provider=provider,
            base_url=base_url,
            headers=headers,
            timeout_seconds=timeout_seconds,
            api_key=None if config is None else config.api_key,
        ),
    )


def _fetch_models(request: DiscoveryRequest) -> tuple[str, ...]:
    if request.provider == "google":
        return _fetch_google_models(request)
    if request.provider == "anthropic":
        return _fetch_anthropic_models(request)
    return _fetch_openai_compatible_models(request)


def _parse_openai_compatible_discovery_payload(payload: object) -> tuple[str, ...]:
    try:
        parsed = _OpenAICompatibleDiscoveryPayload.model_validate(payload)
    except ValidationError as exc:
        raise ValueError("provider model discovery response must be an object") from exc
    if parsed.data is None:
        return ()
    model_ids: list[str] = []
    for item in parsed.data:
        if isinstance(item, str) and item:
            model_ids.append(item)
            continue
        if isinstance(item, _OpenAICompatibleModelItem) and item.id:
            model_ids.append(item.id)
    return tuple(model_ids)


def _parse_anthropic_discovery_payload(payload: object) -> tuple[str, ...]:
    try:
        parsed = _AnthropicDiscoveryPayload.model_validate(payload)
    except ValidationError:
        return ()
    if parsed.data is None:
        return ()
    return tuple(item.id for item in parsed.data if item.id)


def _parse_google_discovery_payload(payload: object) -> tuple[str, ...]:
    try:
        parsed = _GoogleDiscoveryPayload.model_validate(payload)
    except ValidationError:
        return ()
    if parsed.models is None:
        return ()

    model_ids: list[str] = []
    for item in parsed.models:
        raw_name = item.name
        if isinstance(raw_name, str) and raw_name:
            normalized = raw_name[len("models/") :] if raw_name.startswith("models/") else raw_name
            model_ids.append(normalized)
    return tuple(model_ids)


def _fetch_openai_compatible_models(
    request: DiscoveryRequest,
) -> tuple[str, ...]:
    base_url = request.base_url.rstrip("/")
    if base_url.endswith("/v1/models"):
        models_url = base_url
    elif re.search(r"/v[0-9]+(?:beta|alpha)?$", base_url, re.IGNORECASE):
        models_url = f"{base_url}/models"
    elif base_url.endswith("/v1"):
        models_url = f"{base_url}/models"
    else:
        models_url = f"{base_url}/v1/models"

    http_request = Request(url=models_url, headers=request.headers, method="GET")
    with urlopen(http_request, timeout=request.timeout_seconds) as response:  # noqa: S310
        payload = json.loads(response.read().decode("utf-8"))

    return _parse_openai_compatible_discovery_payload(payload)


def _fetch_anthropic_models(request: DiscoveryRequest) -> tuple[str, ...]:
    base_url = request.base_url.rstrip("/")
    models_url = f"{base_url}/models" if base_url.endswith("/v1") else f"{base_url}/v1/models"
    http_request = Request(url=models_url, headers=request.headers, method="GET")
    with urlopen(http_request, timeout=request.timeout_seconds) as response:  # noqa: S310
        payload = json.loads(response.read().decode("utf-8"))

    return _parse_anthropic_discovery_payload(payload)


def _fetch_google_models(request: DiscoveryRequest) -> tuple[str, ...]:
    base_url = request.base_url.rstrip("/")
    if base_url.endswith("/v1beta/models"):
        models_url = base_url
    elif base_url.endswith("/v1beta"):
        models_url = f"{base_url}/models"
    else:
        models_url = f"{base_url}/v1beta/models"

    uses_google_api_key_header = any(
        header_name.lower() == "x-goog-api-key" for header_name in request.headers
    )
    uses_authorization_header = any(
        header_name.lower() == "authorization" for header_name in request.headers
    )
    if (
        request.api_key is not None
        and not uses_authorization_header
        and not uses_google_api_key_header
    ):
        models_url = f"{models_url}?key={quote(request.api_key, safe='')}"

    http_request = Request(url=models_url, headers=request.headers, method="GET")
    with urlopen(http_request, timeout=request.timeout_seconds) as response:  # noqa: S310
        payload = json.loads(response.read().decode("utf-8"))

    return _parse_google_discovery_payload(payload)
