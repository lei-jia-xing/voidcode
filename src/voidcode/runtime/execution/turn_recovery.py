from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from ...core.engine import CallSeed, EngineState
from ...security.redaction import REDACTED_PLACEHOLDER, redact_mapping
from ...tools.contracts import ToolCall, ToolResult
from ..events import REASONING_PERSISTED_LIMIT_CHARS
from ..permission import PendingApproval, PermissionResolution
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
    result: ToolResult
    batch: CallSeed
    started_sequence: int


@dataclass(frozen=True, slots=True)
class InterruptedTurn:
    batch: CallSeed
    started_sequence: int


type RuntimeContinuation = ApprovedInvocation | AnsweredQuestion | InterruptedTurn


def persisted_turn_batch(state: EngineState, *, session_id: str, started_sequence: int) -> dict[str, object]:
    batch = state.batches[-1]
    raw: dict[str, object] = {
        "session_id": session_id,
        "run_id": state.request.run_id,
        "started_sequence": started_sequence,
        "run_step": batch.run_step,
        "calls": [{"tool_name": call.tool_name, "tool_call_id": call.tool_call_id, "arguments": dict(call.arguments)} for call in batch.calls],
        "reasoning": batch.reasoning,
        "completed_call_ids": [result.data["tool_call_id"] for result in batch.results],
    }
    safe = redact_mapping(raw)
    if isinstance(safe["reasoning"], str):
        safe["reasoning"] = safe["reasoning"][:REASONING_PERSISTED_LIMIT_CHARS]
    safe["recoverable"] = safe == raw and REDACTED_PLACEHOLDER not in json.dumps(safe, ensure_ascii=True).lower()
    return safe


def restored_turn_batch(
    metadata: Mapping[str, object],
    *,
    session_id: str,
    tool_results: Sequence[ToolResult],
) -> tuple[CallSeed, int]:
    raw = runtime_state_value(metadata, "turn_batch")
    if not isinstance(raw, dict):
        raise ValueError("pending native turn has no durable authentic batch; migrate the legacy checkpoint before resuming")
    expected_keys = {"session_id", "run_id", "started_sequence", "run_step", "calls", "reasoning", "completed_call_ids", "recoverable"}
    if set(raw) != expected_keys or raw.get("session_id") != session_id:
        raise ValueError("persisted native batch identity or shape does not match its session")
    if raw.get("recoverable") is not True or redact_mapping(raw) != raw or REDACTED_PLACEHOLDER in json.dumps(raw, ensure_ascii=True).lower():
        raise ValueError("native batch contains redacted credentials or reasoning and cannot safely resume")
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
    if any(result.data.get("tool_call_id") not in batch_ids for result in tool_results):
        raise ValueError("durable native result does not belong to the recorded batch")
    matching = tuple(tool_results)
    completed_ids = [result.data.get("tool_call_id") for result in matching]
    if completed_ids != ids[: len(completed_ids)] or recorded_ids != completed_ids[: len(recorded_ids)]:
        raise ValueError("durable native results contain a gap, duplicate, or mismatched completed call identity")
    reasoning = raw.get("reasoning")
    if reasoning is not None and not isinstance(reasoning, str):
        raise ValueError("persisted native reasoning must be text or null")
    return CallSeed(tuple(calls), reasoning=reasoning, completed_results=matching, run_step=run_step), started_sequence
