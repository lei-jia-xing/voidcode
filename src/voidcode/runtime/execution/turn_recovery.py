from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from ...core.turns import CallSeed, ReportedCall
from ...formats.json_values import json_wire_object
from ...security.redaction import REDACTED_PLACEHOLDER, redact_mapping
from ...tools.contracts import ToolCall
from ..events import REASONING_PERSISTED_LIMIT_CHARS
from ..permissions.policy import PendingApproval, PermissionResolution
from ..question import PendingQuestion
from ..session_metadata_helpers import runtime_state_value


@dataclass(frozen=True, slots=True)
class ApprovedInvocation:
    pending: PendingApproval
    decision: PermissionResolution
    call: ToolCall
    batch: CallSeed
    started_sequence: int


@dataclass(frozen=True, slots=True)
class AnsweredQuestion:
    pending: PendingQuestion
    call: ToolCall
    result: ReportedCall
    batch: CallSeed
    started_sequence: int


@dataclass(frozen=True, slots=True)
class InterruptedTurn:
    batch: CallSeed
    started_sequence: int


type RuntimeContinuation = ApprovedInvocation | AnsweredQuestion | InterruptedTurn


def persisted_turn_batch(
    batch: CallSeed, *, session_id: str, run_id: str, started_sequence: int, completed_call_ids: tuple[str, ...] = ()
) -> dict[str, object]:
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("native batch requires its actual producer run identity")
    if batch.run_step is None or batch.run_step < 1:
        raise ValueError("native batch has no actual positive run step")
    if not isinstance(batch.tool_batch_id, str) or not batch.tool_batch_id.strip():
        raise ValueError("native batch has no persisted model-turn identity")
    ids = tuple(call.tool_call_id for call in batch.calls)
    if not ids or any(not call_id for call_id in ids) or len(set(ids)) != len(ids):
        raise ValueError("native batch requires unique original call identities")
    if completed_call_ids != ids[: len(completed_call_ids)]:
        raise ValueError("native completion identities are not the original batch prefix")
    raw: dict[str, object] = {
        "session_id": session_id,
        "run_id": run_id,
        "started_sequence": started_sequence,
        "run_step": batch.run_step,
        "tool_batch_id": batch.tool_batch_id,
        "calls": [
            {
                "tool_name": call.tool_name,
                "tool_call_id": call.tool_call_id,
                "arguments": json_wire_object(call.arguments),
            }
            for call in batch.calls
        ],
        "reasoning": batch.reasoning,
        "completed_call_ids": list(completed_call_ids),
    }
    safe = redact_mapping(raw)
    if isinstance(safe["reasoning"], str):
        safe["reasoning"] = safe["reasoning"][:REASONING_PERSISTED_LIMIT_CHARS]
    safe["recoverable"] = safe == raw and REDACTED_PLACEHOLDER not in json.dumps(safe, ensure_ascii=True).lower()
    return safe


def validated_composition_turn(metadata: Mapping[str, object], *, session_id: str) -> dict[str, object]:
    raw = runtime_state_value(metadata, "composition_turn")
    if not isinstance(raw, dict) or raw.get("session_id") != session_id:
        raise ValueError("pending turn requires its durable composition identity")
    run_id, scope_id = raw.get("run_id"), raw.get("scope_id")
    if not isinstance(run_id, str) or not run_id.strip() or not isinstance(scope_id, str):
        raise ValueError("persisted composition turn requires actual string identities")
    occurrence = raw.get("occurrence")
    if type(occurrence) is not int or occurrence < 1 or scope_id != f"{session_id}:{run_id}:{occurrence}":
        raise ValueError("persisted composition turn identity is invalid")
    if type(raw.get("completed")) is not bool:
        raise ValueError("persisted composition turn requires its completion state")
    cursor = runtime_state_value(metadata, "composition_turn_occurrence")
    if type(cursor) is not int or occurrence != cursor:
        raise ValueError("persisted composition turn does not match its durable cursor")
    invoked_ids = raw.get("invoked_call_ids")
    if (
        not isinstance(invoked_ids, list)
        or any(not isinstance(call_id, str) or not call_id.strip() for call_id in invoked_ids)
        or len(set(invoked_ids)) != len(invoked_ids)
    ):
        raise ValueError("persisted composition turn requires its unique actual invocation identities")
    return raw


def restored_turn_batch(
    metadata: Mapping[str, object],
    *,
    session_id: str,
    tool_results: Sequence[ReportedCall],
) -> tuple[CallSeed, int]:
    raw = runtime_state_value(metadata, "turn_batch")
    if not isinstance(raw, dict):
        raise ValueError("pending native turn has no durable authentic batch; migrate the legacy checkpoint before resuming")
    expected_keys = {
        "session_id",
        "run_id",
        "started_sequence",
        "run_step",
        "tool_batch_id",
        "calls",
        "reasoning",
        "completed_call_ids",
        "recoverable",
    }
    if set(raw) != expected_keys or raw.get("session_id") != session_id:
        raise ValueError("persisted native batch identity or shape does not match its session")
    run_id = raw.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("persisted native batch requires its actual producer run identity")
    turn = validated_composition_turn(metadata, session_id=session_id)
    if run_id != turn["run_id"]:
        raise ValueError("persisted native batch does not belong to its original composition turn")
    if raw.get("recoverable") is not True or redact_mapping(raw) != raw or REDACTED_PLACEHOLDER in json.dumps(raw, ensure_ascii=True).lower():
        raise ValueError("native batch contains redacted credentials or reasoning and cannot safely resume")
    tool_batch_id = raw.get("tool_batch_id")
    if not isinstance(tool_batch_id, str) or not tool_batch_id.strip():
        raise ValueError("persisted native batch requires its canonical model-turn identity")
    started_sequence = raw.get("started_sequence")
    if not isinstance(started_sequence, int) or isinstance(started_sequence, bool) or started_sequence < 0:
        raise ValueError("persisted native batch started_sequence must be a non-negative integer")
    run_step = raw.get("run_step")
    if not isinstance(run_step, int) or isinstance(run_step, bool) or run_step < 1:
        raise ValueError("persisted native batch run_step must be a positive integer")
    raw_calls = raw.get("calls")
    if not isinstance(raw_calls, list) or not raw_calls:
        raise ValueError("persisted native batch calls must be a non-empty list")
    calls: list[ToolCall] = []
    ids: list[str] = []
    for raw_call in raw_calls:
        if not isinstance(raw_call, dict) or set(raw_call) != {"tool_name", "tool_call_id", "arguments"}:
            raise ValueError("persisted native call has an invalid shape")
        name, call_id, arguments = raw_call["tool_name"], raw_call["tool_call_id"], raw_call["arguments"]
        if not isinstance(name, str) or not name or not isinstance(call_id, str) or not call_id or not isinstance(arguments, dict):
            raise ValueError("persisted native call identity and arguments are invalid")
        calls.append(ToolCall(tool_name=name, tool_call_id=call_id, arguments=dict(arguments)))
        ids.append(call_id)
    if len(set(ids)) != len(ids):
        raise ValueError("persisted native batch contains duplicate call identities")
    recorded_ids = raw.get("completed_call_ids")
    if not isinstance(recorded_ids, list) or recorded_ids != ids[: len(recorded_ids)]:
        raise ValueError("persisted completed call identities must be an exact original batch prefix")
    batch_ids = set(ids)
    if any(report.tool_call_id not in batch_ids for report in tool_results):
        raise ValueError("durable native result does not belong to the recorded batch")
    if any(report.tool_batch_id not in (None, tool_batch_id) for report in tool_results):
        raise ValueError("durable native result belongs to a different model-turn batch")
    matching = tuple(replace(report, tool_batch_id=tool_batch_id) for report in tool_results)
    completed_ids = [report.tool_call_id for report in matching]
    if completed_ids != ids[: len(completed_ids)] or recorded_ids != completed_ids[: len(recorded_ids)]:
        raise ValueError("durable native results contain a gap, duplicate, or mismatched completed call identity")
    reasoning = raw.get("reasoning")
    if reasoning is not None and not isinstance(reasoning, str):
        raise ValueError("persisted native reasoning must be text or null")
    return (
        CallSeed(tuple(calls), reasoning=reasoning, completed_reports=matching, run_step=run_step, tool_batch_id=tool_batch_id),
        started_sequence,
    )
