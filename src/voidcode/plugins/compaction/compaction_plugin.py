"""Compaction plugin: the session window projection over the kernel's verified seam.

The plugin decides *what* a projection covers and writes the replacement text; the kernel
decides whether that projection is admissible and measures its accounting itself. So a
projection is declared through ``services.project_window`` and read back through
``services.window_projection`` -- the numbers reported here are the kernel's, never this
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
accounting); the projection row itself belongs to the kernel.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from typing import ClassVar, Literal

from voidcode.sdk.commands import CommandDefinition, CommandInvocation, CommandResult
from voidcode.sdk.hooks import PluginFact, RunFinishedFact, UsageFact
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.plugin import Plugin, StateSchema
from voidcode.sdk.services import EventView, PluginServices
from voidcode.sdk.tools import ToolEffect

from .projection import (
    DEFAULT_KEEP_RECENT_TOKENS,
    SummaryKind,
    clip_summary_text,
    measured_dispatch_input_tokens,
    plan_projection,
    render_deterministic_replacement,
    render_model_replacement,
    resolve_compaction_budget,
    should_compact,
    summary_prompt,
)

SCHEMA_VERSION = 1
#: Event that records one applied projection (kernel-measured bytes, never estimates).
CONTEXT_COMPACTED_EVENT = "context_compacted"
#: How many event pages one observation may consume; the prefix is what matters, not the tail.
_MAX_EVENT_PAGES = 3
_EVENT_PAGE_SIZE = 200

_STATE_KEYS: frozenset[str] = frozenset({"schema_version", "cursor_sequence", "last_projection"})
_PROJECTION_KEYS: frozenset[str] = frozenset({"covered_from_sequence", "covered_to_sequence", "covered_bytes", "dropped_bytes", "summary_kind"})
_CONFIG_KEYS: frozenset[str] = frozenset({"enabled", "threshold_tokens", "reserve_tokens", "keep_recent_tokens", "summary_enabled", "providers"})
_SUMMARY_KINDS: frozenset[str] = frozenset({"deterministic", "model"})

ProjectionReason = Literal["threshold", "manual"]


# --- Durable session state ---


@dataclass(frozen=True, slots=True)
class LastProjection:
    """The kernel-measured accounting of the projection this plugin most recently declared."""

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
    def from_payload(payload: Mapping[str, object] | None) -> CompactionState:
        if payload is None:
            return CompactionState()
        unknown = sorted(set(payload) - _STATE_KEYS)
        if unknown:
            raise ValueError(f"compaction state has unsupported field: {unknown[0]}")
        version = payload.get("schema_version", SCHEMA_VERSION)
        if version != SCHEMA_VERSION:
            raise ValueError(f"compaction state schema_version must be {SCHEMA_VERSION}")
        return CompactionState(
            cursor_sequence=_non_negative_int(payload.get("cursor_sequence", 0), field="cursor_sequence"),
            last_projection=_projection_from_payload(payload.get("last_projection")),
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
    summary_kind = raw.get("summary_kind")
    if summary_kind not in _SUMMARY_KINDS:
        raise ValueError("compaction last_projection summary_kind must be 'deterministic' or 'model'")
    return LastProjection(
        covered_from_sequence=_non_negative_int(raw.get("covered_from_sequence"), field="covered_from_sequence"),
        covered_to_sequence=_non_negative_int(raw.get("covered_to_sequence"), field="covered_to_sequence"),
        covered_bytes=_non_negative_int(raw.get("covered_bytes"), field="covered_bytes"),
        dropped_bytes=_non_negative_int(raw.get("dropped_bytes"), field="dropped_bytes"),
        summary_kind=summary_kind,
    )


def _load_state(services: PluginServices) -> tuple[CompactionState, int]:
    stored, version = services.get_state("session")
    return CompactionState.from_payload(stored), version


# --- Configuration (legacy knob names, strict) ---


@dataclass(frozen=True, slots=True)
class CompactionConfig:
    """The compaction knobs, resolved from the plugin's configuration block."""

    enabled: bool = True
    threshold_tokens: int | None = None
    reserve_tokens: int | None = None
    keep_recent_tokens: int = DEFAULT_KEEP_RECENT_TOKENS
    summary_enabled: bool = False


def _optional_count(raw: object, *, field: str) -> int | None:
    if raw is None:
        return None
    if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
        raise ValueError(f"compaction config '{field}' must be a non-negative integer or null")
    return raw


def parse_compaction_config(config: Mapping[str, object] | None) -> CompactionConfig:
    """Validate one plugin configuration block; an unknown key is an error, never ignored."""
    if config is None:
        return CompactionConfig()
    if not isinstance(config, Mapping):
        raise ValueError("compaction config must be an object")
    unknown = sorted(set(config) - _CONFIG_KEYS)
    if unknown:
        raise ValueError(f"compaction config has unsupported field: {unknown[0]}")
    enabled = config.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("compaction config 'enabled' must be a boolean")
    summary_enabled = config.get("summary_enabled", False)
    if not isinstance(summary_enabled, bool):
        raise ValueError("compaction config 'summary_enabled' must be a boolean")
    keep_recent = _optional_count(config.get("keep_recent_tokens"), field="keep_recent_tokens")
    return CompactionConfig(
        enabled=enabled,
        threshold_tokens=_optional_count(config.get("threshold_tokens"), field="threshold_tokens"),
        reserve_tokens=_optional_count(config.get("reserve_tokens"), field="reserve_tokens"),
        keep_recent_tokens=DEFAULT_KEEP_RECENT_TOKENS if keep_recent is None else keep_recent,
        summary_enabled=summary_enabled,
    )


# --- The projection itself ---


@dataclass(frozen=True, slots=True)
class ProjectionOutcome:
    """Result of one projection attempt, reported in the kernel's own terms."""

    status: Literal["projected", "noop", "refused"]
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


def _read_window(services: PluginServices) -> tuple[tuple[EventView, ...], bool]:
    """Read this session's events from its first one; the flag says the read reached the end."""
    events: list[EventView] = []
    cursor = 0
    reached_end = False
    for _ in range(_MAX_EVENT_PAGES):
        page = services.read_events(after_sequence=cursor, limit=_EVENT_PAGE_SIZE, include_payload=True)
        if not page:
            reached_end = True
            break
        events.extend(page)
        cursor = max(event.sequence for event in page)
        if len(page) < _EVENT_PAGE_SIZE:
            reached_end = True
            break
    return tuple(events), reached_end


def _replacement_text(
    services: PluginServices,
    plan,
    config: CompactionConfig,
    deterministic: str,
) -> tuple[str, SummaryKind]:
    """The replacement, preferring the model summary and degrading to the deterministic text.

    A model answer that would not shrink the window is discarded like a failed call: the
    deterministic projection was already checked against the same bound by the caller.
    """
    if not config.summary_enabled:
        return deterministic, "deterministic"
    try:
        # The legacy 60s bound is the seam's own here: ``services.model_query`` carries no
        # timeout argument, so the kernel's RPC timeout is the only bound available.
        answer = clip_summary_text(services.model_query(summary_prompt(plan)))
    except Exception:  # noqa: BLE001 - a failed summary degrades, it never fails the observation
        return deterministic, "deterministic"
    if answer is None:
        return deterministic, "deterministic"
    model = render_model_replacement(plan, answer)
    if len(model.encode("utf-8")) >= plan.covered_bytes:
        return deterministic, "deterministic"
    return model, "model"


def _measured_projection(services: PluginServices) -> tuple[int, int]:
    """Read back the projection the kernel just verified and take its measured accounting."""
    row = services.window_projection()
    if not isinstance(row, Mapping):
        raise ValueError("the kernel recorded no window projection for this session")
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
    events: Sequence[EventView],
    reached_end: bool,
    measured_input_tokens: int | None,
) -> ProjectionOutcome:
    """Plan, guard, declare and record one projection.

    The size guard runs *before* ``project_window`` so an unshrinkable prefix is refused
    visibly instead of being sent for the kernel to reject, and the kernel's measured numbers
    -- not this plugin's plan -- are what the staged event and the state record.
    """
    plan = plan_projection(
        events,
        previous_covered_to_sequence=None if state.last_projection is None else state.last_projection.covered_to_sequence,
        keep_recent_tokens=config.keep_recent_tokens,
        reached_end=reached_end,
    )
    if plan is None:
        return ProjectionOutcome(status="noop", reason="nothing_new_to_cover", measured_input_tokens=measured_input_tokens)
    # The guard runs before any model call: an unshrinkable prefix is refused without spending
    # a provider turn on a summary the kernel would reject anyway.
    deterministic = render_deterministic_replacement(plan)
    if len(deterministic.encode("utf-8")) >= plan.covered_bytes:
        return ProjectionOutcome(
            status="refused",
            reason=(f"the replacement ({len(deterministic.encode('utf-8'))} bytes) would not shrink the covered prefix ({plan.covered_bytes} bytes)"),
            covered_from_sequence=plan.covered_from_sequence,
            covered_to_sequence=plan.covered_to_sequence,
            measured_input_tokens=measured_input_tokens,
        )
    replacement, summary_kind = _replacement_text(services, plan, config, deterministic)
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
        summary_kind=summary_kind,
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
            summary_kind=summary_kind,
        ),
    )
    services.replace_state(projected.to_payload(), expected_version=version)
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
    A projection this plugin refuses raises, so the kernel records an owned
    ``kernel.observer_error`` instead of the observation silently doing nothing.
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
    state, version = _load_state(services)
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


class CompactCommand:
    """``/compact``: project the covered prefix on demand and report the kernel's accounting."""

    def __init__(self, config: CompactionConfig) -> None:
        self._config = config

    definition: ClassVar[CommandDefinition] = CommandDefinition(
        name="compact",
        description="Summarize the covered conversation prefix into one system message.",
        argument_schema={"type": "object", "properties": {}, "additionalProperties": False},
        # The projection is session state the kernel owns; the command declares it as such.
        effects=frozenset({ToolEffect.SESSION}),
        busy_gate=True,
        client_syntax="/compact",
    )

    def invoke(self, invocation: CommandInvocation, services: PluginServices) -> CommandResult:
        _ = invocation
        state, version = _load_state(services)
        events, reached_end = _read_window(services)
        if not events:
            return CommandResult(
                status="ok",
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
            return CommandResult(
                status="ok",
                output=(
                    f"Compacted events {outcome.covered_from_sequence}-{outcome.covered_to_sequence}: "
                    f"{outcome.covered_bytes} bytes covered -> {outcome.dropped_bytes} dropped ({outcome.summary_kind} summary)."
                ),
                data={"noop": False, "reason": outcome.reason, **outcome.accounting, "measured_input_tokens": outcome.measured_input_tokens},
            )
        if outcome.status == "refused":
            return CommandResult(status="error", error=f"compaction refused: {outcome.reason}", data=dict(outcome.accounting))
        current = _row_accounting(services.window_projection())
        if current["covered_bytes"] is None:
            return CommandResult(
                status="ok",
                output="Nothing new to compact: no coverable prefix remains.",
                data={"noop": True, "reason": outcome.reason, **current, "summary_kind": None, "covered_events": None},
            )
        return CommandResult(
            status="ok",
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


def create_plugin(config: Mapping[str, object] | None = None) -> Plugin:
    resolved = parse_compaction_config(config)
    return Plugin(
        commands=(CompactCommand(resolved),),
        observer=partial(compaction_observer, config=resolved),
        observed_facts=("usage", "run_finished"),
        state=StateSchema(
            version=SCHEMA_VERSION,
            schema={
                "type": "object",
                "required": ["schema_version", "cursor_sequence", "last_projection"],
                "additionalProperties": False,
                "properties": {
                    "schema_version": {"type": "integer", "enum": [SCHEMA_VERSION]},
                    "cursor_sequence": {"type": "integer", "minimum": 0},
                    "last_projection": {
                        "type": ["object", "null"],
                        "required": ["covered_from_sequence", "covered_to_sequence", "covered_bytes", "dropped_bytes", "summary_kind"],
                        "additionalProperties": False,
                        "properties": {
                            "covered_from_sequence": {"type": "integer", "minimum": 0},
                            "covered_to_sequence": {"type": "integer", "minimum": 0},
                            "covered_bytes": {"type": "integer", "minimum": 0},
                            "dropped_bytes": {"type": "integer", "minimum": 0},
                            "summary_kind": {"type": "string", "enum": ["deterministic", "model"]},
                        },
                    },
                },
            },
        ),
    )


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
    "create_plugin",
    "parse_compaction_config",
]
