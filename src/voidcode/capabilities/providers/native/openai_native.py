"""OpenAI Chat Completions provider facade.

Wire codec/response parsing lives in :mod:`openai_wire`, request serialization in
:mod:`openai_payload`, HTTP ownership in :mod:`openai_transport`, and the stream
loop in :mod:`openai_stream`. This module keeps the public provider/transport
surface and the existing SDK monkeypatch seams stable.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, cast

import httpx2
from openai import APIError as OpenAIAPIError
from openai import OpenAI

from voidcode.sdk.provider import (
    ModelContextLimits,
    ProviderExecutionError,
    ProviderStreamEvent,
    ProviderTurnRequest,
    ProviderTurnResult,
)

from ._wire_common import (
    DEFAULT_TIMEOUT_SECONDS,
    PLACEHOLDER_API_KEY,
    OwnedResource,
    Transport,
    TransportError,
    abort_signal_cancelled,
    cancelled_events,
    decode_response_payload,
)
from .config import OpenAIProviderConfig, ProviderEndpointConfig
from .model_catalog import catalog_model_limits
from .openai_payload import build_messages, build_payload
from .openai_stream import stream_turn_events
from .openai_transport import OpenAIHttpTransport
from .openai_wire import OpenAIWireCodec as OpenAIWireCodec
from .openai_wire import (
    api_error_payload,
    map_openai_exception,
    parse_chat_response,
)
from .provider_config import openai_wire_default_base_url


class OpenAIChatCompletionsTransport(OpenAIHttpTransport):
    """OpenAI SDK transport for the Chat Completions wire protocol."""

    def _sdk(self) -> OpenAI:
        with self._client_lock:
            if self._sdk_client is None:
                http_client = self.http_client
                if http_client is None and self.ssl_verify is not None:
                    http_client = httpx2.Client(verify=self.ssl_verify)
                # ``default_headers`` wins over the SDK's own auth header, so the
                # configured scheme stays authoritative. ``max_retries=0`` keeps
                # retry policy owned by the runtime instead of the SDK.
                self._sdk_client = OpenAI(
                    api_key=self.api_key or PLACEHOLDER_API_KEY,
                    base_url=self.base_url,
                    organization=self.organization,
                    project=self.project,
                    default_headers=self._auth_headers(),
                    http_client=http_client,
                    max_retries=0,
                )
            return cast(OpenAI, self._sdk_client)

    def request(self, payload: dict[str, object], *, timeout_seconds: float) -> object:
        try:
            result = self._sdk().chat.completions.create(**cast(Any, self._request_headers(payload)), timeout=timeout_seconds)
        except OpenAIAPIError as exc:
            raise TransportError(api_error_payload(exc)) from exc
        return result

    def list_models(self, *, extra_headers: Mapping[str, str], timeout_seconds: float) -> tuple[str, ...]:
        try:
            response = self._sdk().models.list(**cast(Any, self._request_headers({"extra_headers": dict(extra_headers)})), timeout=timeout_seconds)
        except OpenAIAPIError as exc:
            status = getattr(exc, "status_code", None)
            detail = f"HTTP {status}" if status is not None else type(exc).__name__
            raise TransportError({"message": f"Model listing failed ({detail})", "status_code": status}) from None
        data = getattr(response, "data", None)
        if not isinstance(data, list):
            raise TransportError({"message": "Model listing must contain a data array"})
        names: set[str] = set()
        for item in data:
            name = getattr(item, "id", None)
            if not isinstance(name, str) or not name.strip():
                raise TransportError({"message": "Model listing requires non-empty string model IDs"})
            names.add(name)
        return tuple(sorted(names))


@dataclass(frozen=True, slots=True)
class OpenAIChatCompletionsProvider:
    name: str = "openai"
    config: OpenAIProviderConfig | ProviderEndpointConfig | None = None
    transport: Transport | None = None
    # Headers this gateway requires on every request it serves. They reach the wire
    # through the SDK's ``extra_headers`` argument, which the transport passes to
    # ``create`` from the payload, so the JSON body never carries them.
    extra_request_headers: Mapping[str, str] = field(default_factory=dict)
    # One transport -- and therefore one SDK client and HTTP connection pool --
    # per provider, reused across every turn. Building it per request leaked a
    # pool per turn. First-use initialization is serialized.
    _owned_transport: OwnedResource[Transport] = field(default_factory=OwnedResource, compare=False, repr=False)

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
            )
        endpoint = self._endpoint_config()
        model_map = endpoint.model_map if endpoint is not None else {}
        return model_map.get(model_name) or model_name

    def model_limits(self, model_name: str) -> ModelContextLimits | None:
        """Advertised limits for one model, resolved through this endpoint's model map."""
        if not isinstance(model_name, str) or not model_name.strip():
            return None
        endpoint = self._endpoint_config()
        model_map = endpoint.model_map if endpoint is not None else {}
        return catalog_model_limits(self.name, model_map.get(model_name) or model_name)

    def close(self) -> None:
        self._owned_transport.close()

    def list_models(self) -> tuple[str, ...]:
        transport = self._transport()
        if not isinstance(transport, OpenAIChatCompletionsTransport):
            raise ProviderExecutionError(
                kind="unsupported_feature",
                provider_name=self.name,
                model_name="",
                message="This transport does not support model listing",
                retryable=False,
            )
        timeout = 10.0
        if self.config is not None and self.config.timeout_seconds is not None:
            timeout = min(timeout, self.config.timeout_seconds)
        try:
            return transport.list_models(extra_headers=self.extra_request_headers, timeout_seconds=timeout)
        except TransportError as exc:
            raise map_openai_exception(exc, provider_name=self.name, model_name="", source="model_listing") from None

    def _transport(self) -> Transport:
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

    def _messages(self, request: ProviderTurnRequest) -> list[dict[str, object]]:
        return build_messages(
            request,
            provider_name=request.provider_name or self.name,
            model_name=self._model_name(request),
        )

    def _payload(self, request: ProviderTurnRequest, *, stream: bool) -> dict[str, object]:
        if request.cache_retention not in (None, "none"):
            # This protocol does not implement the requested Anthropic cache control.
            raise ProviderExecutionError(
                kind="unsupported_feature",
                provider_name=request.provider_name or self.name,
                model_name=request.model_name or "unknown",
                message="cache_retention requires an Anthropic Messages-compatible provider",
                retryable=False,
                details={"source": "payload", "reason": "unsupported_cache_retention"},
            )
        return build_payload(
            request,
            provider_name=request.provider_name or self.name,
            model_name=self._model_name(request),
            extra_request_headers=self.extra_request_headers,
            stream=stream,
        )

    def _timeout(self) -> float:
        configured = None if self.config is None else self.config.timeout_seconds
        return DEFAULT_TIMEOUT_SECONDS if configured is None else configured

    def propose_turn(self, request: ProviderTurnRequest) -> ProviderTurnResult:
        provider_name = request.provider_name or self.name
        model_name = request.model_name or "unknown"
        try:
            payload = self._payload(request, stream=False)
            response_payload = decode_response_payload(self._transport().request(payload, timeout_seconds=self._timeout()))
            if response_payload.get("error") is not None or response_payload.get("type") == "error":
                raise TransportError(response_payload)
            return parse_chat_response(response_payload, request, provider_name=provider_name, model_name=model_name)
        except Exception as exc:
            raise map_openai_exception(exc, provider_name=provider_name, model_name=model_name, source="api") from exc

    def stream_turn(self, request: ProviderTurnRequest) -> Iterator[ProviderStreamEvent]:
        provider_name = request.provider_name or self.name
        model_name = request.model_name or "unknown"
        if abort_signal_cancelled(request):
            yield from cancelled_events()
            return
        try:
            payload = self._payload(request, stream=True)
            yield from stream_turn_events(
                request,
                default_provider_name=self.name,
                transport=self._transport(),
                payload=payload,
                timeout_seconds=self._timeout(),
            )
        except Exception as exc:
            raise map_openai_exception(exc, provider_name=provider_name, model_name=model_name, source="stream") from exc


__all__ = [
    "OpenAIChatCompletionsProvider",
    "OpenAIChatCompletionsTransport",
]
