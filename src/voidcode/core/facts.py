"""One decoder for committed SDK facts, shared by live delivery and history."""

from collections.abc import Iterable, Mapping
from typing import get_args

from voidcode.sdk.codec import decode_provider_result
from voidcode.sdk.hooks import ObservedFactKind, StopCause

from .events import EventType
from .storage.store import StoredEvent

_EVENT_KIND_MAP: dict[EventType, ObservedFactKind] = {
    EventType.MODEL_TURN: "usage",
    EventType.PROVIDER_INTERRUPTED: "usage",
    EventType.MODEL_QUERY: "usage",
    EventType.TOOL_COMPLETED: "invocation_finished",
    EventType.TOOL_INTERRUPTED: "invocation_finished",
    EventType.RUN_COMPLETED: "run_finished",
    EventType.RUN_FAILED: "run_finished",
    EventType.RUN_INTERRUPTED: "run_finished",
    EventType.USER_MESSAGE: "run_requested",
    EventType.RUN_RESUMED: "run_requested",
    EventType.APPROVAL_REQUESTED: "approval_requested",
    EventType.CHILD_SPAWNED: "child_spawned",
    EventType.WINDOW_PROJECTED: "context_projected",
    EventType.WAKEUP_FIRED: "wakeup_fired",
}

FACT_EVENT_TYPES: dict[EventType, ObservedFactKind] = _EVENT_KIND_MAP


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return value


def fact_for_event(ev: StoredEvent, events: Iterable[StoredEvent]) -> tuple[ObservedFactKind, dict[str, object]] | None:
    if ev.event_type == EventType.MODEL_QUERY:
        # A governed side query commits its own dispatch record: there is no
        # separate PROVIDER_INTENT to point at, so the event sequence is the
        # exact dispatch sequence observers can relate to their own windows.
        provider_name = ev.payload.get("provider")
        model_name = ev.payload.get("model")
        if not isinstance(provider_name, str) or not provider_name or not isinstance(model_name, str) or not model_name:
            raise ValueError(f"model query fact at sequence {ev.sequence} lacks an authentic provider/model identity")
        raw_usage = ev.payload.get("usage")
        if raw_usage is not None and not isinstance(raw_usage, Mapping):
            raise ValueError(f"model query fact at sequence {ev.sequence} has a malformed usage record")
        owner = ev.payload.get("owner")
        if owner is not None and (not isinstance(owner, str) or not owner):
            raise ValueError(f"model query fact at sequence {ev.sequence} has a malformed owner")
        ms = ev.payload.get("elapsed_ms")
        elapsed_sec = (ms / 1000.0) if isinstance(ms, (int, float)) and not isinstance(ms, bool) else None
        raw_cost = ev.payload.get("cost_usd")
        cost_usd = float(raw_cost) if isinstance(raw_cost, (int, float)) and not isinstance(raw_cost, bool) else None
        return "usage", {
            "session_id": ev.session_id,
            "run_id": ev.run_id,
            "revision_id": ev.revision_id,
            "logical_turn": str(ev.payload.get("logical_turn", "")),
            "event_sequence": ev.sequence,
            "dispatch_sequence": ev.sequence,
            "provider_name": provider_name,
            "model_name": model_name,
            "usage": raw_usage,
            "input_limit_tokens": None,
            "elapsed_seconds": elapsed_sec,
            "cost_usd": cost_usd,
            "source": "model_query",
            "owner": owner,
        }
    if ev.event_type in (EventType.MODEL_TURN, EventType.PROVIDER_INTERRUPTED):
        d_seq = ev.payload.get("dispatch_sequence")
        disp_seq_val = int(d_seq) if isinstance(d_seq, (int, float)) else None
        if disp_seq_val is None:
            raise ValueError(f"usage fact at sequence {ev.sequence} has no dispatch sequence")
        intent_ev = next(
            (e for e in events if e.sequence == disp_seq_val and e.event_type == EventType.PROVIDER_INTENT),
            None,
        )
        if intent_ev is None:
            raise ValueError(f"usage fact at sequence {ev.sequence} has no provider intent at {disp_seq_val}")
        provider_name = intent_ev.payload.get("provider")
        model_name = intent_ev.payload.get("model")
        logical_turn = intent_ev.payload.get("logical_turn")
        if not isinstance(provider_name, str) or not provider_name or not isinstance(model_name, str) or not model_name:
            raise ValueError(f"provider intent at {disp_seq_val} lacks an authentic provider/model identity")
        allocation = intent_ev.payload.get("projection_allocation")
        raw_limit = allocation.get("limit_tokens") if isinstance(allocation, Mapping) else None
        # The allocation is the sizing decision the request was actually assembled with.
        # Absent, null, or malformed is unknown -- never zero or a guessed window.
        input_limit_tokens = raw_limit if isinstance(raw_limit, int) and not isinstance(raw_limit, bool) and raw_limit > 0 else None
        ms = ev.payload.get("elapsed_ms")
        elapsed_sec = (ms / 1000.0) if ms is not None and isinstance(ms, (int, float)) else None
        usage_payload: Mapping[str, object] | None = None
        cost_usd: float | None = None
        if ev.event_type == EventType.MODEL_TURN:
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
            "input_limit_tokens": input_limit_tokens,
            "elapsed_seconds": elapsed_sec,
            "cost_usd": cost_usd,
        }
    if ev.event_type in (EventType.TOOL_COMPLETED, EventType.TOOL_INTERRUPTED):
        call_dict = _mapping(ev.payload.get("call"), "invocation call")
        res_dict = _mapping(ev.payload.get("result"), "invocation result")
        tool_name = call_dict.get("tool_name")
        if not isinstance(tool_name, str) or not tool_name:
            raise ValueError(f"invocation fact at {ev.sequence} lacks a tool identity")
        if res_dict.get("status") not in ("ok", "error"):
            raise ValueError(f"invocation fact at {ev.sequence} lacks an outcome")
        tool_status = "error" if (ev.event_type == EventType.TOOL_INTERRUPTED or res_dict["status"] == "error") else "ok"
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
    if ev.event_type in (EventType.RUN_COMPLETED, EventType.RUN_FAILED, EventType.RUN_INTERRUPTED):
        if ev.event_type == EventType.RUN_INTERRUPTED:
            stop_cause = ev.payload.get("stop_cause")
            if stop_cause not in get_args(StopCause):
                raise ValueError(f"interrupted run event at {ev.sequence} lacks an authentic stop cause")
        elif ev.event_type == EventType.RUN_FAILED:
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
            "status": "completed"
            if ev.event_type == EventType.RUN_COMPLETED
            else "failed"
            if ev.event_type == EventType.RUN_FAILED
            else "interrupted",
            "stop_cause": stop_cause,
            "elapsed_seconds": elapsed_seconds,
            "budget_exhaustion": ev.payload.get("budget_exhaustion"),
        }
    kind = _EVENT_KIND_MAP.get(ev.event_type)
    if kind is None:
        return None
    common = {
        "session_id": ev.session_id,
        "run_id": ev.run_id,
        "revision_id": ev.revision_id,
        "logical_turn": str(ev.payload.get("logical_turn", "")),
        "event_sequence": ev.sequence,
    }
    match kind:
        case "run_requested":
            return kind, {**common, "resumed": ev.event_type == EventType.RUN_RESUMED}
        case "approval_requested":
            return kind, {**common, "request_id": ev.payload["request_id"]}
        case "child_spawned":
            return kind, {**common, "child_session_id": ev.payload["child_session_id"]}
        case "wakeup_fired":
            return kind, {
                **common,
                "owner": ev.payload["owner"],
                "wakeup_id": ev.payload["wakeup_id"],
                "role": ev.payload["role"],
                "payload": _mapping(ev.payload["payload"], "wakeup payload"),
            }
        case "context_projected":
            return kind, {
                **common,
                "owner": ev.payload["owner"],
                "covered_bytes": ev.payload["covered_bytes"],
                "dropped_bytes": ev.payload["dropped_bytes"],
            }
        case _:
            return None
