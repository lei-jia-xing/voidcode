"""Mechanics the native wire adapters share.

Each adapter owns its SDK client and event mapping; only the pieces that are
identical across wires live here: usage coercion, inbound tool-id
normalization, per-turn header resolution, the timeout-guarded stream pump and
the one-slot transport holder a frozen provider uses to own its client.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Generator, Iterator, Mapping
from dataclasses import dataclass, field
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread
from time import monotonic
from typing import Literal, Protocol

from voidcode.sdk.provider import ProviderDoneReason, ProviderExecutionError, ProviderStreamEvent, ProviderTokenUsage

type ProviderErrorSource = Literal["api", "stream", "model_listing"]
type _StreamPumpKind = Literal["item", "done", "error"]

# A declared request header may name the conversation with ``{session_id}``. The
# transport -- and therefore its SDK client -- is cached across turns, so a value
# resolved at construction time would freeze the first conversation's id; it is
# resolved per request instead.
_SESSION_ID_PLACEHOLDER = "{session_id}"


def usage_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if value < 0 or (isinstance(value, float) and not value.is_integer()):
        return None
    return int(value)


def normalize_tool_call_id(value: str | None, *, fallback: str) -> str:
    """Map an inbound tool-call id onto the alphabet every wire accepts."""
    raw = value if isinstance(value, str) and value.strip() else fallback
    return re.sub(r"[^a-zA-Z0-9_-]", "_", raw.strip()) or fallback


_TOOL_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")
_MAX_TOOL_NAME_length = 64
_TOOL_NAME_HASH_length = 8


def safe_tool_name(name: str) -> str:
    """Map a model-chosen tool name onto the alphabet every wire accepts."""
    if _TOOL_NAME_PATTERN.fullmatch(name) and len(name) <= _MAX_TOOL_NAME_length:
        return name
    normalized = re.sub(r"[^a-zA-Z0-9_-]", "_", name).strip("_") or "tool"
    suffix = "_" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:_TOOL_NAME_HASH_length]
    return f"{normalized[: _MAX_TOOL_NAME_length - len(suffix)]}{suffix}"


def parse_tool_arguments(value: object) -> dict[str, object]:
    """Decode one tool-call argument payload into a plain object map."""
    if isinstance(value, Mapping):
        return dict(value)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("tool input was empty")
    decoded = json.loads(value)
    if not isinstance(decoded, dict):
        raise ValueError("tool input was not an object")
    return dict(decoded)


def decode_response_payload(value: object) -> dict[str, object]:
    """Decode one provider response object into its plain mapping.

    SDK responses may already be mappings or expose ``model_dump``; a value that
    is neither is a wire contract violation, not a retryable network failure.
    """
    if isinstance(value, Mapping):
        return dict(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        if isinstance(dumped, Mapping):
            return dict(dumped)
    raise ValueError("provider response must be a mapping")


def response_metadata(payload: Mapping[str, object], *, thinking_blocks: bool = False) -> dict[str, object]:
    """Request identity and served model; vendor-specific blocks are opt-in."""
    metadata: dict[str, object] = {}
    if isinstance(payload.get("id"), str) and payload["id"]:
        metadata["request_id"] = payload["id"]
    if isinstance(payload.get("model"), str) and payload["model"]:
        metadata["served_model"] = payload["model"]
    if thinking_blocks:
        content = payload.get("content")
        if isinstance(content, list):
            blocks = [dict(block) for block in content if isinstance(block, Mapping) and block.get("type") in {"thinking", "redacted_thinking"}]
            if blocks:
                metadata["thinking_blocks"] = blocks
    return metadata


def resolve_extra_request_headers(declared: Mapping[str, str], session_id: str | None) -> dict[str, str]:
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


#: OMP arms both stream watchdogs at 300s (``utils/idle-iterator.ts:4-7``). The
#: idle one is voidcode's existing per-chunk timeout (configurable per provider);
#: the first-event budget is floored at it, because a first token may legitimately
#: take longer than the gap between chunks -- a provider configured with a longer
#: per-chunk timeout gets the same longer first-token allowance
#: (``utils/idle-iterator.ts:56-62,79-91``).
DEFAULT_STREAM_FIRST_EVENT_TIMEOUT_SECONDS = 300.0
#: Fallback per-request timeout when the provider configuration sets none.
DEFAULT_TIMEOUT_SECONDS = 300.0
#: Output cap when the model catalog reports no maximum output.
OUTPUT_CAP_WHEN_UNKNOWN = 64000
#: Satisfies an SDK's "credential required" construction check without sending
#: one; each wire's headers decide whether and which credential is transmitted.
PLACEHOLDER_API_KEY = "voidcode-no-api-key"


def abort_signal_cancelled(request: object) -> bool:
    """Whether the caller already asked for this turn to stop."""
    signal = getattr(request, "abort_signal", None)
    return bool(signal is not None and signal.cancelled)


def iter_stream_with_timeout(
    stream: Iterator[object],
    *,
    timeout_seconds: float,
    provider_name: str,
    model_name: str,
    first_event_timeout_seconds: float | None = None,
    aborted: Callable[[], bool] | None = None,
) -> Generator[object]:
    """Pull ``stream`` on a worker thread, failing when a chunk stalls.

    The SDK stream is consumed off-thread so a chunk that never arrives cannot
    block the caller. The queue enforces the deadline while polling cancellation
    every 50ms when an abort callback is supplied.
    ``first_event_timeout_seconds`` bounds the wait for the *first* chunk
    separately from the idle timeout between chunks (OMP arms both at 300s,
    ``utils/idle-iterator.ts:4-7``); it defaults to ``timeout_seconds`` and is
    never allowed to undershoot it (``utils/idle-iterator.ts:90``), so a slower
    first token is never cut off by a shorter inter-chunk gap.

    ``aborted`` reports caller intent. When a wait expires while the caller has
    already aborted, the failure is ``cancelled`` rather than a provider timeout:
    caller intent dominates (OMP's abort source tracker, ``utils/abort.ts:22-49``).
    """
    if timeout_seconds <= 0:
        raise ProviderExecutionError(
            kind="transient_failure",
            provider_name=provider_name,
            model_name=model_name,
            message="provider stream timeout must be greater than zero",
            retryable=False,
        )
    # The first-event budget never undershoots the idle timeout
    # (``getOpenAIStreamFirstEventTimeoutMs`` returns ``max(base, idleTimeoutMs)``,
    # ``utils/idle-iterator.ts:90``): with both defaults at 300s the two coincide,
    # and a longer per-chunk timeout widens the first-token allowance with it.
    first_event_budget = max(first_event_timeout_seconds or 0.0, timeout_seconds)
    queue: Queue[tuple[_StreamPumpKind, object]] = Queue(maxsize=1)
    stop_event = Event()

    def enqueue(kind: _StreamPumpKind, value: object) -> None:
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
            enqueue("done", None)
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

    thread = Thread(target=pull, name=f"voidcode-{provider_name}-stream", daemon=True)
    thread.start()
    seen_first_event = False
    deadline = monotonic() + first_event_budget
    try:
        while True:
            remaining = max(0.0, deadline - monotonic())
            try:
                kind, value = queue.get(timeout=min(remaining, 0.05) if aborted is not None else remaining)
            except Empty as exc:
                if aborted is not None and aborted():
                    raise ProviderExecutionError(
                        kind="cancelled",
                        provider_name=provider_name,
                        model_name=model_name,
                        message="provider stream cancelled while waiting for a chunk",
                        retryable=False,
                    ) from exc
                if monotonic() < deadline:
                    continue
                raise ProviderExecutionError(
                    kind="transient_failure",
                    provider_name=provider_name,
                    model_name=model_name,
                    message=("provider stream first-event timeout exceeded" if not seen_first_event else "provider stream chunk timeout exceeded"),
                    retryable=True,
                ) from exc
            if kind == "done":
                return
            if kind == "error":
                # The HTTP transport can time out before our queue does. The
                # same caller intent must win regardless of which clock fires.
                if isinstance(value, Exception) and aborted is not None and aborted():
                    raise ProviderExecutionError(
                        kind="cancelled",
                        provider_name=provider_name,
                        model_name=model_name,
                        message="provider stream cancelled while waiting for a chunk",
                        retryable=False,
                    ) from value
                if isinstance(value, BaseException):
                    raise value
                raise RuntimeError(f"provider stream error: {value!r}")
            seen_first_event = True
            yield value
            deadline = monotonic() + timeout_seconds
    finally:
        stop_event.set()
        close_stream()
        if thread.is_alive():
            thread.join(timeout=min(timeout_seconds, 0.1))


@dataclass(slots=True)
class OwnedResource[TransportT]:
    """One owned client or transport, with serialized initialization."""

    value: TransportT | None = None
    lock: Lock = field(default_factory=Lock, repr=False, compare=False)

    def close(self) -> None:
        with self.lock:
            value, self.value = self.value, None
        closer = getattr(value, "close", None)
        if callable(closer):
            closer()


# --- Shared SSE transport: one protocol, one error, usage/done/base mechanics ---


@dataclass(frozen=True, slots=True)
class TransportError(Exception):
    """One failed vendor request: the typed payload the provider maps to execution errors."""

    payload: dict[str, object]
    message: str = "provider request failed"


class Transport(Protocol):
    """One vendor HTTP transport behind the native providers."""

    def request(self, payload: dict[str, object], *, timeout_seconds: float) -> object: ...


def build_usage(
    *,
    input_tokens: int | None,
    output_tokens: int | None,
    cache_read_tokens: int | None = None,
    cache_write_tokens: int | None = None,
    uncached_input_tokens: int | None = None,
) -> ProviderTokenUsage | None:
    """One usage record, or None when every bucket is unknown."""
    if all(value is None for value in (input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, uncached_input_tokens)):
        return None
    return ProviderTokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        uncached_input_tokens=uncached_input_tokens,
    )


def merge_usage(previous: ProviderTokenUsage | None, current: ProviderTokenUsage | None) -> ProviderTokenUsage | None:
    """Field-wise latest-wins merge for stream chunks carrying partial buckets."""
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


def map_done_reason(value: object, table: Mapping[str, ProviderDoneReason]) -> ProviderDoneReason:
    """Map one reported finish token through a vendor table; anything unmapped is unknown."""
    if not isinstance(value, str):
        return "unknown"
    return table.get(value.strip().lower(), "unknown")


def strip_base_url(base_url: str | None, default: str) -> str:
    """Configured host without surrounding whitespace or trailing slashes, or the default."""
    return (base_url or default).strip().rstrip("/") or default


def cancelled_events() -> Generator[ProviderStreamEvent]:
    """The abort-error prelude a cancelled stream yields instead of opening."""
    yield ProviderStreamEvent(kind="error", channel="error", error="provider stream cancelled", error_kind="cancelled")
    yield ProviderStreamEvent(kind="done", done_reason="cancelled")


def open_event_stream(transport: Transport, payload: dict[str, object], *, timeout_seconds: float) -> Iterator[object]:
    """Request one event stream and guard its shape: reraise transported errors, require an iterator."""
    stream = transport.request(payload, timeout_seconds=timeout_seconds)
    if isinstance(stream, BaseException):
        raise stream
    if not isinstance(stream, Iterator):
        raise ValueError("provider stream transport did not return an iterator")
    return stream


def fallback_execution_error(
    exc: Exception, *, provider_name: str, model_name: str, source: ProviderErrorSource, network_error: bool
) -> ProviderExecutionError:
    """Shared _map_exception tail: passthrough plus the generic provider_error."""
    if isinstance(exc, ProviderExecutionError):
        return exc
    return ProviderExecutionError(
        kind="provider_error",
        provider_name=provider_name,
        model_name=model_name,
        message="provider request failed",
        retryable=network_error,
        details={
            "source": source,
            "exception_type": type(exc).__name__,
            **({"network_failure": True} if network_error else {}),
        },
    )
