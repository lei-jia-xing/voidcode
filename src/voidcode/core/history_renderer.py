"""Readable Markdown rendering of committed session events.

One shared projection, used by the core's immutable child-history snapshot and by the
history services, so both agree on what a transcript looks like. It knows durable event
shapes, never a provider or plugin module: user messages, assistant output and tool calls,
tool results, progress reports, and child facts. Provider reasoning and response metadata
are deliberately not rendered.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Protocol

from ..sdk.codec import decode_provider_result, decode_tool_result, encode_tool_call
from ..sdk.json_values import json_wire_object
from ..sdk.services import PAGE_BYTES
from ..sdk.tools import CompositeOutput, TextOutput, composite_ordered_text


class HistoryEvent(Protocol):
    """The attributes a stored event and a service event view both carry."""

    @property
    def sequence(self) -> int: ...

    @property
    def event_type(self) -> str: ...

    @property
    def source(self) -> str: ...

    @property
    def payload(self) -> Mapping[str, object] | None: ...


@dataclass(frozen=True, slots=True)
class HistoryPage:
    """One bounded page plus the cursor that reaches the next byte.

    ``next_after_sequence`` is the last event whose block was fully included (or the incoming
    cursor value when the first block is split). ``next_offset`` is the UTF-8 byte offset into
    the next block; zero means the next event after ``next_after_sequence`` starts at its head.
    ``truncated`` means more content remains.
    """

    text: str
    next_after_sequence: int
    next_offset: int
    truncated: bool


def _json(value: object) -> str:
    wire = json_wire_object(value) if isinstance(value, Mapping) else value
    try:
        return json.dumps(wire, ensure_ascii=False, allow_nan=False, indent=2)
    except TypeError, ValueError:
        return json.dumps(str(value), ensure_ascii=False)


def _result_text(result: object) -> str:
    output = getattr(result, "output", None)
    body = getattr(result, "body", None)
    if isinstance(output, CompositeOutput):
        ordered = composite_ordered_text(output)
        text: str | None = ordered if ordered else output.presentation
    else:
        text = output.text if isinstance(output, TextOutput) else getattr(output, "presentation", None)
    if body is not None:
        try:
            rendered = json.dumps(body.as_payload(), ensure_ascii=False, allow_nan=False)
        except TypeError, ValueError:
            rendered = str(body)
        text = rendered if text is None else f"{rendered}\n{text}"
    error = getattr(result, "error", None)
    if error is not None:
        text = error if text is None else f"{error}\n{text}"
    return text or "(no output)"


def _as_mapping(value: object) -> Mapping[str, object] | None:
    return value if isinstance(value, Mapping) else None


def render_event_block(event: HistoryEvent) -> str | None:
    """One complete readable block for a meaningful event; ``None`` for core bookkeeping."""
    event_type = event.event_type
    payload = event.payload or {}
    header = f"## {event_type} [{event.sequence}]"
    try:
        if event_type == "core.user_message":
            return f"## user [{event.sequence}]\n{payload.get('content', '')}"
        if event_type == "core.model_turn":
            wire = _as_mapping(payload.get("result"))
            if wire is None:
                return f"{header}\n(missing provider result)"
            turn = decode_provider_result(wire)
            lines = [f"## assistant [{event.sequence}]", turn.output or "(no text)"]
            for call in turn.tool_calls:
                encoded = encode_tool_call(call)
                lines.append(f"### call {call.tool_name}")
                lines.append(_json(encoded))
            return "\n".join(lines)
        if event_type in {"core.tool_completed", "core.tool_interrupted"}:
            wire = _as_mapping(payload.get("result"))
            if wire is None:
                return f"{header}\n{_json(payload)}"
            result = decode_tool_result(wire)
            state = "error" if event_type.endswith("interrupted") else "result"
            return f"## tool {result.tool_name} ({state}) [{event.sequence}]\n{_result_text(result)}"
        if event_type == "core.continuation_started":
            inner = _as_mapping(payload.get("payload")) or {}
            role = payload.get("role")
            text = inner.get("text") if role == "user" and isinstance(inner.get("text"), str) else _json(inner)
            return f"## message from {event.source} [{event.sequence}]\n{text}"
        if event_type == "core.wakeup_fired":
            inner = _as_mapping(payload.get("payload")) or {}
            role = payload.get("role")
            text = inner.get("text") if role == "user" and isinstance(inner.get("text"), str) else _json(inner)
            return f"## wakeup from {payload.get('owner', event.source)} [{event.sequence}]\n{text}"
        if event_type == "core.tool_progress":
            return f"## progress from {payload.get('owner', event.source)} [{event.sequence}]\n{_json(payload.get('progress', payload))}"
        if event_type == "core.child_spawned":
            return f"## child started [{event.sequence}]\n{_json(payload)}"
        if event_type == "core.child_finished":
            return f"## child finished [{event.sequence}]\n{_json(payload)}"
    except TypeError, ValueError, KeyError:
        if event_type == "core.model_turn":
            return f"{header}\n(malformed provider result)"
        return f"{header}\n{_json(payload)}"
    if event_type.startswith("plugin."):
        return f"## event {event_type} from {event.source} [{event.sequence}]\n{_json(payload)}"
    if event_type.startswith("core."):
        return None
    return f"{header}\n{_json(payload)}"


def iter_event_blocks(events: Iterable[HistoryEvent]) -> Iterator[str]:
    """Yield every complete readable block, in order, with no size-based loss."""
    for event in events:
        block = render_event_block(event)
        if block is None:
            continue
        if getattr(event, "payload_truncated", False):
            block += "\n[payload truncated by event bound]"
        yield block


def utf8_cut(raw: bytes, limit: int) -> int:
    """Largest UTF-8 boundary <= limit, or the first character when it cannot fit."""
    if limit >= len(raw):
        return len(raw)
    cut = limit
    while cut > 0 and raw[cut] & 0xC0 == 0x80:
        cut -= 1
    if cut == 0:
        cut = 1
        while cut < len(raw) and raw[cut] & 0xC0 == 0x80:
            cut += 1
    return cut


def render_history_page(
    events: Iterable[HistoryEvent],
    *,
    after_sequence: int = 0,
    offset: int = 0,
    page_bytes: int = PAGE_BYTES,
    max_bytes: int | None = None,
) -> HistoryPage:
    """Render one page from a byte cursor; a single large block is split, never clipped."""
    limit_bytes = page_bytes if max_bytes is None else max_bytes
    raw_chunks: list[bytes] = []
    used_bytes = 0
    cursor_sequence = after_sequence
    for event in events:
        block = render_event_block(event)
        if block is None:
            cursor_sequence = event.sequence
            continue
        raw_full = block.encode("utf-8")
        total = len(raw_full)
        pos = offset
        offset = 0
        while pos < total:
            remaining = limit_bytes - used_bytes
            overhead = 2 if raw_chunks else 0
            budget = remaining - overhead
            if budget <= 0:
                return HistoryPage(b"\n\n".join(raw_chunks).decode("utf-8"), cursor_sequence, pos, True)
            window = raw_full[pos:]
            window_length = len(window)
            if window_length <= budget:
                raw_chunks.append(window)
                used_bytes += window_length + overhead
                pos = total
                cursor_sequence = event.sequence
                break
            cut = utf8_cut(window, budget)
            raw_chunks.append(window[:cut])
            return HistoryPage(b"\n\n".join(raw_chunks).decode("utf-8"), cursor_sequence, pos + cut, True)
    return HistoryPage(b"\n\n".join(raw_chunks).decode("utf-8"), cursor_sequence, 0, False)
