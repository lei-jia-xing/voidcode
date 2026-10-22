"""Decode private committed events into public conversation records."""

import json
from collections.abc import Mapping

from voidcode.sdk.codec import decode_provider_result, decode_tool_call, decode_tool_result
from voidcode.sdk.hooks import RequestWindow
from voidcode.sdk.json_values import json_wire_object
from voidcode.sdk.provider import ProviderMessage
from voidcode.sdk.transcript import TranscriptContent, TranscriptDispatch, TranscriptEntry, TranscriptRun, TranscriptTool

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
            )
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
    return TranscriptEntry(event.sequence, event.run_id, event.revision_id, event.source, content)
