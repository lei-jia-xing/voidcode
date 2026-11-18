from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Protocol, cast

import httpx2
from anthropic import Anthropic
from anthropic import APIConnectionError as AnthropicAPIConnectionError
from anthropic import APIError as AnthropicAPIError
from anthropic import APITimeoutError as AnthropicAPITimeoutError
from anthropic import Omit as AnthropicAPIKeyOmit

from voidcode.sdk.json_values import json_wire_object
from voidcode.sdk.provider import (
    ModelContextLimits,
    ProviderDoneReason,
    ProviderExecutionError,
    ProviderStreamEvent,
    ProviderTokenUsage,
    ProviderTurnRequest,
    ProviderTurnResult,
)
from voidcode.sdk.tools import ToolCall

from ._wire_common import (
    DEFAULT_STREAM_FIRST_EVENT_TIMEOUT_SECONDS,
    OwnedResource,
    abort_signal_cancelled,
    iter_stream_with_timeout,
    normalize_tool_call_id,
    parse_tool_arguments,
    resolve_extra_request_headers,
    safe_tool_name,
    usage_int,
)
from .config import AnthropicProviderConfig
from .errors import (
    provider_execution_error_from_payload,
)
from .model_catalog import catalog_model_limits, static_catalog_metadata
from .provider_config import anthropic_wire_default_base_url
from .provider_table import PROVIDER_TABLE_BY_ID
from .reasoning_effort import clamp_effort_to_supported, normalize_reasoning_effort
from .thinking_rules import ThinkingRule, thinking_rule_for
from .trace import write_provider_trace

_DEFAULT_ANTHROPIC_BASE_URL = PROVIDER_TABLE_BY_ID["anthropic"].default_base_url
_DEFAULT_ANTHROPIC_VERSION = "2023-06-01"
_DEFAULT_TIMEOUT_SECONDS = 300.0
# Required output cap when the catalog does not report one.
_OUTPUT_CAP_WHEN_UNKNOWN = 64000
# The SDK refuses to construct a client without a credential, and an explicit
# credential also stops it from reading ambient ones (``ANTHROPIC_API_KEY``,
# profile, workload identity). The placeholder only satisfies that check:
# ``_default_headers`` decides which credential headers are sent.
_PLACEHOLDER_API_KEY = "voidcode-no-api-key"


class AnthropicTransport(Protocol):
    def request(self, payload: dict[str, object], *, timeout_seconds: float) -> object: ...


@dataclass(frozen=True, slots=True)
class AnthropicTransportError(Exception):
    payload: dict[str, object]
    message: str = "provider request failed"


class AnthropicMessagesTransport:
    """HTTP transport for the Anthropic Messages API."""

    def __init__(
        self,
        *,
        base_url: str = _DEFAULT_ANTHROPIC_BASE_URL,
        api_key: str | None = None,
        version: str = _DEFAULT_ANTHROPIC_VERSION,
        beta_headers: tuple[str, ...] = (),
        auth_header: str | None = None,
        bearer_token: str | None = None,
        http_client: httpx2.Client | None = None,
    ) -> None:
        self.base_url = _normalize_base_url(base_url)
        self.api_key = api_key
        self.version = version.strip() or _DEFAULT_ANTHROPIC_VERSION
        self.beta_headers = tuple(dict.fromkeys(value.strip() for value in beta_headers if value.strip()))
        self.auth_header = auth_header
        self.bearer_token = bearer_token
        self.http_client = http_client
        self._client_lock = Lock()
        self._sdk_client: Anthropic | None = None

    def _default_headers(self) -> dict[str, str]:
        # ``Accept`` is deliberately left to the SDK (``application/json`` for
        # streaming too): the official client never sends
        # ``text/event-stream`` and the API selects SSE from the ``stream``
        # body field, so negotiation is body-driven.
        headers = {"anthropic-version": self.version}
        if self.beta_headers:
            headers["anthropic-beta"] = ",".join(self.beta_headers)
        # ``x-api-key`` carries the SDK's API-key header. It is dropped whenever
        # that header is not the credential this transport is configuring: for a
        # custom scheme because the configured header stays authoritative, and for
        # a keyless transport because the constructor placeholder must never reach
        # the wire. The SDK types this mapping as str -> str but honours its own
        # ``Omit`` sentinel, which both removes the header and satisfies its auth
        # check.
        omit_api_key = not self.api_key
        token = self.bearer_token or self.api_key
        if self.auth_header and token:
            headers[self.auth_header] = f"Bearer {token}" if self.auth_header.lower() == "authorization" else token
            # A ``x-api-key`` scheme is the SDK's own header, so the value written
            # above must survive instead of being dropped.
            omit_api_key = self.auth_header.lower() != "x-api-key"
        if omit_api_key:
            headers["x-api-key"] = cast(str, AnthropicAPIKeyOmit())
        return headers

    def close(self) -> None:
        with self._client_lock:
            client, self._sdk_client = self._sdk_client, None
        if client is not None and self.http_client is None:
            client.close()

    def _sdk(self) -> Anthropic:
        with self._client_lock:
            if self._sdk_client is None:
                # ``max_retries=0`` keeps retry/fallback owned by the runtime.
                self._sdk_client = Anthropic(
                    api_key=self.api_key or _PLACEHOLDER_API_KEY,
                    base_url=self.base_url,
                    default_headers=self._default_headers(),
                    http_client=self.http_client,
                    max_retries=0,
                )
            return self._sdk_client

    @staticmethod
    def _api_error_payload(exc: AnthropicAPIError) -> dict[str, object]:
        payload: dict[str, object] = {}
        body = getattr(exc, "body", None)
        if isinstance(body, Mapping):
            payload.update(body)
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
        if isinstance(status_code, int):
            payload["status_code"] = status_code
        headers = getattr(response, "headers", None)
        if isinstance(headers, Mapping):
            payload.setdefault("headers", dict(headers))
        if isinstance(exc, (AnthropicAPITimeoutError, AnthropicAPIConnectionError)):
            payload["network_failure"] = True
            payload["exception_type"] = type(exc).__name__
        payload.setdefault("message", str(exc))
        return payload

    def request(self, payload: dict[str, object], *, timeout_seconds: float) -> object:
        try:
            result = self._sdk().messages.create(**cast(Any, payload), timeout=timeout_seconds)
        except AnthropicAPIError as exc:
            raise AnthropicTransportError(self._api_error_payload(exc)) from exc
        return result


def _normalize_base_url(base_url: str | None) -> str:
    value = (base_url or _DEFAULT_ANTHROPIC_BASE_URL).strip().rstrip("/") or _DEFAULT_ANTHROPIC_BASE_URL
    # The official SDK appends its own ``/v1`` path segment, so a configured
    # host must not carry one or the request path doubles the version.
    return re.sub(r"/v1$", "", value, flags=re.IGNORECASE) or _DEFAULT_ANTHROPIC_BASE_URL


def _usage(payload: Mapping[str, object]) -> ProviderTokenUsage | None:
    raw = payload.get("usage")
    if not isinstance(raw, Mapping):
        return None
    uncached_input = usage_int(raw.get("input_tokens"))
    output_tokens = usage_int(raw.get("output_tokens"))
    cache_read = usage_int(raw.get("cache_read_input_tokens"))
    cache_write = usage_int(raw.get("cache_creation_input_tokens"))
    if uncached_input is None and output_tokens is None and cache_read is None and cache_write is None:
        return None
    # Anthropic's own ``input_tokens`` is uncached-only, so it is normalized here to
    # the inclusive prompt total every other wire already reports
    # (``ProviderTokenUsage.input_tokens``); ``cache_creation_input_tokens`` stays a
    # separate additive bucket.
    input_tokens = uncached_input + cache_read if uncached_input is not None and cache_read is not None else None
    return ProviderTokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        uncached_input_tokens=uncached_input,
    )


def _done_reason(value: object) -> ProviderDoneReason:
    if not isinstance(value, str):
        return "unknown"
    value = value.strip().lower()
    if value in {"end_turn", "stop_sequence", "stop"}:
        return "stop"
    if value == "tool_use":
        return "tool_calls"
    if value in {"max_tokens", "length"}:
        return "length"
    if value in {"refusal"}:
        return "content_filter"
    if value in {"error", "model_context_window_exceeded"}:
        return "error"
    return "unknown"


def _merge_usage(previous: ProviderTokenUsage | None, current: ProviderTokenUsage | None) -> ProviderTokenUsage | None:
    if previous is None:
        return current
    if current is None:
        return previous
    uncached = current.uncached_input_tokens if current.uncached_input_tokens is not None else previous.uncached_input_tokens
    cache_read = current.cache_read_tokens if current.cache_read_tokens is not None else previous.cache_read_tokens
    input_tokens = uncached + cache_read if uncached is not None and cache_read is not None else None
    output_tokens = current.output_tokens if current.output_tokens is not None else previous.output_tokens
    cache_write = current.cache_write_tokens if current.cache_write_tokens is not None else previous.cache_write_tokens
    return ProviderTokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        uncached_input_tokens=uncached,
    )


def _response_metadata(payload: Mapping[str, object]) -> dict[str, object]:
    metadata: dict[str, object] = {}
    if isinstance(payload.get("id"), str) and payload["id"]:
        metadata["request_id"] = payload["id"]
    if isinstance(payload.get("model"), str) and payload["model"]:
        metadata["served_model"] = payload["model"]
    content = payload.get("content")
    if isinstance(content, list):
        thinking_blocks = [dict(block) for block in content if isinstance(block, Mapping) and block.get("type") in {"thinking", "redacted_thinking"}]
        if thinking_blocks:
            metadata["thinking_blocks"] = thinking_blocks
    return metadata


@dataclass(slots=True)
class _ToolAccumulator:
    tool_call_id: str
    tool_name: str
    initial_input: dict[str, object]
    fragments: list[str] = field(default_factory=list)

    @property
    def arguments(self) -> str:
        return "".join(self.fragments)


@dataclass(frozen=True, slots=True)
class AnthropicMessagesProvider:
    name: str = "anthropic"
    config: AnthropicProviderConfig | None = None
    transport: AnthropicTransport | None = None
    auth_header: str | None = None
    # Headers this gateway requires on every request it serves. They reach the wire
    # through the SDK's ``extra_headers`` argument, which the transport passes to
    # ``messages.create`` from the payload, so the JSON body never carries them.
    extra_request_headers: Mapping[str, str] = field(default_factory=dict)
    # One transport -- and therefore one SDK client and HTTP connection pool --
    # per provider, reused across every turn. Building it per request leaked a
    # pool per turn. First-use initialization is serialized.
    _owned_transport: OwnedResource[AnthropicTransport] = field(default_factory=OwnedResource, compare=False, repr=False)

    def provider_config(self) -> AnthropicProviderConfig | None:
        return self.config

    def model_limits(self, model_name: str) -> ModelContextLimits | None:
        """Advertised limits for one model; gateway aliases resolve before dispatch."""
        if not isinstance(model_name, str) or not model_name.strip():
            return None
        return catalog_model_limits(self.name, model_name)

    def close(self) -> None:
        self._owned_transport.close()

    def _transport(self) -> AnthropicTransport:
        if self.transport is not None:
            return self.transport
        owned = self._owned_transport
        with owned.lock:
            if owned.value is None:
                config = self.config
                # A provider whose config names no endpoint resolves to its own vendor
                # default from the Anthropic wire table; one without a default must not
                # borrow Anthropic's host just because its SDK has one. That class
                # default is for direct construction only, never a provider-level
                # fallback, so an endpoint-less provider fails here instead.
                base_url = config.base_url if config and config.base_url else anthropic_wire_default_base_url(self.name)
                if not base_url:
                    raise ProviderExecutionError(
                        kind="not_configured",
                        provider_name=self.name,
                        model_name="unknown",
                        message=f"provider '{self.name}' has no endpoint configured; set providers.{self.name}.base_url and its API key",
                        retryable=False,
                        fallback_allowed=True,
                    )
                owned.value = AnthropicMessagesTransport(
                    base_url=base_url,
                    # Credentials come only from resolved provider config: an ambient
                    # ``ANTHROPIC_API_KEY`` must never be attached to another
                    # Anthropic-wire vendor's endpoint (or to the default base URL when
                    # the provider block is absent).
                    api_key=config.api_key if config else None,
                    auth_header=self.auth_header,
                    version=config.version if config and config.version else _DEFAULT_ANTHROPIC_VERSION,
                    beta_headers=config.beta_headers if config else (),
                )
            return owned.value

    @staticmethod
    def _session_id(request: ProviderTurnRequest) -> str | None:
        """Session id header placeholders resolve against, from request metadata."""
        value = request.metadata.get("session_id")
        return value if isinstance(value, str) else None

    @staticmethod
    def _tool_maps(request: ProviderTurnRequest) -> tuple[dict[str, str], dict[str, str]]:
        original: dict[str, str] = {}
        reverse: dict[str, str] = {}
        names = [tool.name for tool in request.available_tools]
        for message in request.messages:
            if message.tool_name:
                names.append(message.tool_name)
            names.extend(call.tool_name for call in message.tool_calls)
        for name in dict.fromkeys(name for name in names if name):
            candidate = safe_tool_name(name)
            if candidate in reverse and reverse[candidate] != name:
                suffix = "_" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
                candidate = f"{candidate[: 64 - len(suffix)]}{suffix}"
            original[name] = candidate
            reverse[candidate] = name
        return original, reverse

    def _messages_and_system(self, request: ProviderTurnRequest) -> tuple[str | None, list[dict[str, object]]]:
        original_to_provider, _ = self._tool_maps(request)
        system_parts: list[str] = []
        messages: list[dict[str, object]] = []

        def append_message(role: str, block: object) -> None:
            if messages and messages[-1]["role"] == role:
                content = messages[-1].setdefault("content", [])
                if isinstance(content, list):
                    content.append(block)
            else:
                messages.append({"role": role, "content": [block]})

        for message in request.messages:
            if message.role == "system":
                if message.content:
                    system_parts.append(message.content)
                continue
            if message.role == "assistant":
                metadata = json_wire_object(message.metadata)
                thinking_blocks = metadata.get("thinking_blocks")
                # Only the opaque blocks the wire gave back can be replayed: the
                # Messages API requires a signature on a thinking block, so the
                # plain ``reasoning`` text is never turned into one.
                prior = (
                    [item for item in thinking_blocks if isinstance(item, Mapping) and item.get("type") in {"thinking", "redacted_thinking"}]
                    if isinstance(thinking_blocks, list)
                    else []
                )
                blocks: list[dict[str, object]] = []
                if message.content:
                    blocks.append({"type": "text", "text": message.content})
                blocks.extend(
                    {
                        "type": "tool_use",
                        # Ingest normalised this id once; render it verbatim rather than
                        # substituting the tool name, which two calls to the same tool
                        # would share.
                        "id": call.tool_call_id,
                        "name": original_to_provider.get(call.tool_name, call.tool_name),
                        "input": json_wire_object(call.arguments),
                    }
                    for call in message.tool_calls
                )
                for block in [*prior, *blocks]:
                    append_message("assistant", block)
                continue
            if message.role == "tool":
                if message.images:
                    tool_content: list[dict[str, object]] = []
                    if message.content:
                        tool_content.append({"type": "text", "text": message.content})
                    for img in message.images:
                        tool_content.append(
                            {
                                "type": "image",
                                "source": {"type": "base64", "media_type": img.media_type, "data": img.data},
                            }
                        )
                else:
                    tool_content = message.content or ""
                block: dict[str, object] = {
                    "type": "tool_result",
                    "tool_use_id": message.tool_call_id,
                    "content": tool_content,
                }
                if message.is_error:
                    block["is_error"] = True
                append_message("user", block)
                continue
            if message.content:
                append_message("user", {"type": "text", "text": message.content})
            if message.images:
                for img in message.images:
                    append_message(
                        "user",
                        {
                            "type": "image",
                            "source": {"type": "base64", "media_type": img.media_type, "data": img.data},
                        },
                    )
        return ("\n\n".join(system_parts) if system_parts else None), messages

    def _payload(self, request: ProviderTurnRequest, *, stream: bool) -> dict[str, object]:
        provider_name = request.provider_name or self.name
        model_name = request.model_name or "unknown"
        if not request.model_name:
            raise ProviderExecutionError(
                kind="invalid_model",
                provider_name=provider_name,
                model_name=model_name,
                message="provider requires model name",
                retryable=False,
                fallback_allowed=True,
            )
        metadata = static_catalog_metadata(provider_name, request.model_name or "")
        model_name = request.model_name or ""
        if any(bool(m.images) for m in request.messages):
            if metadata is not None and (
                metadata.supports_vision is False or (metadata.modalities_input is not None and "image" not in metadata.modalities_input)
            ):
                raise ProviderExecutionError(
                    kind="unsupported_feature",
                    provider_name=provider_name,
                    model_name=model_name,
                    message=f"model '{model_name}' does not support image input",
                    retryable=False,
                    fallback_allowed=True,
                )
        system, messages = self._messages_and_system(request)
        rule = thinking_rule_for(request.provider_name or self.name, model_name)
        # The Anthropic Messages API requires ``max_tokens``, so the cap is the
        # model's own maximum (OMP's ``maxAllowedTokens``); 64000 is only the
        # unknown-model fallback, never a ceiling over a larger known model.
        max_tokens = metadata.max_output_tokens if metadata is not None and metadata.max_output_tokens else _OUTPUT_CAP_WHEN_UNKNOWN
        payload: dict[str, object] = {"model": request.model_name, "max_tokens": max_tokens, "messages": messages, "stream": stream}
        if system:
            payload["system"] = system
        tools: list[dict[str, object]] = []
        if request.available_tools and (metadata is None or metadata.supports_tools is not False):
            original_to_provider, _ = self._tool_maps(request)
            for tool in request.available_tools:
                schema = json_wire_object(tool.input_schema)
                schema.setdefault("type", "object")
                tools.append({"name": original_to_provider[tool.name], "description": tool.description, "input_schema": schema})
            payload["tools"] = tools
            payload["tool_choice"] = {"type": "auto"}
        _thinking_payload(payload, request=request, rule=rule)
        # Precedence: the request's own retention, else the provider config's
        # (its field default is "short", omp upstream default), else -- with no
        # config at all -- "short".
        retention = request.cache_retention if request.cache_retention is not None else (self.config.cache_retention if self.config else "short")
        if retention in {"short", "long"}:
            cache_control = {"type": "ephemeral", "ttl": "5m" if retention == "short" else "1h"}
            if tools:
                tools[-1]["cache_control"] = cache_control
            elif system:
                payload["system"] = [{"type": "text", "text": system, "cache_control": cache_control}]
        extra_headers = resolve_extra_request_headers(self.extra_request_headers, self._session_id(request))
        if extra_headers:
            # A request option, not a body field: the SDK merges it into the HTTP
            # request and never serializes it into the JSON payload.
            payload["extra_headers"] = extra_headers
        return payload

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
    def _map_exception(exc: Exception, *, provider_name: str, model_name: str, source: str) -> ProviderExecutionError:
        if isinstance(exc, ProviderExecutionError):
            return exc
        if isinstance(exc, AnthropicAPIError):
            return provider_execution_error_from_payload(
                provider_name=provider_name,
                model_name=model_name,
                payload=AnthropicMessagesTransport._api_error_payload(exc),
                source=source,
            )
        if isinstance(exc, AnthropicTransportError):
            return provider_execution_error_from_payload(provider_name=provider_name, model_name=model_name, payload=exc.payload, source=source)
        known_network_failure = isinstance(exc, (httpx2.TransportError, AnthropicAPITimeoutError, AnthropicAPIConnectionError))
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
    def _blocks(response: Mapping[str, object]) -> tuple[str, str | None, tuple[ToolCall, ...]]:
        content = response.get("content")
        if not isinstance(content, list):
            raise ValueError("provider response content must be an array")
        text_parts: list[str] = []
        thinking_parts: list[str] = []
        calls: list[ToolCall] = []
        for index, raw in enumerate(content):
            if not isinstance(raw, Mapping):
                continue
            block_type = raw.get("type")
            if block_type == "text" and isinstance(raw.get("text"), str):
                text_parts.append(raw["text"])
            elif block_type == "thinking" and isinstance(raw.get("thinking"), str):
                thinking_parts.append(raw["thinking"])
            elif block_type == "tool_use":
                name = raw.get("name")
                if not isinstance(name, str) or not name:
                    raise ValueError("provider tool call name must be a non-empty string")
                calls.append(
                    ToolCall(
                        tool_name=name,
                        arguments=parse_tool_arguments(raw.get("input")),
                        tool_call_id=normalize_tool_call_id(
                            raw.get("id") if isinstance(raw.get("id"), str) else None, fallback=f"tool_call_{index + 1}"
                        ),
                    )
                )
        return "".join(text_parts), "".join(thinking_parts) or None, tuple(calls)

    def propose_turn(self, request: ProviderTurnRequest) -> ProviderTurnResult:
        provider_name = request.provider_name or self.name
        model_name = request.model_name or "unknown"
        try:
            payload = self._payload(request, stream=False)
            response = self._response_payload(self._transport().request(payload, timeout_seconds=self._timeout()))
            if response.get("type") == "error" or response.get("error") is not None:
                raise AnthropicTransportError(response)
            stop_reason = response.get("stop_reason")
            # Missing/empty reasons close the model turn as unknown; only this
            # turn is known to have ended, not the user turn or session.
            finish_reason_reported = isinstance(stop_reason, str) and bool(stop_reason.strip())
            text, reasoning, calls = self._blocks(response)
            _original_to_provider, reverse = self._tool_maps(request)
            calls = tuple(
                ToolCall(
                    tool_name=reverse.get(call.tool_name, call.tool_name),
                    arguments=json_wire_object(call.arguments),
                    tool_call_id=call.tool_call_id,
                )
                for call in calls
            )
            metadata = _response_metadata(response)
            done_reason = _done_reason(stop_reason)
            if done_reason == "unknown" and finish_reason_reported:
                # The token is reported but unmapped; preserve it for core diagnostics.
                metadata["finish_reason_raw"] = stop_reason
            write_provider_trace(
                request=payload,
                response=response,
                metadata={
                    "session_id": self._session_id(request),
                    "provider": provider_name,
                    "model": request.model_name,
                    "attempt": request.attempt,
                    "stream": False,
                    "transport": "anthropic_messages",
                },
            )
            return ProviderTurnResult(
                tool_calls=calls,
                output=text,
                reasoning=reasoning,
                usage=_usage(response),
                done_reason=done_reason,
                finish_reason_reported=finish_reason_reported,
                metadata=metadata,
            )
        except Exception as exc:
            raise self._map_exception(exc, provider_name=provider_name, model_name=model_name, source="api") from exc

    def _timeout(self) -> float:
        return _DEFAULT_TIMEOUT_SECONDS if self.config is None or self.config.timeout_seconds is None else self.config.timeout_seconds

    def stream_turn(self, request: ProviderTurnRequest) -> Iterator[ProviderStreamEvent]:
        provider_name = request.provider_name or self.name
        model_name = request.model_name or "unknown"
        if request.abort_signal is not None and request.abort_signal.cancelled:
            yield ProviderStreamEvent(kind="error", channel="error", error="provider stream cancelled", error_kind="cancelled")
            yield ProviderStreamEvent(kind="done", done_reason="cancelled")
            return
        try:
            payload = self._payload(request, stream=True)
            raw_stream = self._transport().request(payload, timeout_seconds=self._timeout())
            if isinstance(raw_stream, BaseException):
                raise raw_stream
            if not isinstance(raw_stream, Iterator):
                raise ValueError("provider stream transport did not return an iterator")
            latest_usage: ProviderTokenUsage | None = None
            metadata: dict[str, object] = {}
            accumulators: dict[int, _ToolAccumulator] = {}
            thinking_blocks: dict[int, dict[str, object]] = {}
            thinking_fragments: dict[int, list[str]] = {}
            reverse = self._tool_maps(request)[1]
            stop_reason: str | None = None
            message_stopped = False
            for raw_chunk in iter_stream_with_timeout(
                raw_stream,
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
                chunk = self._response_payload(raw_chunk)
                if chunk.get("type") == "error" or chunk.get("error") is not None:
                    raise AnthropicTransportError(chunk)
                event_type = chunk.get("type")
                if event_type in {"content_block_start", "content_block_delta"}:
                    index = chunk.get("index")
                    if not isinstance(index, int) or isinstance(index, bool) or index < 0:
                        raise ValueError("provider content block index must be a non-negative integer")
                if event_type == "message_start":
                    message = chunk.get("message")
                    if isinstance(message, Mapping):
                        metadata.update(_response_metadata(message))
                        latest_usage = _merge_usage(latest_usage, _usage(message))
                elif event_type == "content_block_start":
                    if index in accumulators or index in thinking_blocks:
                        raise ValueError("provider stream repeated a tool or thinking block index")
                    block = chunk.get("content_block")
                    if isinstance(block, Mapping) and block.get("type") in {"thinking", "redacted_thinking"}:
                        thinking_blocks[index] = dict(block)
                        if block.get("type") == "thinking":
                            thinking_fragments[index] = [cast(str, block.get("thinking", ""))]
                    elif isinstance(block, Mapping) and block.get("type") == "tool_use":
                        name = block.get("name")
                        raw_input = block.get("input")
                        if not isinstance(name, str) or not name or not isinstance(raw_input, Mapping):
                            raise ValueError("provider tool call requires a name and object input")
                        tool_id = normalize_tool_call_id(
                            block.get("id") if isinstance(block.get("id"), str) else None, fallback=f"tool_call_{index + 1}"
                        )
                        accumulators[index] = _ToolAccumulator(tool_call_id=tool_id, tool_name=name, initial_input=dict(raw_input))
                        yield ProviderStreamEvent(
                            kind="tool_call_start",
                            channel="tool",
                            tool_call_id=tool_id,
                            tool_name=reverse.get(name, name),
                            tool_call_ordinal=index,
                        )

                elif event_type == "content_block_delta":
                    delta = chunk.get("delta")
                    if not isinstance(delta, Mapping):
                        continue
                    delta_type = delta.get("type")
                    if delta_type == "text_delta" and isinstance(delta.get("text"), str):
                        yield ProviderStreamEvent(kind="delta", channel="text", text=delta["text"])
                    elif delta_type == "thinking_delta" and isinstance(delta.get("thinking"), str):
                        thinking_fragments[index].append(delta["thinking"])
                        yield ProviderStreamEvent(kind="delta", channel="reasoning", text=delta["thinking"], metadata={"source": "delta.thinking"})
                    elif delta_type == "signature_delta" and isinstance(delta.get("signature"), str):
                        block = thinking_blocks[index]
                        block["signature"] = cast(str, block.get("signature", "")) + delta["signature"]
                    elif delta_type == "input_json_delta" and isinstance(delta.get("partial_json"), str):
                        fragment = delta["partial_json"]
                        prior = accumulators[index]
                        prior.fragments.append(fragment)
                        yield ProviderStreamEvent(
                            kind="tool_call_delta",
                            channel="tool",
                            tool_call_id=prior.tool_call_id,
                            arguments_delta=fragment,
                            tool_call_ordinal=index,
                            fragment_ordinal=len(prior.fragments) - 1,
                        )
                elif event_type == "message_delta":
                    delta = chunk.get("delta")
                    if isinstance(delta, Mapping) and isinstance(delta.get("stop_reason"), str):
                        stop_reason = delta["stop_reason"]
                    latest_usage = _merge_usage(latest_usage, _usage(chunk))
                elif event_type == "message_stop":
                    message_stopped = True
                    break
                elif event_type == "ping":
                    continue
                elif event_type in {"content_block_stop", "message_start"}:
                    continue
            if not message_stopped:
                raise ProviderExecutionError(
                    kind="provider_error",
                    provider_name=provider_name,
                    model_name=model_name,
                    message="provider stream ended without terminal message_stop",
                    retryable=False,
                    fallback_allowed=True,
                    details={"source": "stream", "reason": "missing_terminal_event"},
                )
            # message_stop is terminal; without deltas the wire's explicit input object is complete.
            for index, accumulator in sorted(accumulators.items()):
                try:
                    parsed = parse_tool_arguments(accumulator.arguments) if accumulator.fragments else accumulator.initial_input
                except (ValueError, json.JSONDecodeError) as exc:
                    raise ProviderExecutionError(
                        kind="provider_error",
                        provider_name=provider_name,
                        model_name=model_name,
                        message="provider stream ended with malformed tool-call arguments",
                        retryable=False,
                        fallback_allowed=True,
                        details={"source": "stream", "reason": "malformed_tool_call_arguments"},
                    ) from exc
                yield ProviderStreamEvent(
                    kind="tool_call_end",
                    channel="tool",
                    tool_call_id=accumulator.tool_call_id,
                    tool_name=reverse.get(accumulator.tool_name, accumulator.tool_name),
                    tool_call_ordinal=index,
                    parsed_arguments=json_wire_object(parsed),
                )
            if thinking_blocks:
                for index, fragments in thinking_fragments.items():
                    thinking_blocks[index]["thinking"] = "".join(fragments)
                metadata["thinking_blocks"] = [block for _, block in sorted(thinking_blocks.items())]
            done_reason = _done_reason(stop_reason)
            if done_reason == "unknown" and isinstance(stop_reason, str) and stop_reason.strip():
                # Preserve reported but unmapped values for core diagnostics.
                metadata["finish_reason_raw"] = stop_reason
            yield ProviderStreamEvent(kind="done", done_reason=done_reason, metadata=metadata or None, usage=latest_usage)
            write_provider_trace(
                request=payload,
                response={"native_stream": True},
                metadata={
                    "session_id": self._session_id(request),
                    "provider": provider_name,
                    "model": request.model_name,
                    "attempt": request.attempt,
                    "stream": True,
                    "transport": "anthropic_messages",
                },
            )
        except Exception as exc:
            mapped = self._map_exception(exc, provider_name=provider_name, model_name=model_name, source="stream")
            # Errors are raised to preserve runtime retry/fallback ownership;
            # cancellation is the sole stream-local terminal event path.
            raise mapped from exc


__all__ = ["AnthropicMessagesProvider", "AnthropicMessagesTransport", "AnthropicTransport", "AnthropicTransportError"]


def _thinking_payload(payload: dict[str, object], *, request: ProviderTurnRequest, rule: ThinkingRule) -> None:
    """Set supported budget/adaptive thinking controls, including explicit disable."""
    if request.reasoning_effort is None:
        return
    metadata = static_catalog_metadata(request.provider_name or rule.provider, request.model_name or "")
    if metadata is not None and metadata.supports_reasoning is False:
        return
    effort = normalize_reasoning_effort(request.reasoning_effort)
    if effort is None:
        return
    if effort in (0, "off"):
        payload["thinking"] = {"type": "disabled"}
        return
    if isinstance(effort, int):
        if effort < 1024:
            raise ProviderExecutionError(
                kind="unsupported_feature",
                provider_name=request.provider_name or rule.provider,
                model_name=request.model_name or "unknown",
                message="Anthropic thinking budget must be zero (disabled) or at least 1024 tokens",
                retryable=False,
                fallback_allowed=True,
            )
        payload["thinking"] = {"type": "enabled", "budget_tokens": effort}
        return
    supported = metadata.supported_effort_levels if metadata is not None else None
    clamped = clamp_effort_to_supported(effort, supported)
    if metadata is not None and metadata.supports_reasoning_effort:
        payload["thinking"] = {"type": "adaptive"}
        payload["output_config"] = {"effort": clamped}
        return
    if rule.mode != "budget":
        return
    budget = rule.budget_for(clamped)
    if budget is None:
        return
    payload["thinking"] = {"type": "enabled", "budget_tokens": budget}
