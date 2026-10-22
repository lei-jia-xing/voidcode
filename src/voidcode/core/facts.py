"""One decoder for committed SDK facts, shared by live delivery and history."""

from collections.abc import Iterable, Mapping
from typing import get_args

from voidcode.sdk.codec import decode_provider_result
from voidcode.sdk.hooks import StopCause

from .storage.store import StoredEvent

FACT_EVENT_TYPES: dict[str, str] = {
    "kernel.model_turn": "usage",
    "kernel.provider_interrupted": "usage",
    "kernel.tool_completed": "invocation_finished",
    "kernel.tool_interrupted": "invocation_finished",
    "kernel.run_completed": "run_finished",
    "kernel.run_failed": "run_finished",
    "kernel.run_interrupted": "run_finished",
    "kernel.user_message": "run_requested",
    "kernel.run_resumed": "run_requested",
    "kernel.approval_requested": "approval_requested",
    "kernel.child_spawned": "child_spawned",
    "kernel.window_projected": "context_projected",
}


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return value


def fact_for_event(ev: StoredEvent, events: Iterable[StoredEvent]) -> tuple[str, dict[str, object]] | None:
    if ev.event_type in ("kernel.model_turn", "kernel.provider_interrupted"):
        d_seq = ev.payload.get("dispatch_sequence")
        disp_seq_val = int(d_seq) if isinstance(d_seq, (int, float)) else None
        if disp_seq_val is None:
            raise ValueError(f"usage fact at sequence {ev.sequence} has no dispatch sequence")
        intent_ev = next(
            (e for e in events if e.sequence == disp_seq_val and e.event_type == "kernel.provider_intent"),
            None,
        )
        if intent_ev is None:
            raise ValueError(f"usage fact at sequence {ev.sequence} has no provider intent at {disp_seq_val}")
        provider_name = intent_ev.payload.get("provider")
        model_name = intent_ev.payload.get("model")
        logical_turn = intent_ev.payload.get("logical_turn")
        if not isinstance(provider_name, str) or not provider_name or not isinstance(model_name, str) or not model_name:
            raise ValueError(f"provider intent at {disp_seq_val} lacks an authentic provider/model identity")
        ms = ev.payload.get("elapsed_ms")
        elapsed_sec = (ms / 1000.0) if ms is not None and isinstance(ms, (int, float)) else None
        usage_payload: Mapping[str, object] | None = None
        cost_usd: float | None = None
        if ev.event_type == "kernel.model_turn":
            turn = decode_provider_result(_mapping(ev.payload["result"], "model turn"))
            usage_payload = turn.usage.metadata_payload() if turn.usage else None
            cost_usd = turn.usage.cost_usd if turn.usage is not None else None
        return "usage", {
            "session_id": ev.session_id,
            "run_id": ev.run_id,
            "revision_id": ev.revision_id,
            "logical_turn": str(logical_turn) if isinstance(logical_turn, str) else "",
            "event_sequence": ev.sequence,
            "dispatch_sequence": disp_seq_val,
            "provider_name": provider_name,
            "model_name": model_name,
            "usage": usage_payload,
            "elapsed_seconds": elapsed_sec,
            "cost_usd": cost_usd,
        }
    if ev.event_type in ("kernel.tool_completed", "kernel.tool_interrupted"):
        call_dict = _mapping(ev.payload.get("call"), "invocation call")
        res_dict = _mapping(ev.payload.get("result"), "invocation result")
        tool_name = call_dict.get("tool_name")
        if not isinstance(tool_name, str) or not tool_name:
            raise ValueError(f"invocation fact at {ev.sequence} lacks a tool identity")
        if res_dict.get("status") not in ("ok", "error"):
            raise ValueError(f"invocation fact at {ev.sequence} lacks an outcome")
        tool_status = "error" if (ev.event_type == "kernel.tool_interrupted" or res_dict["status"] == "error") else "ok"
        metadata = _mapping(ev.payload.get("fact_metadata"), "invocation fact metadata")
        required = {
            "owner",
            "effects",
            "semantics",
            "model",
            "argument_bytes",
            "result_bytes",
            "truncated",
            "partial",
            "error_kind",
            "has_retry_guidance",
            "reads",
            "changes",
            "resource_paths",
        }
        missing = required - metadata.keys()
        if missing:
            raise ValueError(f"invocation fact at {ev.sequence} lacks metadata fields: {', '.join(sorted(missing))}")
        unexpected = metadata.keys() - required
        if unexpected:
            raise ValueError(f"invocation fact at {ev.sequence} has unexpected metadata fields: {', '.join(sorted(unexpected))}")
        if metadata["semantics"] is None:
            raise ValueError(f"invocation fact at {ev.sequence} lacks recorded semantics")
        return "invocation_finished", {
            "session_id": ev.session_id,
            "run_id": ev.run_id,
            "revision_id": ev.revision_id,
            "logical_turn": str(ev.payload.get("logical_turn", "")),
            "event_sequence": ev.sequence,
            "tool_name": tool_name,
            "tool_call_id": call_dict.get("tool_call_id"),
            "status": tool_status,
            "error": str(res_dict["error"]) if "error" in res_dict and res_dict["error"] is not None else None,
            **metadata,
        }
    if ev.event_type in ("kernel.run_completed", "kernel.run_failed", "kernel.run_interrupted"):
        if ev.event_type == "kernel.run_interrupted":
            stop_cause = ev.payload.get("stop_cause")
            if stop_cause not in get_args(StopCause):
                raise ValueError(f"interrupted run event at {ev.sequence} lacks an authentic stop cause")
        elif ev.event_type == "kernel.run_failed":
            stop_cause = "error"
        else:
            stop_cause = "completed"
        # The originating process measured this duration and persisted it with
        # the event; replaying a historical run must never re-measure it.
        measured_ms = ev.payload.get("elapsed_ms")
        elapsed_seconds = measured_ms / 1000.0 if isinstance(measured_ms, (int, float)) and not isinstance(measured_ms, bool) else None
        return "run_finished", {
            "session_id": ev.session_id,
            "run_id": ev.run_id,
            "revision_id": ev.revision_id,
            "logical_turn": str(ev.payload.get("logical_turn", "")),
            "event_sequence": ev.sequence,
            "status": ev.event_type.replace("kernel.run_", ""),
            "stop_cause": stop_cause,
            "elapsed_seconds": elapsed_seconds,
        }
    kind = FACT_EVENT_TYPES.get(ev.event_type)
    common = {
        "session_id": ev.session_id,
        "run_id": ev.run_id,
        "revision_id": ev.revision_id,
        "logical_turn": str(ev.payload.get("logical_turn", "")),
        "event_sequence": ev.sequence,
    }
    if kind == "run_requested":
        return kind, {**common, "resumed": ev.event_type == "kernel.run_resumed"}
    if kind == "approval_requested":
        return kind, {**common, "request_id": ev.payload["request_id"]}
    if kind == "child_spawned":
        return kind, {**common, "child_session_id": ev.payload["child_session_id"]}
    if kind == "context_projected":
        return kind, {
            **common,
            "owner": ev.payload["owner"],
            "covered_bytes": ev.payload["covered_bytes"],
            "dropped_bytes": ev.payload["dropped_bytes"],
        }
    return None
