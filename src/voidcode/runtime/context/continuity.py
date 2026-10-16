from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping

from ...core.transcript import ContextSegment, output_text, project_report
from ...core.turns import ReportedCall
from ..composition import CompositionRef, FrozenComposition
from ..events import EventEnvelope, model_turn_batch_id
from ..session_metadata_helpers import parse_runtime_state_metadata, session_metadata_with_runtime_state_updates

# Recoverable runtime context keys are a subset of the persisted runtime_state
# key-set (RUNTIME_STATE_METADATA_KEYS, contracts.py): context projection
# snapshots are checkpoint-authoritative while the stored row may drift, so
# they are exempted from the resume checkpoint equality check. The key-set
# relationship is documented here instead of being asserted mechanically
# because these keys are deliberately exempted from integrity checking while
# RUNTIME_STATE_METADATA_KEYS describes the full writable key space.
#
# ``reminders`` joins them for the opposite ownership reason: the per-call
# reminder counters are advanced inside the run loop after the iteration
# checkpoint was captured, so a checkpoint taken earlier in the same turn would
# otherwise mismatch the stored row and reject a legitimate resume. The stored
# row is authoritative for these counters (the checkpoint only carries whatever
# the session held when it was written).
_RECOVERABLE_RUNTIME_CONTEXT_KEYS = frozenset({"context_projection", "context_projection_summary", "context_compacted", "reminders"})
# The native call prefix is checkpoint-authoritative; resume validates it against events.
_CHECKPOINT_PROGRESS_RUNTIME_STATE_KEYS = frozenset({"turn_batch", "pending_tool_intent"})
_RECOVERABLE_TOP_LEVEL_CONTEXT_KEYS = frozenset({"context_window"})
_ROW_AUTHORITATIVE_TOP_LEVEL_KEYS = frozenset({"provider_usage"})
# Runtime-owned interaction queue (steer / follow-up) lives in session metadata
# and is delivered at the next provider turn. It is not part of the
# integrity-checked context-continuity truth: a steer enqueued while a session
# waits on approval/question must not poison the approval resume (metadata
# mismatch) and must not be lost — it is preserved from the stored row and
# delivered on the next run after the resolution seals the session.
_INTERACTION_QUEUE_METADATA_KEYS = frozenset({"pending_messages"})
# Phase callbacks advance independently of approval/question checkpoint capture.
# Only these private fields are row-authoritative; all other integrity remains.
_PRIVATE_PHASE_RUNTIME_KEYS = frozenset({"composition_turn_occurrence", "composition_turn"})


def metadata_with_manual_compaction_path(metadata: dict[str, object], events: tuple[EventEnvelope, ...]) -> dict[str, object]:
    """Restore native summary metadata only from the selected event path."""
    metadata = {key: value for key, value in metadata.items() if key != "context_window"}
    runtime_state = session_metadata_with_runtime_state_updates(
        metadata, removed=frozenset({"context_projection", "context_projection_summary", "context_compacted"})
    )
    for event in reversed(events):
        projection = event.payload.get("projection")
        if event.event_type == "runtime.context_compacted" and isinstance(projection, dict) and projection.get("source") == "native_manual":
            runtime_state["context_window"] = dict(event.payload)
            return session_metadata_with_runtime_state_updates(runtime_state, updates={"context_projection": projection})
    state = runtime_state.get("runtime_state")
    if isinstance(state, dict) and not state:
        runtime_state.pop("runtime_state", None)
    return runtime_state


def events_after_manual_compaction(events: tuple[EventEnvelope, ...]) -> tuple[EventEnvelope, ...]:
    """Cut only the native manual summary's prefix on this selected event path."""
    for index in range(len(events) - 1, -1, -1):
        event = events[index]
        projection = event.payload.get("projection")
        if event.event_type == "runtime.context_compacted" and isinstance(projection, Mapping) and projection.get("source") == "native_manual":
            return events[index + 1 :]
    return events


def replayed_conversation_segments_from_events(
    events: tuple[EventEnvelope, ...],
    *,
    reported_call_from_event: Callable[[EventEnvelope], ReportedCall | None],
    current_request_event_id: tuple[str, int] | None = None,
) -> tuple[ContextSegment, ...]:
    """Replay only complete batches whose original membership was persisted."""
    ordered_items: list[ContextSegment | str] = []
    batch_reasoning: dict[str, str | None] = {}
    batch_calls: dict[str, list[tuple[str, str, dict[str, object]]]] = {}
    batch_requests: dict[str, list[str]] = {}
    batch_results: dict[str, dict[str, ReportedCall]] = {}
    invalid_batches: set[str] = set()
    active_batch_id: str | None = None
    for event in events:
        if event.event_type == "runtime.request_received":
            active_batch_id = None
            prompt = event.payload.get("prompt")
            if isinstance(prompt, str) and prompt.strip():
                is_current_request = (event.session_id, event.sequence) == current_request_event_id
                ordered_items.append(
                    ContextSegment(
                        role="user",
                        content=prompt,
                        metadata={
                            "source": "replayed_conversation",
                            "tier": "recent",
                            "kind": "current_user_prompt" if is_current_request else "prior_user_prompt",
                            "sequence": event.sequence,
                        },
                    )
                )
            continue
        if event.event_type == "runtime.user_shell_executed":
            record = event.payload.get("record")
            if isinstance(record, str) and record.strip():
                ordered_items.append(
                    ContextSegment(
                        role="user",
                        content=record,
                        metadata={"source": "replayed_conversation", "tier": "recent", "kind": "prior_user_shell_command"},
                    )
                )
            continue
        if event.event_type == "core.response_ready":
            output = event.payload.get("output_preview")
            if isinstance(output, str) and output.strip():
                ordered_items.append(
                    ContextSegment(
                        role="assistant",
                        content=output,
                        metadata={"source": "replayed_conversation", "tier": "recent", "kind": "prior_assistant_output"},
                    )
                )
            continue
        if event.event_type == "runtime.reasoning_part":
            continue
        if event.event_type == "core.model_turn":
            active_batch_id = model_turn_batch_id(event.session_id, event.sequence)
            raw_calls = event.payload.get("tool_batch_calls")
            calls: list[tuple[str, str, dict[str, object]]] = []
            if raw_calls is not None:
                if not isinstance(raw_calls, list):
                    invalid_batches.add(active_batch_id)
                else:
                    seen_call_ids: set[str] = set()
                    for raw_call in raw_calls:
                        if not isinstance(raw_call, Mapping):
                            invalid_batches.add(active_batch_id)
                            break
                        call_id = raw_call.get("tool_call_id")
                        tool_name = raw_call.get("tool")
                        arguments = raw_call.get("arguments")
                        if (
                            not isinstance(call_id, str)
                            or not call_id.strip()
                            or not isinstance(tool_name, str)
                            or not tool_name.strip()
                            or not isinstance(arguments, Mapping)
                            or call_id in seen_call_ids
                        ):
                            invalid_batches.add(active_batch_id)
                            break
                        seen_call_ids.add(call_id)
                        calls.append((call_id, tool_name, dict(arguments)))
            reasoning = event.payload.get("tool_batch_reasoning")
            if reasoning is not None and not isinstance(reasoning, str):
                invalid_batches.add(active_batch_id)
                reasoning = None
            batch_reasoning[active_batch_id] = reasoning
            batch_calls[active_batch_id] = calls
            batch_requests[active_batch_id] = []
            batch_results[active_batch_id] = {}
            ordered_items.append(active_batch_id)
            continue
        if event.event_type == "core.tool_request_created":
            batch_id = active_batch_id
            if batch_id is None:
                continue
            calls = batch_calls[batch_id]
            requests = batch_requests[batch_id]
            call_index = len(requests)
            call_id = event.payload.get("tool_call_id")
            tool_name = event.payload.get("tool")
            arguments = event.payload.get("arguments")
            if (
                call_index >= len(calls)
                or not isinstance(call_id, str)
                or not isinstance(tool_name, str)
                or not isinstance(arguments, Mapping)
                or calls[call_index] != (call_id, tool_name, dict(arguments))
            ):
                invalid_batches.add(batch_id)
                continue
            requests.append(call_id)
            continue
        if event.event_type != "runtime.tool_completed":
            continue
        report = reported_call_from_event(event)
        if report is None:
            continue
        batch_id = active_batch_id
        if batch_id is None or report.tool_call_id not in batch_requests[batch_id]:
            continue
        if report.tool_call_id in batch_results[batch_id]:
            invalid_batches.add(batch_id)
            continue
        batch_results[batch_id][report.tool_call_id] = report

    segments: list[ContextSegment] = []
    for item in ordered_items:
        if isinstance(item, ContextSegment):
            segments.append(item)
            continue
        calls = batch_calls[item]
        reports = batch_results[item]
        requests = batch_requests[item]
        if item in invalid_batches or not calls or len(requests) != len(calls) or any(call_id not in reports for call_id, _, _ in calls):
            continue
        for call_id, tool_name, arguments in calls:
            segments.append(
                ContextSegment(
                    role="assistant",
                    content=None,
                    tool_call_id=call_id,
                    tool_name=tool_name,
                    tool_arguments=arguments,
                    metadata={"source": "replayed_conversation", "tier": "recent", "kind": "prior_tool_call"},
                    tool_batch_id=item,
                )
            )
        for call_id, tool_name, _arguments in calls:
            report = reports[call_id]
            view = project_report(report)
            bounds = view.output.bounds
            reference = bounds.reference
            metadata: dict[str, object] = {
                "source": "replayed_conversation",
                "tier": "recent",
                "kind": "prior_tool_result",
                "status": view.status,
                "error": view.error,
                "truncated": bounds.truncated,
                "partial": bounds.partial,
                "reference": None if reference is None else reference.uri,
            }
            reasoning = batch_reasoning[item]
            if reasoning:
                metadata["data"] = {"reasoning_content": reasoning}
            segments.append(
                ContextSegment(
                    role="tool",
                    content=output_text(view.output) or "",
                    tool_call_id=call_id,
                    tool_name=tool_name,
                    metadata=metadata,
                    tool_batch_id=item,
                )
            )
    return tuple(segments)


def replayed_conversation_segments_from_segments(
    segments: Iterable[ContextSegment],
) -> tuple[ContextSegment, ...]:
    """Retain provider-context segments belonging to replayed conversation history.

    This is a pure projection of supplied context segments. Runtime request
    boundaries and orchestration remain outside this context module.
    """
    replayed: list[ContextSegment] = []
    for segment in segments:
        metadata = segment.metadata
        if not isinstance(metadata, dict):
            continue
        if metadata.get("source") != "replayed_conversation":
            continue
        content = segment.content
        if content is not None and not isinstance(content, str):
            continue
        role = segment.role
        if role not in {"system", "user", "assistant", "tool"}:
            continue
        replayed.append(
            ContextSegment(
                role=role,
                content=content,
                tool_call_id=segment.tool_call_id,
                tool_name=segment.tool_name,
                tool_arguments=segment.tool_arguments,
                metadata=dict(metadata),
                tool_batch_id=segment.tool_batch_id,
            )
        )
    return tuple(replayed)


def verified_checkpoint_session_metadata(
    *,
    checkpoint_metadata: dict[str, object],
    stored_metadata: dict[str, object],
) -> dict[str, object] | None:
    """Verify a resume checkpoint's metadata against the stored session row.

    The checkpoint metadata is authoritative for resumable context snapshots
    and the atomically advanced native call prefix. Resume validates that prefix
    against the durable event path. Stored metadata may differ in these values,
    provider usage (which may be finalized after the checkpoint), reminder
    counters, and the interaction queue (``pending_messages``); any other delta
    means session truth changed under the checkpoint and resume is rejected
    (returns ``None``). Provider usage and queued messages are merged from the
    row; the queue preserves a pre-seal steer/follow-up, while the usage fields
    preserve the latest measured-or-unreported provider totals.
    """
    if "execution_composition" in checkpoint_metadata:
        return None
    raw_composition = stored_metadata.get("execution_composition")
    if not isinstance(raw_composition, dict):
        return None
    frozen = FrozenComposition.from_payload(raw_composition)
    checkpoint_snapshot = checkpoint_metadata.get("agent_capability_snapshot")
    stored_snapshot = stored_metadata.get("agent_capability_snapshot")
    if not isinstance(checkpoint_snapshot, dict) or not isinstance(stored_snapshot, dict):
        return None
    checkpoint_ref = checkpoint_snapshot.get("composition_ref")
    stored_ref = stored_snapshot.get("composition_ref")
    if not isinstance(checkpoint_ref, dict) or not isinstance(stored_ref, dict):
        return None
    canonical_ref = CompositionRef.model_validate(stored_ref)
    if CompositionRef.model_validate(checkpoint_ref) != canonical_ref:
        return None
    if canonical_ref.binding_id != frozen.binding.binding_id or canonical_ref.plan_id != frozen.plan.plan_id:
        return None
    stored_without_owner = {key: value for key, value in stored_metadata.items() if key != "execution_composition"}
    checkpoint_core = _without_recoverable_context(checkpoint_metadata)
    stored_core = _without_recoverable_context(stored_without_owner)
    checkpoint_core = {key: value for key, value in checkpoint_core.items() if key not in _INTERACTION_QUEUE_METADATA_KEYS}
    stored_core = {key: value for key, value in stored_core.items() if key not in _INTERACTION_QUEUE_METADATA_KEYS}
    if checkpoint_core != stored_core:
        return None
    merged = dict(checkpoint_metadata)
    for key in _ROW_AUTHORITATIVE_TOP_LEVEL_KEYS:
        if key in stored_metadata:
            merged[key] = stored_metadata[key]
        else:
            merged.pop(key, None)
    raw_messages = stored_metadata.get("pending_messages")
    if isinstance(raw_messages, list):
        merged["pending_messages"] = raw_messages
    if "phase_records" in stored_metadata:
        merged["phase_records"] = stored_metadata["phase_records"]
    else:
        merged.pop("phase_records", None)
    state = dict(parse_runtime_state_metadata(merged["runtime_state"])) if "runtime_state" in merged else {}
    stored_state = parse_runtime_state_metadata(stored_metadata["runtime_state"]) if "runtime_state" in stored_metadata else None
    if stored_state is not None:
        if "composition_turn_occurrence" in stored_state:
            state["composition_turn_occurrence"] = stored_state["composition_turn_occurrence"]
        else:
            state.pop("composition_turn_occurrence", None)
        if "composition_turn" in stored_state:
            state["composition_turn"] = stored_state["composition_turn"]
        else:
            state.pop("composition_turn", None)
    else:
        state.pop("composition_turn_occurrence", None)
        state.pop("composition_turn", None)
    if state:
        merged["runtime_state"] = state
    else:
        merged.pop("runtime_state", None)
    return merged


def _without_recoverable_context(metadata: dict[str, object]) -> dict[str, object]:
    stripped = {
        key: value
        for key, value in metadata.items()
        if key not in _RECOVERABLE_TOP_LEVEL_CONTEXT_KEYS | _ROW_AUTHORITATIVE_TOP_LEVEL_KEYS | {"phase_records"}
    }
    runtime_state = stripped.get("runtime_state")
    if isinstance(runtime_state, dict):
        runtime_payload = {
            key: value
            for key, value in parse_runtime_state_metadata(runtime_state).items()
            if key not in _RECOVERABLE_RUNTIME_CONTEXT_KEYS | _CHECKPOINT_PROGRESS_RUNTIME_STATE_KEYS | _PRIVATE_PHASE_RUNTIME_KEYS
        }
        if runtime_payload:
            stripped["runtime_state"] = runtime_payload
        else:
            stripped.pop("runtime_state", None)
    return stripped
