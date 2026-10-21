"""Unit tests for the compaction plugin: budget math, projection text, boundary, state, lanes."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Literal

import pytest

from voidcode.plugins.compaction import compaction_plugin as plugin_module
from voidcode.plugins.compaction.compaction_plugin import (
    CONTEXT_COMPACTED_EVENT,
    SCHEMA_VERSION,
    CompactCommand,
    CompactionConfig,
    CompactionState,
    LastProjection,
    apply_projection,
    compaction_observer,
    create_plugin,
    parse_compaction_config,
)
from voidcode.plugins.compaction.projection import (
    CONTINUITY_SUMMARY_SYSTEM_PROMPT,
    DEFAULT_CONTEXT_RESERVE_TOKENS,
    DEFAULT_KEEP_RECENT_TOKENS,
    PROJECTION_PREFIX,
    clip_summary_text,
    continuity_anchor,
    effective_reserve_tokens,
    event_payload_bytes,
    measured_dispatch_input_tokens,
    measured_tokens_per_character,
    plan_projection,
    recent_tail_character_budget,
    render_deterministic_replacement,
    render_model_replacement,
    resolve_budget_reserve_tokens,
    resolve_compaction_budget,
    resolve_threshold_tokens,
    should_compact,
    summary_prompt,
)
from voidcode.sdk.codec import encode_provider_result, encode_tool_call, encode_tool_result
from voidcode.sdk.commands import CommandInvocation
from voidcode.sdk.hooks import InvocationFinishedFact, RunFinishedFact, UsageFact
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.provider import ProviderTokenUsage, ProviderTurnResult
from voidcode.sdk.services import EventView, PluginServices
from voidcode.sdk.tools import TextOutput, ToolCall, ToolFailure, ToolSuccess

SESSION_ID = "session-1"
RUN_ID = "run-1"
REVISION_ID = "rev-1"
OWNER = "compaction"

_USER_MESSAGE = "Rebuild the parser. The tokenizer must stay allocation free. Never reorder diagnostics."
_WRITE_CALL = ToolCall("write", {"path": "src/parser.py"}, "call-1")
_READ_CALL = ToolCall("read", {"path": "src/parser.py"}, "call-2")


def _event(sequence: int, event_type: str, payload: Mapping[str, object] | None) -> EventView:
    return EventView(
        sequence=sequence,
        event_type=event_type,
        run_id=RUN_ID,
        revision_id=REVISION_ID,
        source="core",
        actor="core",
        payload=payload,
    )


def _intent(sequence: int, *, message_characters: int = 400, tool_schema_characters: int = 100) -> EventView:
    return _event(
        sequence,
        "kernel.provider_intent",
        {
            "provider": "endpoint",
            "model": "test-model",
            "request_window": {
                "message_characters": message_characters,
                "tool_schema_characters": tool_schema_characters,
                "segments": [],
                "tool_count": 2,
            },
        },
    )


def _model_turn(
    sequence: int,
    *,
    dispatch_sequence: int,
    output: str | None = None,
    tool_calls: tuple[ToolCall, ...] = (),
    input_tokens: int | None = 5_000,
) -> EventView:
    return _event(
        sequence,
        "kernel.model_turn",
        {
            "dispatch_sequence": dispatch_sequence,
            "result": encode_provider_result(
                ProviderTurnResult(
                    output=output,
                    tool_calls=tool_calls,
                    usage=None if input_tokens is None else ProviderTokenUsage(input_tokens=input_tokens),
                )
            ),
        },
    )


def _tool_completed(sequence: int, *, batch_sequence: int, call: ToolCall, content: str = "", error: str | None = None) -> EventView:
    result = ToolSuccess(call.tool_name, TextOutput(content)) if error is None else ToolFailure(call.tool_name, error, TextOutput(content))
    return _event(
        sequence,
        "kernel.tool_completed",
        {"batch_sequence": batch_sequence, "call": encode_tool_call(call), "result": encode_tool_result(result)},
    )


def _session_events() -> tuple[EventView, ...]:
    """One complete session: prompt, a write batch, a failing read batch, and a final answer."""
    return (
        _event(1, "kernel.user_message", {"content": _USER_MESSAGE}),
        _intent(2),
        _model_turn(3, dispatch_sequence=2, tool_calls=(_WRITE_CALL,)),
        _tool_completed(4, batch_sequence=3, call=_WRITE_CALL, content="wrote 42 bytes to src/parser.py"),
        _intent(5, message_characters=500),
        _model_turn(6, dispatch_sequence=5, tool_calls=(_READ_CALL,), input_tokens=9_000),
        _tool_completed(7, batch_sequence=6, call=_READ_CALL, error="missing file"),
        _model_turn(8, dispatch_sequence=5, output="done", input_tokens=9_000),
    )


def _plan(events: tuple[EventView, ...] = (), **overrides: object):
    planned = plan_projection(
        events or _session_events(),
        previous_covered_to_sequence=overrides.pop("previous_covered_to_sequence", None),  # type: ignore[arg-type]
        keep_recent_tokens=overrides.pop("keep_recent_tokens", 0),  # type: ignore[arg-type]
        reached_end=overrides.pop("reached_end", True),  # type: ignore[arg-type]
    )
    assert not overrides, overrides
    assert planned is not None
    return planned


class MockServices(PluginServices):
    """The plugin-facing services this suite drives, with the core's own ruler for coverage."""

    def __init__(
        self,
        events: tuple[EventView, ...] = (),
        *,
        state: Mapping[str, object] | None = None,
        version: int = 0,
        model_answer: str | None = None,
        model_error: Exception | None = None,
        refuse: bool = False,
    ) -> None:
        self.events = events
        self.state = state
        self.version = version
        self.model_answer = model_answer
        self.model_error = model_error
        self.refuse = refuse
        self.projections: list[dict[str, object]] = []
        self.staged: list[tuple[str, Mapping[str, object]]] = []
        self.queries: list[str] = []
        self.row: dict[str, object] | None = None

    # --- state ---

    def get_state(self, scope: Literal["session", "workspace"] = "session") -> tuple[Mapping[str, object] | None, int]:
        _ = scope
        return self.state, self.version

    def replace_state(self, state: Mapping[str, object], expected_version: int, scope: Literal["session", "workspace"] = "session") -> int:
        _ = scope
        assert expected_version == self.version, "state CAS conflict in the fake"
        self.state = state
        self.version += 1
        return self.version

    # --- events ---

    def stage_event(self, event_type: str, payload: Mapping[str, object]) -> None:
        self.staged.append((event_type, payload))

    def read_events(self, *, after_sequence: int = 0, limit: int = 100, include_payload: bool = True) -> tuple[EventView, ...]:
        _ = include_payload
        return tuple(event for event in self.events if event.sequence > after_sequence)[:limit]

    # --- window projection ---

    def project_window(self, *, covered_from_sequence: int, covered_to_sequence: int, replacement: str) -> None:
        if self.refuse:
            raise ValueError("core refused the projection")
        covered = tuple(event for event in self.events if event.sequence <= covered_to_sequence)
        covered_bytes = sum(event_payload_bytes(event) for event in covered)
        replacement_bytes = len(replacement.encode("utf-8"))
        assert replacement_bytes < covered_bytes, "the fake core measures what the real one does"
        self.projections.append(
            {
                "covered_from_sequence": covered_from_sequence,
                "covered_to_sequence": covered_to_sequence,
                "replacement": replacement,
            }
        )
        self.row = {
            "owner": OWNER,
            "covered_from_sequence": covered_from_sequence,
            "covered_to_sequence": covered_to_sequence,
            "covered_bytes": covered_bytes,
            "dropped_bytes": covered_bytes - replacement_bytes,
            "replacement": replacement,
            "covered_events": covered_to_sequence,
        }

    def window_projection(self) -> Mapping[str, object] | None:
        return self.row

    # --- model lane ---

    def model_query(self, prompt: str, *, model: str | None = None, max_output_tokens: int | None = None) -> str:
        _ = model, max_output_tokens
        self.queries.append(prompt)
        if self.model_error is not None:
            raise self.model_error
        return self.model_answer or ""


def _context() -> ToolContext:
    return ToolContext(
        workspace=Path("/tmp/workspace"),
        session_id=SESSION_ID,
        run_id=RUN_ID,
        invocation_id="invocation-1",
        revision_id=REVISION_ID,
        actor="plugin",
    )


def _usage(sequence: int, *, input_tokens: int | None) -> UsageFact:
    return UsageFact(
        session_id=SESSION_ID,
        run_id=RUN_ID,
        revision_id=REVISION_ID,
        logical_turn="turn-1",
        event_sequence=sequence,
        dispatch_sequence=sequence,
        provider_name="endpoint",
        model_name="test-model",
        usage=None if input_tokens is None else ProviderTokenUsage(input_tokens=input_tokens),
    )


# --- Budget math (ported threshold/reserve resolution) ---


def test_reserve_is_15_percent_of_the_window_with_the_legacy_floor() -> None:
    assert effective_reserve_tokens(200_000, DEFAULT_CONTEXT_RESERVE_TOKENS) == 30_000
    assert effective_reserve_tokens(10_000, 0) == 1_500
    assert effective_reserve_tokens(None, 0) == 0
    assert effective_reserve_tokens(0, 0) == 0
    # An explicit reserve always wins; only the defaulted one recovers from a small window.
    assert resolve_budget_reserve_tokens(200_000, reserve_tokens=1_000) == 1_000
    assert resolve_budget_reserve_tokens(200_000) == 30_000
    # Small-window recovery: the defaulted 16_384 floor cannot fit, so 15% is used instead.
    assert effective_reserve_tokens(10_000, DEFAULT_CONTEXT_RESERVE_TOKENS) == DEFAULT_CONTEXT_RESERVE_TOKENS
    assert resolve_budget_reserve_tokens(10_000) == 1_500


def test_threshold_clamps_an_explicit_value_and_derives_the_default_one() -> None:
    assert resolve_threshold_tokens(200_000, threshold_tokens=120_000) == 120_000
    # The explicit threshold is clamped to [1, window - 1], never past the window itself.
    assert resolve_threshold_tokens(200_000, threshold_tokens=999_999) == 199_999
    assert resolve_threshold_tokens(200_000, threshold_tokens=0) == 1
    assert resolve_threshold_tokens(200_000, threshold_tokens=None) == 200_000 - 30_000
    # No window and no explicit threshold leaves nothing to size a threshold with.
    assert resolve_threshold_tokens(None, threshold_tokens=None) == 0
    assert resolve_threshold_tokens(None, threshold_tokens=80_000) == 80_000


def test_trigger_subtracts_the_reserve_from_an_explicit_threshold() -> None:
    explicit = resolve_compaction_budget(None, threshold_tokens=40_000, reserve_tokens=4_000)
    assert explicit == plugin_module.resolve_compaction_budget(None, threshold_tokens=40_000, reserve_tokens=4_000)
    assert explicit.threshold_tokens == 40_000
    assert explicit.reserve_tokens == 4_000
    assert explicit.explicit_threshold is True
    assert explicit.trigger_tokens == 36_000
    # The derived threshold already has the reserve taken out, so it is not subtracted twice.
    derived = resolve_compaction_budget(200_000)
    assert derived.threshold_tokens == 170_000
    assert derived.trigger_tokens == 170_000
    assert derived.explicit_threshold is False
    # Nothing to size an unsized gate: it never fires.
    unsized = resolve_compaction_budget(None)
    assert unsized.trigger_tokens == 0
    assert should_compact(10**9, unsized) is False


def test_should_compact_compares_measured_tokens_against_the_trigger() -> None:
    budget = resolve_compaction_budget(None, threshold_tokens=1_000, reserve_tokens=0)
    assert should_compact(1_001, budget) is True
    assert should_compact(1_000, budget) is False
    assert should_compact(None, budget) is False
    assert should_compact(10**9, resolve_compaction_budget(None, threshold_tokens=0)) is False


# --- Deterministic replacement text (legacy shape, verbatim) ---


def test_deterministic_replacement_carries_the_legacy_sections_verbatim() -> None:
    plan = _plan()
    assert plan.covered_from_sequence == 1
    assert plan.covered_to_sequence == 8
    assert plan.covered_event_count == 8
    assert plan.covered_bytes == sum(event_payload_bytes(event) for event in _session_events())
    assert plan.retained_tool_result_count == 0
    # The exact provider-facing text: legacy sections, the dropped preview, and the anchor.
    assert render_deterministic_replacement(plan) == (
        "Runtime context projection:\n"
        "## Objective\n"
        "Rebuild the parser. The tokenizer must stay allocation free. Never reorder diagnostics.\n"
        "\n"
        "## Constraints\n"
        "- Rebuild the parser. The tokenizer must stay allocation free. Never reorder diagnostics.\n"
        "\n"
        "## Progress Completed\n"
        '- Tool result compacted: write ok path=src/parser.py content_preview="wrote 42 bytes to src/parser.py"\n'
        "\n"
        "## Blockers / Open Questions\n"
        '- Tool error compacted: read error path=src/parser.py error_preview="missing file"\n'
        "\n"
        "## Relevant Files / Commands / Errors\n"
        "- file:src/parser.py\n"
        "\n"
        "## Dropped Tool Preview\n"
        "Compacted 2 earlier tool results:\n"
        '1. write ok path=src/parser.py content_preview="wrote 42 bytes to src/parser.py"\n'
        '2. read error path=src/parser.py error_preview="missing file"\n'
        "continuity:8f451100f442b57f"
    )


def test_objective_is_previewed_at_160_chars_and_constraints_match_the_legacy_markers() -> None:
    long_prompt = "A" * 200
    events = (
        _event(1, "kernel.user_message", {"content": long_prompt}),
        _event(2, "kernel.user_message", {"content": "Always keep the public API stable."}),
        _event(3, "kernel.user_message", {"content": "Keep it boring."}),
        _intent(4),
        _model_turn(5, dispatch_sequence=4, output="ok"),
    )
    plan = _plan(events)
    assert plan.objective == f"{'A' * 160}..."
    # Only lines carrying a legacy marker word become constraints; plain lines do not.
    assert plan.constraints == ("Always keep the public API stable.",)
    text = render_deterministic_replacement(plan)
    assert "## Constraints\n- Always keep the public API stable." in text
    assert "Keep it boring." not in text
    # Nothing was dropped, so the legacy dropped-preview section is absent.
    assert "## Dropped Tool Preview" not in text


# --- Boundary selection: never split a call/result pair ---


def test_boundary_stops_before_an_unanswered_tool_call() -> None:
    events = (
        _event(1, "kernel.user_message", {"content": _USER_MESSAGE}),
        _intent(2),
        _model_turn(3, dispatch_sequence=2, tool_calls=(_WRITE_CALL,)),
        _intent(4),
        _model_turn(5, dispatch_sequence=4, output="still waiting"),
    )
    plan = _plan(events)
    # The batch opened at sequence 3 is never covered: only its call exists so far.
    assert plan.covered_to_sequence == 2
    assert plan.covered_tool_results == ()
    assert plan.covered_event_count == 2


def test_boundary_requires_every_call_of_a_multi_call_batch_to_be_answered() -> None:
    second_call = ToolCall("bash", {"command": "pytest -q"}, "call-3")
    events = (
        _event(1, "kernel.user_message", {"content": _USER_MESSAGE}),
        _intent(2),
        _model_turn(3, dispatch_sequence=2, tool_calls=(_WRITE_CALL, second_call)),
        _tool_completed(4, batch_sequence=3, call=_WRITE_CALL, content="wrote 42 bytes"),
    )
    plan = _plan(events)
    assert plan.covered_to_sequence == 2
    full = _plan((*events, _tool_completed(5, batch_sequence=3, call=second_call, content="1 passed")))
    assert full.covered_to_sequence == 5
    assert [fact.tool_name for fact in full.covered_tool_results] == ["write", "bash"]


def test_a_truncated_payload_ends_the_covered_prefix() -> None:
    events = list(_session_events())
    events[5] = EventView(
        sequence=6,
        event_type="kernel.model_turn",
        run_id=RUN_ID,
        revision_id=REVISION_ID,
        source="core",
        actor="core",
        payload={"truncated": True, "original_bytes": 4_096},
        payload_truncated=True,
    )
    plan = _plan(tuple(events))
    # The unreadable turn is never covered; the last balanced boundary before it is the intent.
    assert plan.covered_to_sequence == 5
    assert plan.covered_event_count == 5


def test_retention_keeps_the_newest_content_raw_at_the_measured_rate() -> None:
    events = _session_events()
    # 9_000 prompt tokens for 600 measured characters: 15 tokens per character.
    assert measured_tokens_per_character(events) == 15.0
    assert measured_dispatch_input_tokens(events) == 9_000
    assert recent_tail_character_budget(events, keep_recent_tokens=1_500, reached_end=True) == 100
    covered = _plan(events, keep_recent_tokens=1_500)
    # The last model turn is bigger than 100 characters, so it stays raw and the boundary
    # falls back to the last balanced sequence before it (the read batch's report).
    assert covered.covered_to_sequence == 7
    assert covered.retained_tool_result_count == 0
    # Without a measurement there is no honest conversion: nothing is retained.
    assert recent_tail_character_budget(events, keep_recent_tokens=1_500, reached_end=False) == 0
    assert recent_tail_character_budget(events[:1], keep_recent_tokens=1_500, reached_end=True) == 0


# --- Plan refusals and no-ops ---


def test_plan_is_none_when_the_window_has_nothing_new_to_cover() -> None:
    events = _session_events()
    assert plan_projection(events, previous_covered_to_sequence=8, keep_recent_tokens=0, reached_end=True) is None
    assert plan_projection((), previous_covered_to_sequence=None, keep_recent_tokens=0, reached_end=True) is None


def test_apply_projection_refuses_a_replacement_that_would_not_shrink_the_window() -> None:
    # One tiny event: any labelled projection of it is bigger than the event itself.
    events = (_event(1, "kernel.user_message", {"content": "hi"}),)
    services = MockServices(events)
    outcome = apply_projection(
        services,
        state=CompactionState(),
        version=0,
        config=CompactionConfig(keep_recent_tokens=0),
        reason="manual",
        events=events,
        reached_end=True,
        measured_input_tokens=None,
    )
    assert outcome.status == "refused"
    assert "would not shrink" in outcome.reason
    assert services.projections == []
    assert services.staged == []


def test_apply_projection_is_a_noop_when_nothing_is_coverable() -> None:
    services = MockServices(_session_events())
    outcome = apply_projection(
        services,
        state=CompactionState(cursor_sequence=8, last_projection=LastProjection(1, 8, 4_000, 1_000, "deterministic")),
        version=3,
        config=CompactionConfig(keep_recent_tokens=0),
        reason="manual",
        events=_session_events(),
        reached_end=True,
        measured_input_tokens=12_000,
    )
    assert outcome.status == "noop"
    assert outcome.reason == "nothing_new_to_cover"
    assert outcome.measured_input_tokens == 12_000
    assert services.projections == []
    assert services.state is None


# --- Applying a projection: core-measured accounting, state, staged event ---


def test_apply_projection_reports_the_core_measured_accounting() -> None:
    events = _session_events()
    services = MockServices(events)
    outcome = apply_projection(
        services,
        state=CompactionState(),
        version=0,
        config=CompactionConfig(keep_recent_tokens=0),
        reason="threshold",
        events=events,
        reached_end=True,
        measured_input_tokens=9_000,
    )
    assert outcome.status == "projected"
    assert outcome.summary_kind == "deterministic"
    assert outcome.covered_from_sequence == 1
    assert outcome.covered_to_sequence == 8
    assert outcome.covered_bytes == services.row["covered_bytes"]
    assert outcome.dropped_bytes == services.row["dropped_bytes"]
    assert outcome.dropped_bytes == outcome.covered_bytes - len(services.row["replacement"].encode("utf-8"))
    assert services.staged == [
        (
            CONTEXT_COMPACTED_EVENT,
            {
                "covered_from_sequence": 1,
                "covered_to_sequence": 8,
                "covered_bytes": outcome.covered_bytes,
                "dropped_bytes": outcome.dropped_bytes,
                "covered_events": 8,
                "summary_kind": "deterministic",
                "reason": "threshold",
                "measured_input_tokens": 9_000,
                "replacement_bytes": len(services.row["replacement"].encode("utf-8")),
            },
        )
    ]
    assert CompactionState.from_payload(services.state) == CompactionState(
        cursor_sequence=8,
        last_projection=LastProjection(
            covered_from_sequence=1,
            covered_to_sequence=8,
            covered_bytes=outcome.covered_bytes,
            dropped_bytes=outcome.dropped_bytes,
            summary_kind="deterministic",
        ),
    )


# --- Model summary lane (stubbed model_query) ---


def test_model_summary_lane_replaces_the_text_and_keeps_the_real_accounting() -> None:
    events = _session_events()
    services = MockServices(events, model_answer="  MODEL SUMMARY TEXT  ")
    outcome = apply_projection(
        services,
        state=CompactionState(),
        version=0,
        config=CompactionConfig(keep_recent_tokens=0, summary_enabled=True),
        reason="manual",
        events=events,
        reached_end=True,
        measured_input_tokens=9_000,
    )
    assert outcome.status == "projected"
    assert outcome.summary_kind == "model"
    anchor = continuity_anchor(summary_text="MODEL SUMMARY TEXT", dropped_count=2, retained_count=0)
    assert services.row["replacement"] == f"{PROJECTION_PREFIX}\nMODEL SUMMARY TEXT\n{anchor}"
    assert outcome.covered_bytes == services.row["covered_bytes"]
    assert len(services.queries) == 1
    prompt = services.queries[0]
    assert prompt.startswith(CONTINUITY_SUMMARY_SYSTEM_PROMPT)
    assert "tool=write status=ok\nwrote 42 bytes to src/parser.py" in prompt
    assert "user\n" + _USER_MESSAGE in prompt
    assert len(prompt.encode("utf-8")) <= 64 * 1024


def test_model_summary_failure_and_blank_answer_fall_back_to_the_deterministic_text() -> None:
    events = _session_events()
    failing = MockServices(events, model_error=RuntimeError("provider exploded"))
    failed = apply_projection(
        failing,
        state=CompactionState(),
        version=0,
        config=CompactionConfig(keep_recent_tokens=0, summary_enabled=True),
        reason="manual",
        events=events,
        reached_end=True,
        measured_input_tokens=9_000,
    )
    assert failed.status == "projected"
    assert failed.summary_kind == "deterministic"
    assert failing.row["replacement"] == render_deterministic_replacement(_plan(events))
    blank = MockServices(events, model_answer="   \n  ")
    blanked = apply_projection(
        blank,
        state=CompactionState(),
        version=0,
        config=CompactionConfig(keep_recent_tokens=0, summary_enabled=True),
        reason="manual",
        events=events,
        reached_end=True,
        measured_input_tokens=9_000,
    )
    assert blanked.summary_kind == "deterministic"


def test_a_model_summary_that_would_grow_the_window_falls_back_to_the_deterministic_text() -> None:
    events = _session_events()
    services = MockServices(events, model_answer="X" * 20_000)
    outcome = apply_projection(
        services,
        state=CompactionState(),
        version=0,
        config=CompactionConfig(keep_recent_tokens=0, summary_enabled=True),
        reason="manual",
        events=events,
        reached_end=True,
        measured_input_tokens=9_000,
    )
    assert outcome.status == "projected"
    assert outcome.summary_kind == "deterministic"
    assert services.row["replacement"] == render_deterministic_replacement(_plan(events))


def test_an_unshrinkable_prefix_is_refused_before_the_model_lane_is_asked() -> None:
    events = (_event(1, "kernel.user_message", {"content": "hi"}),)
    services = MockServices(events, model_answer="SHORT")
    outcome = apply_projection(
        services,
        state=CompactionState(),
        version=0,
        config=CompactionConfig(keep_recent_tokens=0, summary_enabled=True),
        reason="threshold",
        events=events,
        reached_end=True,
        measured_input_tokens=9_000,
    )
    assert outcome.status == "refused"
    assert services.queries == []


def test_summary_output_is_clipped_at_the_legacy_bound() -> None:
    assert clip_summary_text("  x  ") == "x"
    assert clip_summary_text("   ") is None
    assert clip_summary_text("y" * 9_000) == "y" * 8_000
    from voidcode.plugins.compaction.projection import CONTINUITY_SUMMARY_OUTPUT_CHARS

    assert CONTINUITY_SUMMARY_OUTPUT_CHARS == 8_000


def test_summary_prompt_stays_inside_the_model_query_bound() -> None:
    huge = "Z" * 70_000
    events = (_event(1, "kernel.user_message", {"content": huge}), _intent(2), _model_turn(3, dispatch_sequence=2, output=huge))
    prompt = summary_prompt(_plan(events))
    assert len(prompt.encode("utf-8")) <= 64 * 1024
    assert prompt.startswith(CONTINUITY_SUMMARY_SYSTEM_PROMPT)


def test_model_replacement_is_a_pure_function_of_the_summary() -> None:
    plan = _plan()
    text = render_model_replacement(plan, "SUMMARY")
    assert text == f"{PROJECTION_PREFIX}\nSUMMARY\n{continuity_anchor(summary_text='SUMMARY', dropped_count=2, retained_count=0)}"


# --- Observer: trigger, cursor, gating ---


def test_observer_projects_once_the_measured_dispatch_crosses_the_trigger() -> None:
    events = _session_events()
    services = MockServices(events)
    config = CompactionConfig(threshold_tokens=1_000, reserve_tokens=0, keep_recent_tokens=0)
    compaction_observer(_usage(8, input_tokens=9_000), _context(), services, config=config)
    assert len(services.projections) == 1
    state = CompactionState.from_payload(services.state)
    assert state.cursor_sequence == 8
    assert state.last_projection is not None
    # A second delivery for the same tip repeats nothing.
    compaction_observer(_usage(8, input_tokens=9_000), _context(), services, config=config)
    assert len(services.projections) == 1
    assert len(services.staged) == 1


def test_observer_stays_idle_below_the_trigger_without_or_unmeasured() -> None:
    events = _session_events()
    config = CompactionConfig(threshold_tokens=1_000, reserve_tokens=0, keep_recent_tokens=0)
    below = MockServices(events)
    compaction_observer(_usage(8, input_tokens=1_000), _context(), below, config=config)
    assert below.projections == []
    assert below.state is None
    unknown = MockServices(events)
    compaction_observer(_usage(8, input_tokens=None), _context(), unknown, config=config)
    assert unknown.projections == []
    disabled = MockServices(events)
    compaction_observer(_usage(8, input_tokens=9_000), _context(), disabled, config=CompactionConfig(enabled=False))
    assert disabled.projections == []
    assert disabled.state is None


def test_observer_ignores_fact_kinds_it_does_not_subscribe_to() -> None:
    services = MockServices(_session_events())
    config = CompactionConfig(threshold_tokens=1_000, reserve_tokens=0, keep_recent_tokens=0)
    fact = InvocationFinishedFact(
        session_id=SESSION_ID,
        run_id=RUN_ID,
        revision_id=REVISION_ID,
        logical_turn="turn-1",
        event_sequence=8,
        tool_name="write",
        tool_call_id="call-1",
        status="ok",
    )
    compaction_observer(fact, _context(), services, config=config)
    assert services.projections == []
    assert services.state is None


def test_observer_projects_at_run_finish_when_the_last_dispatch_crossed_the_trigger() -> None:
    services = MockServices(_session_events())
    config = CompactionConfig(threshold_tokens=1_000, reserve_tokens=0, keep_recent_tokens=0)
    fact = RunFinishedFact(
        session_id=SESSION_ID,
        run_id=RUN_ID,
        revision_id=REVISION_ID,
        logical_turn="turn-1",
        event_sequence=8,
        status="completed",
    )
    compaction_observer(fact, _context(), services, config=config)
    assert len(services.projections) == 1


# --- Manual command ---


def test_compact_command_reports_accounting_and_noops_on_a_second_call() -> None:
    events = _session_events()
    services = MockServices(events)
    command = CompactCommand(CompactionConfig(keep_recent_tokens=0))
    invocation = CommandInvocation(context=_context(), name="compact", arguments={})
    first = command.invoke(invocation, services)
    assert first.status == "ok"
    assert first.data is not None
    assert first.data["noop"] is False
    assert first.data["covered_from_sequence"] == 1
    assert first.data["covered_to_sequence"] == 8
    assert first.data["covered_bytes"] == services.row["covered_bytes"]
    assert first.data["dropped_bytes"] == services.row["dropped_bytes"]
    assert first.data["summary_kind"] == "deterministic"
    assert (
        first.output
        == f"Compacted events 1-8: {services.row['covered_bytes']} bytes covered -> {services.row['dropped_bytes']} dropped (deterministic summary)."
    )

    second = command.invoke(invocation, services)
    assert second.status == "ok"
    assert second.data is not None
    assert second.data["noop"] is True
    assert second.data["reason"] == "nothing_new_to_cover"
    # The same accounting the core holds, not a re-measurement.
    assert second.data["covered_bytes"] == services.row["covered_bytes"]
    assert second.data["dropped_bytes"] == services.row["dropped_bytes"]
    assert "Nothing new to compact" in (second.output or "")
    assert len(services.projections) == 1


def test_compact_command_refusal_and_empty_session_are_visible() -> None:
    events = (_event(1, "kernel.user_message", {"content": "hi"}),)
    services = MockServices(events)
    command = CompactCommand(CompactionConfig(keep_recent_tokens=0))
    invocation = CommandInvocation(context=_context(), name="compact", arguments={})
    refused = command.invoke(invocation, services)
    assert refused.status == "error"
    assert "compaction refused" in (refused.error or "")
    empty = command.invoke(invocation, MockServices(()))
    assert empty.status == "ok"
    assert empty.data == {"noop": True, "reason": "no_events"}


def test_compact_command_declares_a_busy_gated_session_effect() -> None:
    definition = CompactCommand(CompactionConfig()).definition
    assert definition.name == "compact"
    assert definition.busy_gate is True
    assert {effect.value for effect in definition.effects} == {"session"}
    assert definition.client_syntax == "/compact"


# --- State and config validation ---


def test_state_round_trips_and_rejects_unknown_keys() -> None:
    state = CompactionState(cursor_sequence=7, last_projection=LastProjection(1, 6, 4_096, 1_024, "model"))
    payload = state.to_payload()
    assert payload["schema_version"] == SCHEMA_VERSION
    assert CompactionState.from_payload(payload) == state
    assert CompactionState.from_payload(None) == CompactionState()
    assert CompactionState.from_payload({"schema_version": 1, "cursor_sequence": 0, "last_projection": None}) == CompactionState()
    with pytest.raises(ValueError, match="unsupported field"):
        CompactionState.from_payload({"schema_version": 1, "cursor_sequence": 0, "last_projection": None, "extra": 1})
    with pytest.raises(ValueError, match="schema_version"):
        CompactionState.from_payload({"schema_version": 2, "cursor_sequence": 0, "last_projection": None})
    with pytest.raises(ValueError, match="cursor_sequence"):
        CompactionState.from_payload({"schema_version": 1, "cursor_sequence": True, "last_projection": None})
    with pytest.raises(ValueError, match="missing field"):
        CompactionState.from_payload({"schema_version": 1, "cursor_sequence": 0, "last_projection": {"summary_kind": "model"}})
    with pytest.raises(ValueError, match="summary_kind"):
        CompactionState.from_payload(
            {
                "schema_version": 1,
                "cursor_sequence": 0,
                "last_projection": {
                    "covered_from_sequence": 1,
                    "covered_to_sequence": 2,
                    "covered_bytes": 3,
                    "dropped_bytes": 1,
                    "summary_kind": "guessed",
                },
            }
        )


def test_config_parsing_uses_legacy_defaults_and_rejects_unknown_keys() -> None:
    assert parse_compaction_config(None) == CompactionConfig()
    default = parse_compaction_config({})
    assert default.enabled is True
    assert default.summary_enabled is False
    assert default.threshold_tokens is None
    assert default.reserve_tokens is None
    assert default.keep_recent_tokens == DEFAULT_KEEP_RECENT_TOKENS
    parsed = parse_compaction_config(
        {"enabled": False, "threshold_tokens": 90_000, "reserve_tokens": 0, "keep_recent_tokens": 1_000, "summary_enabled": True}
    )
    assert parsed == CompactionConfig(enabled=False, threshold_tokens=90_000, reserve_tokens=0, keep_recent_tokens=1_000, summary_enabled=True)
    with pytest.raises(ValueError, match="unsupported field"):
        parse_compaction_config({"threshold": 1})
    with pytest.raises(ValueError, match="must be a boolean"):
        parse_compaction_config({"enabled": "yes"})
    with pytest.raises(ValueError, match="threshold_tokens"):
        parse_compaction_config({"threshold_tokens": -1})
    with pytest.raises(ValueError, match="reserve_tokens"):
        parse_compaction_config({"reserve_tokens": 1.5})
    with pytest.raises(ValueError, match="keep_recent_tokens"):
        parse_compaction_config({"keep_recent_tokens": True})


def test_create_plugin_registers_the_command_and_the_observer() -> None:
    plugin = create_plugin({"threshold_tokens": 1_000})
    assert [command.definition.name for command in plugin.commands] == ["compact"]
    assert plugin.observer is not None
    assert plugin.tools == ()
    assert plugin.context is None
    assert plugin.migrate_state is None
