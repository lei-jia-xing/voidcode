from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from queue import Empty, Full, Queue
from threading import Event, Thread
from typing import Any, Protocol, cast

import httpx

from ..tools.contracts import ToolCall
from ..tools.output import redacted_argument_keys_for_tool, sanitize_tool_arguments, sanitize_tool_result_data, strip_redaction_sentinels
from .config import OpenAIProviderConfig
from .errors import (
    provider_execution_error_from_api_payload,
    provider_execution_error_from_stream_payload,
    redact_provider_error_details,
    redact_provider_error_message,
)
from .protocol import (
    ProviderExecutionError,
    ProviderStreamEvent,
    ProviderTokenUsage,
    ProviderTurnRequest,
    ProviderTurnResult,
    ProviderWireMaterialization,
    WirePrefixDescriptor,
)
from .reasoning_effort import clamp_effort_to_supported, map_effort_for_provider, normalize_reasoning_effort
from .trace import write_provider_trace

_STREAM_TIMEOUT_SENTINEL = object()


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


_DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
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


class OpenAIChatCompletionsTransport:
    """HTTP transport for the OpenAI Chat Completions wire protocol."""

    def __init__(
        self,
        *,
        base_url: str = _DEFAULT_OPENAI_BASE_URL,
        api_key: str | None = None,
        organization: str | None = None,
        project: str | None = None,
        http_transport: httpx.BaseTransport | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = _normalize_base_url(base_url)
        self.api_key = api_key
        self.organization = organization
        self.project = project
        self.http_transport = http_transport
        self.client = client

    def request(self, payload: dict[str, object], *, timeout_seconds: float) -> object:
        if bool(payload.get("stream")):
            return self._iter_stream(payload, timeout_seconds=timeout_seconds)
        return _response_json_or_error(self._post(payload, timeout_seconds=timeout_seconds))

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if self.organization:
            headers["OpenAI-Organization"] = self.organization
        if self.project:
            headers["OpenAI-Project"] = self.project
        return headers

    def _post(self, payload: dict[str, object], *, timeout_seconds: float) -> httpx.Response:
        if self.client is not None:
            return self.client.post(f"{self.base_url}/chat/completions", headers=self._headers(), json=payload, timeout=timeout_seconds)
        with httpx.Client(transport=self.http_transport, timeout=timeout_seconds) as client:
            return client.post(f"{self.base_url}/chat/completions", headers=self._headers(), json=payload)

    def _iter_stream(self, payload: dict[str, object], *, timeout_seconds: float) -> Iterator[dict[str, object]]:
        if self.client is not None:
            with self.client.stream(
                "POST", f"{self.base_url}/chat/completions", headers=self._headers(), json=payload, timeout=timeout_seconds
            ) as response:
                yield from _iter_sse_response(response)
            return
        with httpx.Client(transport=self.http_transport, timeout=timeout_seconds) as client:
            with client.stream("POST", f"{self.base_url}/chat/completions", headers=self._headers(), json=payload) as response:
                yield from _iter_sse_response(response)


def _normalize_base_url(base_url: str | None) -> str:
    value = (base_url or _DEFAULT_OPENAI_BASE_URL).strip().rstrip("/") or _DEFAULT_OPENAI_BASE_URL
    return value if re.search(r"/v[0-9]+(?:beta|alpha)?$", value, flags=re.IGNORECASE) else f"{value}/v1"


def _response_json_or_error(response: httpx.Response) -> dict[str, object]:
    try:
        payload = response.json()
    except ValueError:
        payload = {"message": response.text}
    result = dict(cast(dict[str, object], payload)) if isinstance(payload, dict) else {"message": "provider response was not a JSON object"}
    if response.status_code >= 400:
        result.setdefault("status_code", response.status_code)
        result.setdefault("headers", dict(response.headers))
        raise OpenAITransportError(result)
    return result


def _response_error_payload(response: httpx.Response) -> dict[str, object]:
    try:
        payload = response.json()
    except ValueError:
        payload = {"message": response.text}
    result = dict(cast(dict[str, object], payload)) if isinstance(payload, dict) else {"message": str(payload)}
    result.setdefault("status_code", response.status_code)
    result.setdefault("headers", dict(response.headers))
    return result


def _iter_sse_response(response: httpx.Response) -> Iterator[dict[str, object]]:
    if response.status_code >= 400:
        raise OpenAITransportError(_response_error_payload(response))
    for line in response.iter_lines():
        if isinstance(line, bytes):
            line = line.decode("utf-8", errors="replace")
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            payload = json.loads(data)
        except json.JSONDecodeError as exc:
            raise OpenAITransportError({"message": "provider stream returned invalid JSON", "details": {"line": data[:256]}}) from exc
        if not isinstance(payload, dict):
            raise OpenAITransportError({"message": "provider stream event was not a JSON object"})
        yield cast(dict[str, object], payload)


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


@dataclass(frozen=True, slots=True)
class OpenAIChatCompletionsProvider:
    name: str = "openai"
    config: OpenAIProviderConfig | None = None
    transport: OpenAITransport | None = None

    def provider_config(self) -> OpenAIProviderConfig | None:
        return self.config

    def _transport(self) -> OpenAITransport:
        if self.transport is not None:
            return self.transport
        config = self.config
        return OpenAIChatCompletionsTransport(
            base_url=config.base_url if config and config.base_url else _DEFAULT_OPENAI_BASE_URL,
            api_key=(config.api_key if config else None) or os.environ.get("OPENAI_API_KEY"),
            organization=config.organization if config else None,
            project=config.project if config else None,
        )

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
        messages: list[dict[str, object]] = []
        for segment in request.assembled_context.segments:
            if segment.role == "assistant" and segment.tool_name is not None:
                arguments = json.dumps(self._visible_arguments(segment.tool_name, segment.tool_arguments or {}), ensure_ascii=False, sort_keys=True)
                messages.append(
                    {
                        "role": "assistant",
                        "content": segment.content,
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
        if not request.model_name:
            raise ProviderExecutionError(
                kind="invalid_model",
                provider_name=request.provider_name or self.name,
                model_name="unknown",
                message="provider requires model name",
                retryable=False,
                fallback_allowed=True,
            )
        wire = self._wire(request)
        payload: dict[str, object] = {"model": request.model_name, "messages": wire.messages, "stream": stream}
        if wire.tools:
            payload["tools"] = wire.tools
            payload["tool_choice"] = "auto"
        if request.reasoning_effort:
            effort = normalize_reasoning_effort(request.reasoning_effort)
            supported = request.model_metadata.supported_effort_levels if request.model_metadata is not None else None
            mapped = map_effort_for_provider(
                provider_name="openai", model_name=request.model_name, effort=clamp_effort_to_supported(effort, supported)
            )
            payload.update(cast(dict[str, object], mapped.get("extra_body")) if isinstance(mapped.get("extra_body"), dict) else mapped)
        if stream:
            payload["stream_options"] = {"include_usage": True}
        return payload

    def _timeout(self) -> float:
        return _DEFAULT_TIMEOUT_SECONDS if self.config is None or self.config.timeout_seconds is None else self.config.timeout_seconds

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
            if "finish_reason" not in choice:
                raise ProviderExecutionError(
                    kind="transient_failure",
                    provider_name=provider_name,
                    model_name=model_name,
                    message="provider response omitted finish_reason",
                    retryable=False,
                    fallback_allowed=True,
                    details={"source": "response", "reason": "missing_finish_reason"},
                )
            message = choice.get("message")
            message_mapping = cast(Mapping[str, object], message) if isinstance(message, Mapping) else {}
            content = message_mapping.get("content")
            return ProviderTurnResult(
                tool_calls=self._tool_calls(message_mapping, self._tool_maps(request)[1]),
                output=content if isinstance(content, str) else "",
                reasoning=_reasoning(message_mapping),
                usage=usage,
                done_reason=cast(Any, _done_reason(choice.get("finish_reason"))),
                finish_reason_reported=True,
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
                chunk = self._response_payload(raw_chunk)
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
                    finish_reason_reported = True
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
            if not finish_reason_reported:
                raise ProviderExecutionError(
                    kind="transient_failure",
                    provider_name=provider_name,
                    model_name=model_name,
                    message="provider stream ended without finish_reason",
                    retryable=False,
                    fallback_allowed=True,
                    details={"source": "stream", "reason": "missing_finish_reason"},
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


__all__ = ["OpenAIChatCompletionsProvider", "OpenAIChatCompletionsTransport", "OpenAITransport", "OpenAITransportError"]
