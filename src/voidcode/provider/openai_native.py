from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from queue import Empty, Full, Queue
from threading import Event, Thread
from typing import Any, Protocol, cast

import httpx
from openai import APIError as OpenAIAPIError
from openai import OpenAI, omit

from ..tools.contracts import ToolCall
from ..tools.output import redacted_argument_keys_for_tool, sanitize_tool_arguments, sanitize_tool_result_data, strip_redaction_sentinels
from .config import OpenAIProviderConfig, ProviderEndpointConfig
from .errors import (
    provider_execution_error_from_api_payload,
    provider_execution_error_from_stream_payload,
    redact_provider_error_details,
    redact_provider_error_message,
)
from .model_catalog import ToolFeedbackMode
from .protocol import (
    ProviderExecutionError,
    ProviderStreamEvent,
    ProviderTokenUsage,
    ProviderTurnRequest,
    ProviderTurnResult,
    ProviderWireMaterialization,
    WirePrefixDescriptor,
)
from .provider_config import openai_wire_default_base_url
from .reasoning_effort import clamp_effort_to_supported, map_effort_for_provider, normalize_reasoning_effort
from .trace import write_provider_trace

_STREAM_TIMEOUT_SENTINEL = object()
_PROVIDERS_REQUIRING_REASONING_CONTENT_WITH_TOOL_CALLS = frozenset({"deepseek"})


def _reasoning_content_from_tool_data(segment: object) -> str | None:
    metadata = getattr(segment, "metadata", None)
    if not isinstance(metadata, Mapping):
        return None
    data = metadata.get("data")
    if not isinstance(data, Mapping):
        return None
    reasoning_content = data.get("reasoning_content")
    return reasoning_content if isinstance(reasoning_content, str) and reasoning_content else None


def _requires_reasoning_content_with_tool_calls(*, provider_name: str | None, model_name: str, raw_model: str | None) -> bool:
    if (provider_name or "").strip().lower() in _PROVIDERS_REQUIRING_REASONING_CONTENT_WITH_TOOL_CALLS:
        return True
    candidates = [model_name]
    if raw_model is not None:
        candidates.append(raw_model)
    return any(candidate.strip().lower().startswith("deepseek-") for candidate in candidates)


def _iter_stream_with_timeout(
    stream: Iterator[object],
    *,
    timeout_seconds: float,
    provider_name: str,
    model_name: str,
) -> Iterator[object]:
    if timeout_seconds <= 0:
        raise ProviderExecutionError(
            kind="transient_failure",
            provider_name=provider_name,
            model_name=model_name,
            message="provider stream timeout must be greater than zero",
            retryable=False,
            fallback_allowed=True,
        )
    queue: Queue[tuple[str, object]] = Queue(maxsize=1)
    stop_event = Event()

    def enqueue(kind: str, value: object) -> None:
        while not stop_event.is_set():
            try:
                queue.put((kind, value), timeout=0.01)
                return
            except Full:
                continue

    def pull() -> None:
        try:
            for item in stream:
                if stop_event.is_set():
                    return
                enqueue("item", item)
            enqueue("done", _STREAM_TIMEOUT_SENTINEL)
        except BaseException as exc:
            enqueue("error", exc)

    def close_stream() -> None:
        closer = getattr(stream, "close", None)
        if not callable(closer):
            return
        try:
            closer()
        except Exception:
            return

    thread = Thread(target=pull, name="voidcode-openai-stream", daemon=True)
    thread.start()
    try:
        while True:
            try:
                kind, value = queue.get(timeout=timeout_seconds)
            except Empty as exc:
                stop_event.set()
                close_stream()
                thread.join(timeout=min(timeout_seconds, 0.1))
                raise ProviderExecutionError(
                    kind="transient_failure",
                    provider_name=provider_name,
                    model_name=model_name,
                    message="provider stream chunk timeout exceeded",
                    retryable=True,
                    fallback_allowed=True,
                ) from exc
            if kind == "done":
                return
            if kind == "error":
                raise cast(BaseException, value)
            yield value
    finally:
        stop_event.set()
        close_stream()
        if thread.is_alive():
            thread.join(timeout=min(timeout_seconds, 0.1))


# Construction default of ``OpenAIChatCompletionsTransport`` for direct use only.
# It is never a provider-level fallback: a provider resolves its own vendor
# default (``provider_config.openai_wire_default_base_url``) or fails with
# ``not_configured``.
_DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
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
    the deleted SSE parser used: ``provider_execution_error_from_stream_payload``
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
    terminating blank line is discarded. A stream that ends without a
    recognized finish reason is still a terminal response: the turn resolves to
    the canonical ``unknown`` reason and the graph treats it as a completed,
    stop-equivalent state (see ``_done_reason``). Events the decoder does emit
    but cannot decode as a JSON object are reported as a bounded, non-leaking
    stream error payload.

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
        http_client: httpx.Client | None = None,
    ) -> None:
        self.base_url = normalize_openai_base_url(base_url)
        self.api_key = api_key
        self.organization = organization
        self.project = project
        self.auth_header = auth_header
        self.auth_scheme = auth_scheme
        self.ssl_verify = ssl_verify
        self.http_client = http_client
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
        merged: dict[str, object] = dict(cast(Mapping[str, object], extra)) if isinstance(extra, Mapping) else {}
        if not any(str(name).lower() == "authorization" for name in merged):
            merged["Authorization"] = omit
            kwargs["extra_headers"] = merged
        return kwargs

    def _sdk(self) -> OpenAI:
        if self._sdk_client is None:
            http_client = self.http_client
            if http_client is None and self.ssl_verify is not None:
                http_client = httpx.Client(verify=self.ssl_verify)
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
            payload.update(cast(Mapping[str, object], body))
        elif isinstance(body, str) and body:
            payload["message"] = body
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
        if isinstance(status_code, int):
            payload["status_code"] = status_code
        headers = getattr(response, "headers", None)
        if isinstance(headers, Mapping):
            payload.setdefault("headers", dict(cast(Mapping[str, object], headers)))
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


def _normalize_tool_call_id(value: str | None, *, fallback: str) -> str:
    raw = value if isinstance(value, str) and value.strip() else fallback
    return re.sub(r"[^a-zA-Z0-9_-]", "_", raw.strip()) or fallback


def _usage_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    result = int(value)
    return result if result >= 0 else None


def _usage(payload: Mapping[str, object]) -> ProviderTokenUsage | None:
    raw = payload.get("usage")
    if not isinstance(raw, Mapping):
        return None
    input_tokens = _usage_int(raw.get("prompt_tokens"))
    output_tokens = _usage_int(raw.get("completion_tokens"))
    details = raw.get("prompt_tokens_details")
    cache_read = _usage_int(details.get("cached_tokens")) if isinstance(details, Mapping) else None
    if input_tokens is None and output_tokens is None:
        return None
    uncached = max(0, input_tokens - cache_read) if input_tokens is not None and cache_read is not None else None
    return ProviderTokenUsage(input_tokens=input_tokens, output_tokens=output_tokens, cache_read_tokens=cache_read, uncached_input_tokens=uncached)


def _done_reason(value: object) -> str:
    if not isinstance(value, str):
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
    """Return the provider's finish-reason token verbatim, for failure diagnostics.

    ``_done_reason`` collapses anything it does not recognize to ``"unknown"``, which
    hides the provider's actual token in the failure a caller sees. The raw value is
    carried as ``finish_reason_raw`` on turn-result/stream-event metadata so the
    unsupported-finish-reason failure can name the token instead of only ``"unknown"``.
    """
    if isinstance(value, str):
        return value or None
    if value is None:
        return None
    try:
        return json.dumps(value, ensure_ascii=True, sort_keys=True)
    except TypeError, ValueError:
        return None


def _reasoning(message: Mapping[str, object]) -> str | None:
    for key in ("reasoning_content", "reasoning"):
        value = message.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _parse_arguments(value: object) -> dict[str, object]:
    if isinstance(value, Mapping):
        return dict(cast(Mapping[str, object], value))
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return dict(cast(dict[str, object], decoded)) if isinstance(decoded, dict) else {}


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


def _empty_tool_feedback_overrides() -> dict[str, ToolFeedbackMode]:
    return {}


def _empty_extra_request_headers() -> dict[str, str]:
    return {}


# A declared request header may name the conversation with ``{session_id}``. The
# transport -- and therefore its SDK client -- is cached across turns, so a value
# resolved at construction time would freeze the first conversation's id; it is
# resolved per request instead.
_SESSION_ID_PLACEHOLDER = "{session_id}"


def _resolve_extra_request_headers(declared: Mapping[str, str], session_id: str | None) -> dict[str, str]:
    """Resolve declared request headers for one turn, dropping ones with no value.

    A declaration whose value names ``{session_id}`` is omitted when the request
    carries no session id: an empty header is not a routable conversation.
    """
    resolved: dict[str, str] = {}
    for name, value in declared.items():
        if _SESSION_ID_PLACEHOLDER in value:
            if not session_id:
                continue
            value = value.replace(_SESSION_ID_PLACEHOLDER, session_id)
        resolved[name] = value
    return resolved


@dataclass(slots=True)
class _OwnedTransport:
    """One-slot holder letting a frozen provider own a single transport."""

    value: OpenAITransport | None = None


@dataclass(frozen=True, slots=True)
class OpenAIChatCompletionsProvider:
    name: str = "openai"
    config: OpenAIProviderConfig | ProviderEndpointConfig | None = None
    transport: OpenAITransport | None = None
    tool_feedback_model_overrides: Mapping[str, ToolFeedbackMode] = field(default_factory=_empty_tool_feedback_overrides)
    # Headers this gateway requires on every request it serves. They reach the wire
    # through the SDK's ``extra_headers`` argument, which the transport passes to
    # ``create`` from the payload, so the JSON body never carries them.
    extra_request_headers: Mapping[str, str] = field(default_factory=_empty_extra_request_headers)
    # One transport -- and therefore one SDK client and HTTP connection pool --
    # per provider, reused across every turn. Building it per request leaked a
    # pool per turn. Only the first-use race can drop one losing transport.
    _owned_transport: _OwnedTransport = field(default_factory=_OwnedTransport, compare=False, repr=False)

    def provider_config(self) -> OpenAIProviderConfig | ProviderEndpointConfig | None:
        return self.config

    def _config_value(self, name: str, default: object = None) -> object:
        return default if self.config is None else getattr(self.config, name, default)

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
        model_map = self._config_value("model_map", {})
        mapped = model_map.get(model_name, model_name) if isinstance(model_map, Mapping) else model_name
        return mapped if isinstance(mapped, str) and mapped else model_name

    def _transport(self) -> OpenAITransport:
        if self.transport is not None:
            return self.transport
        owned = self._owned_transport
        if owned.value is None:
            # Credentials come only from resolved provider config: an ambient
            # ``OPENAI_API_KEY`` must never be attached to another vendor's endpoint
            # (or to the default base URL when the provider block is absent).
            # ``provider_configs_from_env`` already resolves that variable into
            # ``providers.openai``. A missing key means "send no credential" -- the
            # transport still hands the SDK a placeholder so it never sends an empty one.
            base_url = cast(str | None, self._config_value("base_url"))
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
            owned.value = OpenAIChatCompletionsTransport(
                base_url=base_url,
                api_key=cast(str | None, self._config_value("api_key")),
                organization=cast(str | None, self._config_value("organization", self._config_value("openai_organization"))),
                project=cast(str | None, self._config_value("project", self._config_value("openai_project"))),
                auth_header=cast(str | None, self._config_value("auth_header")),
                auth_scheme=cast(str, self._config_value("auth_scheme", "bearer")),
                ssl_verify=cast(bool | None, self._config_value("ssl_verify")),
            )
        return owned.value

    @staticmethod
    def _tool_maps(request: ProviderTurnRequest) -> tuple[dict[str, str], dict[str, str]]:
        original: dict[str, str] = {}
        reverse: dict[str, str] = {}
        names = [tool.name for tool in request.available_tools if tool.name]
        names.extend(segment.tool_name for segment in request.assembled_context.segments if segment.tool_name)
        for name in dict.fromkeys(names):
            candidate = _safe_tool_name(name)
            if candidate in reverse and reverse[candidate] != name:
                suffix = "_" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:_TOOL_NAME_HASH_LENGTH]
                candidate = f"{candidate[: _MAX_TOOL_NAME_LENGTH - len(suffix)]}{suffix}"
            original[name] = candidate
            reverse[candidate] = name
        return original, reverse

    @staticmethod
    def _visible_arguments(tool_name: str | None, arguments: dict[str, object]) -> dict[str, object]:
        sanitized = sanitize_tool_arguments(arguments)
        stripped = strip_redaction_sentinels(sanitized, redacted_keys=redacted_argument_keys_for_tool(tool_name))
        return cast(dict[str, object], stripped) if isinstance(stripped, dict) else {}

    def _messages(self, request: ProviderTurnRequest) -> list[dict[str, object]]:
        original_to_provider, _ = self._tool_maps(request)
        if self._tool_feedback_mode_for_request(request) == "synthetic_user_message":
            return self._synthetic_feedback_messages(request, original_to_provider)
        requires_reasoning_content = _requires_reasoning_content_with_tool_calls(
            provider_name=request.provider_name or self.name,
            # The mapped name is what actually reaches the provider, so a
            # ``model_map`` alias onto a deepseek model must still replay.
            model_name=self._model_name(request),
            raw_model=request.raw_model,
        )
        reasoning_content_by_tool_call_id: dict[str, str] = {}
        if requires_reasoning_content:
            for segment in request.assembled_context.segments:
                if segment.role != "tool" or not segment.tool_call_id:
                    continue
                reasoning_content = _reasoning_content_from_tool_data(segment)
                if reasoning_content:
                    reasoning_content_by_tool_call_id[segment.tool_call_id] = reasoning_content
        messages: list[dict[str, object]] = []
        for segment in request.assembled_context.segments:
            if segment.role == "assistant" and segment.tool_name is not None:
                arguments = json.dumps(self._visible_arguments(segment.tool_name, segment.tool_arguments or {}), ensure_ascii=False, sort_keys=True)
                messages.append(
                    {
                        "role": "assistant",
                        "content": segment.content,
                        # DeepSeek requires the prior reasoning_content on the
                        # assistant tool-call message of a replayed turn.
                        **(
                            {"reasoning_content": reasoning_content_by_tool_call_id.get(segment.tool_call_id or "") or " "}
                            if requires_reasoning_content
                            else {}
                        ),
                        "tool_calls": [
                            {
                                "id": _normalize_tool_call_id(segment.tool_call_id, fallback=segment.tool_name),
                                "type": "function",
                                "function": {"name": original_to_provider.get(segment.tool_name, segment.tool_name), "arguments": arguments},
                            }
                        ],
                    }
                )
            elif segment.role == "tool":
                metadata = segment.metadata or {}
                raw_data = metadata.get("data")
                data = sanitize_tool_result_data(cast(dict[str, object], raw_data)) if isinstance(raw_data, dict) else {}
                result = {
                    "tool_name": original_to_provider.get(segment.tool_name or "", segment.tool_name),
                    "content": segment.content or "",
                    "status": metadata.get("status"),
                    "error": metadata.get("error"),
                    "data": {key: value for key, value in data.items() if key not in {"tool_call_id", "arguments"}},
                    "truncated": metadata.get("truncated"),
                    "partial": metadata.get("partial"),
                    "reference": metadata.get("reference"),
                }
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": _normalize_tool_call_id(segment.tool_call_id, fallback=segment.tool_name or "voidcode_tool"),
                        "content": json.dumps(result, ensure_ascii=False, sort_keys=True),
                    }
                )
            else:
                messages.append({"role": segment.role, "content": segment.content})
        return messages

    def _tool_feedback_mode_for_request(self, request: ProviderTurnRequest) -> ToolFeedbackMode:
        mapped_model = self._model_name(request)
        mode = self.tool_feedback_model_overrides.get(mapped_model)
        if mode is None and request.model_name is not None:
            mode = self.tool_feedback_model_overrides.get(request.model_name)
        if mode is not None:
            return mode
        metadata = request.model_metadata.tool_feedback_mode if request.model_metadata is not None else None
        return metadata if metadata is not None else "standard"

    def _synthetic_feedback_messages(self, request: ProviderTurnRequest, original_to_provider: Mapping[str, str]) -> list[dict[str, object]]:
        """Replay tool results as a synthetic user turn.

        Some gateways reject the OpenAI ``tool`` role. Those models receive the
        completed tool results as a single user message instead, with prior-run
        results kept inside the replayed history.
        """
        tool_feedback_lines: list[str] = []
        for result in request.assembled_context.tool_results:
            if getattr(result, "source", None) == "replayed_conversation":
                continue
            raw_data = result.data
            sanitized_data = sanitize_tool_result_data(raw_data) if isinstance(raw_data, dict) else {}
            raw_arguments = sanitized_data.get("arguments")
            sanitized_arguments = (
                self._visible_arguments(result.tool_name, cast(dict[str, object], raw_arguments)) if isinstance(raw_arguments, dict) else {}
            )
            payload = {
                "tool_name": original_to_provider.get(result.tool_name, result.tool_name),
                "arguments": sanitized_arguments,
                "status": result.status,
                "content": result.content or "",
                "error": result.error,
                "data": {key: value for key, value in sanitized_data.items() if key not in {"tool_call_id", "arguments"}},
                "truncated": result.truncated,
                "partial": result.partial,
                "reference": result.reference,
            }
            tool_feedback_lines.append(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        messages: list[dict[str, object]] = []
        for segment in request.assembled_context.segments:
            is_replayed = (segment.metadata or {}).get("source") == "replayed_conversation"
            if segment.role == "assistant" and segment.tool_name is not None:
                continue
            if segment.role == "tool":
                if is_replayed:
                    messages.append(
                        {
                            "role": "user",
                            "content": f"[Previous run tool result for {segment.tool_name}]\n{segment.content or ''}",
                        }
                    )
                continue
            messages.append({"role": segment.role, "content": segment.content})
        if tool_feedback_lines:
            messages.append(
                {
                    "role": "user",
                    "content": "\n".join(
                        (
                            "Completed tool calls for current request:",
                            "Use these results as latest state. Do not repeat completed calls unless retry is required.",
                            *tool_feedback_lines,
                        )
                    ),
                }
            )
        return messages

    def _wire(self, request: ProviderTurnRequest) -> ProviderWireMaterialization:
        messages = self._messages(request)
        original_to_provider, _ = self._tool_maps(request)
        tools: list[dict[str, object]] = []
        for tool in request.available_tools:
            schema = tool.input_schema or {}
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
        retention = request.cache_retention if request.cache_retention is not None else self._config_value("cache_retention", "none")
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
        payload: dict[str, object] = {"model": model_name, "messages": wire.messages, "stream": stream}
        if wire.tools:
            payload["tools"] = wire.tools
            payload["tool_choice"] = "auto"
        if request.reasoning_effort:
            effort = normalize_reasoning_effort(request.reasoning_effort)
            supported = request.model_metadata.supported_effort_levels if request.model_metadata is not None else None
            mapped = map_effort_for_provider(
                provider_name=request.provider_name or self.name,
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
                    merged.update(cast(dict[str, object], existing))
                merged.update(cast(dict[str, object], extra_body))
                payload["extra_body"] = merged
            else:
                payload.update(mapped)
        extra_headers = _resolve_extra_request_headers(self.extra_request_headers, request.session_id)
        if extra_headers:
            # A request option, not a body field: the SDK merges it into the HTTP
            # request and never serializes it into the JSON payload.
            payload["extra_headers"] = extra_headers
        if stream:
            payload["stream_options"] = {"include_usage": True}
        return payload

    def _timeout(self) -> float:
        configured = self._config_value("timeout_seconds")
        return _DEFAULT_TIMEOUT_SECONDS if not isinstance(configured, int | float) else float(configured)

    @staticmethod
    def _response_payload(value: object) -> dict[str, object]:
        if isinstance(value, Mapping):
            return dict(cast(Mapping[str, object], value))
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            dumped = model_dump()
            if isinstance(dumped, Mapping):
                return dict(cast(Mapping[str, object], dumped))
        raise ValueError("provider response must be a mapping")

    @staticmethod
    def _stream_chunk_payload(value: object) -> dict[str, object]:
        """Decode one stream event, reporting undecodable events as stream errors.

        The SDK tolerates event shapes it cannot model, so a non-object event (e.g. a
        JSON array) reaches the transport as a non-mapping and is re-typed here rather
        than reported as an unclassified transient failure.
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
            factory = provider_execution_error_from_stream_payload if source == "stream" else provider_execution_error_from_api_payload
            return factory(provider_name=provider_name, model_name=model_name, payload=exc.payload)
        message = redact_provider_error_message(str(exc)) or "provider request failed"
        details = redact_provider_error_details({"exception_type": type(exc).__name__, "exception_message": message})
        return ProviderExecutionError(
            kind="transient_failure",
            provider_name=provider_name,
            model_name=model_name,
            message=message,
            retryable=True,
            fallback_allowed=True,
            details=cast(dict[str, object], details),
        )

    @staticmethod
    def _tool_calls(message: Mapping[str, object], reverse: Mapping[str, str]) -> tuple[ToolCall, ...]:
        raw_calls = message.get("tool_calls")
        if not isinstance(raw_calls, list):
            return ()
        parsed: list[ToolCall] = []
        for index, item in enumerate(raw_calls):
            if not isinstance(item, Mapping) or not isinstance(item.get("function"), Mapping):
                continue
            function = cast(Mapping[str, object], item["function"])
            if not isinstance(function.get("name"), str):
                continue
            provider_name = cast(str, function["name"])
            runtime_name = reverse.get(provider_name, provider_name)
            explicit_id = item.get("id") if isinstance(item.get("id"), str) else None
            fallback = f"{runtime_name}_{index + 1}" if len(raw_calls) > 1 else runtime_name
            parsed.append(
                ToolCall(
                    tool_name=runtime_name,
                    arguments=_parse_arguments(function.get("arguments")),
                    tool_call_id=_normalize_tool_call_id(explicit_id, fallback=fallback),
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
                return ProviderTurnResult(output="", usage=usage, metadata=metadata)
            choice = cast(Mapping[str, object], choices[0])
            message = choice.get("message")
            message_mapping = cast(Mapping[str, object], message) if isinstance(message, Mapping) else {}
            content = message_mapping.get("content")
            raw_finish_reason = choice.get("finish_reason")
            done_reason = _done_reason(raw_finish_reason)
            raw_token = _raw_finish_reason(raw_finish_reason)
            if done_reason == "unknown" and raw_token is not None:
                # Keep the provider's own token so an unrecognized reason stays
                # diagnosable in the graph's debug resolution.
                metadata["finish_reason_raw"] = raw_token
            return ProviderTurnResult(
                tool_calls=self._tool_calls(message_mapping, self._tool_maps(request)[1]),
                output=content if isinstance(content, str) else "",
                reasoning=_reasoning(message_mapping),
                usage=usage,
                done_reason=cast(Any, done_reason),
                # Reported only when the provider carried a usable value: an absent
                # key or an explicit null is the silent-truncation case.
                finish_reason_reported=raw_finish_reason is not None and raw_finish_reason != "",
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
            raw_finish_reason_token: str | None = None
            accumulators: dict[int, _ToolAccumulator] = {}
            for raw_chunk in _iter_stream_with_timeout(
                cast(Iterator[object], stream),
                timeout_seconds=self._timeout(),
                provider_name=provider_name,
                model_name=model_name,
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
                choice = cast(Mapping[str, object], choices[0])
                raw_finish_reason = choice.get("finish_reason")
                if isinstance(raw_finish_reason, str) and raw_finish_reason:
                    done_reason = _done_reason(raw_finish_reason)
                    # Latched with the reason (a trailing usage-only chunk must not clear
                    # either), and cleared when the latched reason is recognized.
                    raw_token = _raw_finish_reason(raw_finish_reason)
                    raw_finish_reason_token = raw_token if done_reason == "unknown" else None
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
                if not isinstance(raw_tool_calls, list):
                    continue
                for raw_tool in raw_tool_calls:
                    if not isinstance(raw_tool, Mapping):
                        continue
                    index = raw_tool.get("index") if isinstance(raw_tool.get("index"), int) else 0
                    previous = accumulators.get(index, _ToolAccumulator())
                    raw_id = raw_tool.get("id")
                    tool_id = _normalize_tool_call_id(raw_id if isinstance(raw_id, str) else previous.tool_call_id, fallback=f"tool_call_{index + 1}")
                    function = raw_tool.get("function")
                    name = previous.tool_name
                    fragment: str | None = None
                    if isinstance(function, Mapping):
                        if isinstance(function.get("name"), str):
                            name = cast(str, function["name"])
                        if isinstance(function.get("arguments"), str):
                            fragment = cast(str, function["arguments"])
                    complete_first = False
                    if fragment and not previous.fragments:
                        try:
                            complete_first = isinstance(json.loads(fragment), dict)
                        except json.JSONDecodeError:
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
        reverse = self._tool_maps(request)[1]
        completed: list[tuple[int, _ToolAccumulator, dict[str, object], str]] = []
        for index, accumulator in sorted(accumulators.items()):
            if accumulator.tool_name is None:
                continue
            try:
                parsed = json.loads(accumulator.arguments)
            except json.JSONDecodeError as exc:
                raise ProviderExecutionError(
                    kind="stream_tool_feedback_shape",
                    provider_name=provider_name,
                    model_name=model_name,
                    message="provider stream ended with incomplete tool-call arguments",
                    retryable=False,
                    fallback_allowed=True,
                    details={"tool_name": accumulator.tool_name, "tool_call_id": accumulator.tool_call_id},
                ) from exc
            if not isinstance(parsed, dict):
                raise ProviderExecutionError(
                    kind="stream_tool_feedback_shape",
                    provider_name=provider_name,
                    model_name=model_name,
                    message="provider stream tool-call arguments were not an object",
                    retryable=False,
                    fallback_allowed=True,
                    details={"tool_name": accumulator.tool_name, "tool_call_id": accumulator.tool_call_id},
                )
            runtime_name = reverse.get(accumulator.tool_name, accumulator.tool_name)
            completed.append((index, accumulator, cast(dict[str, object], parsed), runtime_name))
        if any(accumulator.explicit_streaming for _index, accumulator, _parsed, _name in completed):
            for index, accumulator, parsed, runtime_name in completed:
                if accumulator.explicit_streaming:
                    tool_id = _normalize_tool_call_id(
                        accumulator.tool_call_id,
                        fallback=runtime_name if len(completed) == 1 else f"{runtime_name}_{index + 1}",
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
                        "tool_call_id": _normalize_tool_call_id(
                            accumulator.tool_call_id,
                            fallback=runtime_name if len(completed) == 1 else f"{runtime_name}_{index + 1}",
                        ),
                    }
                )
            event_payload: dict[str, object] = event_calls[0] if len(event_calls) == 1 else {"tool_calls": event_calls}
            yield ProviderStreamEvent(kind="content", channel="tool", text=json.dumps(event_payload))
        if raw_finish_reason_token is not None:
            # Surfaced on the done event so an unrecognized finish reason is diagnosable
            # from the provider's own token rather than the collapsed "unknown".
            metadata["finish_reason_raw"] = raw_finish_reason_token
        yield ProviderStreamEvent(kind="done", done_reason=cast(Any, done_reason), metadata=metadata or None, usage=latest_usage)
        write_provider_trace(
            request=payload,
            response={"native_stream": True},
            metadata={
                "session_id": request.session_id,
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
