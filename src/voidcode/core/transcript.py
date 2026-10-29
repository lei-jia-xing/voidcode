"""Decode private committed events into public conversation records."""

import json
from collections.abc import Mapping, Sequence

from voidcode.sdk.codec import decode_provider_result, decode_tool_call, decode_tool_result
from voidcode.sdk.hooks import RequestWindow
from voidcode.sdk.icons import Icon
from voidcode.sdk.json_values import json_wire_object
from voidcode.sdk.provider import ProviderMessage
from voidcode.sdk.transcript import (
    ContextRecord,
    TranscriptChild,
    TranscriptContent,
    TranscriptDispatch,
    TranscriptEntry,
    TranscriptInterrupted,
    TranscriptRun,
    TranscriptTool,
)

from .events import EventType
from .storage.store import StoredEvent


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("transcript record requires an object")
    return value


def transcript_entry(event: StoredEvent) -> TranscriptEntry | None:
    payload = event.payload
    content: TranscriptContent
    match event.event_type:
        case EventType.USER_MESSAGE:
            text = payload["content"]
            if not isinstance(text, str):
                raise ValueError("user message content must be a string")
            content = ProviderMessage("user", text)
        case EventType.CONTINUATION_STARTED | EventType.WAKEUP_FIRED:
            inner = _mapping(payload["payload"])
            rendered = json.dumps(json_wire_object(inner), ensure_ascii=False)
            if payload["role"] == "user":
                text = inner.get("text")
                content = ProviderMessage("user", text if isinstance(text, str) else rendered)
            else:
                wakeup = event.event_type == EventType.WAKEUP_FIRED
                label = str(payload["owner"]) if wakeup else event.source
                origin = "wakeup" if wakeup else "continuation"
                content = ProviderMessage("system", f"[Durable {origin} from {label}]\n{rendered}")
        case EventType.MODEL_TURN:
            content = decode_provider_result(_mapping(payload["result"]))
        case EventType.TOOL_COMPLETED | EventType.TOOL_INTERRUPTED:
            content = TranscriptTool(
                decode_tool_call(_mapping(payload["call"])),
                decode_tool_result(_mapping(payload["result"])),
                interrupted=event.event_type == EventType.TOOL_INTERRUPTED,
                batch_sequence=_sequence(payload["batch_sequence"]),
                deferred=payload.get("deferred", False) is True,
                icon=_icon(payload.get("icon")),
            )
        case EventType.PROVIDER_INTERRUPTED:
            content = TranscriptInterrupted()
        case EventType.CHILD_SPAWNED:
            child = payload["child_session_id"]
            if not isinstance(child, str) or not child:
                raise ValueError("child identity must be a non-empty string")
            content = TranscriptChild(child)
        case EventType.PROVIDER_INTENT:
            provider, model = payload["provider"], payload["model"]
            if not isinstance(provider, str) or not provider or not isinstance(model, str) or not model:
                raise ValueError("dispatch requires provider and model identities")
            content = TranscriptDispatch(provider, model, RequestWindow.from_payload(_mapping(payload["request_window"])))
        case EventType.RUN_COMPLETED | EventType.RUN_FAILED | EventType.RUN_INTERRUPTED:
            status = (
                "completed"
                if event.event_type == EventType.RUN_COMPLETED
                else "failed"
                if event.event_type == EventType.RUN_FAILED
                else "interrupted"
            )
            content = TranscriptRun(status)
        case _:
            return None
    dispatch = _sequence(payload["dispatch_sequence"]) if event.event_type in {EventType.MODEL_TURN, EventType.PROVIDER_INTERRUPTED} else None
    return TranscriptEntry(event.sequence, event.run_id, event.revision_id, event.source, content, dispatch)


def _sequence(value: object) -> int:
    if type(value) is not int or value < 1:
        raise ValueError("transcript correlation sequence must be a positive integer")
    return value


def _icon(value: object) -> Icon | None:
    """Birth icon from the committed payload; old events carry none."""
    if value is None:
        return None
    icon = _mapping(value)
    glyph = icon.get("glyph")
    if not isinstance(glyph, str):
        raise ValueError("tool icon requires a glyph string")
    return Icon(glyph=glyph)


def context_records(events: Sequence[StoredEvent]) -> tuple[ContextRecord, ...]:
    """The shared prefix validator for context readers and projection admission."""
    from voidcode.sdk.provider import ProviderTurnResult

    pending: dict[tuple[int, str | None], str] = {}
    valid = True
    records: list[ContextRecord] = []
    for event in events:
        entry = transcript_entry(event)
        content = None if entry is None else entry.content
        if isinstance(content, ProviderTurnResult):
            for call in content.tool_calls:
                key = (event.sequence, call.tool_call_id)
                if key in pending:
                    valid = False
                pending[key] = call.tool_name
        elif isinstance(content, TranscriptTool) and not content.deferred:
            key = (content.batch_sequence, content.call.tool_call_id)
            if pending.pop(key, None) != content.call.tool_name:
                valid = False
        size = len(
            json.dumps(json_wire_object(event.payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        )
        records.append(ContextRecord(event.sequence, size, valid and not pending, entry))
    return tuple(records)
