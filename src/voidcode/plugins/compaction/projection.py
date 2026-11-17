"""Model-summary continuity projection and the compaction budget math.

Ported from legacy ``runtime/context/window.py`` (the threshold/reserve resolution and the
summary lane's constants) so this plugin stands on its own once the legacy tree is deleted.
This module only *plans*: the core re-verifies the boundary and measures the accounting of
every declared projection (``services.project_window``), so nothing here is trusted about
coverage.

Rulers: a token number is either provider-reported (a dispatch's usage record, a usage
fact) or a legacy constant. Sizes this module compares against each other are measured on
the core's own ruler -- the canonical JSON bytes of a committed event payload -- never a
UTF-8-bytes/4 token estimate, which the plugin has no way to make truthful.
"""

from __future__ import annotations

import hashlib
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from voidcode.sdk.provider import ProviderMessage, ProviderTurnResult
from voidcode.sdk.tools import CompositeOutput, TextOutput, ToolFailure, ToolOutput, ToolResult, composite_ordered_text
from voidcode.sdk.transcript import ContextRecord, TranscriptDispatch, TranscriptInterrupted, TranscriptTool

# --- Legacy constants (``runtime/context/window.py``) ---

#: The output reserve is 15% of the window (``_TOKEN_RESERVE_*``), with a fixed floor.
_TOKEN_RESERVE_NUMERATOR = 15
_TOKEN_RESERVE_DENOMINATOR = 100
DEFAULT_CONTEXT_RESERVE_TOKENS = 16_384
#: Legacy ``DEFAULT_KEEP_RECENT_TOOL_TOKENS``: how much recent content stays verbatim.
DEFAULT_KEEP_RECENT_TOKENS = 20_000
#: The provider-facing prefix and the anchor that identifies one projection's text.
PROJECTION_PREFIX = "Runtime context projection:"
CONTINUITY_ANCHOR_PREFIX = "continuity:"

#: The model summary (legacy ``service.py:_CONTINUITY_SUMMARY_SYSTEM_PROMPT``), verbatim.
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


def event_payload_bytes(event: ContextRecord) -> int:
    """Use the core's measured source bytes, never serialize private payloads."""
    return event.payload_bytes


def measured_dispatch_input_tokens(events: Sequence[ContextRecord]) -> int | None:
    for event in reversed(events):
        if isinstance(event.content, TranscriptInterrupted):
            return None
        if isinstance(event.content, ProviderTurnResult):
            return None if event.content.usage is None else event.content.usage.input_tokens
    return None


def measured_tokens_per_character(events: Sequence[ContextRecord]) -> float | None:
    """Pair provider usage with the exact dispatched request's measured window."""
    for event in reversed(events):
        if isinstance(event.content, TranscriptInterrupted):
            return None
        if not isinstance(event.content, ProviderTurnResult):
            continue
        tokens = None if event.content.usage is None else event.content.usage.input_tokens
        dispatch = None if event.entry is None else event.entry.dispatch_sequence
        window = next((item.content.window for item in events if item.sequence == dispatch and isinstance(item.content, TranscriptDispatch)), None)
        characters = None if window is None else window.total_characters
        if tokens is None or tokens <= 0 or characters is None or characters <= 0:
            return None
        return tokens / characters
    return None


def recent_tail_character_budget(events: Sequence[ContextRecord], *, keep_recent_tokens: int, reached_end: bool) -> int:
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

    tool_name: str
    status: Literal["ok", "error"]
    #: Result output as the provider saw it, and the failure reason when there was one.
    content: str
    error: str | None = None

    @property
    def text(self) -> str:
        """Provider-visible body (legacy ``result_payload_text``): output, then error."""
        return f"{self.content}\n{self.error}" if self.error else self.content


def _output_text(output: ToolOutput) -> str:
    if isinstance(output, CompositeOutput):
        ordered = composite_ordered_text(output)
        return ordered if ordered else (output.presentation or "")
    return output.text if isinstance(output, TextOutput) else (output.presentation or "")


def _result_error(result: ToolResult) -> str | None:
    """The failure reason as the provider saw it, or None for a successful result."""
    if not isinstance(result, ToolFailure):
        return None
    error = result.error.strip()
    return error or None


def tool_result_fact(event: ContextRecord) -> ToolResultFact | None:
    content = event.content
    if not isinstance(content, TranscriptTool):
        return None
    result, call = content.result, content.call
    return ToolResultFact(
        tool_name=call.tool_name,
        status="ok" if not content.interrupted and not isinstance(result, ToolFailure) else "error",
        content=_output_text(result.output),
        error=_result_error(result),
    )


def _model_turn_output(event: ContextRecord) -> str | None:
    return event.content.output if isinstance(event.content, ProviderTurnResult) else None


# --- The covered prefix: boundary, accounting, and the replacement it earns ---


@dataclass(frozen=True, slots=True)
class ProjectionPlan:
    """One coverable prefix and everything the replacement text is built from."""

    covered_from_sequence: int
    covered_to_sequence: int
    covered_event_count: int
    covered_bytes: int
    covered_tool_result_count: int
    retained_tool_result_count: int
    summary_material: tuple[str, ...]


def _summary_material(events: Sequence[ContextRecord]) -> tuple[str, ...]:
    """Bounded, oldest-first transcript of the covered prefix for the model summary.

    Each item is previewed under its own cap so one oversized payload cannot crowd out the
    rest, and the running total stops the list (legacy ``_continuity_summary_input``).
    """
    parts: list[str] = []
    used = 0
    for index, event in enumerate(events):
        if isinstance(event.content, TranscriptTool):
            fact = tool_result_fact(event)
            if fact is None:
                continue
            body = f"[{index}] tool={fact.tool_name} status={fact.status}\n{fact.text[:_CONTINUITY_SUMMARY_RESULT_CHARS]}"
            used += min(len(fact.text), _CONTINUITY_SUMMARY_RESULT_CHARS) + _CONTINUITY_SUMMARY_RESULT_CHARS // 2
        elif isinstance(event.content, ProviderMessage):
            text = event.content.content
            if text is None:
                continue
            body = f"[{index}] {event.content.role}\n{text[:_CONTINUITY_SUMMARY_RESULT_CHARS]}"
            used += min(len(text), _CONTINUITY_SUMMARY_RESULT_CHARS) + _CONTINUITY_SUMMARY_RESULT_CHARS // 2
        elif isinstance(event.content, ProviderTurnResult):
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
    events: Sequence[ContextRecord],
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
    readable = tuple(events)
    if not readable:
        return None
    boundaries = tuple(event.sequence for event in readable if event.safe_boundary)
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
        visible = [event.sequence for event in readable if isinstance(event.content, (ProviderMessage, ProviderTurnResult, TranscriptTool))]
        candidates = [sequence for sequence in candidates if bisect_right(visible, sequence) > bisect_right(visible, previous_covered_to_sequence)]
    if not candidates:
        return None
    covered_to_sequence = candidates[-1]
    if covered_to_sequence > last_boundary:
        return None
    covered = tuple(event for event in readable if event.sequence <= covered_to_sequence)
    covered_tool_result_count = sum(1 for event in covered if isinstance(event.content, TranscriptTool))
    retained_tool_result_count = sum(1 for event in readable if event.sequence > covered_to_sequence and isinstance(event.content, TranscriptTool))
    return ProjectionPlan(
        covered_from_sequence=readable[0].sequence,
        covered_to_sequence=covered_to_sequence,
        covered_event_count=len(covered),
        covered_bytes=sum(event_payload_bytes(event) for event in covered),
        covered_tool_result_count=covered_tool_result_count,
        retained_tool_result_count=retained_tool_result_count,
        summary_material=_summary_material(covered),
    )


def continuity_anchor(*, summary_text: str, dropped_count: int, retained_count: int) -> str:
    """Legacy anchor: the sha256 of the counts and the exact summary text, 16 hex chars."""
    digest = hashlib.sha256(f"{dropped_count}:{retained_count}:{summary_text}".encode()).hexdigest()[:16]
    return f"{CONTINUITY_ANCHOR_PREFIX}{digest}"


def render_model_replacement(plan: ProjectionPlan, summary: str) -> str:
    """The replacement: the model's text stands in for the whole covered prefix."""
    text = summary.strip()
    anchor = continuity_anchor(
        summary_text=text,
        dropped_count=plan.covered_tool_result_count,
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
    "clip_summary_text",
    "continuity_anchor",
    "effective_reserve_tokens",
    "event_payload_bytes",
    "measured_dispatch_input_tokens",
    "measured_tokens_per_character",
    "plan_projection",
    "recent_tail_character_budget",
    "render_model_replacement",
    "resolve_budget_reserve_tokens",
    "resolve_compaction_budget",
    "resolve_threshold_tokens",
    "should_compact",
    "summary_prompt",
    "tool_result_fact",
]
