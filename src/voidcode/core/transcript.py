"""Decode private committed events into public conversation records."""

import json
from collections.abc import Mapping, Sequence

from voidcode.sdk.codec import decode_provider_result, decode_tool_call, decode_tool_result
from voidcode.sdk.hooks import RequestWindow
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

from .storage.store import StoredEvent


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("transcript record requires an object")
    return value


def transcript_entry(event: StoredEvent) -> TranscriptEntry | None:
    payload = event.payload
    content: TranscriptContent
    match event.event_type:
        case "kernel.user_message":
            text = payload["content"]
            if not isinstance(text, str):
                raise ValueError("user message content must be a string")
            content = ProviderMessage("user", text)
        case "kernel.continuation_started" | "kernel.wakeup_fired":
            inner = _mapping(payload["payload"])
            rendered = json.dumps(json_wire_object(inner), ensure_ascii=False)
            if payload["role"] == "user":
                text = inner.get("text")
                content = ProviderMessage("user", text if isinstance(text, str) else rendered)
            else:
                wakeup = event.event_type == "kernel.wakeup_fired"
                label = str(payload["owner"]) if wakeup else event.source
                origin = "wakeup" if wakeup else "continuation"
                content = ProviderMessage("system", f"[Durable {origin} from {label}]\n{rendered}")
        case "kernel.model_turn":
            content = decode_provider_result(_mapping(payload["result"]))
        case "kernel.tool_completed" | "kernel.tool_interrupted":
            content = TranscriptTool(
                decode_tool_call(_mapping(payload["call"])),
                decode_tool_result(_mapping(payload["result"])),
                interrupted=event.event_type == "kernel.tool_interrupted",
                batch_sequence=_sequence(payload["batch_sequence"]),
                deferred=payload.get("deferred", False) is True,
            )
        case "kernel.provider_interrupted":
            content = TranscriptInterrupted()
        case "kernel.child_spawned":
            child = payload["child_session_id"]
            if not isinstance(child, str) or not child:
                raise ValueError("child identity must be a non-empty string")
            content = TranscriptChild(child)
        case "kernel.provider_intent":
            provider, model = payload["provider"], payload["model"]
            if not isinstance(provider, str) or not provider or not isinstance(model, str) or not model:
                raise ValueError("dispatch requires provider and model identities")
            content = TranscriptDispatch(provider, model, RequestWindow.from_payload(_mapping(payload["request_window"])))
        case "kernel.run_completed" | "kernel.run_failed" | "kernel.run_interrupted":
            status = (
                "completed" if event.event_type == "kernel.run_completed" else "failed" if event.event_type == "kernel.run_failed" else "interrupted"
            )
            content = TranscriptRun(status)
        case _:
            return None
    dispatch = _sequence(payload["dispatch_sequence"]) if event.event_type in {"kernel.model_turn", "kernel.provider_interrupted"} else None
    return TranscriptEntry(event.sequence, event.run_id, event.revision_id, event.source, content, dispatch)


def _sequence(value: object) -> int:
    if type(value) is not int or value < 1:
        raise ValueError("transcript correlation sequence must be a positive integer")
    return value


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
