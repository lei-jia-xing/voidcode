"""Unit tests for the compaction plugin: budget math, projection text, boundary, state, lanes."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from tests.fixtures.fake_services import FakeServices
from voidcode.core.storage.store import StoredEvent
from voidcode.core.transcript import context_records
from voidcode.plugins.compaction import compaction_plugin, create_plugin
from voidcode.plugins.compaction.compaction_plugin import (
    CONTEXT_COMPACTED_EVENT,
    SCHEMA_VERSION,
    CompactCommand,
    CompactionConfig,
    CompactionState,
    LastProjection,
    apply_projection,
    compaction_observer,
    parse_compaction_config,
    prepare_compaction_deactivation,
)
from voidcode.plugins.compaction.projection import (
    CONTINUITY_SUMMARY_SYSTEM_PROMPT,
    DEFAULT_CONTEXT_RESERVE_TOKENS,
    DEFAULT_KEEP_RECENT_TOKENS,
    PROJECTION_PREFIX,
    clip_summary_text,
    continuity_anchor,
    effective_reserve_tokens,
    measured_dispatch_input_tokens,
    measured_tokens_per_character,
    plan_projection,
    provider_visible_characters,
    recent_tail_character_budget,
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
from voidcode.sdk.json_values import json_wire_object
from voidcode.sdk.provider import ProviderMessage, ProviderTokenUsage, ProviderTurnResult
from voidcode.sdk.services import EventView
from voidcode.sdk.tools import Change, TextOutput, ToolCall, ToolFailure, ToolSuccess

SESSION_ID = "session-1"
RUN_ID = "run-1"
REVISION_ID = "rev-1"
OWNER = "compaction"

_USER_MESSAGE = "Rebuild the parser. The tokenizer must stay allocation free. Never reorder diagnostics."
_WRITE_CALL = ToolCall("write", {"path": "src/parser.py"}, "call-1")
_READ_CALL = ToolCall("read", {"path": "src/parser.py"}, "call-2")


def _loaded(payload: Mapping[str, object] | None) -> CompactionState:
    services = FakeServices(owner=OWNER)
    if payload is not None:
        services.seed_state(payload)
    return compaction_plugin._store(services).load()[0]


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


def _intent(
    sequence: int,
    *,
    message_characters: int = 400,
    tool_schema_characters: int = 100,
    image_count: int = 0,
) -> EventView:
    segments = (
        [
            {
                "role": "user",
                "characters": 10,
                "sha256": "0" * 64,
                "tool_calls": 0,
                "reasoning_characters": 0,
                "image_count": image_count,
                "image_bytes": 100 if image_count else 0,
            }
        ]
        if image_count
        else []
    )
    return _event(
        sequence,
        "core.provider_intent",
        {
            "provider": "endpoint",
            "model": "test-model",
            "request_window": {
                "message_characters": message_characters,
                "tool_schema_characters": tool_schema_characters,
                "segments": segments,
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
        "core.model_turn",
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
    result = (
        ToolSuccess(call.tool_name, TextOutput(content), changes=(Change("src/parser.py"),) if call == _WRITE_CALL else ())
        if error is None
        else ToolFailure(call.tool_name, error, TextOutput(content))
    )
    return _event(
        sequence,
        "core.tool_completed",
        {"batch_sequence": batch_sequence, "call": encode_tool_call(call), "result": encode_tool_result(result)},
    )


def _session_events() -> tuple[EventView, ...]:
    """One complete session: prompt, a write batch, a failing read batch, and a final answer."""
    return (
        _event(1, "core.user_message", {"content": _USER_MESSAGE}),
        _intent(2),
        _model_turn(3, dispatch_sequence=2, tool_calls=(_WRITE_CALL,)),
        _tool_completed(4, batch_sequence=3, call=_WRITE_CALL, content="wrote 42 bytes to src/parser.py"),
        _intent(5, message_characters=500),
        _model_turn(6, dispatch_sequence=5, tool_calls=(_READ_CALL,), input_tokens=9_000),
        _tool_completed(7, batch_sequence=6, call=_READ_CALL, error="missing file"),
        _model_turn(8, dispatch_sequence=5, output="done", input_tokens=9_000),
    )


def _message_events(count: int) -> tuple[EventView, ...]:
    return tuple(_event(sequence, "core.user_message", {"content": f"message {sequence}"}) for sequence in range(1, count + 1))


def _records(events):
    return context_records(
        tuple(StoredEvent(SESSION_ID, e.sequence, e.run_id, e.event_type, e.source, e.actor, e.revision_id, e.payload) for e in events)
    )


def _size(event):
    return len(json.dumps(json_wire_object(event.payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _plan(events: tuple[EventView, ...] = (), **overrides: object):
    planned = plan_projection(
        _records(events or _session_events()),
        previous_covered_to_sequence=overrides.pop("previous_covered_to_sequence", None),  # type: ignore[arg-type]
        keep_recent_tokens=overrides.pop("keep_recent_tokens", 0),  # type: ignore[arg-type]
        reached_end=overrides.pop("reached_end", True),  # type: ignore[arg-type]
    )
    assert not overrides, overrides
    assert planned is not None
    return planned


def _context() -> ToolContext:
    return ToolContext(
        workspace=Path("/tmp/workspace"),
        session_id=SESSION_ID,
        run_id=RUN_ID,
        invocation_id="invocation-1",
        revision_id=REVISION_ID,
        actor="plugin",
    )


def _usage(sequence: int, *, input_tokens: int | None, input_limit_tokens: int | None = None) -> UsageFact:
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
        input_limit_tokens=input_limit_tokens,
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
    # Zero is the explicit "off" spelling with or without a window; it never becomes 1.
    assert resolve_threshold_tokens(200_000, threshold_tokens=0) == 0
    assert resolve_threshold_tokens(None, threshold_tokens=0) == 0
    assert resolve_threshold_tokens(200_000, threshold_tokens=None) == 200_000 - 30_000
    # No window and no explicit threshold leaves nothing to size a threshold with.
    assert resolve_threshold_tokens(None, threshold_tokens=None) == 0
    assert resolve_threshold_tokens(None, threshold_tokens=80_000) == 80_000


def test_trigger_subtracts_the_reserve_from_an_explicit_threshold() -> None:
    explicit = resolve_compaction_budget(None, threshold_tokens=40_000, reserve_tokens=4_000)
    assert explicit.threshold_tokens == 40_000
    assert explicit.reserve_tokens == 4_000
    assert explicit.explicit_threshold is True
    assert explicit.trigger_tokens == 36_000
    # The derived threshold already has the reserve taken out, so it is not subtracted twice.
    derived = resolve_compaction_budget(200_000)
    assert derived.threshold_tokens == 170_000
    assert derived.trigger_tokens == 170_000
    assert derived.explicit_threshold is False
    # Nothing to size an unsized gate: it never fires, with or without a window.
    unsized = resolve_compaction_budget(None)
    assert unsized.trigger_tokens == 0
    assert should_compact(10**9, unsized) is False
    zero = resolve_compaction_budget(200_000, threshold_tokens=0)
    assert zero.trigger_tokens == 0
    assert should_compact(10**9, zero) is False
    # A defaulted reserve that cannot fit under an explicit threshold is dropped, not
    # allowed to collapse the trigger to 1.
    small = resolve_compaction_budget(None, threshold_tokens=10_000)
    assert small.reserve_tokens == 0
    assert small.trigger_tokens == 10_000
    with pytest.raises(ValueError, match="reserve_tokens must be smaller than threshold_tokens"):
        resolve_compaction_budget(None, threshold_tokens=10_000, reserve_tokens=10_000)
    # An explicit reserve that leaves no usable derived threshold is refused.
    with pytest.raises(ValueError, match="no usable compaction threshold"):
        resolve_compaction_budget(200_000, reserve_tokens=199_999)


def test_should_compact_compares_measured_tokens_against_the_trigger() -> None:
    budget = resolve_compaction_budget(None, threshold_tokens=1_000, reserve_tokens=0)
    assert should_compact(1_001, budget) is True
    assert should_compact(1_000, budget) is False
    assert should_compact(None, budget) is False
    assert should_compact(10**9, resolve_compaction_budget(None, threshold_tokens=0)) is False


# --- Plan shape: coverage, counts, and the material handed to the model ---


def test_plan_carries_the_coverage_and_the_summary_material() -> None:
    plan = _plan()
    assert plan.covered_from_sequence == 1
    assert plan.covered_to_sequence == 8
    assert plan.covered_event_count == 8
    assert plan.covered_bytes == sum(_size(event) for event in _session_events())
    assert plan.covered_tool_result_count == 2
    assert plan.retained_tool_result_count == 0
    assert plan.summary_truncated is False
    material = "\n\n".join(plan.summary_material)
    assert _USER_MESSAGE in material
    assert "tool=write status=ok" in material
    assert "args=" in material
    assert "src/parser.py" in material
    assert "wrote 42 bytes to src/parser.py" in material
    assert "tool=read status=error" in material
    assert "missing file" in material


def test_a_todo_result_reaches_the_summary_material_without_per_tool_discard() -> None:
    """No per-tool discard: a lone todo result still reaches the model summary."""
    todo_call = ToolCall("todo", {"op": "view"}, "call-todo")
    events = (
        _event(1, "core.user_message", {"content": _USER_MESSAGE}),
        _intent(2),
        _model_turn(3, dispatch_sequence=2, tool_calls=(todo_call,)),
        _tool_completed(4, batch_sequence=3, call=todo_call, content="2 phases, 1 in progress"),
        _model_turn(5, dispatch_sequence=2, output="noted", input_tokens=9_000),
    )
    material = "\n\n".join(_plan(events).summary_material)
    assert "tool=todo status=ok" in material
    assert "2 phases, 1 in progress" in material


def test_a_long_prefix_keeps_the_earliest_constraints_and_the_newest_work() -> None:
    write_call = ToolCall(
        "write",
        {"path": "src/parser.py", "content": "ALLOCATION-FREE-CONTENT"},
        "call-write",
    )
    events = [
        _event(1, "core.user_message", {"content": _USER_MESSAGE}),
        _intent(2),
        _model_turn(3, dispatch_sequence=2, tool_calls=(write_call,)),
        _tool_completed(4, batch_sequence=3, call=write_call, content="wrote 42 bytes to src/parser.py"),
    ]
    for index in range(30):
        output = f"WORK-{index:02d}: " + ("x" * 2_500)
        if index == 29:
            output = "NEWEST-COVERED-WORK " + ("x" * 2_500)
        events.append(_model_turn(5 + index, dispatch_sequence=2, output=output, input_tokens=9_000))
    plan = _plan(tuple(events), keep_recent_tokens=0)
    assert plan.summary_truncated is True
    prompt = summary_prompt(plan)
    # The earliest constraint survives, even though the material is a sample.
    assert _USER_MESSAGE in prompt
    # The intent a result line alone would lose reaches the summarizer.
    assert "src/parser.py" in prompt
    assert "ALLOCATION-FREE-CONTENT" in prompt
    # The newest covered work survives, not only the oldest handful of events.
    assert "NEWEST-COVERED-WORK" in prompt
    # Omitted material and the trust boundary are disclosed.
    assert "omitted" in prompt
    assert "untrusted data" in prompt
    assert len(prompt.encode("utf-8")) <= 64 * 1024


# --- Boundary selection: never split a call/result pair ---


def test_boundary_stops_before_an_unanswered_tool_call() -> None:
    events = (
        _event(1, "core.user_message", {"content": _USER_MESSAGE}),
        _intent(2),
        _model_turn(3, dispatch_sequence=2, tool_calls=(_WRITE_CALL,)),
        _intent(4),
        _model_turn(5, dispatch_sequence=4, output="still waiting"),
    )
    plan = _plan(events)
    # The batch opened at sequence 3 is never covered: only its call exists so far.
    assert plan.covered_to_sequence == 2
    assert plan.covered_tool_result_count == 0
    assert plan.covered_event_count == 2


def test_boundary_requires_every_call_of_a_multi_call_batch_to_be_answered() -> None:
    second_call = ToolCall("bash", {"command": "pytest -q"}, "call-3")
    events = (
        _event(1, "core.user_message", {"content": _USER_MESSAGE}),
        _intent(2),
        _model_turn(3, dispatch_sequence=2, tool_calls=(_WRITE_CALL, second_call)),
        _tool_completed(4, batch_sequence=3, call=_WRITE_CALL, content="wrote 42 bytes"),
    )
    plan = _plan(events)
    assert plan.covered_to_sequence == 2
    full = _plan((*events, _tool_completed(5, batch_sequence=3, call=second_call, content="1 passed")))
    assert full.covered_to_sequence == 5
    assert full.covered_tool_result_count == 2


@pytest.mark.parametrize("count", [601, 1201])
def test_read_window_pages_to_the_session_end_without_duplicates_or_gaps(count: int) -> None:
    records, reached_end = compaction_plugin._read_window(FakeServices(_message_events(count)))
    assert reached_end is True
    assert [record.sequence for record in records] == list(range(1, count + 1))
    assert all(record.safe_boundary for record in records)


def test_paging_across_a_native_batch_boundary_keeps_the_result_coverable() -> None:
    late_call = ToolCall("read", {"path": "late.txt"}, "late-call")
    events = (
        *_message_events(598),
        _intent(599),
        _model_turn(600, dispatch_sequence=599, tool_calls=(late_call,)),
        _tool_completed(601, batch_sequence=600, call=late_call, content="late read"),
    )
    records, reached_end = compaction_plugin._read_window(FakeServices(events))
    assert reached_end is True
    assert [record.sequence for record in records] == list(range(1, 602))
    assert not records[599].safe_boundary
    assert records[600].safe_boundary
    plan = plan_projection(records, previous_covered_to_sequence=None, keep_recent_tokens=0, reached_end=reached_end)
    assert plan is not None
    assert plan.covered_to_sequence == 601


def test_plan_covers_a_completed_batch_beyond_the_old_600_event_cap() -> None:
    late_call = ToolCall("write", {"path": "late.txt", "content": "late content"}, "late-call")
    events = (
        *_message_events(600),
        _intent(601),
        _model_turn(602, dispatch_sequence=601, tool_calls=(late_call,)),
        _tool_completed(603, batch_sequence=602, call=late_call, content="late result"),
    )
    records, reached_end = compaction_plugin._read_window(FakeServices(events))
    assert reached_end is True
    plan = plan_projection(records, previous_covered_to_sequence=None, keep_recent_tokens=0, reached_end=reached_end)
    assert plan is not None
    assert plan.covered_to_sequence == 603
    material = "\n\n".join(plan.summary_material)
    assert "late result" in material
    assert "late.txt" in material


def test_manual_compact_projects_beyond_600_events() -> None:
    services = FakeServices(_message_events(601), model_answer="the long session, summarized")
    command = CompactCommand(CompactionConfig(keep_recent_tokens=0))
    result = command.invoke(CommandInvocation(context=_context(), name="compact", arguments={}), services)
    assert result.status == "ok"
    assert result.data is not None
    assert result.data["noop"] is False
    assert result.data["covered_to_sequence"] == 601
    assert "message 601" in services.prompts[0]


def test_run_finished_observes_beyond_600_events_with_an_explicit_threshold() -> None:
    events = (
        *_message_events(600),
        _intent(601),
        _model_turn(602, dispatch_sequence=601, output="done", input_tokens=9_000),
    )
    services = FakeServices(events, model_answer="the long session, summarized")
    config = CompactionConfig(threshold_tokens=1_000, reserve_tokens=0, keep_recent_tokens=0)
    fact = RunFinishedFact(
        session_id=SESSION_ID,
        run_id=RUN_ID,
        revision_id=REVISION_ID,
        logical_turn="turn-1",
        event_sequence=602,
        status="completed",
    )
    compaction_observer(fact, _context(), services, config=config)
    assert len(services.projections) == 1
    state = compaction_plugin._store(services).load()[0]
    assert state.last_projection is not None
    assert state.last_projection.covered_to_sequence == 602


def test_large_payloads_remain_coverable_without_silent_truncation() -> None:
    events = list(_session_events())
    events[7] = _model_turn(8, dispatch_sequence=5, output="完整正文" * 30_000)
    plan = _plan(tuple(events))
    assert plan.covered_to_sequence == 8
    assert plan.covered_event_count == 8
    assert plan.covered_bytes == sum(_size(event) for event in events)


def test_retention_spends_the_tail_budget_in_provider_visible_characters() -> None:
    events = _session_events()
    # 9_000 prompt tokens for 600 measured characters: 15 tokens per character.
    assert measured_tokens_per_character(_records(events)) == 15.0
    assert measured_dispatch_input_tokens(_records(events)) == 9_000
    assert recent_tail_character_budget(_records(events), keep_recent_tokens=1_500, reached_end=True) == 100
    covered = _plan(events, keep_recent_tokens=1_500)
    # The newest six events are 49 provider-visible characters; adding the 87-character
    # user message exceeds the 100-character budget, so the prompt is covered and the
    # recent intents/turns/tool results stay raw.
    assert covered.covered_to_sequence == 1
    assert covered.retained_tool_result_count == 2
    # Without a measurement there is no honest conversion: nothing is retained.
    assert recent_tail_character_budget(_records(events), keep_recent_tokens=1_500, reached_end=False) == 0
    assert recent_tail_character_budget(_records(events[:1]), keep_recent_tokens=1_500, reached_end=True) == 0


def test_retention_counts_provider_visible_characters_not_payload_bytes() -> None:
    multibyte = "解析器" * 30  # 90 characters, 270 UTF-8 bytes.
    events = (
        _event(1, "core.user_message", {"content": _USER_MESSAGE}),
        _intent(2, message_characters=500),
        _model_turn(3, dispatch_sequence=2, output="done", input_tokens=9_000),
        _event(4, "core.user_message", {"content": multibyte}),
    )
    records = _records(events)
    assert provider_visible_characters(records[3]) == 90
    assert records[3].payload_bytes > 100, "payload bytes and provider-visible characters diverge"
    assert recent_tail_character_budget(records, keep_recent_tokens=1_500, reached_end=True) == 100
    covered = _plan(events, keep_recent_tokens=1_500)
    # 90 + "done" (4) fits; adding the 87-character first prompt does not. The multibyte
    # message is retained by character count even though its payload is over the budget.
    assert covered.covered_to_sequence == 1


def test_an_image_bearing_newest_dispatch_disables_the_retention_rate() -> None:
    events = (
        _event(1, "core.user_message", {"content": _USER_MESSAGE}),
        _intent(2, image_count=1),
        _model_turn(3, dispatch_sequence=2, output="done", input_tokens=9_000),
    )
    records = _records(events)
    assert measured_tokens_per_character(records) is None
    assert recent_tail_character_budget(records, keep_recent_tokens=1_500, reached_end=True) == 0
    covered = _plan(events, keep_recent_tokens=1_500)
    # With no honest rate the cut is the newest boundary: the prefix is covered as far as
    # it can be rather than converting image tokens through a text-only character count.
    assert covered.covered_to_sequence == 3


# --- Plan no-ops ---


def test_plan_is_none_when_the_window_has_nothing_new_to_cover() -> None:
    events = _session_events()
    assert plan_projection(_records(events), previous_covered_to_sequence=8, keep_recent_tokens=0, reached_end=True) is None
    assert plan_projection((), previous_covered_to_sequence=None, keep_recent_tokens=0, reached_end=True) is None


def test_apply_projection_raises_for_a_summary_that_would_not_shrink_the_window() -> None:
    # One tiny event: any labelled projection of it is bigger than the event itself.
    events = (_event(1, "core.user_message", {"content": "hi"}),)
    services = FakeServices(events, model_answer="a summary longer than the two bytes it replaces")
    with pytest.raises(ValueError, match="would not shrink"):
        apply_projection(
            services,
            state=CompactionState(),
            version=0,
            config=CompactionConfig(keep_recent_tokens=0),
            reason="manual",
            events=_records(events),
            reached_end=True,
            measured_input_tokens=None,
        )
    assert services.projections == []
    assert services.staged == []
    assert services.state is None


def test_apply_projection_is_a_noop_when_nothing_is_coverable() -> None:
    services = FakeServices(_session_events())
    outcome = apply_projection(
        services,
        state=CompactionState(cursor_sequence=8, last_projection=LastProjection(1, 8, 4_000, 1_000, "model")),
        version=3,
        config=CompactionConfig(keep_recent_tokens=0),
        reason="manual",
        events=_records(_session_events()),
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
    services = FakeServices(events, model_answer="the parser work, summarized")
    outcome = apply_projection(
        services,
        state=CompactionState(),
        version=0,
        config=CompactionConfig(keep_recent_tokens=0),
        reason="threshold",
        events=_records(events),
        reached_end=True,
        measured_input_tokens=9_000,
    )
    assert outcome.status == "projected"
    assert outcome.summary_kind == "model"
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
                "summary_kind": "model",
                "reason": "threshold",
                "measured_input_tokens": 9_000,
                "replacement_bytes": len(services.row["replacement"].encode("utf-8")),
            },
        )
    ]
    assert compaction_plugin._store(services).load()[0] == CompactionState(
        cursor_sequence=8,
        last_projection=LastProjection(
            covered_from_sequence=1,
            covered_to_sequence=8,
            covered_bytes=outcome.covered_bytes,
            dropped_bytes=outcome.dropped_bytes,
            summary_kind="model",
        ),
    )


# --- Model summary (stubbed model_query): the only lane ---


def test_model_summary_replaces_the_text_and_keeps_the_real_accounting() -> None:
    events = _session_events()
    services = FakeServices(events, model_answer="  MODEL SUMMARY TEXT  ")
    outcome = apply_projection(
        services,
        state=CompactionState(),
        version=0,
        config=CompactionConfig(keep_recent_tokens=0),
        reason="manual",
        events=_records(events),
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
    assert "tool=write status=ok" in prompt
    assert "args=" in prompt
    assert "src/parser.py" in prompt
    assert "wrote 42 bytes to src/parser.py" in prompt
    assert "user\n" + _USER_MESSAGE in prompt
    assert len(prompt.encode("utf-8")) <= 64 * 1024


def test_model_summary_failure_and_blank_answer_raise_and_stage_nothing() -> None:
    events = _session_events()
    failing = FakeServices(events, model_error=RuntimeError("provider exploded"))
    with pytest.raises(RuntimeError, match="provider exploded"):
        apply_projection(
            failing,
            state=CompactionState(),
            version=0,
            config=CompactionConfig(keep_recent_tokens=0),
            reason="manual",
            events=_records(events),
            reached_end=True,
            measured_input_tokens=9_000,
        )
    assert failing.projections == []
    assert failing.staged == []
    assert failing.state is None
    blank = FakeServices(events, model_answer="   \n  ")
    with pytest.raises(ValueError, match="blank"):
        apply_projection(
            blank,
            state=CompactionState(),
            version=0,
            config=CompactionConfig(keep_recent_tokens=0),
            reason="manual",
            events=_records(events),
            reached_end=True,
            measured_input_tokens=9_000,
        )
    assert blank.projections == []
    assert blank.staged == []
    assert blank.state is None


def test_a_model_summary_that_would_grow_the_window_raises_and_stages_nothing() -> None:
    events = _session_events()
    services = FakeServices(events, model_answer="X" * 20_000)
    with pytest.raises(ValueError, match="would not shrink"):
        apply_projection(
            services,
            state=CompactionState(),
            version=0,
            config=CompactionConfig(keep_recent_tokens=0),
            reason="manual",
            events=_records(events),
            reached_end=True,
            measured_input_tokens=9_000,
        )
    assert services.projections == []
    assert services.staged == []
    assert services.state is None


def test_a_failed_core_declaration_stages_no_state_or_event() -> None:
    events = _session_events()
    services = FakeServices(events, model_answer="the parser work, summarized")
    services.refuse = True
    with pytest.raises(ValueError, match="core refused"):
        apply_projection(
            services,
            state=CompactionState(),
            version=0,
            config=CompactionConfig(keep_recent_tokens=0),
            reason="threshold",
            events=_records(events),
            reached_end=True,
            measured_input_tokens=9_000,
        )
    assert services.projections == []
    assert services.staged == []
    assert services.state is None


def test_summary_output_is_clipped_at_the_legacy_bound() -> None:
    assert clip_summary_text("  x  ") == "x"
    assert clip_summary_text("   ") is None
    assert clip_summary_text("y" * 9_000) == "y" * 8_000
    from voidcode.plugins.compaction.projection import CONTINUITY_SUMMARY_OUTPUT_CHARS

    assert CONTINUITY_SUMMARY_OUTPUT_CHARS == 8_000


def test_summary_prompt_stays_inside_the_model_query_bound() -> None:
    huge = "Z" * 70_000
    events = (_event(1, "core.user_message", {"content": huge}), _intent(2), _model_turn(3, dispatch_sequence=2, output=huge))
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
    services = FakeServices(events, model_answer="the parser work, summarized")
    config = CompactionConfig(threshold_tokens=1_000, reserve_tokens=0, keep_recent_tokens=0)
    compaction_observer(_usage(8, input_tokens=9_000), _context(), services, config=config)
    assert len(services.projections) == 1
    state = compaction_plugin._store(services).load()[0]
    assert state.cursor_sequence == 8
    assert state.last_projection is not None
    # A second delivery for the same tip repeats nothing.
    compaction_observer(_usage(8, input_tokens=9_000), _context(), services, config=config)
    assert len(services.projections) == 1
    assert len(services.staged) == 1


def test_observer_uses_the_persisted_input_limit_without_an_explicit_threshold() -> None:
    services = FakeServices(_session_events(), model_answer="the parser work, summarized")
    config = CompactionConfig(keep_recent_tokens=0)
    # 200_000 - the default 15% reserve = a 170_000 trigger; 180_000 crosses it.
    compaction_observer(_usage(8, input_tokens=180_000, input_limit_tokens=200_000), _context(), services, config=config)
    assert len(services.projections) == 1
    assert compaction_plugin._store(services).load()[0].last_projection is not None


def test_observer_absent_input_limit_stays_off_without_an_explicit_threshold() -> None:
    services = FakeServices(_session_events())
    compaction_observer(_usage(8, input_tokens=10**9), _context(), services, config=CompactionConfig(keep_recent_tokens=0))
    assert services.projections == []
    assert services.state is None


def test_observer_explicit_threshold_still_wins_with_a_persisted_limit() -> None:
    services = FakeServices(_session_events(), model_answer="the parser work, summarized")
    config = CompactionConfig(threshold_tokens=100_000, reserve_tokens=0, keep_recent_tokens=0)
    # The derived gate for 1_000_000 would be 850_000; the explicit threshold compacts at
    # 100_000, so 200_000 measured input still projects.
    compaction_observer(_usage(8, input_tokens=200_000, input_limit_tokens=1_000_000), _context(), services, config=config)
    assert len(services.projections) == 1


def test_observer_stays_idle_below_the_trigger_without_or_unmeasured() -> None:
    events = _session_events()
    config = CompactionConfig(threshold_tokens=1_000, reserve_tokens=0, keep_recent_tokens=0)
    below = FakeServices(events)
    compaction_observer(_usage(8, input_tokens=1_000), _context(), below, config=config)
    assert below.projections == []
    assert below.state is None
    unknown = FakeServices(events)
    compaction_observer(_usage(8, input_tokens=None), _context(), unknown, config=config)
    assert unknown.projections == []
    disabled = FakeServices(events)
    compaction_observer(_usage(8, input_tokens=9_000), _context(), disabled, config=CompactionConfig(enabled=False))
    assert disabled.projections == []
    assert disabled.state is None


def test_observer_does_not_read_history_when_the_gate_is_unsized() -> None:
    class _NoReadServices(FakeServices):
        def read_context_history(self, *, after_sequence: int = 0, limit: int = 600):
            raise AssertionError("an unsized gate must not read the session prefix")

    services = _NoReadServices(_session_events())
    fact = RunFinishedFact(
        session_id=SESSION_ID,
        run_id=RUN_ID,
        revision_id=REVISION_ID,
        logical_turn="turn-1",
        event_sequence=8,
        status="completed",
    )
    compaction_observer(fact, _context(), services, config=CompactionConfig())
    assert services.projections == []
    assert services.state is None


def test_observer_ignores_fact_kinds_it_does_not_subscribe_to() -> None:
    services = FakeServices(_session_events())
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
    services = FakeServices(_session_events(), model_answer="the parser work, summarized")
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
    services = FakeServices(events, model_answer="the parser work, summarized")
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
    assert first.data["summary_kind"] == "model"
    from voidcode.sdk.views import FieldsViewData

    assert first.presentation.view_type == "fields"
    rows = {row.label: row.value for row in FieldsViewData.from_payload(first.presentation.data).rows}
    assert rows["Result"] == "Compacted"
    assert rows["First covered event"] == "1"
    assert rows["Last covered event"] == "8"
    assert rows["Covered bytes"] == str(services.row["covered_bytes"])
    assert rows["Summary source"] == "model"
    assert (
        first.output
        == f"Compacted events 1-8: {services.row['covered_bytes']} bytes covered -> {services.row['dropped_bytes']} dropped (model summary)."
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
    rows = {row.label: row.value for row in FieldsViewData.from_payload(second.presentation.data).rows}
    assert rows["Result"] == "Nothing new to compact"
    assert rows["Covered bytes"] == str(services.row["covered_bytes"])


def test_compact_command_size_guard_and_empty_session_are_visible() -> None:
    events = (_event(1, "core.user_message", {"content": "hi"}),)
    services = FakeServices(events, model_answer="a summary longer than the two bytes it replaces")
    command = CompactCommand(CompactionConfig(keep_recent_tokens=0))
    invocation = CommandInvocation(context=_context(), name="compact", arguments={})
    with pytest.raises(ValueError, match="would not shrink"):
        command.invoke(invocation, services)
    empty = command.invoke(invocation, FakeServices(()))
    assert empty.status == "ok"
    assert empty.data == {"noop": True, "reason": "no_events"}
    from voidcode.sdk.views import FieldsViewData

    rows = {row.label: row.value for row in FieldsViewData.from_payload(empty.presentation.data).rows}
    assert rows["Covered bytes"] == "Unknown"
    assert rows["Measured input tokens"] == "Unknown"


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
    assert _loaded(None) == CompactionState()
    assert CompactionState.from_payload({"schema_version": SCHEMA_VERSION, "cursor_sequence": 0, "last_projection": None}) == CompactionState()
    with pytest.raises(ValueError, match="unsupported field"):
        _loaded({"schema_version": SCHEMA_VERSION, "cursor_sequence": 0, "last_projection": None, "extra": 1})
    with pytest.raises(ValueError, match="schema_version"):
        CompactionState.from_payload({"schema_version": SCHEMA_VERSION + 1, "cursor_sequence": 0, "last_projection": None})
    with pytest.raises(ValueError, match="cursor_sequence"):
        CompactionState.from_payload({"schema_version": SCHEMA_VERSION, "cursor_sequence": True, "last_projection": None})
    with pytest.raises(ValueError, match="missing field"):
        CompactionState.from_payload({"schema_version": SCHEMA_VERSION, "cursor_sequence": 0, "last_projection": {"summary_kind": "model"}})
    with pytest.raises(ValueError, match="summary_kind"):
        CompactionState.from_payload(
            {
                "schema_version": SCHEMA_VERSION,
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
    assert default.threshold_tokens is None
    assert default.reserve_tokens is None
    assert default.keep_recent_tokens == DEFAULT_KEEP_RECENT_TOKENS
    parsed = parse_compaction_config({"enabled": False, "threshold_tokens": 90_000, "reserve_tokens": 0, "keep_recent_tokens": 1_000})
    assert parsed == CompactionConfig(enabled=False, threshold_tokens=90_000, reserve_tokens=0, keep_recent_tokens=1_000)
    # Zero is an explicit "off", so an otherwise large reserve is irrelevant.
    assert parse_compaction_config({"threshold_tokens": 0, "reserve_tokens": 10_000}) == CompactionConfig(threshold_tokens=0, reserve_tokens=10_000)
    # An explicit reserve at or above an explicit threshold is refused, not collapsed to 1.
    with pytest.raises(ValueError, match="reserve_tokens must be smaller than threshold_tokens"):
        parse_compaction_config({"threshold_tokens": 1_000, "reserve_tokens": 1_000})
    # The retired model flag is an unknown key now, not a knob.
    with pytest.raises(ValueError, match="summary_enabled"):
        parse_compaction_config({"summary_enabled": True})
    # The reserved providers key is accepted and ignored.
    assert parse_compaction_config({"providers": {"endpoint": {}}, "threshold_tokens": 5}) == CompactionConfig(threshold_tokens=5)
    with pytest.raises(ValueError, match="threshold"):
        parse_compaction_config({"threshold": 1})
    with pytest.raises(ValueError, match="enabled"):
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


def test_summary_material_includes_bounded_declared_arguments_and_ignores_non_transcript_records() -> None:
    from voidcode.sdk.transcript import ContextRecord, TranscriptChild, TranscriptEntry, TranscriptTool

    long_value = "Z" * 2_000
    call = ToolCall(
        "unrelated_name",
        {"targets": "actual.py", "path": "misleading.py", "command": "display text", "zpayload": long_value},
        "substitute",
    )
    result = ToolSuccess("unrelated_name", TextOutput("moved"), changes=(Change("actual.py", "move", "old.py"),))
    records = (
        ContextRecord(1, 1_000, True, TranscriptEntry(1, RUN_ID, REVISION_ID, "client", ProviderMessage("user", _USER_MESSAGE))),
        ContextRecord(2, 1_000, True, TranscriptEntry(2, RUN_ID, REVISION_ID, "custom", TranscriptTool(call, result, False, 1, False))),
        ContextRecord(3, 1_000, True, TranscriptEntry(3, RUN_ID, REVISION_ID, "custom", TranscriptChild("actual-child"))),
    )
    plan = plan_projection(records, previous_covered_to_sequence=None, keep_recent_tokens=0, reached_end=True)
    assert plan.covered_tool_result_count == 1
    material = "\n\n".join(plan.summary_material)
    # Declared intent is included (bounded), while observed output and status stay separate.
    assert "tool=unrelated_name status=ok" in material
    assert "args=" in material
    assert "misleading.py" in material
    assert "display text" in material
    assert "moved" in material
    assert "Z" * 100 in material
    assert long_value not in material
    assert "[truncated]" in material
    # Bookkeeping without a transcript entry never enters the summary material.
    assert "actual-child" not in material


def test_prepare_deactivation_clears_the_live_projection_but_keeps_the_cursor() -> None:
    state = CompactionState(
        cursor_sequence=12,
        last_projection=LastProjection(1, 10, 9_000, 1_000, "model"),
    ).to_payload()
    prepared = prepare_compaction_deactivation(state, "session", SESSION_ID, 12)
    assert prepared["schema_version"] == SCHEMA_VERSION
    assert prepared["cursor_sequence"] == 12
    assert prepared["last_projection"] is None
    assert CompactionState.from_payload(prepared) == CompactionState(cursor_sequence=12)


def test_interrupted_dispatch_cannot_reuse_an_older_usage_measurement():
    events = (*_session_events(), _intent(9), _event(10, "core.provider_interrupted", {"dispatch_sequence": 9}))
    records = _records(events)
    assert measured_dispatch_input_tokens(records) is None
    assert measured_tokens_per_character(records) is None
