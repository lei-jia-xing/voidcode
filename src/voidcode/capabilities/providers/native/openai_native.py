from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Protocol, cast

import httpx2
from openai import APIConnectionError as OpenAIAPIConnectionError
from openai import APIError as OpenAIAPIError
from openai import APITimeoutError as OpenAIAPITimeoutError
from openai import OpenAI, omit

from voidcode.sdk.json_values import json_wire_object
from voidcode.sdk.provider import (
    ProviderDoneReason,
    ProviderExecutionError,
    ProviderStreamEvent,
    ProviderTokenUsage,
    ProviderTurnRequest,
    ProviderTurnResult,
    ProviderWireMaterialization,
    WirePrefixDescriptor,
)
from voidcode.sdk.tools import ToolCall

from ._wire_common import (
    DEFAULT_STREAM_FIRST_EVENT_TIMEOUT_SECONDS,
    OwnedResource,
    abort_signal_cancelled,
    iter_stream_with_timeout,
    normalize_tool_call_id,
    resolve_extra_request_headers,
    usage_int,
)
from .config import OpenAIProviderConfig, ProviderEndpointConfig
from .errors import (
    provider_execution_error_from_payload,
)
from .model_catalog import ProviderModelMetadata, static_catalog_metadata
from .provider_config import openai_wire_default_base_url
from .provider_table import PROVIDER_TABLE_BY_ID
from .reasoning_effort import clamp_effort_to_supported, normalize_reasoning_effort, reasoning_kwargs
from .thinking_rules import thinking_rule_for
from .trace import write_provider_trace

#: OMP's fallback cap when a model's own maximum is unknown (``types.ts:70``).
_OUTPUT_CAP_WHEN_UNKNOWN = 64000


def _output_cap(metadata: ProviderModelMetadata | None) -> int:
    """The cap the kimi family always gets: the model's own maximum, else 64000."""
    return metadata.max_output_tokens if metadata is not None and metadata.max_output_tokens else _OUTPUT_CAP_WHEN_UNKNOWN


def _requires_reasoning_content_with_tool_calls(*, provider_name: str | None, model_name: str) -> bool:
    """Whether this model's tool-call replay must carry its reasoning content.

    The answer is data (``thinking_rules.json``:
    ``requires_reasoning_content_for_tool_calls``), never a provider name or a
    model prefix.
    """
    rule = thinking_rule_for(provider_name or "", model_name)
    return rule.requires_reasoning_content_for_tool_calls


# Construction default of ``OpenAIChatCompletionsTransport`` for direct use only.
# It is never a provider-level fallback: a provider resolves its own vendor
# default (``provider_config.openai_wire_default_base_url``) or fails with
# ``not_configured``.
_DEFAULT_OPENAI_BASE_URL = PROVIDER_TABLE_BY_ID["openai"].default_base_url
# The SDK refuses to construct a client without a credential; the placeholder only
# satisfies that check -- ``_auth_headers``/``_request_headers`` decide what is sent.
_PLACEHOLDER_API_KEY = "voidcode-no-api-key"
_DEFAULT_TIMEOUT_SECONDS = 300.0
_TOOL_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")
_MAX_TOOL_NAME_LENGTH = 64
_TOOL_NAME_HASH_LENGTH = 8


class OpenAITransport(Protocol):
    def request(self, payload: dict[str, object], *, timeout_seconds: float) -> object: ...


@dataclass(frozen=True, slots=True)
class OpenAITransportError(Exception):
    payload: dict[str, object]
    message: str = "provider request failed"


def _stream_event_error_payload(exc: Exception) -> dict[str, object]:
    """Bounded, non-leaking payload for a stream event the transport cannot decode.

    The SDK's decoder raises parser/model errors directly (``JSONDecodeError``,
    pydantic errors), so they are reported through the same typed payload shape
    the deleted SSE parser used: ``provider_execution_error_from_payload``
    then classifies them as a transient stream failure with ``source``/guidance
    details. Raw parser text is deliberately not forwarded.
    """
    return {
        "message": "provider stream event was not a valid JSON object",
        "details": {"reason": "invalid_stream_event", "exception_type": type(exc).__name__},
    }


class OpenAIChatCompletionsTransport:
    """Official OpenAI SDK transport for the Chat Completions wire protocol.

    The SDK owns HTTP, SSE decoding, and error typing. Runtime owns retry and
    fallback, so SDK-internal retries are disabled.

    Stream framing must be spec-compliant: the SDK's SSE decoder dispatches an
    event only on the blank line that terminates it, so events separated by a
    single newline are never delivered and a final event without its
    terminating blank line is discarded. A stream that ends without a finish
    reason has an ``unknown`` terminal reason rather than an invented stop; an
    unrecognized reported token is likewise preserved as ``unknown`` with its
    raw value in metadata. The explicit transport terminal marker distinguishes
    a complete model turn from a missing terminal event. Events the decoder
    emits but cannot decode as a JSON object are reported as a bounded,
    non-leaking stream error payload.

    Auth headers reproduce the configured ``auth_scheme``/``auth_header``
    contract exactly (see ``_auth_headers``): ``none`` -- or no key -- sends no
    credential at all, ``token`` sends the raw key, ``bearer`` sends
    ``Bearer <key>``.
    """

    def __init__(
        self,
        *,
        base_url: str = _DEFAULT_OPENAI_BASE_URL,
        api_key: str | None = None,
        organization: str | None = None,
        project: str | None = None,
        auth_header: str | None = None,
        auth_scheme: str = "bearer",
        ssl_verify: bool | None = None,
        http_client: httpx2.Client | None = None,
    ) -> None:
        self.base_url = normalize_openai_base_url(base_url)
        self.api_key = api_key
        self.organization = organization
        self.project = project
        self.auth_header = auth_header
        self.auth_scheme = auth_scheme
        self.ssl_verify = ssl_verify
        self.http_client = http_client
        self._client_lock = Lock()
        self._sdk_client: OpenAI | None = None

    def _auth_headers(self) -> dict[str, str]:
        """Resolve the configured auth scheme into the headers this wire carries.

        Mirrors the deleted LiteLLM contract and ``model_catalog._headers_for_discovery``:
        ``none`` (or a missing key) sends no credential at all, ``token`` sends the
        raw key, ``bearer`` sends ``Bearer <key>``, and ``auth_header`` only selects
        which header carries it.
        """
        if not self.api_key or self.auth_scheme == "none":
            return {}
        header_name = self.auth_header or "Authorization"
        if self.auth_scheme == "token":
            return {header_name: self.api_key}
        return {header_name: f"Bearer {self.api_key}"}

    def _request_headers(self, payload: Mapping[str, object]) -> dict[str, object]:
        """Return the SDK kwargs for a request, dropping the SDK's own auth default.

        The SDK always adds ``Authorization: Bearer <api_key>`` for whichever key it
        was constructed with (the configured key, or the placeholder when there is
        none). When the resolved scheme does not use that header the default has to be
        omitted per request: ``default_headers`` can add headers but cannot remove one
        the SDK owns. A per-request ``extra_headers`` override still wins.
        """
        kwargs: dict[str, object] = dict(payload)
        auth_headers = self._auth_headers()
        if any(name.lower() == "authorization" for name in auth_headers):
            return kwargs
        extra = kwargs.get("extra_headers")
        merged: dict[str, object] = dict(extra) if isinstance(extra, Mapping) else {}
        if not any(str(name).lower() == "authorization" for name in merged):
            merged["Authorization"] = omit
            kwargs["extra_headers"] = merged
        return kwargs

    def close(self) -> None:
        with self._client_lock:
            client, self._sdk_client = self._sdk_client, None
        if client is not None and self.http_client is None:
            client.close()

    def _sdk(self) -> OpenAI:
        with self._client_lock:
            if self._sdk_client is None:
                http_client = self.http_client
                if http_client is None and self.ssl_verify is not None:
                    http_client = httpx2.Client(verify=self.ssl_verify)
                # ``default_headers`` wins over the SDK's own auth header, so the
                # configured scheme stays authoritative. ``max_retries=0`` keeps
                # retry/fallback owned by the runtime instead of the SDK.
                self._sdk_client = OpenAI(
                    api_key=self.api_key or _PLACEHOLDER_API_KEY,
                    base_url=self.base_url,
                    organization=self.organization,
                    project=self.project,
                    default_headers=self._auth_headers(),
                    http_client=http_client,
                    max_retries=0,
                )
            return self._sdk_client

    @staticmethod
    def _api_error_payload(exc: OpenAIAPIError) -> dict[str, object]:
        payload: dict[str, object] = {}
        body = getattr(exc, "body", None)
        if isinstance(body, Mapping):
            payload.update(body)
        elif isinstance(body, str) and body:
            payload["message"] = body
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
        if isinstance(status_code, int):
            payload["status_code"] = status_code
        headers = getattr(response, "headers", None)
        if isinstance(headers, Mapping):
            payload.setdefault("headers", dict(headers))
        if isinstance(exc, (OpenAIAPITimeoutError, OpenAIAPIConnectionError)):
            payload["network_failure"] = True
            payload["exception_type"] = type(exc).__name__
        payload.setdefault("message", str(exc))
        return payload

    def request(self, payload: dict[str, object], *, timeout_seconds: float) -> object:
        try:
            result = self._sdk().chat.completions.create(**cast(Any, self._request_headers(payload)), timeout=timeout_seconds)
        except OpenAIAPIError as exc:
            raise OpenAITransportError(self._api_error_payload(exc)) from exc
        if bool(payload.get("stream")):
            return self._iter_sdk_stream(result)
        return result

    def _iter_sdk_stream(self, stream: object) -> Iterator[object]:
        try:
            yield from cast(Iterator[object], stream)
        except OpenAIAPIError as exc:
            raise OpenAITransportError(self._api_error_payload(exc)) from exc
        except Exception as exc:
            raise OpenAITransportError(_stream_event_error_payload(exc)) from exc


def normalize_openai_base_url(base_url: str | None) -> str:
    """Normalize a chat-completions base URL exactly as the transport will use it."""
    value = (base_url or _DEFAULT_OPENAI_BASE_URL).strip().rstrip("/") or _DEFAULT_OPENAI_BASE_URL
    return value if re.search(r"/v[0-9]+(?:beta|alpha)?$", value, flags=re.IGNORECASE) else f"{value}/v1"


def _safe_tool_name(name: str) -> str:
    if _TOOL_NAME_PATTERN.fullmatch(name) and len(name) <= _MAX_TOOL_NAME_LENGTH:
        return name
    normalized = re.sub(r"[^a-zA-Z0-9_-]", "_", name).strip("_") or "tool"
    suffix = "_" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:_TOOL_NAME_HASH_LENGTH]
    return f"{normalized[: _MAX_TOOL_NAME_LENGTH - len(suffix)]}{suffix}"


def _usage(payload: Mapping[str, object]) -> ProviderTokenUsage | None:
    raw = payload.get("usage")
    if not isinstance(raw, Mapping):
        return None
    # ``prompt_tokens`` is the inclusive prompt total (``cached_tokens`` is a
    # component of it), so it is stored as-is; the uncached remainder is derived
    # for pricing/telemetry only.
    input_tokens = usage_int(raw.get("prompt_tokens"))
    output_tokens = usage_int(raw.get("completion_tokens"))
    details = raw.get("prompt_tokens_details")
    cache_read = usage_int(details.get("cached_tokens")) if isinstance(details, Mapping) else None
    if input_tokens is None and output_tokens is None:
        return None
    uncached = max(0, input_tokens - cache_read) if input_tokens is not None and cache_read is not None else None
    return ProviderTokenUsage(input_tokens=input_tokens, output_tokens=output_tokens, cache_read_tokens=cache_read, uncached_input_tokens=uncached)


def _done_reason(value: object) -> ProviderDoneReason:
    if not isinstance(value, str) or not value.strip():
        return "unknown"
    value = value.strip().lower()
    if value in {"stop", "end_turn"}:
        return "stop"
    if value in {"tool_calls", "tool_use"}:
        return "tool_calls"
    if value == "function_call":
        return "function_call"
    if value in {"length", "max_tokens"}:
        return "length"
    if value == "content_filter":
        return "content_filter"
    if value == "error":
        return "error"
    return "unknown"


def _raw_finish_reason(value: object) -> str | None:
    """Return a reported finish-reason token verbatim for diagnostics."""
    if isinstance(value, str):
        return value.strip() or None
    if value is None:
        return None
    try:
        return json.dumps(value, ensure_ascii=True, sort_keys=True)
    except TypeError, ValueError:
        return None


def _raise_finish_reason_error(raw_reason: str, *, provider_name: str, model_name: str) -> None:
    raise ProviderExecutionError(
        kind="provider_error",
        provider_name=provider_name,
        model_name=model_name,
        message=f"provider reported finish_reason={raw_reason}",
        retryable=False,
        fallback_allowed=True,
        details={"source": "finish_reason", "reason": "provider_error", "finish_reason_raw": raw_reason},
    )


def _reasoning(message: Mapping[str, object]) -> str | None:
    for key in ("reasoning_content", "reasoning"):
        value = message.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _parse_arguments(value: object) -> dict[str, object]:
    if isinstance(value, Mapping):
        return dict(value)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("tool input was empty")
    decoded = json.loads(value)
    if not isinstance(decoded, dict):
        raise ValueError("tool input was not an object")
    return dict(decoded)


def _session_id(request: ProviderTurnRequest) -> str:
    """The session id this turn declares, empty when it declares none.

    Execution identity travels in request metadata, not as a request field. An
    empty id is what the legacy adapter passed when it had none, and it resolves
    a ``{session_id}`` header the same way: the declaration is dropped.
    """
    value = request.metadata.get("session_id")
    return value if isinstance(value, str) else ""


@dataclass(frozen=True, slots=True)
class _ToolAccumulator:
    tool_call_id: str | None = None
    tool_name: str | None = None
    fragments: tuple[str, ...] = ()
    explicit_streaming: bool = False
    started: bool = False

    @property
    def arguments(self) -> str:
        return "".join(self.fragments)


@dataclass(frozen=True, slots=True)
class OpenAIChatCompletionsProvider:
    name: str = "openai"
    config: OpenAIProviderConfig | ProviderEndpointConfig | None = None
    transport: OpenAITransport | None = None
    synthetic_tool_feedback_models: frozenset[str] = frozenset()
    # Headers this gateway requires on every request it serves. They reach the wire
    # through the SDK's ``extra_headers`` argument, which the transport passes to
    # ``create`` from the payload, so the JSON body never carries them.
    extra_request_headers: Mapping[str, str] = field(default_factory=dict)
    # One transport -- and therefore one SDK client and HTTP connection pool --
    # per provider, reused across every turn. Building it per request leaked a
    # pool per turn. First-use initialization is serialized.
    _owned_transport: OwnedResource[OpenAITransport] = field(default_factory=OwnedResource, compare=False, repr=False)

    def provider_config(self) -> OpenAIProviderConfig | ProviderEndpointConfig | None:
        return self.config

    def _endpoint_config(self) -> ProviderEndpointConfig | None:
        """The endpoint-shaped config, ``None`` for OpenAI's own shape or no config."""
        config = self.config
        return config if isinstance(config, ProviderEndpointConfig) else None

    def _model_name(self, request: ProviderTurnRequest) -> str:
        model_name = request.model_name
        if not model_name:
            raise ProviderExecutionError(
                kind="invalid_model",
                provider_name=request.provider_name or self.name,
                model_name="unknown",
                message="provider requires model name",
                retryable=False,
                fallback_allowed=True,
            )
        endpoint = self._endpoint_config()
        model_map = endpoint.model_map if endpoint is not None else {}
        return model_map.get(model_name) or model_name

    def close(self) -> None:
        self._owned_transport.close()

    def _transport(self) -> OpenAITransport:
        if self.transport is not None:
            return self.transport
        owned = self._owned_transport
        with owned.lock:
            if owned.value is None:
                # Credentials come only from resolved provider config: an ambient
                # ``OPENAI_API_KEY`` must never be attached to another vendor's endpoint
                # (or to the default base URL when the provider block is absent).
                # A missing configured key means "send no credential" -- the
                # transport still hands the SDK a placeholder so it never sends an empty one.
                config = self.config
                endpoint = config if isinstance(config, ProviderEndpointConfig) else None
                openai_config = config if isinstance(config, OpenAIProviderConfig) else None
                base_url = None if config is None else config.base_url
                if not base_url:
                    # A provider whose config names no endpoint resolves to its own
                    # vendor default; one without a default must not borrow another
                    # vendor's host. ``OpenAIChatCompletionsTransport``'s class
                    # default is for direct construction only, never a provider-level
                    # fallback, so an endpoint-less provider fails here instead.
                    base_url = openai_wire_default_base_url(self.name)
                if not base_url:
                    raise ProviderExecutionError(
                        kind="not_configured",
                        provider_name=self.name,
                        model_name="unknown",
                        message=f"provider '{self.name}' has no endpoint configured; set providers.{self.name}.base_url and its API key",
                        retryable=False,
                        fallback_allowed=True,
                    )
                # The two config shapes spell these two credentials differently:
                # OpenAI's own config names them ``organization``/``project``, an
                # endpoint config ``openai_organization``/``openai_project``. Only an
                # endpoint config carries the auth header/scheme and the TLS switch.
                organization = None if openai_config is None else openai_config.organization
                project = None if openai_config is None else openai_config.project
                if endpoint is not None:
                    organization = endpoint.openai_organization
                    project = endpoint.openai_project
                owned.value = OpenAIChatCompletionsTransport(
                    base_url=base_url,
                    api_key=None if config is None else config.api_key,
                    organization=organization,
                    project=project,
                    auth_header=endpoint.auth_header if endpoint is not None else None,
                    auth_scheme=endpoint.auth_scheme if endpoint is not None else "bearer",
                    ssl_verify=endpoint.ssl_verify if endpoint is not None else None,
                )
            return owned.value

    @staticmethod
    def _tool_maps(request: ProviderTurnRequest) -> tuple[dict[str, str], dict[str, str]]:
        original: dict[str, str] = {}
        reverse: dict[str, str] = {}
        names = [tool.name for tool in request.available_tools if tool.name]
        for message in request.messages:
            names.extend(call.tool_name for call in message.tool_calls if call.tool_name)
            if message.tool_name:
                names.append(message.tool_name)
        for name in dict.fromkeys(names):
            candidate = _safe_tool_name(name)
            if candidate in reverse and reverse[candidate] != name:
                suffix = "_" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:_TOOL_NAME_HASH_LENGTH]
                candidate = f"{candidate[: _MAX_TOOL_NAME_LENGTH - len(suffix)]}{suffix}"
            original[name] = candidate
            reverse[candidate] = name
        return original, reverse

    def _messages(self, request: ProviderTurnRequest) -> list[dict[str, object]]:
        original_to_provider, _ = self._tool_maps(request)
        if self._model_name(request) in self.synthetic_tool_feedback_models or request.model_name in self.synthetic_tool_feedback_models:
            return self._synthetic_feedback_messages(request, original_to_provider)
        requires_reasoning_content = _requires_reasoning_content_with_tool_calls(
            provider_name=request.provider_name or self.name,
            # The mapped name is what actually reaches the provider, so a
            # ``model_map`` alias onto a deepseek model must still replay.
            model_name=self._model_name(request),
        )
        messages: list[dict[str, object]] = []
        for message in request.messages:
            if message.role == "assistant" and message.tool_calls:
                # A reasoning-locked provider still returns tool-call turns with
                # zero reasoning tokens when its own effort ladder decides not
                # to think (observed on the real DeepSeek wire); replaying that
                # authentic shape must not fail. Carry the provider's own
                # reasoning verbatim when it exists and never synthesize it when
                # the vendor produced none.
                tool_calls: list[dict[str, object]] = []
                for call in message.tool_calls:
                    arguments = json.dumps(
                        json_wire_object(call.arguments),
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    tool_calls.append(
                        {
                            # Ingest normalised this id once; render it verbatim
                            # rather than substituting the tool name, which two
                            # calls to the same tool would share.
                            "id": call.tool_call_id,
                            "type": "function",
                            "function": {
                                "name": original_to_provider.get(call.tool_name, call.tool_name),
                                "arguments": arguments,
                            },
                        }
                    )
                messages.append(
                    {
                        "role": "assistant",
                        "content": message.content,
                        # DeepSeek requires prior reasoning_content on every
                        # assistant turn of a tools request, including turns
                        # without tool calls; the core persists the provider's
                        # own text, so replay it verbatim when it exists.
                        **({"reasoning_content": message.reasoning} if requires_reasoning_content and message.reasoning else {}),
                        "tool_calls": tool_calls,
                    }
                )
                continue
            if message.role == "tool":
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": message.tool_call_id,
                        "content": message.content or "",
                    }
                )
            else:
                replayed: dict[str, object] = {"role": message.role, "content": message.content}
                if message.role == "assistant" and requires_reasoning_content and message.reasoning:
                    # DeepSeek's tools contract requires prior reasoning on all
                    # assistant turns, not only turns that carried a tool call.
                    replayed["reasoning_content"] = message.reasoning
                messages.append(replayed)
        return messages

    def _synthetic_feedback_messages(self, request: ProviderTurnRequest, original_to_provider: Mapping[str, str]) -> list[dict[str, object]]:
        """Keep conversation order while converting tool feedback for selected gateways."""
        call_arguments: dict[str, Mapping[str, object]] = {}
        messages: list[dict[str, object]] = []
        for message in request.messages:
            for call in message.tool_calls:
                if call.tool_call_id is not None:
                    call_arguments[call.tool_call_id] = call.arguments
            if message.role == "tool":
                arguments = call_arguments.pop(message.tool_call_id or "", None)
                payload = {
                    "tool_name": original_to_provider.get(message.tool_name or "", message.tool_name),
                    "tool_call_id": message.tool_call_id,
                    "arguments": json_wire_object(arguments) if arguments is not None else None,
                    "status": "error" if message.is_error else "ok",
                    "content": message.content or "",
                }
                messages.append({"role": "user", "content": json.dumps(payload, ensure_ascii=False, sort_keys=True)})
            elif message.role != "assistant" or not message.tool_calls or message.content:
                messages.append({"role": message.role, "content": message.content})
        return messages

    def _wire(self, request: ProviderTurnRequest) -> ProviderWireMaterialization:
        messages = self._messages(request)
        original_to_provider, _ = self._tool_maps(request)
        tools: list[dict[str, object]] = []
        for tool in request.available_tools:
            schema = json_wire_object(tool.input_schema)
            if schema.get("type") == "object" or "properties" in schema or "additionalProperties" in schema:
                parameters = dict(schema)
                parameters.setdefault("type", "object")
            else:
                properties = dict(schema)
                required = properties.pop("required", None)
                parameters = {"type": "object", "properties": properties, "additionalProperties": True}
                if isinstance(required, list) and all(isinstance(value, str) for value in required):
                    parameters["required"] = list(required)
            tools.append(
                {
                    "type": "function",
                    "function": {"name": original_to_provider.get(tool.name, tool.name), "description": tool.description, "parameters": parameters},
                }
            )
        stable_messages: list[dict[str, object]] = []
        for message in messages:
            if message.get("role") != "system":
                break
            stable_messages.append(message)
        canonical = json.dumps(
            {"assembly_version": 1, "messages": stable_messages, "tools": tools}, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        tool_bytes = json.dumps(tools, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return ProviderWireMaterialization(
            messages=messages,
            tools=tools,
            prefix=WirePrefixDescriptor(
                canonical_bytes=canonical,
                canonical_hash=hashlib.sha256(canonical).hexdigest(),
                materialized_message_count=len(messages),
                tool_generation=hashlib.sha256(tool_bytes).hexdigest(),
                assembly_version=1,
            ),
        )

    def _payload(self, request: ProviderTurnRequest, *, stream: bool) -> dict[str, object]:
        # Precedence: the request's own retention, else the endpoint config's
        # (its field default is "none"), else -- with no endpoint config -- "none".
        endpoint = self._endpoint_config()
        retention = request.cache_retention if request.cache_retention is not None else (endpoint.cache_retention if endpoint is not None else "none")
        if retention not in (None, "none"):
            # Prompt caching is an Anthropic Messages feature; this adapter would
            # silently drop the request, so fail explicitly instead. Fallback stays
            # allowed, matching the deleted LiteLLM backend: another provider in the
            # chain may be Anthropic-compatible. ``cache_retention`` is not exposed
            # by any schema that reaches this adapter (only ``providers.anthropic``
            # parses it, and that config is served by ``anthropic_native``), so in
            # practice this guard fires on the protocol-level
            # ``ProviderTurnRequest.cache_retention`` field.
            raise ProviderExecutionError(
                kind="unsupported_feature",
                provider_name=request.provider_name or self.name,
                model_name=request.model_name or "unknown",
                message="cache_retention requires an Anthropic Messages-compatible provider",
                retryable=False,
                fallback_allowed=True,
                details={"source": "payload", "reason": "unsupported_cache_retention"},
            )
        model_name = self._model_name(request)
        wire = self._wire(request)
        # The request carries no catalog object, so the shipped catalog entry for
        # the provider and the mapped model is the authority the legacy
        # ``request.model_metadata`` used to be.
        metadata = static_catalog_metadata(request.provider_name or self.name, model_name)
        rule = thinking_rule_for(request.provider_name or self.name, model_name)
        payload: dict[str, object] = {"model": model_name, "messages": wire.messages, "stream": stream}
        # The catalog is the authority on what this model accepts: a model that
        # cannot call tools is not handed a tool schema, and one that does not
        # reason is not handed a reasoning knob.
        if wire.tools and (metadata is None or metadata.supports_tools is not False):
            payload["tools"] = wire.tools
            if not rule.requires_reasoning_content_for_tool_calls:
                payload["tool_choice"] = "auto"
        if rule.sends_output_cap_by_default:
            # OMP sends no output cap unless the caller asks for one; the kimi
            # family is the one exception, and it sends the model's own maximum
            # (``resolveOpenAIOutputTokenParam``, openai-shared.ts:630-647).
            payload[rule.max_tokens_field_or_default()] = _output_cap(metadata)
        if request.reasoning_effort and (metadata is None or metadata.supports_reasoning is not False):
            effort = normalize_reasoning_effort(request.reasoning_effort)
            supported = metadata.supported_effort_levels if metadata is not None else None
            mapped = reasoning_kwargs(
                rule=rule,
                # The clamp decides the level from the model's own metadata; the
                # mapping only picks the request field (and needs the raw levels
                # again to resolve an explicit "off").
                effort=clamp_effort_to_supported(effort, supported),
                supported_levels=supported,
            )
            extra_body = mapped.get("extra_body")
            if isinstance(extra_body, dict):
                # The SDK merges ``extra_body`` into the JSON body; flattening its
                # contents would instead reach ``create()`` as SDK parameters and
                # raise TypeError for keys the SDK does not know.
                merged: dict[str, object] = {}
                existing = payload.get("extra_body")
                if isinstance(existing, dict):
                    merged.update(existing)
                merged.update(extra_body)
                payload["extra_body"] = merged
            else:
                payload.update(mapped)
        extra_headers = resolve_extra_request_headers(self.extra_request_headers, _session_id(request))
        if extra_headers:
            # A request option, not a body field: the SDK merges it into the HTTP
            # request and never serializes it into the JSON payload.
            payload["extra_headers"] = extra_headers
        if stream:
            payload["stream_options"] = {"include_usage": True}
        return payload

    def _timeout(self) -> float:
        configured = None if self.config is None else self.config.timeout_seconds
        return _DEFAULT_TIMEOUT_SECONDS if configured is None else configured

    @staticmethod
    def _response_payload(value: object) -> dict[str, object]:
        if isinstance(value, Mapping):
            return dict(value)
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            dumped = model_dump()
            if isinstance(dumped, Mapping):
                return dict(dumped)
        raise ValueError("provider response must be a mapping")

    @staticmethod
    def _stream_chunk_payload(value: object) -> dict[str, object]:
        """Decode one stream event, reporting undecodable events as stream errors.

        The SDK tolerates event shapes it cannot model, so a non-object event (e.g. a
        JSON array) reaches the transport as a non-mapping and is re-typed here rather
        than being reported as a retryable network failure; it is a non-retryable malformed provider stream.
        """
        try:
            return OpenAIChatCompletionsProvider._response_payload(value)
        except ValueError as exc:
            raise OpenAITransportError(_stream_event_error_payload(exc)) from exc

    @staticmethod
    def _map_exception(exc: Exception, *, provider_name: str, model_name: str, source: str) -> ProviderExecutionError:
        if isinstance(exc, ProviderExecutionError):
            return exc
        if isinstance(exc, OpenAITransportError):
            return provider_execution_error_from_payload(provider_name=provider_name, model_name=model_name, payload=exc.payload, source=source)
        known_network_failure = isinstance(exc, (httpx2.TransportError, OpenAIAPITimeoutError, OpenAIAPIConnectionError))
        return ProviderExecutionError(
            kind="provider_error",
            provider_name=provider_name,
            model_name=model_name,
            message="provider request failed",
            retryable=known_network_failure,
            fallback_allowed=True,
            details={
                "source": source,
                "exception_type": type(exc).__name__,
                **({"network_failure": True} if known_network_failure else {}),
            },
        )

    @staticmethod
    def _tool_calls(message: Mapping[str, object], reverse: Mapping[str, str], *, provider_name: str, model_name: str) -> tuple[ToolCall, ...]:
        raw_calls = message.get("tool_calls")
        if raw_calls is None:
            return ()
        if not isinstance(raw_calls, list):
            raise ProviderExecutionError(
                kind="provider_error",
                provider_name=provider_name,
                model_name=model_name,
                message="provider returned malformed tool calls",
                retryable=False,
                fallback_allowed=True,
                details={"source": "api", "reason": "malformed_tool_calls"},
            )
        parsed: list[ToolCall] = []
        for index, item in enumerate(raw_calls):
            if not isinstance(item, Mapping) or not isinstance(item.get("function"), Mapping):
                raise ProviderExecutionError(
                    kind="provider_error",
                    provider_name=provider_name,
                    model_name=model_name,
                    message="provider returned a malformed tool call",
                    retryable=False,
                    fallback_allowed=True,
                    details={"source": "api", "reason": "malformed_tool_call"},
                )
            function = item["function"]
            if not isinstance(function.get("name"), str) or not function["name"]:
                raise ProviderExecutionError(
                    kind="provider_error",
                    provider_name=provider_name,
                    model_name=model_name,
                    message="provider returned a malformed tool call",
                    retryable=False,
                    fallback_allowed=True,
                    details={"source": "api", "reason": "malformed_tool_call"},
                )
            provider_tool_name = function["name"]
            runtime_name = reverse.get(provider_tool_name, provider_tool_name)
            explicit_id = item.get("id") if isinstance(item.get("id"), str) else None
            parsed.append(
                ToolCall(
                    tool_name=runtime_name,
                    arguments=_parse_arguments(function.get("arguments")),
                    tool_call_id=normalize_tool_call_id(explicit_id, fallback=f"tool_call_{index + 1}"),
                )
            )
        return tuple(parsed)

    def propose_turn(self, request: ProviderTurnRequest) -> ProviderTurnResult:
        provider_name = request.provider_name or self.name
        model_name = request.model_name or "unknown"
        try:
            payload = self._payload(request, stream=False)
            response_payload = self._response_payload(self._transport().request(payload, timeout_seconds=self._timeout()))
            if response_payload.get("error") is not None or response_payload.get("type") == "error":
                raise OpenAITransportError(response_payload)
            usage = _usage(response_payload)
            metadata = _response_metadata(response_payload)
            choices = response_payload.get("choices")
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
                raise ProviderExecutionError(
                    kind="provider_error",
                    provider_name=provider_name,
                    model_name=model_name,
                    message="provider response contained no valid choices",
                    retryable=False,
                    fallback_allowed=True,
                    details={"source": "api", "reason": "malformed_response"},
                )
            choice = choices[0]
            message = choice.get("message")
            if not isinstance(message, Mapping):
                raise ProviderExecutionError(
                    kind="provider_error",
                    provider_name=provider_name,
                    model_name=model_name,
                    message="provider response choice contained no valid message",
                    retryable=False,
                    fallback_allowed=True,
                    details={"source": "api", "reason": "malformed_response"},
                )
            message_mapping = message
            content = message_mapping.get("content")
            if content is not None and not isinstance(content, str):
                raise ProviderExecutionError(
                    kind="provider_error",
                    provider_name=provider_name,
                    model_name=model_name,
                    message="provider response message content was not text",
                    retryable=False,
                    fallback_allowed=True,
                    details={"source": "api", "reason": "malformed_response"},
                )
            raw_finish_reason = choice.get("finish_reason")
            done_reason = _done_reason(raw_finish_reason)
            raw_token = _raw_finish_reason(raw_finish_reason)
            if done_reason == "error" and raw_token is not None:
                _raise_finish_reason_error(raw_token, provider_name=provider_name, model_name=model_name)
            if raw_token is None:
                metadata["finish_reason_reported"] = False
            return ProviderTurnResult(
                tool_calls=self._tool_calls(
                    message_mapping,
                    self._tool_maps(request)[1],
                    provider_name=provider_name,
                    model_name=model_name,
                ),
                output=content if isinstance(content, str) else "",
                reasoning=_reasoning(message_mapping),
                usage=usage,
                done_reason=done_reason,
                finish_reason_reported=raw_token is not None,
                metadata=metadata,
            )
        except Exception as exc:
            raise self._map_exception(exc, provider_name=provider_name, model_name=model_name, source="api") from exc

    def stream_turn(self, request: ProviderTurnRequest) -> Iterator[ProviderStreamEvent]:
        provider_name = request.provider_name or self.name
        model_name = request.model_name or "unknown"
        if request.abort_signal is not None and request.abort_signal.cancelled:
            yield ProviderStreamEvent(kind="error", channel="error", error="provider stream cancelled", error_kind="cancelled")
            yield ProviderStreamEvent(kind="done", done_reason="cancelled")
            return
        try:
            payload = self._payload(request, stream=True)
            stream = self._transport().request(payload, timeout_seconds=self._timeout())
            if isinstance(stream, BaseException):
                raise stream
            if not isinstance(stream, Iterator):
                raise ValueError("provider stream transport did not return an iterator")
            latest_usage: ProviderTokenUsage | None = None
            metadata: dict[str, object] = {}
            done_reason = "unknown"
            finish_reason_reported = False
            raw_finish_reason_token: str | None = None
            accumulators: dict[int, _ToolAccumulator] = {}
            for raw_chunk in iter_stream_with_timeout(
                stream,
                timeout_seconds=self._timeout(),
                provider_name=provider_name,
                model_name=model_name,
                first_event_timeout_seconds=DEFAULT_STREAM_FIRST_EVENT_TIMEOUT_SECONDS,
                aborted=lambda: abort_signal_cancelled(request),
            ):
                if request.abort_signal is not None and request.abort_signal.cancelled:
                    yield ProviderStreamEvent(kind="error", channel="error", error="provider stream cancelled", error_kind="cancelled")
                    yield ProviderStreamEvent(kind="done", done_reason="cancelled")
                    return
                chunk = self._stream_chunk_payload(raw_chunk)
                if chunk.get("error") is not None or chunk.get("type") == "error":
                    raise OpenAITransportError(chunk)
                metadata.update(_response_metadata(chunk))
                latest_usage = _usage(chunk) or latest_usage
                choices = chunk.get("choices")
                if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
                    continue
                choice = choices[0]
                raw_finish_reason = choice.get("finish_reason")
                if isinstance(raw_finish_reason, str) and raw_finish_reason.strip():
                    finish_reason_reported = True
                    done_reason = _done_reason(raw_finish_reason)
                    raw_finish_reason_token = raw_finish_reason if done_reason in {"error", "unknown"} else None
                delta = choice.get("delta")
                if not isinstance(delta, Mapping):
                    continue
                for key in ("reasoning_content", "reasoning"):
                    value = delta.get(key)
                    if isinstance(value, str) and value:
                        yield ProviderStreamEvent(kind="delta", channel="reasoning", text=value, metadata={"source": f"delta.{key}"})
                content = delta.get("content")
                if isinstance(content, str) and content:
                    yield ProviderStreamEvent(kind="delta", channel="text", text=content)
                raw_tool_calls = delta.get("tool_calls")
                if raw_tool_calls is None:
                    continue
                if not isinstance(raw_tool_calls, list):
                    raise ProviderExecutionError(
                        kind="provider_error",
                        provider_name=provider_name,
                        model_name=model_name,
                        message="provider stream tool_calls was not a list",
                        retryable=False,
                        fallback_allowed=True,
                        details={"source": "stream", "reason": "malformed_tool_calls"},
                    )
                for raw_tool in raw_tool_calls:
                    if not isinstance(raw_tool, Mapping):
                        raise ProviderExecutionError(
                            kind="provider_error",
                            provider_name=provider_name,
                            model_name=model_name,
                            message="provider stream tool call was not an object",
                            retryable=False,
                            fallback_allowed=True,
                            details={"source": "stream", "reason": "malformed_tool_call"},
                        )
                    raw_index = raw_tool.get("index")
                    if raw_index is not None and (not isinstance(raw_index, int) or isinstance(raw_index, bool)):
                        raise ProviderExecutionError(
                            kind="provider_error",
                            provider_name=provider_name,
                            model_name=model_name,
                            message="provider stream tool call index was malformed",
                            retryable=False,
                            fallback_allowed=True,
                            details={"source": "stream", "reason": "malformed_tool_call"},
                        )
                    index = raw_index if isinstance(raw_index, int) else 0
                    previous = accumulators.get(index, _ToolAccumulator())
                    raw_id = raw_tool.get("id")
                    if raw_id is not None and (not isinstance(raw_id, str) or not raw_id):
                        raise ProviderExecutionError(
                            kind="provider_error",
                            provider_name=provider_name,
                            model_name=model_name,
                            message="provider stream tool call id was malformed",
                            retryable=False,
                            fallback_allowed=True,
                            details={"source": "stream", "reason": "malformed_tool_call"},
                        )
                    if isinstance(raw_id, str) and previous.tool_call_id not in (None, raw_id):
                        raise ProviderExecutionError(
                            kind="provider_error",
                            provider_name=provider_name,
                            model_name=model_name,
                            message="provider stream changed a tool call id",
                            retryable=False,
                            fallback_allowed=True,
                            details={"source": "stream", "reason": "conflicting_tool_call_id"},
                        )
                    tool_id = normalize_tool_call_id(raw_id if isinstance(raw_id, str) else previous.tool_call_id, fallback=f"tool_call_{index + 1}")
                    function = raw_tool.get("function")
                    if function is not None and not isinstance(function, Mapping):
                        raise ProviderExecutionError(
                            kind="provider_error",
                            provider_name=provider_name,
                            model_name=model_name,
                            message="provider stream function payload was malformed",
                            retryable=False,
                            fallback_allowed=True,
                            details={"source": "stream", "reason": "malformed_tool_call"},
                        )
                    name = previous.tool_name
                    fragment: str | None = None
                    if isinstance(function, Mapping):
                        raw_name = function.get("name")
                        if raw_name is not None and (not isinstance(raw_name, str) or not raw_name):
                            raise ProviderExecutionError(
                                kind="provider_error",
                                provider_name=provider_name,
                                model_name=model_name,
                                message="provider stream function name was malformed",
                                retryable=False,
                                fallback_allowed=True,
                                details={"source": "stream", "reason": "malformed_tool_call"},
                            )
                        if isinstance(raw_name, str):
                            name = raw_name
                        raw_arguments = function.get("arguments")
                        if raw_arguments is not None and not isinstance(raw_arguments, str):
                            raise ProviderExecutionError(
                                kind="provider_error",
                                provider_name=provider_name,
                                model_name=model_name,
                                message="provider stream function arguments were malformed",
                                retryable=False,
                                fallback_allowed=True,
                                details={"source": "stream", "reason": "malformed_tool_call_arguments"},
                            )
                        if isinstance(raw_arguments, str):
                            fragment = raw_arguments
                    complete_first = False
                    if fragment and not previous.fragments:
                        try:
                            complete_first = isinstance(json.loads(fragment), dict)
                        except json.JSONDecodeError:
                            # Deliberate probe, not swallowed failure: an incomplete
                            # fragment is exactly the answer "this is a delta".
                            pass
                    explicit = previous.explicit_streaming or (isinstance(raw_id, str) and bool(raw_id) and not complete_first)
                    started = previous.started
                    reverse = self._tool_maps(request)[1]
                    if explicit and name is not None and not started:
                        yield ProviderStreamEvent(
                            kind="tool_call_start", channel="tool", tool_call_id=tool_id, tool_name=reverse.get(name, name), tool_call_ordinal=index
                        )
                        started = True
                    if explicit and fragment:
                        yield ProviderStreamEvent(
                            kind="tool_call_delta",
                            channel="tool",
                            tool_call_id=tool_id,
                            arguments_delta=fragment,
                            tool_call_ordinal=index,
                            fragment_ordinal=len(previous.fragments),
                        )
                    accumulators[index] = _ToolAccumulator(
                        raw_id if isinstance(raw_id, str) else previous.tool_call_id,
                        name,
                        previous.fragments + ((fragment,) if fragment is not None else ()),
                        explicit,
                        started,
                    )
        except Exception as exc:
            raise self._map_exception(exc, provider_name=provider_name, model_name=model_name, source="stream") from exc
        if not finish_reason_reported:
            raise ProviderExecutionError(
                kind="provider_error",
                provider_name=provider_name,
                model_name=model_name,
                message="provider stream ended without a finish_reason",
                retryable=False,
                fallback_allowed=True,
                details={"source": "stream", "reason": "missing_terminal_event"},
            )
        completed: list[tuple[int, _ToolAccumulator, dict[str, object], str]] = []
        for index, accumulator in sorted(accumulators.items()):
            if accumulator.tool_name is None:
                raise ProviderExecutionError(
                    kind="provider_error",
                    provider_name=provider_name,
                    model_name=model_name,
                    message="provider stream contained a malformed tool call",
                    retryable=False,
                    fallback_allowed=True,
                    details={"source": "stream", "reason": "malformed_tool_call"},
                )
            try:
                parsed = json.loads(accumulator.arguments)
            except json.JSONDecodeError as exc:
                raise ProviderExecutionError(
                    kind="provider_error",
                    provider_name=provider_name,
                    model_name=model_name,
                    message="provider stream ended with malformed tool-call arguments",
                    retryable=False,
                    fallback_allowed=True,
                    details={"source": "stream", "reason": "malformed_tool_call_arguments"},
                ) from exc
            if not isinstance(parsed, dict):
                raise ProviderExecutionError(
                    kind="provider_error",
                    provider_name=provider_name,
                    model_name=model_name,
                    message="provider stream tool-call arguments were not an object",
                    retryable=False,
                    fallback_allowed=True,
                    details={"source": "stream", "reason": "malformed_tool_call_arguments"},
                )
            runtime_name = reverse.get(accumulator.tool_name, accumulator.tool_name)
            completed.append((index, accumulator, parsed, runtime_name))
        if raw_finish_reason_token is not None:
            if done_reason == "error":
                _raise_finish_reason_error(raw_finish_reason_token, provider_name=provider_name, model_name=model_name)
            metadata["finish_reason_raw"] = raw_finish_reason_token
        if any(accumulator.explicit_streaming for _index, accumulator, _parsed, _name in completed):
            for index, accumulator, parsed, runtime_name in completed:
                if accumulator.explicit_streaming:
                    tool_id = normalize_tool_call_id(
                        accumulator.tool_call_id,
                        fallback=f"tool_call_{index + 1}",
                    )
                    yield ProviderStreamEvent(
                        kind="tool_call_end",
                        channel="tool",
                        tool_call_id=tool_id,
                        tool_name=runtime_name,
                        tool_call_ordinal=index,
                        parsed_arguments=parsed,
                    )
            completed = [entry for entry in completed if not entry[1].explicit_streaming]
        if completed:
            event_calls: list[dict[str, object]] = []
            for index, accumulator, parsed, runtime_name in completed:
                event_calls.append(
                    {
                        "tool_name": runtime_name,
                        "arguments": parsed,
                        "tool_call_id": normalize_tool_call_id(
                            accumulator.tool_call_id,
                            fallback=f"tool_call_{index + 1}",
                        ),
                    }
                )
            event_payload: dict[str, object] = event_calls[0] if len(event_calls) == 1 else {"tool_calls": event_calls}
            yield ProviderStreamEvent(kind="content", channel="tool", text=json.dumps(event_payload))
        metadata["finish_reason_reported"] = finish_reason_reported
        yield ProviderStreamEvent(kind="done", done_reason=done_reason, metadata=metadata, usage=latest_usage)
        write_provider_trace(
            request=payload,
            response={"native_stream": True},
            metadata={
                "session_id": _session_id(request) or None,
                "provider": provider_name,
                "model": request.model_name,
                "attempt": request.attempt,
                "stream": True,
                "transport": "openai_chat_completions",
            },
        )


def _response_metadata(payload: Mapping[str, object]) -> dict[str, object]:
    metadata: dict[str, object] = {}
    if isinstance(payload.get("id"), str) and payload["id"]:
        metadata["request_id"] = payload["id"]
    if isinstance(payload.get("model"), str) and payload["model"]:
        metadata["served_model"] = payload["model"]
    return metadata


__all__ = [
    "OpenAIChatCompletionsProvider",
    "OpenAIChatCompletionsTransport",
    "OpenAITransport",
    "OpenAITransportError",
    "normalize_openai_base_url",
]
