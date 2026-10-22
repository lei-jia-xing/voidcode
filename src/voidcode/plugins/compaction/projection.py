"""Deterministic continuity projection and the compaction budget math.

Ported from legacy ``runtime/context/window.py`` (the continuity projection, the
threshold/reserve resolution and the summary lane's constants) so this plugin stands on
its own once the legacy tree is deleted. This module only *plans*: the core re-verifies
the boundary and measures the accounting of every declared projection
(``services.project_window``), so nothing here is trusted about coverage.

Rulers: a token number is either provider-reported (a dispatch's usage record, a usage
fact) or a legacy constant. Sizes this module compares against each other are measured on
the core's own ruler -- the canonical JSON bytes of a committed event payload -- never a
UTF-8-bytes/4 token estimate, which the plugin has no way to make truthful.
"""

from __future__ import annotations

import hashlib
import json
from bisect import bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from voidcode.sdk.codec import decode_provider_result, decode_tool_call, decode_tool_result
from voidcode.sdk.json_values import json_wire_object
from voidcode.sdk.services import EventView
from voidcode.sdk.tools import TextOutput, ToolFailure, ToolOutput, ToolResult

# --- Legacy constants (``runtime/context/window.py``) ---

#: Chars of the first covered user message that become the projection's objective.
_CONTINUITY_OBJECTIVE_PREVIEW_CHARS = 160
#: Bounded item/char caps of every preview list the deterministic projection carries.
_COMPACTION_PREVIEW_ITEM_LIMIT = 8
_COMPACTION_PREVIEW_CHAR_LIMIT = 240
#: The output reserve is 15% of the window (``_TOKEN_RESERVE_*``), with a fixed floor.
_TOKEN_RESERVE_NUMERATOR = 15
_TOKEN_RESERVE_DENOMINATOR = 100
DEFAULT_CONTEXT_RESERVE_TOKENS = 16_384
#: Legacy ``DEFAULT_KEEP_RECENT_TOOL_TOKENS``: how much recent content stays verbatim.
DEFAULT_KEEP_RECENT_TOKENS = 20_000
#: The provider-facing prefix and the anchor that identifies one projection's text.
PROJECTION_PREFIX = "Runtime context projection:"
CONTINUITY_ANCHOR_PREFIX = "continuity:"
DROPPED_PREVIEW_TITLE = "## Dropped Tool Preview"

#: The opt-in model summary (legacy ``service.py:_CONTINUITY_SUMMARY_SYSTEM_PROMPT``), verbatim.
CONTINUITY_SUMMARY_SYSTEM_PROMPT = (
    "You compress conversation history that is about to be discarded from a coding agent's context window.\n"
    "Summarize the work: the objective, user constraints that must survive verbatim, what was completed, "
    "decisions taken, files/commands/errors that matter, verification state, and open blockers.\n"
    "Be specific and factual; never invent progress, files or results that the transcript does not show.\n"
    "Output the summary text only -- no preamble, no commentary, no markdown fences."
)
#: Chars of covered material handed to the model, per item and in total (legacy caps).
_CONTINUITY_SUMMARY_RESULT_CHARS = 4_000
_CONTINUITY_SUMMARY_INPUT_CHARS = 60_000
#: Chars kept from the answer: longer than this is not a summary.
CONTINUITY_SUMMARY_OUTPUT_CHARS = 8_000
#: ``services.model_query`` accepts one prompt of at most 64 KiB (core-enforced).
MODEL_QUERY_MAX_BYTES = 64 * 1024

#: Facts from committed events: one dispatch, one provider turn, one tool batch member.
MODEL_TURN_EVENT = "kernel.model_turn"
PROVIDER_INTENT_EVENT = "kernel.provider_intent"
PROVIDER_INTERRUPTED_EVENT = "kernel.provider_interrupted"
USER_MESSAGE_EVENT = "kernel.user_message"
TOOL_RESULT_EVENTS: frozenset[str] = frozenset({"kernel.tool_completed", "kernel.tool_interrupted"})
#: Events the core renders into the provider window (``engine._messages``). Only these make a
#: prefix worth covering, so this plugin's own bookkeeping can never justify another projection.
PROVIDER_VISIBLE_EVENTS: frozenset[str] = frozenset(
    {USER_MESSAGE_EVENT, "kernel.continuation_started", "kernel.wakeup_fired", MODEL_TURN_EVENT} | TOOL_RESULT_EVENTS
)

SummaryKind = Literal["deterministic", "model"]


# --- Budget: the legacy threshold/reserve resolution ---


def _coerce_threshold_int(value: int | None) -> int | None:
    if value is None or isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _reserve_floor(floor: int) -> int:
    return floor if isinstance(floor, int) and not isinstance(floor, bool) and floor >= 0 else 0


def effective_reserve_tokens(context_window: int | None, floor: int = 0) -> int:
    """15% output reserve over the catalog window; None/degenerate -> floor, never raises."""
    safe_floor = _reserve_floor(floor)
    if context_window is None or isinstance(context_window, bool) or not isinstance(context_window, int) or context_window <= 0:
        return safe_floor
    return max(safe_floor, (context_window * _TOKEN_RESERVE_NUMERATOR) // _TOKEN_RESERVE_DENOMINATOR)


def resolve_budget_reserve_tokens(
    context_window: int | None,
    *,
    reserve_tokens: int | None = None,
    floor: int = DEFAULT_CONTEXT_RESERVE_TOKENS,
) -> int:
    """Budget reserve with the small-window recovery for a *defaulted* floor.

    A defaulted reserve that is impossible for the window (``reserve >= cw - 15%*cw`` or
    ``reserve >= cw``) falls back to the 15% proportional reserve so the derived threshold
    stays usable. An explicit ``reserve_tokens`` always wins, provenance being the argument
    being ``None`` rather than a value comparison.
    """
    if reserve_tokens is not None and isinstance(reserve_tokens, int) and not isinstance(reserve_tokens, bool) and reserve_tokens >= 0:
        return reserve_tokens
    reserve = effective_reserve_tokens(context_window, floor)
    if context_window is None or isinstance(context_window, bool) or not isinstance(context_window, int) or context_window <= 0:
        return reserve
    proportional = max(1, (context_window * _TOKEN_RESERVE_NUMERATOR) // _TOKEN_RESERVE_DENOMINATOR)
    if reserve >= context_window - proportional or reserve >= context_window:
        return proportional
    return reserve


def _clamp_threshold(value: int, context_window: int) -> int:
    upper = max(1, context_window - 1)
    return max(1, min(value, upper))


def resolve_threshold_tokens(
    context_window: int | None,
    *,
    threshold_tokens: int | None = None,
    reserve_tokens: int | None = None,
    floor: int = DEFAULT_CONTEXT_RESERVE_TOKENS,
) -> int:
    """An explicit fixed threshold (clamped ``[1, cw-1]``) beats the derived one.

    An explicit ``threshold_tokens`` is authoritative even without a catalog window: only
    the reserve-derived threshold needs one. No window and no explicit threshold leaves
    nothing to size a threshold with, which resolves to ``0`` ("unsized").
    """
    fixed = _coerce_threshold_int(threshold_tokens)
    if context_window is None or isinstance(context_window, bool) or not isinstance(context_window, int) or context_window <= 0:
        return max(1, fixed) if fixed is not None and fixed > 0 else 0
    if fixed is not None:
        return _clamp_threshold(fixed, context_window)
    reserve = resolve_budget_reserve_tokens(context_window, reserve_tokens=reserve_tokens, floor=floor)
    return max(1, min(context_window - 1, context_window - reserve))


@dataclass(frozen=True, slots=True)
class CompactionBudget:
    """The resolved compaction gate for one observation."""

    threshold_tokens: int
    reserve_tokens: int
    #: Measured usage must exceed this. ``0`` means the gate has nothing to size it with.
    trigger_tokens: int
    explicit_threshold: bool

    def as_payload(self) -> dict[str, object]:
        return {
            "threshold_tokens": self.threshold_tokens,
            "reserve_tokens": self.reserve_tokens,
            "trigger_tokens": self.trigger_tokens,
            "explicit_threshold": self.explicit_threshold,
        }


def resolve_compaction_budget(
    context_window: int | None,
    *,
    threshold_tokens: int | None = None,
    reserve_tokens: int | None = None,
    floor: int = DEFAULT_CONTEXT_RESERVE_TOKENS,
) -> CompactionBudget:
    """Resolve the trigger the measured dispatch usage is compared against.

    An explicit ``threshold_tokens`` is the budget the window must stay under; the reserve it
    keeps free for the answer is taken out of it (after the legacy clamp), so the trigger is
    ``threshold - reserve``. Without an explicit threshold the derived legacy value *is* the
    threshold (``context_window - reserve``, the reserve already taken out), so it is used
    as-is rather than double-subtracted.
    """
    threshold = resolve_threshold_tokens(
        context_window,
        threshold_tokens=threshold_tokens,
        reserve_tokens=reserve_tokens,
        floor=floor,
    )
    reserve = resolve_budget_reserve_tokens(context_window, reserve_tokens=reserve_tokens, floor=floor)
    fixed = _coerce_threshold_int(threshold_tokens)
    explicit = fixed is not None and fixed > 0
    if threshold <= 0:
        return CompactionBudget(threshold_tokens=0, reserve_tokens=reserve, trigger_tokens=0, explicit_threshold=explicit)
    trigger = max(1, threshold - reserve) if explicit else threshold
    return CompactionBudget(threshold_tokens=threshold, reserve_tokens=reserve, trigger_tokens=trigger, explicit_threshold=explicit)


def should_compact(measured_tokens: int | None, budget: CompactionBudget) -> bool:
    """True when a measured dispatch crossed the trigger; unknown usage never compacts."""
    if measured_tokens is None or budget.trigger_tokens <= 0:
        return False
    return measured_tokens > budget.trigger_tokens


# --- Measurement: the core's own ruler over committed events ---


def event_payload_bytes(event: EventView) -> int:
    """Canonical JSON bytes of one event payload, the ruler the core covers in."""
    payload = event.payload
    if not isinstance(payload, Mapping):
        return 0
    return len(
        json.dumps(
            json_wire_object(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    )


def _completed_turn_usage(event: EventView) -> tuple[int | None, int | None]:
    """``(input_tokens, dispatch_sequence)`` of one completed provider turn."""
    payload = event.payload
    if not isinstance(payload, Mapping):
        return None, None
    raw_dispatch = payload.get("dispatch_sequence")
    dispatch = int(raw_dispatch) if isinstance(raw_dispatch, (int, float)) and not isinstance(raw_dispatch, bool) else None
    raw = payload.get("result")
    if not isinstance(raw, Mapping):
        return None, dispatch
    try:
        turn = decode_provider_result(raw)
    except Exception:  # noqa: BLE001 - a payload this plugin cannot read is simply not a measurement
        return None, dispatch
    usage = turn.usage
    return (None if usage is None else usage.input_tokens), dispatch


def measured_dispatch_input_tokens(events: Sequence[EventView]) -> int | None:
    """Provider-reported prompt tokens of the newest dispatch in this window, if reported.

    The newest dispatch decides: an interrupted or unreadable turn leaves the value unknown
    rather than reporting an older dispatch's numbers as the current window size.
    """
    for event in reversed(events):
        if event.event_type == PROVIDER_INTERRUPTED_EVENT:
            return None
        if event.event_type == MODEL_TURN_EVENT:
            tokens, _dispatch = _completed_turn_usage(event)
            return tokens
    return None


def _request_window_characters(events: Sequence[EventView], dispatch_sequence: int | None) -> int | None:
    """Measured characters of the request one dispatch sent (core ``request_window``)."""
    if dispatch_sequence is None:
        return None
    for event in events:
        if event.sequence != dispatch_sequence or event.event_type != PROVIDER_INTENT_EVENT:
            continue
        payload = event.payload
        if not isinstance(payload, Mapping):
            return None
        window = payload.get("request_window")
        if not isinstance(window, Mapping):
            return None
        messages = window.get("message_characters")
        schemas = window.get("tool_schema_characters")
        characters = 0
        for value in (messages, schemas):
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                characters += value
        return characters or None
    return None


def measured_tokens_per_character(events: Sequence[EventView]) -> float | None:
    """Prompt tokens the provider charged per measured request character, newest dispatch.

    This session's own report is the only honest conversion this plugin has between the
    legacy token knobs and the character-sized content it can actually measure.
    """
    for event in reversed(events):
        if event.event_type != MODEL_TURN_EVENT:
            continue
        tokens, dispatch = _completed_turn_usage(event)
        characters = _request_window_characters(events, dispatch)
        if tokens is None or tokens <= 0 or characters is None:
            return None
        return tokens / characters
    return None


def recent_tail_character_budget(events: Sequence[EventView], *, keep_recent_tokens: int, reached_end: bool) -> int:
    """Characters the raw tail may keep, at the rate this session was actually charged.

    ``0`` retains nothing, which covers the prefix as far as it can be covered. That is the
    answer when there is no measured rate to convert with, and also when the read window does
    not reach the session end: everything this plugin can see is then older than the tail the
    retention is meant to protect.
    """
    if keep_recent_tokens <= 0 or not reached_end:
        return 0
    rate = measured_tokens_per_character(events)
    if rate is None or rate <= 0:
        return 0
    return int(keep_recent_tokens / rate)


# --- Committed tool results, as the projection reads them ---


@dataclass(frozen=True, slots=True)
class ToolResultFact:
    """One committed tool result in provider-visible form."""

    sequence: int
    tool_name: str
    status: Literal["ok", "error"]
    arguments: Mapping[str, object]
    #: Result output as the provider saw it, and the failure reason when there was one.
    content: str
    error: str | None = None
    artifact_id: str | None = None
    artifact_byte_count: int | None = None
    artifact_line_count: int | None = None

    @property
    def text(self) -> str:
        """Provider-visible body (legacy ``result_payload_text``): output, then error."""
        return f"{self.content}\n{self.error}" if self.error else self.content

    def argument_string(self, key: str) -> str | None:
        """A non-empty string argument, or None: the preview labels only real values."""
        value = self.arguments.get(key)
        return value if isinstance(value, str) and value else None

    def preview(self, *, max_preview_chars: int = _COMPACTION_PREVIEW_CHAR_LIMIT) -> str:
        """The legacy one-line preview: identity, refs, then a clipped content/error excerpt."""
        parts = [self.tool_name, self.status]
        if self.artifact_id is not None:
            parts.extend((f"artifact_id={self.artifact_id}", f"uri=voidcode://artifact/{self.artifact_id}"))
            if self.artifact_byte_count is not None:
                parts.append(f"byte_count={self.artifact_byte_count}")
            if self.artifact_line_count is not None:
                parts.append(f"line_count={self.artifact_line_count}")
            return " ".join(parts)
        for key in ("path", "pattern", "command"):
            value = self.argument_string(key)
            if value is not None:
                parts.append(f"{key}={value}")
        preview_source = self.content or (self.error or "")
        if preview_source:
            clipped = preview_source[:max_preview_chars]
            if len(preview_source) > max_preview_chars:
                clipped = f"{clipped}..."
            parts.append(f'{"content_preview" if self.content else "error_preview"}="{clipped}"')
        return " ".join(parts)


def _output_text(output: ToolOutput) -> str:
    return output.text if isinstance(output, TextOutput) else (output.presentation or "")


def _artifact_metadata(result: ToolResult, key: str) -> object:
    reference = result.output.bounds.reference
    metadata = reference.artifact if reference is not None else None
    if not isinstance(metadata, Mapping):
        return None
    artifact = metadata.get("artifact")
    if isinstance(artifact, Mapping) and artifact.get(key) is not None:
        return artifact[key]
    return metadata.get(key)


def _artifact_string(result: ToolResult, key: str) -> str | None:
    value = _artifact_metadata(result, key)
    return value if isinstance(value, str) and value else None


def _artifact_int(result: ToolResult, key: str) -> int | None:
    value = _artifact_metadata(result, key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _result_error(result: ToolResult) -> str | None:
    """The failure reason as the provider saw it, or None for a successful result."""
    if not isinstance(result, ToolFailure):
        return None
    error = result.error.strip()
    return error or None


def tool_result_fact(event: EventView) -> ToolResultFact | None:
    """Decode one tool-result event; ``None`` when its payload cannot be read truthfully."""
    payload = event.payload
    if not isinstance(payload, Mapping):
        return None
    raw_call = payload.get("call")
    raw_result = payload.get("result")
    if not isinstance(raw_result, Mapping):
        return None
    try:
        result = decode_tool_result(raw_result)
        call = decode_tool_call(raw_call) if isinstance(raw_call, Mapping) else None
    except Exception:  # noqa: BLE001 - an unreadable payload is never reported as a tool result
        return None
    tool_name = result.tool_name or (call.tool_name if call is not None else "")
    if not tool_name:
        return None
    return ToolResultFact(
        sequence=event.sequence,
        tool_name=tool_name,
        # An interrupted invocation never completed, so its outcome is an error.
        status="ok" if event.event_type == "kernel.tool_completed" and not isinstance(result, ToolFailure) else "error",
        arguments=call.arguments if call is not None else {},
        content=_output_text(result.output),
        error=_result_error(result),
        artifact_id=_artifact_string(result, "artifact_id"),
        artifact_byte_count=_artifact_int(result, "byte_count") or _artifact_int(result, "original_byte_count"),
        artifact_line_count=_artifact_int(result, "line_count") or _artifact_int(result, "original_line_count"),
    )


def _model_turn_output(event: EventView) -> str | None:
    payload = event.payload
    if not isinstance(payload, Mapping):
        return None
    raw = payload.get("result")
    if not isinstance(raw, Mapping):
        return None
    try:
        turn = decode_provider_result(raw)
    except Exception:  # noqa: BLE001 - unreadable turns contribute no summary material
        return None
    return turn.output or None


def _user_message_text(event: EventView) -> str | None:
    payload = event.payload
    if not isinstance(payload, Mapping):
        return None
    content = payload.get("content")
    return content if isinstance(content, str) and content.strip() else None


# --- The covered prefix: boundary, accounting, and the replacement it earns ---


@dataclass(frozen=True, slots=True)
class ProjectionPlan:
    """One coverable prefix and everything the replacement text is built from."""

    covered_from_sequence: int
    covered_to_sequence: int
    covered_event_count: int
    covered_bytes: int
    covered_tool_results: tuple[ToolResultFact, ...]
    retained_tool_result_count: int
    objective: str | None
    constraints: tuple[str, ...]
    progress: tuple[str, ...]
    blockers: tuple[str, ...]
    references: tuple[str, ...]
    delegated: tuple[str, ...]
    summary_material: tuple[str, ...]


def _balanced_boundaries(events: Sequence[EventView]) -> tuple[int, ...]:
    """Sequences where the prefix up to them holds no half-finished native tool batch.

    The core verifies exactly this on the real payloads (an assistant tool call and its
    report are covered together or not at all), so the plugin walks the same rule and stops
    only on a boundary the core will accept.
    """
    boundaries: list[int] = []
    calls = 0
    results = 0
    for event in events:
        if event.event_type == MODEL_TURN_EVENT:
            payload = event.payload
            raw = payload.get("result") if isinstance(payload, Mapping) else None
            if isinstance(raw, Mapping):
                try:
                    calls += len(decode_provider_result(raw).tool_calls)
                except Exception:  # noqa: BLE001 - an unreadable turn cannot be certified as a boundary
                    return tuple(boundaries)
        elif event.event_type in TOOL_RESULT_EVENTS:
            results += 1
            if tool_result_fact(event) is None:
                return tuple(boundaries)
        if calls == results:
            boundaries.append(event.sequence)
    return tuple(boundaries)


def _readable_prefix(events: Sequence[EventView]) -> tuple[EventView, ...]:
    """Events up to the first whose payload ``read_events`` truncated.

    A truncated payload is an event this plugin cannot certify: the core would count tool
    calls from the full payload, so covering blind is exactly how a boundary stops being one.
    """
    readable: list[EventView] = []
    for event in events:
        if event.payload_truncated or not isinstance(event.payload, Mapping):
            break
        readable.append(event)
    return tuple(readable)


def _line_preview(value: str, *, limit: int) -> str:
    collapsed = " ".join(part.strip() for part in value.splitlines() if part.strip())
    if len(collapsed) <= limit:
        return collapsed
    return f"{collapsed[:limit]}..."


_LEGACY_CONSTRAINT_MARKERS = ("must", "must not", "never", "always", "do not", "don't", "forbidden")


def _constraint_lines(prompt: str) -> tuple[str, ...]:
    constraints: list[str] = []
    for raw_line in prompt.splitlines():
        line = raw_line.strip(" -\t")
        lowered = line.lower()
        if line and any(marker in lowered for marker in _LEGACY_CONSTRAINT_MARKERS):
            constraints.append(line)
    return tuple(constraints[:8])


def _merge_unique_strings(*groups: tuple[str, ...], limit: int = 12) -> tuple[str, ...]:
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for value in group:
            stripped = value.strip()
            if not stripped or stripped in seen:
                continue
            seen.add(stripped)
            merged.append(stripped)
            if len(merged) >= limit:
                return tuple(merged)
    return tuple(merged)


def _facts_from_tool_results(
    results: Sequence[ToolResultFact],
    *,
    preview_item_limit: int,
    preview_char_limit: int,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    progress: list[str] = []
    blockers: list[str] = []
    refs: list[str] = []
    delegated: list[str] = []
    for result in results[:preview_item_limit]:
        preview = result.preview(max_preview_chars=preview_char_limit)
        if result.status == "ok":
            progress.append(f"Tool result compacted: {preview}")
        else:
            blockers.append(f"Tool error compacted: {preview}")
        path = result.argument_string("path")
        command = result.argument_string("command")
        if path is not None:
            refs.append(f"file:{path}")
        if command is not None:
            refs.append(f"command:{command}")
        if result.tool_name in {"task", "background_task"}:
            delegated.append(preview)
    return tuple(progress), tuple(blockers), tuple(refs), tuple(delegated)


def _summary_material(events: Sequence[EventView]) -> tuple[str, ...]:
    """Bounded, oldest-first transcript of the covered prefix for the model lane.

    Each item is previewed under its own cap so one oversized payload cannot crowd out the
    rest, and the running total stops the list (legacy ``_continuity_summary_input``).
    """
    parts: list[str] = []
    used = 0
    for index, event in enumerate(events):
        if event.event_type in TOOL_RESULT_EVENTS:
            fact = tool_result_fact(event)
            if fact is None:
                continue
            body = f"[{index}] tool={fact.tool_name} status={fact.status}\n{fact.text[:_CONTINUITY_SUMMARY_RESULT_CHARS]}"
            used += min(len(fact.text), _CONTINUITY_SUMMARY_RESULT_CHARS) + _CONTINUITY_SUMMARY_RESULT_CHARS // 2
        elif event.event_type == USER_MESSAGE_EVENT:
            text = _user_message_text(event)
            if text is None:
                continue
            body = f"[{index}] user\n{text[:_CONTINUITY_SUMMARY_RESULT_CHARS]}"
            used += min(len(text), _CONTINUITY_SUMMARY_RESULT_CHARS) + _CONTINUITY_SUMMARY_RESULT_CHARS // 2
        elif event.event_type == MODEL_TURN_EVENT:
            output = _model_turn_output(event)
            if output is None:
                continue
            body = f"[{index}] assistant\n{output[:_CONTINUITY_SUMMARY_RESULT_CHARS]}"
            used += min(len(output), _CONTINUITY_SUMMARY_RESULT_CHARS) + _CONTINUITY_SUMMARY_RESULT_CHARS // 2
        else:
            continue
        parts.append(body)
        if used >= _CONTINUITY_SUMMARY_INPUT_CHARS:
            break
    return tuple(parts)


def plan_projection(
    events: Sequence[EventView],
    *,
    previous_covered_to_sequence: int | None,
    keep_recent_tokens: int,
    reached_end: bool,
) -> ProjectionPlan | None:
    """Plan the prefix a projection would cover, or ``None`` when there is nothing new to cover.

    Coverage starts at the session's first event (the core refuses anything else) and ends
    at the last batch-closing boundary at or before the retention cut. The cut leaves the
    newest ``keep_recent_tokens`` worth of content raw, converted through this session's own
    measured characters-per-token rate; with no usable rate the cut is the newest boundary,
    so the prefix is covered as far as it can be. Extending an existing projection also
    requires newly covering at least one provider-visible event.
    """
    readable = _readable_prefix(events)
    if not readable:
        return None
    boundaries = _balanced_boundaries(readable)
    if not boundaries:
        return None
    last_boundary = boundaries[-1]

    cut_sequence = readable[-1].sequence + 1
    if reached_end:
        budget = recent_tail_character_budget(events, keep_recent_tokens=keep_recent_tokens, reached_end=reached_end)
        if budget > 0:
            # An event is atomic: a positive budget always retains the newest one, and older
            # events join it only while the budget covers their measured bytes.
            cut_sequence = readable[-1].sequence
            spent = 0
            for event in reversed(readable):
                spent += event_payload_bytes(event)
                if spent > budget:
                    break
                cut_sequence = event.sequence
    candidates = [
        sequence
        for sequence in boundaries
        if sequence < cut_sequence and (previous_covered_to_sequence is None or sequence > previous_covered_to_sequence)
    ]
    if previous_covered_to_sequence is not None:
        # A re-projection must newly cover something the provider would actually see. Without
        # this, a projection's own committed events would justify the next one forever.
        visible = [event.sequence for event in readable if event.event_type in PROVIDER_VISIBLE_EVENTS]
        candidates = [sequence for sequence in candidates if bisect_right(visible, sequence) > bisect_right(visible, previous_covered_to_sequence)]
    if not candidates:
        return None
    covered_to_sequence = candidates[-1]
    if covered_to_sequence > last_boundary:
        return None

    covered = tuple(event for event in readable if event.sequence <= covered_to_sequence)
    covered_tool_results = tuple(
        fact for fact in (tool_result_fact(event) for event in covered if event.event_type in TOOL_RESULT_EVENTS) if fact is not None
    )
    retained_tool_results = tuple(
        fact
        for fact in (tool_result_fact(event) for event in readable if event.sequence > covered_to_sequence and event.event_type in TOOL_RESULT_EVENTS)
        if fact is not None
    )
    user_messages = tuple(
        text for text in (_user_message_text(event) for event in covered if event.event_type == USER_MESSAGE_EVENT) if text is not None
    )
    objective = _line_preview(user_messages[0], limit=_CONTINUITY_OBJECTIVE_PREVIEW_CHARS) if user_messages else None
    constraints = _merge_unique_strings(*(_constraint_lines(message) for message in user_messages), limit=12)
    progress, blockers, refs, delegated = _facts_from_tool_results(
        covered_tool_results,
        preview_item_limit=_COMPACTION_PREVIEW_ITEM_LIMIT,
        preview_char_limit=_COMPACTION_PREVIEW_CHAR_LIMIT,
    )
    return ProjectionPlan(
        covered_from_sequence=readable[0].sequence,
        covered_to_sequence=covered_to_sequence,
        covered_event_count=len(covered),
        covered_bytes=sum(event_payload_bytes(event) for event in covered),
        covered_tool_results=covered_tool_results,
        retained_tool_result_count=len(retained_tool_results),
        objective=objective,
        constraints=constraints,
        progress=tuple(_merge_unique_strings(progress, limit=16)),
        blockers=tuple(_merge_unique_strings(blockers, limit=12)),
        references=tuple(_merge_unique_strings(refs, limit=16)),
        delegated=tuple(_merge_unique_strings(delegated, limit=12)),
        summary_material=_summary_material(covered),
    )


def continuity_sections(plan: ProjectionPlan) -> tuple[str, ...]:
    """The legacy section rendering (``_continuity_summary_text``), minus an empty section."""
    sections: list[str] = []

    def add_section(title: str, values: tuple[str, ...] | str | None) -> None:
        if isinstance(values, str):
            value = values.strip()
            if value:
                sections.append(f"## {title}\n{value}")
            return
        if not values:
            return
        lines = "\n".join(f"- {value}" for value in values if value.strip())
        if lines:
            sections.append(f"## {title}\n{lines}")

    add_section("Objective", plan.objective)
    add_section("Constraints", plan.constraints)
    add_section("Progress Completed", plan.progress)
    add_section("Blockers / Open Questions", plan.blockers)
    add_section("Relevant Files / Commands / Errors", plan.references)
    add_section("Delegated / Background Tasks", plan.delegated)
    return tuple(sections)


def dropped_tool_preview(plan: ProjectionPlan) -> tuple[str, ...]:
    """The legacy ``## Dropped Tool Preview`` body: counted previews, capped items, remainder."""
    previewable = tuple(plan.covered_tool_results)
    lines = [f"Compacted {len(plan.covered_tool_results)} earlier tool results:"]
    for index, fact in enumerate(previewable[:_COMPACTION_PREVIEW_ITEM_LIMIT], start=1):
        lines.append(f"{index}. {fact.preview()}")
    remaining = len(previewable) - min(_COMPACTION_PREVIEW_ITEM_LIMIT, len(previewable))
    if remaining > 0:
        lines.append(f"... and {remaining} more")
    return tuple(lines)


def continuity_anchor(*, summary_text: str, dropped_count: int, retained_count: int) -> str:
    """Legacy anchor: the sha256 of the counts and the exact summary text, 16 hex chars."""
    digest = hashlib.sha256(f"{dropped_count}:{retained_count}:{summary_text}".encode()).hexdigest()[:16]
    return f"{CONTINUITY_ANCHOR_PREFIX}{digest}"


def body_text(plan: ProjectionPlan) -> str:
    """The section text an anchor is taken over (legacy ``state.summary_text``)."""
    return "\n\n".join(continuity_sections(plan))


def render_deterministic_replacement(plan: ProjectionPlan) -> str:
    """The legacy deterministic projection, labelled and anchored for the provider.

    Legacy dropped the whole continuity text when nothing was pruned; here the covered prefix
    always exists, so the sections stand on their own and only the dropped-tool preview (which
    has nothing to list) is left out.
    """
    summary = body_text(plan)
    text = f"{PROJECTION_PREFIX}\n{summary}" if summary else PROJECTION_PREFIX
    if plan.covered_tool_results:
        text = f"{text}\n\n{DROPPED_PREVIEW_TITLE}\n{chr(10).join(dropped_tool_preview(plan))}"
    anchor = continuity_anchor(
        summary_text=summary,
        dropped_count=len(plan.covered_tool_results),
        retained_count=plan.retained_tool_result_count,
    )
    return f"{text}\n{anchor}"


def render_model_replacement(plan: ProjectionPlan, summary: str) -> str:
    """The model lane's replacement: the model's text stands in for the whole projection."""
    text = summary.strip()
    anchor = continuity_anchor(
        summary_text=text,
        dropped_count=len(plan.covered_tool_results),
        retained_count=plan.retained_tool_result_count,
    )
    return f"{PROJECTION_PREFIX}\n{text}\n{anchor}"


def summary_prompt(plan: ProjectionPlan) -> str:
    """The one-shot summary request: the legacy instruction, then the bounded covered material.

    ``services.model_query`` carries a single prompt (no separate system role), so the legacy
    system instruction opens it. The whole prompt stays inside the core's 64 KiB bound.
    """
    head = f"{CONTINUITY_SUMMARY_SYSTEM_PROMPT}\n\n"
    material = "\n\n".join(plan.summary_material)[:_CONTINUITY_SUMMARY_INPUT_CHARS]
    room = MODEL_QUERY_MAX_BYTES - len(head.encode("utf-8"))
    encoded = material.encode("utf-8")
    if len(encoded) > room:
        material = encoded[:room].decode("utf-8", errors="ignore")
    return f"{head}{material}"


def clip_summary_text(value: str) -> str | None:
    """The legacy answer bound: blank answers are absent, long ones are truncated."""
    return value.strip()[:CONTINUITY_SUMMARY_OUTPUT_CHARS] or None


__all__ = [
    "CONTINUITY_ANCHOR_PREFIX",
    "CONTINUITY_SUMMARY_OUTPUT_CHARS",
    "CONTINUITY_SUMMARY_SYSTEM_PROMPT",
    "DEFAULT_CONTEXT_RESERVE_TOKENS",
    "DEFAULT_KEEP_RECENT_TOKENS",
    "MODEL_QUERY_MAX_BYTES",
    "PROJECTION_PREFIX",
    "CompactionBudget",
    "ProjectionPlan",
    "ToolResultFact",
    "body_text",
    "clip_summary_text",
    "continuity_anchor",
    "continuity_sections",
    "dropped_tool_preview",
    "effective_reserve_tokens",
    "event_payload_bytes",
    "measured_dispatch_input_tokens",
    "measured_tokens_per_character",
    "plan_projection",
    "recent_tail_character_budget",
    "render_deterministic_replacement",
    "render_model_replacement",
    "resolve_budget_reserve_tokens",
    "resolve_compaction_budget",
    "resolve_threshold_tokens",
    "should_compact",
    "summary_prompt",
    "tool_result_fact",
]
