"""OpenAI Chat Completions wire codec, response parsing, and error mapping.

This module owns only protocol knowledge: usage/done mapping, response and
tool-call parsing, stream accumulators, and the typed mapping from SDK errors.
It performs no HTTP work and holds no provider configuration.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import ClassVar

import httpx2
from openai import APIConnectionError as OpenAIAPIConnectionError
from openai import APIError as OpenAIAPIError
from openai import APITimeoutError as OpenAIAPITimeoutError
from pydantic import ValidationError

from voidcode.sdk.provider import (
    ProviderDoneReason,
    ProviderExecutionError,
    ProviderTokenUsage,
    ProviderTurnRequest,
    ProviderTurnResult,
)
from voidcode.sdk.tools import ToolCall

from ..registry import PROVIDER_TABLE_BY_ID
from ._wire_common import (
    OUTPUT_CAP_WHEN_UNKNOWN,
    ProviderErrorSource,
    TransportError,
    build_usage,
    decode_response_payload,
    fallback_execution_error,
    map_done_reason,
    normalize_tool_call_id,
    parse_tool_arguments,
    response_metadata,
    strip_base_url,
    tool_name_maps,
    usage_int,
)
from .errors import provider_execution_error_from_payload
from .model_catalog import ProviderModelMetadata
from .thinking_rules import thinking_rule_for

# Construction default of ``OpenAIChatCompletionsTransport`` for direct use only.
# It is never a provider-level fallback: a provider resolves its own vendor
# default (``provider_config.openai_wire_default_base_url``) or fails with
# ``not_configured``.
DEFAULT_OPENAI_BASE_URL = PROVIDER_TABLE_BY_ID["openai"].default_base_url


def output_cap(metadata: ProviderModelMetadata | None) -> int:
    """The cap the kimi family always gets: the model's own maximum, else 64000."""
    return metadata.max_output_tokens if metadata is not None and metadata.max_output_tokens else OUTPUT_CAP_WHEN_UNKNOWN


def requires_reasoning_content_with_tool_calls(*, provider_name: str | None, model_name: str) -> bool:
    """Whether this model's tool-call replay must carry its reasoning content.

    The answer is data (``thinking_rules.json``:
    ``requires_reasoning_content_for_tool_calls``), never a provider name or a
    model prefix.
    """
    rule = thinking_rule_for(provider_name or "", model_name)
    return rule.requires_reasoning_content_for_tool_calls


def stream_event_error_payload(exc: Exception) -> dict[str, object]:
    """Bounded, non-leaking payload for a stream event the transport cannot decode.

    The SDK's decoder raises parser/model errors directly (``JSONDecodeError``,
    pydantic errors), so they are reported through the same typed payload shape
    the deleted SSE parser used: ``provider_execution_error_from_payload``
    then classifies the malformed event with ``source``/guidance details.
    Raw parser text is deliberately not forwarded.
    """
    return {
        "message": "provider stream event was not a valid JSON object",
        "details": {"reason": "invalid_stream_event", "exception_type": type(exc).__name__},
    }


def api_error_payload(exc: OpenAIAPIError) -> dict[str, object]:
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


def map_openai_exception(exc: Exception, *, provider_name: str, model_name: str, source: ProviderErrorSource) -> ProviderExecutionError:
    if isinstance(exc, OpenAIAPIError):
        return provider_execution_error_from_payload(
            provider_name=provider_name, model_name=model_name, payload=api_error_payload(exc), source=source
        )
    if source == "stream" and isinstance(exc, (json.JSONDecodeError, ValidationError)):
        return provider_execution_error_from_payload(
            provider_name=provider_name, model_name=model_name, payload=stream_event_error_payload(exc), source=source
        )
    if isinstance(exc, TransportError):
        return provider_execution_error_from_payload(provider_name=provider_name, model_name=model_name, payload=exc.payload, source=source)
    return fallback_execution_error(
        exc,
        provider_name=provider_name,
        model_name=model_name,
        source=source,
        network_error=isinstance(exc, (httpx2.TransportError, OpenAIAPITimeoutError, OpenAIAPIConnectionError)),
    )


class OpenAIWireCodec:
    """OpenAI wire knowledge: usage fields, done tokens, base-URL rules."""

    DONE_REASONS: ClassVar[dict[str, ProviderDoneReason]] = {
        "stop": "stop",
        "end_turn": "stop",
        "tool_calls": "tool_calls",
        "tool_use": "tool_calls",
        "function_call": "function_call",
        "length": "length",
        "max_tokens": "length",
        "content_filter": "content_filter",
        "error": "error",
    }

    @staticmethod
    def normalize_base_url(base_url: str | None) -> str:
        """Normalize a chat-completions base URL exactly as the transport will use it."""
        value = strip_base_url(base_url, DEFAULT_OPENAI_BASE_URL)
        return value if re.search(r"/v[0-9]+(?:beta|alpha)?$", value, flags=re.IGNORECASE) else f"{value}/v1"

    @staticmethod
    def usage(payload: Mapping[str, object]) -> ProviderTokenUsage | None:
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
        uncached = input_tokens - cache_read if input_tokens is not None and cache_read is not None and cache_read <= input_tokens else None
        return build_usage(input_tokens=input_tokens, output_tokens=output_tokens, cache_read_tokens=cache_read, uncached_input_tokens=uncached)

    @staticmethod
    def done_reason(value: object) -> ProviderDoneReason:
        return map_done_reason(value, OpenAIWireCodec.DONE_REASONS)


def raw_finish_reason(value: object) -> str | None:
    """Return a reported finish-reason token verbatim for diagnostics."""
    if isinstance(value, str):
        return value.strip() or None
    if value is None:
        return None
    try:
        return json.dumps(value, ensure_ascii=True, sort_keys=True)
    except TypeError, ValueError:
        return None


def raise_finish_reason_error(raw_reason: str, *, provider_name: str, model_name: str) -> None:
    raise ProviderExecutionError(
        kind="provider_error",
        provider_name=provider_name,
        model_name=model_name,
        message=f"provider reported finish_reason={raw_reason}",
        retryable=False,
        details={"source": "finish_reason", "reason": "provider_error", "finish_reason_raw": raw_reason},
    )


def reasoning_from_message(message: Mapping[str, object]) -> str | None:
    for key in ("reasoning_content", "reasoning"):
        value = message.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def session_id(request: ProviderTurnRequest) -> str:
    """The session id this turn declares, empty when it declares none.

    Execution identity travels in request metadata, not as a request field. An
    empty id is what the legacy adapter passed when it had none, and it resolves
    a ``{session_id}`` header the same way: the declaration is dropped.
    """
    value = request.metadata.get("session_id")
    return value if isinstance(value, str) else ""


def stream_chunk_payload(value: object) -> dict[str, object]:
    """Decode one stream event, reporting undecodable events as stream errors.

    The SDK tolerates event shapes it cannot model, so a non-object event (e.g. a
    JSON array) reaches the transport as a non-mapping and is re-typed here rather
    than being reported as a retryable network failure; it is a non-retryable malformed provider stream.
    """
    try:
        return decode_response_payload(value)
    except ValueError as exc:
        raise TransportError(stream_event_error_payload(exc)) from exc


@dataclass(slots=True)
class ToolAccumulator:
    tool_call_id: str | None = None
    tool_name: str | None = None
    fragments: list[str] = field(default_factory=list)
    explicit_streaming: bool = False
    started: bool = False

    @property
    def arguments(self) -> str:
        return "".join(self.fragments)


def parse_response_tool_calls(
    message: Mapping[str, object],
    request: ProviderTurnRequest,
    *,
    provider_name: str,
    model_name: str,
) -> tuple[ToolCall, ...]:
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
            details={"source": "api", "reason": "malformed_tool_calls"},
        )
    reverse = tool_name_maps(request)[1]
    parsed: list[ToolCall] = []
    for index, item in enumerate(raw_calls):
        if not isinstance(item, Mapping) or not isinstance(item.get("function"), Mapping):
            raise ProviderExecutionError(
                kind="provider_error",
                provider_name=provider_name,
                model_name=model_name,
                message="provider returned a malformed tool call",
                retryable=False,
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
                details={"source": "api", "reason": "malformed_tool_call"},
            )
        provider_tool_name = function["name"]
        runtime_name = reverse.get(provider_tool_name, provider_tool_name)
        explicit_id = item.get("id")
        if explicit_id is not None and (not isinstance(explicit_id, str) or not explicit_id):
            raise ValueError("provider tool call id was malformed")
        parsed.append(
            ToolCall(
                tool_name=runtime_name,
                arguments=parse_tool_arguments(function.get("arguments")),
                tool_call_id=normalize_tool_call_id(explicit_id, fallback=f"tool_call_{index + 1}"),
            )
        )
    return tuple(parsed)


def parse_chat_response(
    response_payload: Mapping[str, object],
    request: ProviderTurnRequest,
    *,
    provider_name: str,
    model_name: str,
) -> ProviderTurnResult:
    usage = OpenAIWireCodec.usage(response_payload)
    metadata = response_metadata(response_payload)
    choices = response_payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
        raise ProviderExecutionError(
            kind="provider_error",
            provider_name=provider_name,
            model_name=model_name,
            message="provider response contained no valid choices",
            retryable=False,
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
            details={"source": "api", "reason": "malformed_response"},
        )
    raw_finish_reason_value = choice.get("finish_reason")
    done_reason = OpenAIWireCodec.done_reason(raw_finish_reason_value)
    raw_token = raw_finish_reason(raw_finish_reason_value)
    if done_reason == "error" and raw_token is not None:
        raise_finish_reason_error(raw_token, provider_name=provider_name, model_name=model_name)
    if raw_token is None:
        metadata["finish_reason_reported"] = False
    return ProviderTurnResult(
        tool_calls=parse_response_tool_calls(
            message_mapping,
            request,
            provider_name=provider_name,
            model_name=model_name,
        ),
        output=content if isinstance(content, str) else "",
        reasoning=reasoning_from_message(message_mapping),
        usage=usage,
        done_reason=done_reason,
        finish_reason_reported=raw_token is not None,
        metadata=metadata,
    )


__all__ = [
    "DEFAULT_OPENAI_BASE_URL",
    "OpenAIWireCodec",
    "ToolAccumulator",
    "api_error_payload",
    "map_openai_exception",
    "output_cap",
    "parse_chat_response",
    "parse_response_tool_calls",
    "raise_finish_reason_error",
    "raw_finish_reason",
    "reasoning_from_message",
    "requires_reasoning_content_with_tool_calls",
    "session_id",
    "stream_chunk_payload",
    "stream_event_error_payload",
]
