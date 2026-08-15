from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from importlib.resources import files as _resource_files
from typing import Literal, cast
from urllib.error import URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict, ValidationError

from .config import LiteLLMProviderConfig
from .reasoning_effort import CANONICAL_EFFORTS

type ToolFeedbackMode = Literal["standard", "synthetic_user_message"]


@dataclass(frozen=True, slots=True)
class ProviderModelMetadata:
    context_window: int | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    supports_tools: bool | None = None
    supports_vision: bool | None = None
    supports_streaming: bool | None = None
    supports_reasoning: bool | None = None
    supports_json_mode: bool | None = None
    cost_per_input_token: float | None = None
    cost_per_output_token: float | None = None
    cost_per_cache_read_token: float | None = None
    cost_per_cache_write_token: float | None = None
    supports_reasoning_effort: bool | None = None
    default_reasoning_effort: str | None = None
    supported_effort_levels: tuple[str, ...] | None = None
    supports_reasoning_summary: bool | None = None
    supports_thinking_budget: bool | None = None
    supports_interleaved_reasoning: bool | None = None
    reasoning_visibility: str | None = None
    modalities_input: tuple[str, ...] | None = None
    modalities_output: tuple[str, ...] | None = None
    model_status: str | None = None
    tool_feedback_mode: ToolFeedbackMode | None = None
    derived_max_input_tokens: bool = field(default=False, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.default_reasoning_effort is not None and self.default_reasoning_effort not in CANONICAL_EFFORTS:
            raise ValueError(f"default_reasoning_effort must be one of {CANONICAL_EFFORTS}; got {self.default_reasoning_effort!r}")
        if self.max_input_tokens is not None or self.context_window is None:
            return
        if self.max_output_tokens is None:
            object.__setattr__(self, "max_input_tokens", self.context_window)
            object.__setattr__(self, "derived_max_input_tokens", True)
            return
        object.__setattr__(
            self,
            "max_input_tokens",
            max(1, self.context_window - self.max_output_tokens),
        )
        object.__setattr__(self, "derived_max_input_tokens", True)

    def payload(self) -> dict[str, int | float | bool | str | list[str]]:
        payload: dict[str, int | float | bool | str | list[str]] = {}
        if self.context_window is not None:
            payload["context_window"] = self.context_window
        if self.max_input_tokens is not None:
            payload["max_input_tokens"] = self.max_input_tokens
        if self.max_output_tokens is not None:
            payload["max_output_tokens"] = self.max_output_tokens
        if self.supports_tools is not None:
            payload["supports_tools"] = self.supports_tools
        if self.supports_vision is not None:
            payload["supports_vision"] = self.supports_vision
        if self.supports_streaming is not None:
            payload["supports_streaming"] = self.supports_streaming
        if self.supports_reasoning is not None:
            payload["supports_reasoning"] = self.supports_reasoning
        if self.supports_json_mode is not None:
            payload["supports_json_mode"] = self.supports_json_mode
        if self.cost_per_input_token is not None:
            payload["cost_per_input_token"] = self.cost_per_input_token
        if self.cost_per_output_token is not None:
            payload["cost_per_output_token"] = self.cost_per_output_token
        if self.cost_per_cache_read_token is not None:
            payload["cost_per_cache_read_token"] = self.cost_per_cache_read_token
        if self.cost_per_cache_write_token is not None:
            payload["cost_per_cache_write_token"] = self.cost_per_cache_write_token
        if self.supports_reasoning_effort is not None:
            payload["supports_reasoning_effort"] = self.supports_reasoning_effort
        if self.default_reasoning_effort is not None:
            payload["default_reasoning_effort"] = self.default_reasoning_effort
        if self.supported_effort_levels is not None:
            payload["supported_effort_levels"] = list(self.supported_effort_levels)
        if self.supports_reasoning_summary is not None:
            payload["supports_reasoning_summary"] = self.supports_reasoning_summary
        if self.supports_thinking_budget is not None:
            payload["supports_thinking_budget"] = self.supports_thinking_budget
        if self.supports_interleaved_reasoning is not None:
            payload["supports_interleaved_reasoning"] = self.supports_interleaved_reasoning
        if self.reasoning_visibility is not None:
            payload["reasoning_visibility"] = self.reasoning_visibility
        if self.modalities_input is not None:
            payload["modalities_input"] = list(self.modalities_input)
        if self.modalities_output is not None:
            payload["modalities_output"] = list(self.modalities_output)
        if self.model_status is not None:
            payload["model_status"] = self.model_status
        if self.tool_feedback_mode is not None:
            payload["tool_feedback_mode"] = self.tool_feedback_mode
        return payload


@lru_cache(maxsize=1)
def _load_static_catalog() -> dict[str, dict[str, ProviderModelMetadata]]:
    try:
        raw = _resource_files("voidcode.provider").joinpath("model_catalog_data.json").read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return {}
    data = json.loads(raw)
    result: dict[str, dict[str, ProviderModelMetadata]] = {}
    for provider, models in data.items():
        if not isinstance(models, dict):
            continue
        per_provider: dict[str, ProviderModelMetadata] = {}
        for model_id, entry in models.items():
            if not isinstance(entry, dict):
                continue
            per_provider[model_id.strip().lower()] = ProviderModelMetadata(
                context_window=_positive_int(entry.get("context_window")),
                max_output_tokens=_positive_int(entry.get("max_output_tokens")),
                cost_per_input_token=_non_negative_float(entry.get("cost_per_input_token")),
                cost_per_output_token=_non_negative_float(entry.get("cost_per_output_token")),
                cost_per_cache_read_token=_non_negative_float(entry.get("cost_per_cache_read_token")),
                cost_per_cache_write_token=_non_negative_float(entry.get("cost_per_cache_write_token")),
                supports_tools=_optional_bool(entry.get("supports_tools")),
                supports_reasoning=_optional_bool(entry.get("supports_reasoning")),
                supports_vision=_optional_bool(entry.get("supports_vision")),
                modalities_input=_modalities(entry.get("modalities_input")),
                model_status=_optional_str(entry.get("model_status")),
            )
        result[provider.strip().lower()] = per_provider
    return result


def _static_catalog_metadata(provider_name: str, model_name: str) -> ProviderModelMetadata | None:
    provider = provider_name.strip().lower()
    model = model_name.strip().lower()
    if not model:
        return None
    return _load_static_catalog().get(provider, {}).get(model)


@dataclass(frozen=True, slots=True)
class DiscoveryRequest:
    provider: str
    base_url: str
    headers: dict[str, str]
    timeout_seconds: float
    api_key: str | None


@dataclass(frozen=True, slots=True)
class ModelDiscoveryFetchResult:
    models: tuple[str, ...]
    model_metadata: dict[str, ProviderModelMetadata] = field(default_factory=dict)


type ModelCatalogFetcher = Callable[[DiscoveryRequest], tuple[str, ...] | ModelDiscoveryFetchResult]


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
    context_window: int | None = None
    context_length: int | None = None
    max_context_length: int | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    input_cost_per_token: float | None = None
    output_cost_per_token: float | None = None
    cache_read_input_token_cost: float | None = None
    cache_creation_input_token_cost: float | None = None
    supports_tools: bool | None = None
    supports_vision: bool | None = None
    supports_streaming: bool | None = None
    supports_reasoning: bool | None = None
    supports_json_mode: bool | None = None
    supports_reasoning_effort: bool | None = None
    default_reasoning_effort: str | None = None
    supported_effort_levels: list[str] | None = None
    supports_reasoning_summary: bool | None = None
    supports_thinking_budget: bool | None = None
    supports_interleaved_reasoning: bool | None = None
    reasoning_visibility: str | None = None
    modalities: list[str] | None = None
    input_modalities: list[str] | None = None
    output_modalities: list[str] | None = None
    status: str | None = None
    tool_feedback_mode: str | None = None


class _OpenAICompatibleDiscoveryPayload(_DiscoveryPayloadModel):
    data: list[str | _OpenAICompatibleModelItem] | None = None


class _AnthropicDiscoveryPayload(_DiscoveryPayloadModel):
    data: list[_OpenAICompatibleModelItem] | None = None


class _GoogleModelItem(_DiscoveryPayloadModel):
    name: str | None = None
    inputTokenLimit: int | None = None
    outputTokenLimit: int | None = None
    supportedGenerationMethods: list[str] | None = None


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
            fetch_result = active_fetcher(discovery_plan.request)
            if isinstance(fetch_result, ModelDiscoveryFetchResult):
                discovered = fetch_result.models
                discovered_metadata = fetch_result.model_metadata
            else:
                discovered = fetch_result
                discovered_metadata = {}
        except (ValueError, OSError, TimeoutError, URLError):
            discovered = ()
            discovered_metadata = {}
            source = "fallback"
            refresh_status = "failed"
            error_message = "remote model discovery failed"
    else:
        source = "fallback"
        refresh_status = "skipped"
        error_message = discovery_plan.skip_reason
        discovered_metadata = {}

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

    model_metadata: dict[str, ProviderModelMetadata] = {}
    for model in deduped:
        metadata = discovered_metadata.get(model)
        if metadata is None:
            metadata = _static_catalog_metadata(provider_name, model)
        if metadata is not None:
            model_metadata[model] = metadata

    return ModelDiscoveryResult(
        models=tuple(deduped),
        model_metadata=model_metadata,
        source=source,
        last_refresh_status=refresh_status,
        last_error=error_message,
        discovery_mode=discovery_plan.discovery_mode,
    )


def _tool_feedback_mode(value: object) -> ToolFeedbackMode | None:
    if value in {"standard", "synthetic_user_message"}:
        return cast(ToolFeedbackMode, value)
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


def _build_discovery_plan(*, provider_name: str, config: LiteLLMProviderConfig | None) -> ModelDiscoveryPlan:
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
    if provider == "google" and config is not None and config.api_key is not None and config.auth_scheme == "bearer" and config.auth_header is None:
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


def _fetch_models(request: DiscoveryRequest) -> ModelDiscoveryFetchResult:
    if request.provider == "google":
        return _fetch_google_models(request)
    if request.provider == "anthropic":
        return _fetch_anthropic_models(request)
    return _fetch_openai_compatible_models(request)


def _positive_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _non_negative_float(value: object) -> float | None:
    if isinstance(value, int | float) and not isinstance(value, bool) and value >= 0:
        return float(value)
    return None


def _optional_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _modalities(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, list):
        return None
    raw_items = cast(Iterable[object], value)
    modalities = tuple(item for item in raw_items if isinstance(item, str) and item)
    return modalities or None


def _canonical_effort(value: object) -> str | None:
    effort = _optional_str(value)
    if effort is None or effort not in CANONICAL_EFFORTS:
        return None
    return effort


def _metadata_from_discovery_item(
    item: _OpenAICompatibleModelItem,
) -> ProviderModelMetadata | None:
    raw = item.model_dump()
    context_window = (
        _positive_int(raw.get("context_window")) or _positive_int(raw.get("context_length")) or _positive_int(raw.get("max_context_length"))
    )
    input_modalities = _modalities(raw.get("input_modalities")) or _modalities(raw.get("modalities"))
    output_modalities = _modalities(raw.get("output_modalities"))
    metadata = ProviderModelMetadata(
        context_window=context_window,
        max_input_tokens=_positive_int(raw.get("max_input_tokens")),
        max_output_tokens=_positive_int(raw.get("max_output_tokens")),
        supports_tools=_optional_bool(raw.get("supports_tools")),
        supports_vision=_optional_bool(raw.get("supports_vision")),
        supports_streaming=_optional_bool(raw.get("supports_streaming")),
        supports_reasoning=_optional_bool(raw.get("supports_reasoning")),
        supports_json_mode=_optional_bool(raw.get("supports_json_mode")),
        cost_per_input_token=_non_negative_float(raw.get("input_cost_per_token")),
        cost_per_output_token=_non_negative_float(raw.get("output_cost_per_token")),
        cost_per_cache_read_token=_non_negative_float(raw.get("cache_read_input_token_cost")),
        cost_per_cache_write_token=_non_negative_float(raw.get("cache_creation_input_token_cost")),
        supports_reasoning_effort=_optional_bool(raw.get("supports_reasoning_effort")),
        default_reasoning_effort=_canonical_effort(raw.get("default_reasoning_effort")),
        supported_effort_levels=_modalities(raw.get("supported_effort_levels")),
        supports_interleaved_reasoning=_optional_bool(raw.get("supports_interleaved_reasoning")),
        modalities_input=input_modalities,
        modalities_output=output_modalities,
        model_status=_optional_str(raw.get("status")),
        tool_feedback_mode=_tool_feedback_mode(raw.get("tool_feedback_mode")),
    )
    return metadata if metadata.payload() else None


def _metadata_from_google_item(item: _GoogleModelItem, *, model_name: str) -> ProviderModelMetadata | None:
    context_window = item.inputTokenLimit
    metadata = ProviderModelMetadata(
        context_window=context_window,
        max_input_tokens=item.inputTokenLimit,
        max_output_tokens=item.outputTokenLimit,
        supports_tools=("generateContent" in item.supportedGenerationMethods if item.supportedGenerationMethods is not None else None),
        supports_vision=True,
        supports_streaming=("streamGenerateContent" in item.supportedGenerationMethods if item.supportedGenerationMethods is not None else None),
        supports_json_mode=True,
        model_status="preview" if "preview" in model_name.lower() else "active",
    )
    return metadata if metadata.payload() else None


def _parse_openai_compatible_discovery_payload(payload: object) -> ModelDiscoveryFetchResult:
    try:
        parsed = _OpenAICompatibleDiscoveryPayload.model_validate(payload)
    except ValidationError as exc:
        raise ValueError("provider model discovery response must be an object") from exc
    if parsed.data is None:
        return ModelDiscoveryFetchResult(models=())
    model_ids: list[str] = []
    model_metadata: dict[str, ProviderModelMetadata] = {}
    for item in parsed.data:
        if isinstance(item, str) and item:
            model_ids.append(item)
            continue
        if isinstance(item, _OpenAICompatibleModelItem) and item.id:
            model_ids.append(item.id)
            metadata = _metadata_from_discovery_item(item)
            if metadata is not None:
                model_metadata[item.id] = metadata
    return ModelDiscoveryFetchResult(models=tuple(model_ids), model_metadata=model_metadata)


def _parse_anthropic_discovery_payload(payload: object) -> ModelDiscoveryFetchResult:
    try:
        parsed = _AnthropicDiscoveryPayload.model_validate(payload)
    except ValidationError:
        return ModelDiscoveryFetchResult(models=())
    if parsed.data is None:
        return ModelDiscoveryFetchResult(models=())
    model_ids: list[str] = []
    model_metadata: dict[str, ProviderModelMetadata] = {}
    for item in parsed.data:
        if not item.id:
            continue
        model_ids.append(item.id)
        metadata = _metadata_from_discovery_item(item)
        if metadata is not None:
            model_metadata[item.id] = metadata
    return ModelDiscoveryFetchResult(models=tuple(model_ids), model_metadata=model_metadata)


def _parse_google_discovery_payload(payload: object) -> ModelDiscoveryFetchResult:
    try:
        parsed = _GoogleDiscoveryPayload.model_validate(payload)
    except ValidationError:
        return ModelDiscoveryFetchResult(models=())
    if parsed.models is None:
        return ModelDiscoveryFetchResult(models=())

    model_ids: list[str] = []
    model_metadata: dict[str, ProviderModelMetadata] = {}
    for item in parsed.models:
        raw_name = item.name
        if isinstance(raw_name, str) and raw_name:
            normalized = raw_name[len("models/") :] if raw_name.startswith("models/") else raw_name
            model_ids.append(normalized)
            metadata = _metadata_from_google_item(item, model_name=normalized)
            if metadata is not None:
                model_metadata[normalized] = metadata
    return ModelDiscoveryFetchResult(models=tuple(model_ids), model_metadata=model_metadata)


def _fetch_openai_compatible_models(
    request: DiscoveryRequest,
) -> ModelDiscoveryFetchResult:
    base_url = request.base_url.rstrip("/")
    if base_url.endswith("/v1/models"):
        models_url = base_url
    elif re.search(r"/v[0-9]+(?:beta|alpha)?$", base_url, re.IGNORECASE):
        models_url = f"{base_url}/models"
    elif base_url.endswith("/v1"):
        models_url = f"{base_url}/models"
    else:
        models_url = f"{base_url}/v1/models"

    http_request = Request(url=models_url, headers=_http_headers(request.headers), method="GET")
    with urlopen(http_request, timeout=request.timeout_seconds) as response:  # noqa: S310
        payload = json.loads(response.read().decode("utf-8"))

    return _parse_openai_compatible_discovery_payload(payload)


def _fetch_anthropic_models(request: DiscoveryRequest) -> ModelDiscoveryFetchResult:
    base_url = request.base_url.rstrip("/")
    models_url = f"{base_url}/models" if base_url.endswith("/v1") else f"{base_url}/v1/models"
    http_request = Request(url=models_url, headers=_http_headers(request.headers), method="GET")
    with urlopen(http_request, timeout=request.timeout_seconds) as response:  # noqa: S310
        payload = json.loads(response.read().decode("utf-8"))

    return _parse_anthropic_discovery_payload(payload)


def _fetch_google_models(request: DiscoveryRequest) -> ModelDiscoveryFetchResult:
    base_url = request.base_url.rstrip("/")
    if base_url.endswith("/v1beta/models"):
        models_url = base_url
    elif base_url.endswith("/v1beta"):
        models_url = f"{base_url}/models"
    else:
        models_url = f"{base_url}/v1beta/models"

    uses_google_api_key_header = any(header_name.lower() == "x-goog-api-key" for header_name in request.headers)
    uses_authorization_header = any(header_name.lower() == "authorization" for header_name in request.headers)
    if request.api_key is not None and not uses_authorization_header and not uses_google_api_key_header:
        models_url = f"{models_url}?key={quote(request.api_key, safe='')}"

    http_request = Request(url=models_url, headers=_http_headers(request.headers), method="GET")
    with urlopen(http_request, timeout=request.timeout_seconds) as response:  # noqa: S310
        payload = json.loads(response.read().decode("utf-8"))

    return _parse_google_discovery_payload(payload)


def _http_headers(configured: Mapping[str, str]) -> dict[str, str]:
    headers = dict(configured)
    lower_names = {name.lower() for name in headers}
    if "accept" not in lower_names:
        headers["Accept"] = "application/json"
    if "user-agent" not in lower_names:
        headers["User-Agent"] = "voidcode-model-discovery/1.0"
    return headers
