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

from ..sdk.codec import decode_provider_result, decode_tool_result, encode_tool_call
from ..sdk.history import PAGE_BYTES, HistoryEvent, HistoryPage
from ..sdk.json_values import json_wire_object
from ..sdk.tools import TextOutput


def _json(value: object) -> str:
    wire = json_wire_object(value) if isinstance(value, Mapping) else value
    try:
        return json.dumps(wire, ensure_ascii=False, allow_nan=False, indent=2)
    except TypeError, ValueError:
        return json.dumps(str(value), ensure_ascii=False)


def _result_text(result: object) -> str:
    output = getattr(result, "output", None)
    body = getattr(result, "body", None)
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
    chunks: list[str] = []
    used_bytes = 0
    cursor_sequence = after_sequence
    for event in events:
        block = render_event_block(event)
        if block is None:
            cursor_sequence = event.sequence
            continue
        raw = block.encode("utf-8")
        if offset > 0:
            raw = raw[offset:]
            offset = 0
        while raw:
            remaining = limit_bytes - used_bytes
            overhead = 2 if chunks else 0
            budget = remaining - overhead
            if budget <= 0:
                return HistoryPage("\n\n".join(chunks), cursor_sequence, len(block.encode("utf-8")) - len(raw), True)
            if len(raw) <= budget:
                chunks.append(raw.decode("utf-8"))
                used_bytes += len(raw) + overhead
                raw = b""
                cursor_sequence = event.sequence
                break
            cut = utf8_cut(raw, budget)
            chunk = raw[:cut].decode("utf-8")
            chunks.append(chunk)
            remaining_bytes = len(raw) - cut
            next_offset = len(block.encode("utf-8")) - remaining_bytes
            return HistoryPage("\n\n".join(chunks), cursor_sequence, next_offset, True)
    return HistoryPage("\n\n".join(chunks), cursor_sequence, 0, False)
