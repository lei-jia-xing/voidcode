"""The OpenAI Chat Completions streaming event loop.

The loop owns chunk decoding, tool-call fragment accumulation, terminal/usage
facts, and progress events. It performs no SDK client construction and does not
map exceptions; the provider facade wraps it in the existing error mapping.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping

from voidcode.sdk.provider import (
    ProviderExecutionError,
    ProviderStreamEvent,
    ProviderTokenUsage,
    ProviderTurnRequest,
)

from ._wire_common import (
    DEFAULT_STREAM_FIRST_EVENT_TIMEOUT_SECONDS,
    Transport,
    TransportError,
    abort_signal_cancelled,
    cancelled_events,
    iter_stream_with_timeout,
    normalize_tool_call_id,
    open_event_stream,
    response_metadata,
    tool_name_maps,
)
from .openai_wire import (
    OpenAIWireCodec,
    ToolAccumulator,
    raise_finish_reason_error,
    session_id,
    stream_chunk_payload,
)
from .trace import write_provider_trace


def stream_turn_events(
    request: ProviderTurnRequest,
    *,
    default_provider_name: str,
    transport: Transport,
    payload: dict[str, object],
    timeout_seconds: float,
) -> Iterator[ProviderStreamEvent]:
    """Yield one provider stream's normalized events.

    Exceptions propagate unchanged for the provider boundary to map, matching
    the previous in-class loop.
    """
    provider_name = request.provider_name or default_provider_name
    model_name = request.model_name or "unknown"
    stream = open_event_stream(transport, payload, timeout_seconds=timeout_seconds)
    latest_usage: ProviderTokenUsage | None = None
    metadata: dict[str, object] = {}
    done_reason = "unknown"
    finish_reason_reported = False
    raw_finish_reason_token: str | None = None
    accumulators: dict[int, ToolAccumulator] = {}
    reverse = tool_name_maps(request)[1]
    for raw_chunk in iter_stream_with_timeout(
        stream,
        timeout_seconds=timeout_seconds,
        provider_name=provider_name,
        model_name=model_name,
        first_event_timeout_seconds=DEFAULT_STREAM_FIRST_EVENT_TIMEOUT_SECONDS,
        aborted=lambda: abort_signal_cancelled(request),
    ):
        if abort_signal_cancelled(request):
            yield from cancelled_events()
            return
        chunk = stream_chunk_payload(raw_chunk)
        if chunk.get("error") is not None or chunk.get("type") == "error":
            raise TransportError(chunk)
        metadata.update(response_metadata(chunk))
        latest_usage = OpenAIWireCodec.usage(chunk) or latest_usage
        choices = chunk.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
            continue
        choice = choices[0]
        raw_finish_reason = choice.get("finish_reason")
        if isinstance(raw_finish_reason, str) and raw_finish_reason.strip():
            finish_reason_reported = True
            done_reason = OpenAIWireCodec.done_reason(raw_finish_reason)
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
                    details={"source": "stream", "reason": "malformed_tool_call"},
                )
            raw_index = raw_tool.get("index")
            if raw_index is not None and (not isinstance(raw_index, int) or isinstance(raw_index, bool) or raw_index < 0):
                raise ProviderExecutionError(
                    kind="provider_error",
                    provider_name=provider_name,
                    model_name=model_name,
                    message="provider stream tool call index was malformed",
                    retryable=False,
                    details={"source": "stream", "reason": "malformed_tool_call"},
                )
            index = raw_index if isinstance(raw_index, int) else 0
            previous = accumulators.setdefault(index, ToolAccumulator())
            raw_id = raw_tool.get("id")
            if raw_id is not None and (not isinstance(raw_id, str) or not raw_id):
                raise ProviderExecutionError(
                    kind="provider_error",
                    provider_name=provider_name,
                    model_name=model_name,
                    message="provider stream tool call id was malformed",
                    retryable=False,
                    details={"source": "stream", "reason": "malformed_tool_call"},
                )
            if isinstance(raw_id, str) and previous.tool_call_id not in (None, raw_id):
                raise ProviderExecutionError(
                    kind="provider_error",
                    provider_name=provider_name,
                    model_name=model_name,
                    message="provider stream changed a tool call id",
                    retryable=False,
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
            previous.tool_call_id = raw_id if isinstance(raw_id, str) else previous.tool_call_id
            previous.tool_name = name
            if fragment is not None:
                previous.fragments.append(fragment)
            previous.explicit_streaming = explicit
            previous.started = started
    if not finish_reason_reported:
        raise ProviderExecutionError(
            kind="provider_error",
            provider_name=provider_name,
            model_name=model_name,
            message="provider stream ended without a finish_reason",
            retryable=False,
            details={"source": "stream", "reason": "missing_terminal_event"},
        )
    completed: list[tuple[int, ToolAccumulator, dict[str, object], str]] = []
    for index, accumulator in sorted(accumulators.items()):
        if accumulator.tool_name is None:
            raise ProviderExecutionError(
                kind="provider_error",
                provider_name=provider_name,
                model_name=model_name,
                message="provider stream contained a malformed tool call",
                retryable=False,
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
                details={"source": "stream", "reason": "malformed_tool_call_arguments"},
            ) from exc
        if not isinstance(parsed, dict):
            raise ProviderExecutionError(
                kind="provider_error",
                provider_name=provider_name,
                model_name=model_name,
                message="provider stream tool-call arguments were not an object",
                retryable=False,
                details={"source": "stream", "reason": "malformed_tool_call_arguments"},
            )
        runtime_name = reverse.get(accumulator.tool_name, accumulator.tool_name)
        completed.append((index, accumulator, parsed, runtime_name))
    if raw_finish_reason_token is not None:
        if done_reason == "error":
            raise_finish_reason_error(raw_finish_reason_token, provider_name=provider_name, model_name=model_name)
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
            "session_id": session_id(request) or None,
            "provider": provider_name,
            "model": request.model_name,
            "attempt": request.attempt,
            "stream": True,
            "transport": "openai_chat_completions",
        },
    )


__all__ = ["stream_turn_events"]
