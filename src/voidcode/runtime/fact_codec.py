from __future__ import annotations

from dataclasses import dataclass
from typing import Final, cast

from ..core.turns import (
    LoopStepFact,
    ModelTurnFact,
    ResponseReadyFact,
    StreamFact,
    ToolCompletedFact,
    ToolRequestedFact,
    TurnFact,
    normalize_call_result,
)
from ..security.redaction import redact_mapping
from ..tools.contracts import ToolCall
from ..tools.output import sanitize_tool_arguments
from .events import EventEnvelope, EventSource
from .execution.tool_result_projection import _tool_completed_payload
from .runtime_debug import prompt_and_tool_results_from_debug_events
from .session import SessionState
from .tool_call_preview import WRITE_PREVIEW_TOOLS

FACT_CODEC_VERSION: Final[int] = 1
DURABLE_FACT_EVENT_TYPES: Final[frozenset[str]] = frozenset(
    {
        "graph.loop_step",
        "graph.model_turn",
        "graph.response_ready",
        "graph.tool_request_created",
        "runtime.tool_completed",
    }
)


@dataclass(frozen=True, slots=True)
class EncodedFact:
    event_type: str
    source: EventSource
    payload: dict[str, object]
    persistable: bool


def require_fact_codec_version(version: int) -> None:
    if isinstance(version, bool) or version != FACT_CODEC_VERSION:
        raise ValueError(f"unsupported execution fact codec version: {version}; migration is required")


def encode_fact(fact: TurnFact, *, version: int = FACT_CODEC_VERSION, session: SessionState | None = None) -> EncodedFact:
    """Project a typed fact once; only the redacted result may reach rows/UI."""
    require_fact_codec_version(version)
    source: EventSource = "graph"
    persistable = not isinstance(fact, StreamFact)
    payload: dict[str, object] = {}
    if isinstance(fact, LoopStepFact):
        event_type = "graph.loop_step"
        payload.update(step=fact.step, phase=fact.phase)
    elif isinstance(fact, ModelTurnFact):
        event_type = "graph.model_turn"
        payload.update(turn=fact.turn, mode=fact.mode, prompt=fact.prompt)
        if fact.mode == "provider":
            payload.update(provider=fact.provider, model=fact.model, attempt=fact.attempt, streaming=fact.streaming)
    elif isinstance(fact, ResponseReadyFact):
        event_type = "graph.response_ready"
        payload["output_preview"] = fact.output_preview
        if fact.finish_reason is not None:
            payload["finish_reason"] = fact.finish_reason
        if fact.finish_reason_reported is not None:
            payload["finish_reason_reported"] = fact.finish_reason_reported
    elif isinstance(fact, StreamFact):
        event = fact.event
        event_type = f"graph.{fact.kind}"
        payload.update(kind=event.kind, channel=event.channel)
        for key, value in (
            ("text", event.text),
            ("tool_call_id", event.tool_call_id),
            ("tool_name", event.tool_name),
            ("ordinal", event.tool_call_ordinal),
            ("fragment_ordinal", event.fragment_ordinal),
            ("metadata", event.metadata),
            ("error", event.error),
            ("done_reason", event.done_reason),
        ):
            if value is not None:
                payload[key] = value
        if (fact.tool_name or event.tool_name) not in WRITE_PREVIEW_TOOLS:
            if event.arguments_delta is not None:
                payload["arguments_delta"] = event.arguments_delta
            if event.parsed_arguments is not None:
                payload["parsed_arguments"] = event.parsed_arguments
        if fact.diff_preview is not None:
            payload["diff_preview"] = fact.diff_preview
        if event.error_kind is not None:
            payload["diagnostics"] = {"kind": event.error_kind, "summary": event.error, "details": event.metadata or {}}
        if event.usage is not None:
            payload["usage"] = event.usage.metadata_payload()
    elif isinstance(fact, ToolRequestedFact):
        event_type = "graph.tool_request_created"
        call = fact.call
        if not call.tool_call_id:
            raise ValueError("requested execution fact has no original native call identity")
        payload.update(tool=call.tool_name, tool_call_id=call.tool_call_id, arguments=sanitize_tool_arguments(call.arguments))
        if isinstance(path := call.arguments.get("path"), str):
            payload["path"] = path
        if fact.diff_preview is not None:
            payload["diff_preview"] = fact.diff_preview
    else:
        event_type, source = "runtime.tool_completed", "tool"
        call_id = fact.call.tool_call_id
        if not call_id:
            raise ValueError("completed execution fact has no original native call identity")
        payload.update(
            _tool_completed_payload(
                session=session,
                tool_result=normalize_call_result(fact.call, fact.result),
                tool_call_id=call_id,
                sanitized_arguments=sanitize_tool_arguments(fact.call.arguments),
            )
        )
    return EncodedFact(event_type, source, redact_mapping(payload), persistable)


def _text(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise ValueError(f"persisted fact {key} must be a string")
    return value


def _integer(payload: dict[str, object], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"persisted fact {key} must be a non-negative integer")
    return value


def _boolean(payload: dict[str, object], key: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"persisted fact {key} must be a boolean")
    return value


def decode_fact(event: EventEnvelope, *, version: int = FACT_CODEC_VERSION) -> TurnFact | None:
    """Read supported v1 facts without executing providers, tools or hooks."""
    require_fact_codec_version(version)
    payload = event.payload
    if event.event_type == "graph.loop_step":
        phase = _text(payload, "phase")
        if phase not in {"plan", "finalize"}:
            raise ValueError("persisted loop phase is unsupported")
        return LoopStepFact(_integer(payload, "step"), phase)
    if event.event_type == "graph.model_turn":
        mode = _text(payload, "mode")
        if mode == "deterministic":
            return ModelTurnFact(_integer(payload, "turn"), mode, _text(payload, "prompt"))
        if mode != "provider":
            raise ValueError("persisted model-turn mode is unsupported")
        return ModelTurnFact(
            _integer(payload, "turn"),
            mode,
            _text(payload, "prompt"),
            provider=_text(payload, "provider"),
            model=_text(payload, "model"),
            attempt=_integer(payload, "attempt"),
            streaming=_boolean(payload, "streaming"),
        )
    if event.event_type == "graph.response_ready":
        reason = _text(payload, "finish_reason") if "finish_reason" in payload else None
        reported = _boolean(payload, "finish_reason_reported") if "finish_reason_reported" in payload else None
        return ResponseReadyFact(_text(payload, "output_preview"), reason, reported)
    if event.event_type in {"graph.tool_request_created", "runtime.tool_completed"}:
        call_id = _text(payload, "tool_call_id")
        if not call_id:
            raise ValueError("legacy execution fact has no authentic call identity; migration is required")
        arguments = payload.get("arguments")
        if not isinstance(arguments, dict) or not all(isinstance(key, str) for key in arguments):
            raise ValueError("persisted native arguments must be an object")
        call = ToolCall(tool_name=_text(payload, "tool"), tool_call_id=call_id, arguments=cast(dict[str, object], arguments))
        if event.event_type == "graph.tool_request_created":
            preview = payload.get("diff_preview")
            if preview is not None and not isinstance(preview, dict):
                raise ValueError("persisted tool preview must be an object")
            return ToolRequestedFact(call, cast(dict[str, object] | None, preview))
        if payload.get("status") not in {"ok", "error"}:
            raise ValueError("persisted native result status is unsupported")
        _, results = prompt_and_tool_results_from_debug_events((event,))
        return ToolCompletedFact(call, results[0])
    if event.event_type in {"graph.provider_stream", "graph.tool_call_start", "graph.tool_call_delta", "graph.tool_call_end"}:
        raise ValueError("live-only provider facts do not belong in the durable log")
    # Product governance/capability rows retain their runtime owner, not a fake
    # generic core fact. Pages keep their real parent edges and cursor positions.
    return None
