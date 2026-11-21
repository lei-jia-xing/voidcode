"""Compaction plugin: the session window projection over the core's verified seam.

The plugin decides *what* a projection covers and writes the replacement text; the core
decides whether that projection is admissible and measures its accounting itself. So a
projection is declared through ``services.project_window`` and read back through
``services.window_projection`` -- the numbers reported here are the core's, never this
plugin's own estimate.

Trigger (``enabled`` + ``threshold_tokens``/``reserve_tokens``): the provider-reported prompt
tokens of the newest dispatch, compared against the resolved threshold. Coverage always
starts at the session's first event and ends at a sequence that closes a whole native tool
batch, and the prefix is never covered past the point where the replacement would stop
shrinking the window: that check is made before the call and a refusal is visible.

Manual ``/compact`` runs the same projection on demand -- the user is the gate there, not
``enabled`` -- and a call with nothing new to cover is a no-op that reports the projection
already in force.

State is this plugin's own bookkeeping only (the observation cursor and the last projection's
accounting); the projection row itself belongs to the core.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from voidcode.capabilities.plugin_state import PluginStateStore
from voidcode.sdk.commands import CommandDefinition, CommandInvocation, CommandPresentation, CommandResult
from voidcode.sdk.hooks import PluginFact, RunFinishedFact, UsageFact
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.services import HistoryServices, PluginServices, StateServices, ToolServices
from voidcode.sdk.tools import ToolEffect
from voidcode.sdk.transcript import ContextRecord

from .projection import (
    DEFAULT_KEEP_RECENT_TOKENS,
    SummaryKind,
    clip_summary_text,
    measured_dispatch_input_tokens,
    plan_projection,
    render_model_replacement,
    resolve_compaction_budget,
    should_compact,
    summary_prompt,
)

SCHEMA_VERSION = 2
#: STABLE cumulative fact: staged via ``services.stage_event`` as
#: ``plugin.compaction.context_compacted``, one event per applied projection (threshold or
#: manual) carrying core-measured accounting. Consumers count these events for the cumulative
#: total; ``window_projection()`` is latest-only and never a substitute.
CONTEXT_COMPACTED_EVENT = "context_compacted"
#: How many event pages one observation may consume; the prefix is what matters, not the tail.
_MAX_EVENT_PAGES = 3
_EVENT_PAGE_SIZE = 200

_PROJECTION_KEYS: frozenset[str] = frozenset({"covered_from_sequence", "covered_to_sequence", "covered_bytes", "dropped_bytes", "summary_kind"})

ProjectionReason = Literal["threshold", "manual"]


# --- Durable session state ---


@dataclass(frozen=True, slots=True)
class LastProjection:
    """The core-measured accounting of the projection this plugin most recently declared."""

    covered_from_sequence: int
    covered_to_sequence: int
    covered_bytes: int
    dropped_bytes: int
    summary_kind: SummaryKind

    def to_payload(self) -> dict[str, object]:
        return {
            "covered_from_sequence": self.covered_from_sequence,
            "covered_to_sequence": self.covered_to_sequence,
            "covered_bytes": self.covered_bytes,
            "dropped_bytes": self.dropped_bytes,
            "summary_kind": self.summary_kind,
        }


@dataclass(frozen=True, slots=True)
class CompactionState:
    """This plugin's session-scope state: how far it observed, and what it last projected."""

    cursor_sequence: int = 0
    last_projection: LastProjection | None = None

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "cursor_sequence": self.cursor_sequence,
            "last_projection": None if self.last_projection is None else self.last_projection.to_payload(),
        }

    @staticmethod
    def from_payload(payload: Mapping[str, object]) -> CompactionState:
        if payload["schema_version"] != SCHEMA_VERSION:
            raise ValueError(f"compaction state schema_version must be {SCHEMA_VERSION}")
        return CompactionState(
            cursor_sequence=_non_negative_int(payload["cursor_sequence"], field="cursor_sequence"),
            last_projection=_projection_from_payload(payload["last_projection"]),
        )


def _non_negative_int(value: object, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"compaction state field '{field}' must be a non-negative integer")
    return value


def _projection_from_payload(raw: object) -> LastProjection | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ValueError("compaction state last_projection must be an object or null")
    unknown = sorted(set(raw) - _PROJECTION_KEYS)
    if unknown:
        raise ValueError(f"compaction last_projection has unsupported field: {unknown[0]}")
    missing = sorted(_PROJECTION_KEYS - set(raw))
    if missing:
        raise ValueError(f"compaction last_projection is missing field: {missing[0]}")
    if raw["summary_kind"] != "model":
        raise ValueError("compaction last_projection summary_kind must be 'model'")
    return LastProjection(
        covered_from_sequence=_non_negative_int(raw["covered_from_sequence"], field="covered_from_sequence"),
        covered_to_sequence=_non_negative_int(raw["covered_to_sequence"], field="covered_to_sequence"),
        covered_bytes=_non_negative_int(raw["covered_bytes"], field="covered_bytes"),
        dropped_bytes=_non_negative_int(raw["dropped_bytes"], field="dropped_bytes"),
        summary_kind="model",
    )


def _store(services: StateServices) -> PluginStateStore[CompactionState]:
    """This plugin's session-state store: strict keys, typed parse, CAS save."""
    return PluginStateStore(
        services,
        keys=frozenset({"schema_version", "cursor_sequence", "last_projection"}),
        parse=CompactionState.from_payload,
        serialize=CompactionState.to_payload,
        empty=CompactionState,
        unknown=lambda extra: f"compaction state has unsupported field: {extra[0]}",
    )


# --- Configuration (strict) ---


class CompactionConfig(BaseModel):
    """The compaction knobs, resolved from the plugin's configuration block."""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    enabled: bool = True
    threshold_tokens: int | None = Field(default=None, ge=0)
    reserve_tokens: int | None = Field(default=None, ge=0)
    keep_recent_tokens: int = Field(default=DEFAULT_KEEP_RECENT_TOKENS, ge=0)


def parse_compaction_config(config: Mapping[str, object] | None) -> CompactionConfig:
    """Validate one plugin configuration block; an unknown key is an error, never ignored."""
    # Reserved config key: the core injects its resolved providers map into every factory.
    raw = {key: value for key, value in (config or {}).items() if key != "providers"}
    return CompactionConfig.model_validate(raw)


# --- The projection itself ---


@dataclass(frozen=True, slots=True)
class ProjectionOutcome:
    """Result of one projection attempt, reported in the core's own terms."""

    status: Literal["projected", "noop"]
    reason: str
    covered_from_sequence: int | None = None
    covered_to_sequence: int | None = None
    covered_bytes: int | None = None
    dropped_bytes: int | None = None
    covered_events: int | None = None
    summary_kind: SummaryKind | None = None
    measured_input_tokens: int | None = None

    @property
    def accounting(self) -> dict[str, object]:
        return {
            "covered_from_sequence": self.covered_from_sequence,
            "covered_to_sequence": self.covered_to_sequence,
            "covered_bytes": self.covered_bytes,
            "dropped_bytes": self.dropped_bytes,
            "covered_events": self.covered_events,
            "summary_kind": self.summary_kind,
        }


def _read_window(services: HistoryServices) -> tuple[tuple[ContextRecord, ...], bool]:
    history = services.read_context_history(limit=_MAX_EVENT_PAGES * _EVENT_PAGE_SIZE)
    return history.records, history.reached_end


def _replacement_text(services: ToolServices, plan) -> str:
    """The replacement: the model's summary, or a loud failure when it cannot be produced.

    A failed, blank, or unshrinkable answer raises ``ValueError``: there is no second lane to
    fall back to. (The legacy 60s bound is the seam's own here: ``services.model_query``
    carries no timeout argument, so the core's RPC timeout is the only bound available.)
    """
    answer = clip_summary_text(services.model_query(summary_prompt(plan)))
    if answer is None:
        raise ValueError("the model summary was blank")
    replacement = render_model_replacement(plan, answer)
    if len(replacement.encode("utf-8")) >= plan.covered_bytes:
        raise ValueError(
            f"the model replacement ({len(replacement.encode('utf-8'))} bytes) would not shrink the covered prefix ({plan.covered_bytes} bytes)"
        )
    return replacement


def _measured_projection(services: StateServices) -> tuple[int, int]:
    """Read back the projection the core just verified and take its measured accounting."""
    row = services.window_projection()
    if not isinstance(row, Mapping):
        raise ValueError("the core recorded no window projection for this session")
    covered_bytes = row.get("covered_bytes")
    dropped_bytes = row.get("dropped_bytes")
    if not isinstance(covered_bytes, int) or not isinstance(dropped_bytes, int):
        raise ValueError("the recorded window projection carries no measured accounting")
    return covered_bytes, dropped_bytes


def apply_projection(
    services: PluginServices,
    *,
    state: CompactionState,
    version: int,
    config: CompactionConfig,
    reason: ProjectionReason,
    events: Sequence[ContextRecord],
    reached_end: bool,
    measured_input_tokens: int | None,
) -> ProjectionOutcome:
    """Plan, guard, summarize, declare and record one projection.

    The summary is the replacement: when it cannot shrink the prefix the projection raises
    instead of being sent for the core to reject, and the core's measured numbers -- not
    this plugin's plan -- are what the staged event and the state record.
    """
    plan = plan_projection(
        events,
        previous_covered_to_sequence=None if state.last_projection is None else state.last_projection.covered_to_sequence,
        keep_recent_tokens=config.keep_recent_tokens,
        reached_end=reached_end,
    )
    if plan is None:
        return ProjectionOutcome(status="noop", reason="nothing_new_to_cover", measured_input_tokens=measured_input_tokens)
    replacement = _replacement_text(services, plan)
    replacement_bytes = len(replacement.encode("utf-8"))
    services.project_window(
        covered_from_sequence=plan.covered_from_sequence,
        covered_to_sequence=plan.covered_to_sequence,
        replacement=replacement,
    )
    covered_bytes, dropped_bytes = _measured_projection(services)
    outcome = ProjectionOutcome(
        status="projected",
        reason=reason,
        covered_from_sequence=plan.covered_from_sequence,
        covered_to_sequence=plan.covered_to_sequence,
        covered_bytes=covered_bytes,
        dropped_bytes=dropped_bytes,
        covered_events=plan.covered_event_count,
        summary_kind="model",
        measured_input_tokens=measured_input_tokens,
    )
    tip = max(state.cursor_sequence, events[-1].sequence)
    projected = CompactionState(
        cursor_sequence=tip,
        last_projection=LastProjection(
            covered_from_sequence=plan.covered_from_sequence,
            covered_to_sequence=plan.covered_to_sequence,
            covered_bytes=covered_bytes,
            dropped_bytes=dropped_bytes,
            summary_kind="model",
        ),
    )
    _store(services).save(projected, version)
    services.stage_event(
        CONTEXT_COMPACTED_EVENT,
        {
            **outcome.accounting,
            "reason": reason,
            "measured_input_tokens": measured_input_tokens,
            "replacement_bytes": replacement_bytes,
        },
    )
    return outcome


# --- Observation ---


def compaction_observer(
    fact: PluginFact,
    context: ToolContext,
    services: PluginServices,
    *,
    config: CompactionConfig,
) -> None:
    """Project the covered prefix once the newest dispatch's measured prompt crossed the gate.

    ``usage`` is the per-dispatch measurement and the timely trigger; ``run_finished`` catches
    the end of a run, where the newest dispatch in the window supplies the same measurement.
    A model summary that fails, comes back blank, or would not shrink the prefix raises, so
    the core records an owned ``core.observer_error`` instead of the observation silently
    doing nothing.
    """
    _ = context
    if not isinstance(fact, (UsageFact, RunFinishedFact)) or not config.enabled:
        return
    budget = resolve_compaction_budget(None, threshold_tokens=config.threshold_tokens, reserve_tokens=config.reserve_tokens)
    measured: int | None = None
    if isinstance(fact, UsageFact):
        # A dispatch's own report decides its own gate, before the window is even read.
        measured = None if fact.usage is None else fact.usage.input_tokens
        if not should_compact(measured, budget):
            return
    state, version = _store(services).load()
    events, reached_end = _read_window(services)
    if not events:
        return
    tip = events[-1].sequence
    if state.last_projection is not None and tip <= state.cursor_sequence:
        # This plugin already acted on this tip: re-reading it can only repeat that decision.
        return
    if not isinstance(fact, UsageFact):
        # The end of a run has no report of its own: the newest dispatch in the window is the
        # measurement, and only a crossed gate justifies the read that measured it.
        measured = measured_dispatch_input_tokens(events)
        if not should_compact(measured, budget):
            return
    apply_projection(
        services,
        state=state,
        version=version,
        config=config,
        reason="threshold",
        events=events,
        reached_end=reached_end,
        measured_input_tokens=measured,
    )


# --- Command: the manual compaction ---


def _row_accounting(row: Mapping[str, object] | None) -> dict[str, object]:
    """The accounting of the projection in force, or empty fields when there is none."""
    if not isinstance(row, Mapping):
        return {"covered_from_sequence": None, "covered_to_sequence": None, "covered_bytes": None, "dropped_bytes": None}
    return {
        "covered_from_sequence": row.get("covered_from_sequence"),
        "covered_to_sequence": row.get("covered_to_sequence"),
        "covered_bytes": row.get("covered_bytes"),
        "dropped_bytes": row.get("dropped_bytes"),
    }


def _compact_receipt(*, output: str, data: Mapping[str, object]) -> CommandResult:
    rows = [{"label": "Result", "value": "Nothing new to compact" if data["noop"] else "Compacted"}]
    labels = (
        ("reason", "Reason"),
        ("covered_from_sequence", "First covered event"),
        ("covered_to_sequence", "Last covered event"),
        ("covered_bytes", "Covered bytes"),
        ("dropped_bytes", "Dropped bytes"),
        ("summary_kind", "Summary source"),
        ("measured_input_tokens", "Measured input tokens"),
    )
    rows.extend({"label": label, "value": "Unknown" if data.get(key) is None else str(data[key])} for key, label in labels)
    return CommandResult(
        status="ok",
        output=output,
        data=data,
        presentation=CommandPresentation(title="Conversation compaction", view_type="fields", data={"rows": rows}),
    )


class CompactCommand:
    """``/compact``: project the covered prefix on demand and report the core's accounting."""

    def __init__(self, config: CompactionConfig) -> None:
        self._config = config

    definition: ClassVar[CommandDefinition] = CommandDefinition(
        name="compact",
        description="Summarize the covered conversation prefix into one system message.",
        argument_schema={"type": "object", "properties": {}, "additionalProperties": False},
        # The projection is session state the core owns; the command declares it as such.
        effects=frozenset({ToolEffect.SESSION}),
        busy_gate=True,
        client_syntax="/compact",
    )

    def invoke(self, invocation: CommandInvocation, services: PluginServices) -> CommandResult:
        _ = invocation
        state, version = _store(services).load()
        events, reached_end = _read_window(services)
        if not events:
            return _compact_receipt(
                output="Nothing to compact: this session has no committed events.",
                data={"noop": True, "reason": "no_events"},
            )
        outcome = apply_projection(
            services,
            state=state,
            version=version,
            config=self._config,
            reason="manual",
            events=events,
            reached_end=reached_end,
            measured_input_tokens=measured_dispatch_input_tokens(events),
        )
        if outcome.status == "projected":
            return _compact_receipt(
                output=(
                    f"Compacted events {outcome.covered_from_sequence}-{outcome.covered_to_sequence}: "
                    f"{outcome.covered_bytes} bytes covered -> {outcome.dropped_bytes} dropped ({outcome.summary_kind} summary)."
                ),
                data={"noop": False, "reason": outcome.reason, **outcome.accounting, "measured_input_tokens": outcome.measured_input_tokens},
            )
        current = _row_accounting(services.window_projection())
        if current["covered_bytes"] is None:
            return _compact_receipt(
                output="Nothing new to compact: no coverable prefix remains.",
                data={"noop": True, "reason": outcome.reason, **current, "summary_kind": None, "covered_events": None},
            )
        return _compact_receipt(
            output=(
                f"Nothing new to compact: the projection in force covers events {current['covered_from_sequence']}-"
                f"{current['covered_to_sequence']} ({current['covered_bytes']} bytes -> {current['dropped_bytes']} dropped)."
            ),
            data={
                "noop": True,
                "reason": outcome.reason,
                **current,
                "summary_kind": None if state.last_projection is None else state.last_projection.summary_kind,
                "covered_events": None,
            },
        )


def prepare_compaction_deactivation(
    state: Mapping[str, object],
    scope: str,
    scope_key: str,
    tip_sequence: int | None,
) -> Mapping[str, object]:
    """Pure deactivation transform: bookkeeping survives; the projection row does not.

    The cursor and last-projection accounting are this plugin's own history, so the parked
    copy keeps the payload untouched for re-enable. The live projection itself is core-owned
    and the activation transaction already deletes it (the raw window becomes visible again),
    so there is no live work for this transform to refuse.
    """
    _ = (scope, scope_key, tip_sequence)
    return state


__all__ = [
    "CONTEXT_COMPACTED_EVENT",
    "DEFAULT_KEEP_RECENT_TOKENS",
    "SCHEMA_VERSION",
    "CompactCommand",
    "CompactionConfig",
    "CompactionState",
    "LastProjection",
    "ProjectionOutcome",
    "apply_projection",
    "compaction_observer",
    "parse_compaction_config",
]
