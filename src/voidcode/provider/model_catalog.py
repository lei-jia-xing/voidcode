from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, fields
from functools import lru_cache
from importlib.resources import files as _resource_files
from typing import Final, Literal, TypeIs, cast
from urllib.error import URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict, ValidationError

from .config import ProviderEndpointConfig, RoutedWire
from .provider_config import provider_has_model_listing
from .reasoning_effort import CANONICAL_EFFORTS

type ToolFeedbackMode = Literal["standard", "synthetic_user_message"]

_TOOL_FEEDBACK_MODES: Final[tuple[ToolFeedbackMode, ...]] = ("standard", "synthetic_user_message")


def is_tool_feedback_mode(value: object) -> TypeIs[ToolFeedbackMode]:
    """Whether an untrusted ``tool_feedback_mode`` token names one of the known modes."""
    return value in _TOOL_FEEDBACK_MODES


def tool_feedback_mode(value: object) -> ToolFeedbackMode | None:
    """The declared tool-feedback mode, ``None`` when the value declares none."""
    return value if is_tool_feedback_mode(value) else None


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
    api: str | None = None
    display_name: str | None = None
    #: omp encoding name driving exact token counting (``O200kBase``,
    #: ``deepseek-v3``, ...). ``None`` means no exact tokenizer and the runtime
    #: falls back to the byte-count estimate. See ``provider/tokenizer.py``.
    tokenizer: str | None = None
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
        # Upstream sometimes reports an output cap equal to (or larger than) the
        # whole window, which would leave a nonsensical input budget of a few
        # tokens. Only a derivation that keeps at least half the window for input
        # is a real cap; otherwise the window itself is the honest answer.
        derived = self.context_window - self.max_output_tokens
        object.__setattr__(
            self,
            "max_input_tokens",
            derived if derived >= self.context_window // 2 else self.context_window,
        )
        object.__setattr__(self, "derived_max_input_tokens", True)

    def payload(self) -> dict[str, int | float | bool | str | list[str]]:
        payload: dict[str, int | float | bool | str | list[str]] = {}
        for model_field in fields(self):
            # ``derived_max_input_tokens`` is internal bookkeeping, not metadata.
            if not model_field.init:
                continue
            value = getattr(self, model_field.name)
            if value is None:
                continue
            payload[model_field.name] = list(value) if isinstance(value, tuple) else value
        return payload


@lru_cache(maxsize=1)
def _load_static_catalog() -> dict[str, dict[str, ProviderModelMetadata]]:
    # A packaged resource: a missing file means a broken install, not an empty
    # catalog, so the read stays loud instead of quietly reporting no metadata.
    raw = _resource_files("voidcode.provider").joinpath("model_catalog_data.json").read_text(encoding="utf-8")
    return _static_catalog_from_payload(json.loads(raw))


#: Every key a shipped catalog entry may carry: the loader constructs
#: ``ProviderModelMetadata`` from them, so a key outside this set -- a typo, or a
#: generator that started emitting a new field without the loader -- would be
#: silently dropped instead of read.
_CATALOG_FIELDS: Final[frozenset[str]] = frozenset(model_field.name for model_field in fields(ProviderModelMetadata))


def _static_catalog_from_payload(data: object) -> dict[str, dict[str, ProviderModelMetadata]]:
    if not isinstance(data, dict):
        raise ValueError("model_catalog_data.json must hold an object of providers")
    result: dict[str, dict[str, ProviderModelMetadata]] = {}
    for provider, models in cast(dict[str, object], data).items():
        if not isinstance(models, dict):
            continue
        per_provider: dict[str, ProviderModelMetadata] = {}
        for model_id, entry in models.items():
            if not isinstance(entry, dict):
                continue
            unknown = sorted(set(entry) - _CATALOG_FIELDS)
            if unknown:
                raise ValueError(f"model catalog entry {provider}/{model_id} carries unknown fields: {unknown}")
            per_provider[model_id.strip().lower()] = ProviderModelMetadata(
                context_window=_positive_int(entry.get("context_window")),
                max_input_tokens=_positive_int(entry.get("max_input_tokens")),
                max_output_tokens=_positive_int(entry.get("max_output_tokens")),
                cost_per_input_token=_non_negative_float(entry.get("cost_per_input_token")),
                cost_per_output_token=_non_negative_float(entry.get("cost_per_output_token")),
                cost_per_cache_read_token=_non_negative_float(entry.get("cost_per_cache_read_token")),
                cost_per_cache_write_token=_non_negative_float(entry.get("cost_per_cache_write_token")),
                supports_tools=_optional_bool(entry.get("supports_tools")),
                supports_reasoning=_optional_bool(entry.get("supports_reasoning")),
                supports_reasoning_effort=_optional_bool(entry.get("supports_reasoning_effort")),
                default_reasoning_effort=_canonical_effort(entry.get("default_reasoning_effort")),
                supported_effort_levels=_modalities(entry.get("supported_effort_levels")),
                supports_vision=_optional_bool(entry.get("supports_vision")),
                modalities_input=_modalities(entry.get("modalities_input")),
                modalities_output=_modalities(entry.get("modalities_output")),
                model_status=_optional_str(entry.get("model_status")),
                api=_optional_str(entry.get("api")),
                display_name=_optional_str(entry.get("display_name")),
                tokenizer=_optional_str(entry.get("tokenizer")),
            )
        result[provider.strip().lower()] = per_provider
    return result


def static_catalog_metadata(provider_name: str, model_name: str) -> ProviderModelMetadata | None:
    """Shipped catalog metadata for one provider/model, independent of discovery.

    Discovery results take precedence where they exist; this lookup is the offline
    fallback so a run does not need a successful `/models` refresh (or a warm
    catalog cache) before the runtime can clamp a reasoning effort to the model.
    """
    provider = provider_name.strip().lower()
    model = model_name.strip().lower()
    if not model:
        return None
    return _load_static_catalog().get(provider, {}).get(model)


@dataclass(frozen=True, slots=True)
class DiscoveryRequest:
    wire: RoutedWire
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
        "configured_base_url",
        "disabled",
        "unavailable",
    ]


@dataclass(frozen=True, slots=True)
class ModelDiscoveryPlan:
    discovery_mode: Literal[
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


class _GoogleModelItem(_DiscoveryPayloadModel):
    name: str | None = None
    inputTokenLimit: int | None = None
    outputTokenLimit: int | None = None
    supportedGenerationMethods: list[str] | None = None


class _GoogleDiscoveryPayload(_DiscoveryPayloadModel):
    models: list[_GoogleModelItem] | None = None


def discover_available_models(
    provider_name: str,
    config: ProviderEndpointConfig | None,
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
        except ValueError, OSError, TimeoutError, URLError:
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
            metadata = static_catalog_metadata(provider_name, model)
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


def _timeout_for_discovery(config: ProviderEndpointConfig | None) -> float:
    if config is None or config.timeout_seconds is None:
        return 10.0
    return max(1.0, float(config.timeout_seconds))


def _headers_for_discovery(config: ProviderEndpointConfig | None) -> dict[str, str]:
    if config is None or config.api_key is None or config.auth_scheme == "none":
        return {}
    header_name = config.auth_header or "Authorization"
    if config.auth_scheme == "token":
        return {header_name: config.api_key}
    return {header_name: f"Bearer {config.api_key}"}


def _build_discovery_plan(*, provider_name: str, config: ProviderEndpointConfig | None) -> ModelDiscoveryPlan:
    if not provider_has_model_listing(provider_name.strip().lower(), config):
        return ModelDiscoveryPlan(
            discovery_mode="disabled",
            request=None,
            skip_reason="provider has no model listing",
        )
    if config is not None and config.base_url:
        return _discovery_plan_from_base_url(
            config=config,
            base_url=config.base_url.rstrip("/"),
        )
    return ModelDiscoveryPlan(
        discovery_mode="unavailable",
        request=None,
        skip_reason="provider has no model discovery endpoint",
    )


def _discovery_plan_from_base_url(
    *,
    config: ProviderEndpointConfig,
    base_url: str,
) -> ModelDiscoveryPlan:
    headers = _headers_for_discovery(config)
    if config.wire == "google-generative-ai" and config.api_key is not None and config.auth_scheme == "bearer" and config.auth_header is None:
        # A bearer credential with no explicit header goes in Google's own key
        # header rather than ``Authorization``.
        headers = {"x-goog-api-key": config.api_key}
    if config.wire == "anthropic-messages":
        # The Anthropic Messages wire owns its own header set -- the version
        # header plus the vendor's credential header -- instead of OpenAI's
        # ``Authorization: Bearer``.
        headers = {"anthropic-version": "2023-06-01", **headers}

    return ModelDiscoveryPlan(
        discovery_mode="configured_base_url",
        request=DiscoveryRequest(
            wire=config.wire,
            base_url=base_url,
            headers=headers,
            timeout_seconds=_timeout_for_discovery(config),
            api_key=config.api_key,
        ),
    )


def _fetch_models(request: DiscoveryRequest) -> ModelDiscoveryFetchResult:
    if request.wire == "google-generative-ai":
        return _fetch_google_models(request)
    return _fetch_data_models(request)


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
    raw_items = value
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
        tool_feedback_mode=tool_feedback_mode(raw.get("tool_feedback_mode")),
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


def _parse_google_discovery_payload(payload: object) -> ModelDiscoveryFetchResult:
    try:
        parsed = _GoogleDiscoveryPayload.model_validate(payload)
    except ValidationError as exc:
        raise ValueError("provider model discovery response must be an object") from exc
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


#: A version segment anywhere in the base path (``/v1``, ``/v3beta``, ``/v1/openai``):
#: a base that already names an API version gets the listing appended to itself,
#: because the vendor's listing lives under the version it documents. Probing every
#: provider in ``provider_table.json`` (P5 matrix, ``/tmp/p5_probe.json``) confirmed
#: this rule reproduces the live listing URL for all of them, and that the previous
#: "append /v1/models unless the base ends with /v1" form produced
#: ``.../v1/openai/v1/models`` (404) for a base that carries the version mid-path
#: (deepinfra: ``https://api.deepinfra.com/v1/openai`` -> live listing at
#: ``.../v1/openai/models``).
_VERSION_SEGMENT = re.compile(r"/v[0-9]+(?:beta|alpha)?(?:/|$)", re.IGNORECASE)


def _models_url(wire: str, base_url: str) -> str:
    """The listing URL one wire requests for a provider's base URL."""
    base = base_url.rstrip("/")
    if wire == "anthropic-messages":
        # The Anthropic wire appends its own version segment to the base URL.
        return f"{base}/models" if base.endswith("/v1") else f"{base}/v1/models"
    if wire == "google-generative-ai":
        if base.endswith("/v1beta/models"):
            return base
        return f"{base}/models" if base.endswith("/v1beta") else f"{base}/v1beta/models"
    if base.endswith("/v1/models"):
        return base
    if _VERSION_SEGMENT.search(base):
        return f"{base}/models"
    return f"{base}/v1/models"


def _fetch_data_models(
    request: DiscoveryRequest,
) -> ModelDiscoveryFetchResult:
    models_url = _models_url(request.wire, request.base_url)

    http_request = Request(url=models_url, headers=_http_headers(request.headers), method="GET")
    with urlopen(http_request, timeout=request.timeout_seconds) as response:  # noqa: S310
        payload = json.loads(response.read().decode("utf-8"))

    return _parse_openai_compatible_discovery_payload(payload)


def _fetch_google_models(request: DiscoveryRequest) -> ModelDiscoveryFetchResult:
    models_url = _models_url(request.wire, request.base_url)
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
