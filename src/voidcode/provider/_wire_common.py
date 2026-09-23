"""Mechanics the native wire adapters share.

Each adapter owns its SDK client and event mapping; only the pieces that are
identical across wires live here: usage coercion, inbound tool-id
normalization, per-turn header resolution, the timeout-guarded stream pump and
the one-slot transport holder a frozen provider uses to own its client.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from queue import Empty, Full, Queue
from threading import Event, Thread
from typing import cast

from .protocol import ProviderExecutionError

# A declared request header may name the conversation with ``{session_id}``. The
# transport -- and therefore its SDK client -- is cached across turns, so a value
# resolved at construction time would freeze the first conversation's id; it is
# resolved per request instead.
_SESSION_ID_PLACEHOLDER = "{session_id}"

_STREAM_TIMEOUT_SENTINEL = object()


def usage_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    result = int(value)
    return result if result >= 0 else None


def normalize_tool_call_id(value: str | None, *, fallback: str) -> str:
    """Map an inbound tool-call id onto the alphabet every wire accepts."""
    raw = value if isinstance(value, str) and value.strip() else fallback
    return re.sub(r"[^a-zA-Z0-9_-]", "_", raw.strip()) or fallback


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


def iter_stream_with_timeout(
    stream: Iterator[object],
    *,
    timeout_seconds: float,
    provider_name: str,
    model_name: str,
) -> Iterator[object]:
    """Pull ``stream`` on a worker thread, failing when a chunk stalls.

    The SDK stream is consumed off-thread so a chunk that never arrives cannot
    block the caller: the queue hands control back every ``timeout_seconds``.
    """
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

    thread = Thread(target=pull, name=f"voidcode-{provider_name}-stream", daemon=True)
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


@dataclass(slots=True)
class OwnedTransport[TransportT]:
    """One-slot holder letting a frozen provider own a single transport."""

    value: TransportT | None = None
