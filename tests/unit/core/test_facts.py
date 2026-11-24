"""Facts use frozen declarations, source cursors, and atomic core-owned projections."""

from dataclasses import asdict
from sqlite3 import IntegrityError

import pytest

from voidcode.core.facts import fact_for_event
from voidcode.core.plugins import LeaseContext, LeaseIdentity, PluginRuntime
from voidcode.core.storage.store import EventDraft, SessionStatus, SqliteStore
from voidcode.sdk.codec import decode_tool_result, encode_provider_result, encode_tool_result
from voidcode.sdk.hooks import ContextProjectedFact, InvocationFinishedFact, RunFinishedFact, RunRequestedFact, UsageFact
from voidcode.sdk.provider import ProviderTokenUsage, ProviderTurnResult
from voidcode.sdk.services import BudgetExhaustion
from voidcode.sdk.tools import Change, ReadObservation, TextOutput, ToolSuccess


@pytest.fixture
def runtime(tmp_path):
    with SqliteStore(":memory:") as store:
        revision = store.put_revision({})
        store.record_workspace_revision(str(tmp_path), revision)
        store.create_session("session", str(tmp_path), revision)
        with PluginRuntime((), workspace=tmp_path, revision_id=revision, store=store) as runtime:
            yield runtime


def append(runtime, *drafts):
    return runtime.require_store().commit_staged(
        "session",
        run_id="run",
        revision_id=runtime.revision_id,
        events=drafts,
        status=SessionStatus.COMPLETED,
    )


def event(kind, **payload):
    return EventDraft(kind, "core", "core", payload)


def test_pages_advance_over_private_events_and_keep_batch_observations_together(runtime):
    append(
        runtime,
        event("private.one"),
        event("private.two"),
        event(
            "core.tool_completed",
            call={"tool_name": "arbitrary_name"},
            result={"status": "ok"},
            fact_metadata={
                "owner": "original_owner",
                "effects": ["write"],
                "semantics": ["resource.edit"],
                "model": None,
                "argument_bytes": 2,
                "result_bytes": 10,
                "truncated": False,
                "partial": False,
                "error_kind": None,
                "has_retry_guidance": False,
                "reads": [],
                "resource_paths": [],
                "changes": [asdict(Change("a")), asdict(Change("b"))],
            },
        ),
        event("private.last"),
    )
    first = runtime.fact_page("session", 0, 2)
    assert not first.facts and first.has_more and first.next_sequence == 2
    last = runtime.fact_page("session", first.next_sequence, 2)
    assert len(last.facts) == 1 and last.next_sequence == 4 and not last.has_more
    fact = last.facts[0]
    assert isinstance(fact, InvocationFinishedFact)
    assert len(fact.changes) == 2 and fact.owner == "original_owner"
    assert fact.effects == ("write",)
    assert runtime.fact_page("session", 4, 2).next_sequence == 4
    source = runtime.require_store().events("session")[2]
    kind, payload = fact_for_event(source, ())
    assert runtime._typed_fact(kind, payload, "session", "run", runtime.revision_id, "") == fact


@pytest.mark.parametrize("metadata", [None, {}, {"semantics": []}])
def test_invocation_without_current_metadata_is_rejected(runtime, metadata):
    append(runtime, event("core.tool_completed", call={"tool_name": "read"}, result={"status": "ok"}, fact_metadata=metadata))
    with pytest.raises(ValueError, match="invocation fact.*metadata"):
        runtime.fact_page("session", 0, 10)


def test_unknown_usage_is_visible_after_cursor_passes_inflight_intent(runtime):
    append(runtime, event("core.provider_intent", provider="endpoint", model="model", logical_turn="turn"))
    first = runtime.fact_page("session", 0, 10)
    assert not first.facts and first.next_sequence == 1
    append(runtime, event("core.provider_interrupted", dispatch_sequence=1))
    fact = runtime.fact_page("session", first.next_sequence, 10).facts[0]
    assert isinstance(fact, UsageFact) and fact.usage is None
    assert fact.input_limit_tokens is None
    assert fact.dispatch_sequence == 1 and fact.event_sequence == 2 and fact.elapsed_seconds is None


def test_usage_fact_carries_the_persisted_projection_allocation_input_limit(runtime):
    append(
        runtime,
        event(
            "core.provider_intent",
            provider="endpoint",
            model="model",
            logical_turn="turn",
            dispatch_sequence=1,
            request_window={"message_characters": 10, "tool_schema_characters": 2, "segments": [], "tool_count": 0},
            projection_allocation={
                "policy": "fixed",
                "reason": "within_ceiling",
                "limit_tokens": 150_000,
                "limit_source": "max_input_tokens",
                "rate_tokens_per_char": None,
                "per_tool_max_bytes": None,
                "tool_count": 0,
            },
        ),
        event(
            "core.model_turn",
            dispatch_sequence=1,
            result=encode_provider_result(ProviderTurnResult(output="ok", usage=ProviderTokenUsage(input_tokens=10))),
        ),
    )
    page = runtime.fact_page("session", 0, 10)
    assert len(page.facts) == 1
    fact = page.facts[0]
    assert isinstance(fact, UsageFact)
    assert fact.input_limit_tokens == 150_000
    assert asdict(fact)["input_limit_tokens"] == 150_000
    # The historical page and the live observer decode the same committed intent.
    source = runtime.require_store().events("session")[1]
    kind, payload = fact_for_event(source, runtime.require_store().events("session"))
    assert runtime._typed_fact(kind, payload, "session", "run", runtime.revision_id, str(payload.get("logical_turn", ""))) == fact


def test_resource_changes_roundtrip_and_failures_cannot_claim_successful_changes():
    result = ToolSuccess("substitute", TextOutput("done"), coverage=(ReadObservation("a", "a" * 64),), changes=(Change("b", "move", "a"),))
    assert decode_tool_result(encode_tool_result(result)) == result
    payload = encode_tool_result(result)
    payload.update(status="error", error="failed")
    with pytest.raises(ValueError):
        decode_tool_result(payload)
    with pytest.raises(ValueError):
        Change("b", "move")


def test_projection_and_fact_commit_or_rollback_together(runtime):
    append(runtime, event("core.user_message", content="large input " * 100))
    lease = LeaseContext(LeaseIdentity("token", "compressor", "session", runtime.workspace, "run", runtime.revision_id, "inv", False))
    runtime.store_window_projection(lease, 1, 1, "summary")
    store = runtime.require_store()
    assert store.window_projection("session") is None
    append(runtime, *lease.staging.events)
    projection = store.window_projection("session")
    assert projection is not None and projection.replacement == "summary"
    fact = runtime.fact_page("session", 1, 10).facts[0]
    assert isinstance(fact, ContextProjectedFact)
    assert fact.owner == "compressor" and fact.dropped_bytes == projection.dropped_bytes
    bad = event("core.window_projected", **{**lease.staging.events[0].payload, "replacement": "different"})
    with pytest.raises(IntegrityError):
        # An invalid event insert rolls back the projection upsert in the same transaction.
        append(runtime, bad, EventDraft(None, "core", "core", {}))
    assert store.window_projection("session") == projection
    assert len(store.events("session")) == 2


@pytest.mark.parametrize("payload", [{}, {"call": {}, "result": {"status": "ok"}}, {"call": {"tool_name": "read"}, "result": {}}])
def test_corrupt_history_never_becomes_a_successful_or_anonymous_invocation(runtime, payload):
    append(runtime, event("core.tool_completed", **payload))
    with pytest.raises(ValueError):
        runtime.fact_page("session", 0, 10)


def test_model_query_event_is_one_truthful_usage_fact(runtime):
    append(
        runtime,
        event(
            "core.model_query",
            provider="endpoint",
            model="exact-model",
            usage={
                "input_tokens": 120,
                "output_tokens": 45,
                "cache_read_tokens": 30,
                "cache_write_tokens": None,
                "uncached_input_tokens": 90,
                "cost_usd": 0.25,
            },
            cost_usd=0.25,
            elapsed_ms=1500,
            owner="find",
        ),
    )
    page = runtime.fact_page("session", 0, 10)
    assert len(page.facts) == 1
    fact = page.facts[0]
    assert isinstance(fact, UsageFact)
    assert fact.source == "model_query" and fact.owner == "find"
    # The side query's own event is its dispatch record; there is no provider intent.
    assert fact.event_sequence == fact.dispatch_sequence == 1
    assert fact.provider_name == "endpoint" and fact.model_name == "exact-model"
    assert fact.usage is not None
    assert (fact.usage.input_tokens, fact.usage.output_tokens, fact.usage.cache_read_tokens) == (120, 45, 30)
    assert (fact.cost_usd, fact.elapsed_seconds, fact.input_limit_tokens) == (0.25, 1.5, None)

    # Unknown usage stays unknown; the fact never substitutes a zero.
    append(
        runtime,
        event("core.model_query", provider="endpoint", model="exact-model", usage=None, cost_usd=None, elapsed_ms=0, owner="btw"),
    )
    unknown = runtime.fact_page("session", 1, 10).facts[0]
    assert isinstance(unknown, UsageFact)
    assert unknown.source == "model_query" and unknown.owner == "btw"
    assert unknown.usage is None and unknown.cost_usd is None and unknown.elapsed_seconds == 0.0


def test_run_finished_fact_carries_typed_budget_exhaustion(runtime):
    exhaustion = {
        "dimension": "provider_turns",
        "source": "max_steps",
        "limit": 1,
        "consumed": 1,
        "remaining": 0,
        "continue_with": "resume_with_max_steps",
    }
    append(
        runtime,
        event("core.run_interrupted", stop_cause="limit_reached", budget_exhaustion=exhaustion, elapsed_ms=1200),
    )
    fact = runtime.fact_page("session", 0, 10).facts[0]
    assert isinstance(fact, RunFinishedFact)
    assert fact.status == "interrupted" and fact.stop_cause == "limit_reached"
    assert fact.elapsed_seconds == 1.2
    assert fact.budget_exhaustion == BudgetExhaustion(**exhaustion)

    append(runtime, event("core.run_completed", elapsed_ms=100))
    completed = runtime.fact_page("session", 1, 10).facts[0]
    assert isinstance(completed, RunFinishedFact)
    assert completed.status == "completed" and completed.budget_exhaustion is None


@pytest.mark.parametrize(
    "exhaustion",
    [
        {
            "dimension": "owner_tokens",
            "source": "max_input_tokens",
            "limit": 120,
            "consumed": 130,
            "remaining": 0,
            "continue_with": "raise_owner_budget",
        },
        {
            "dimension": "owner_cost_usd",
            "source": "max_cost_usd",
            "limit": 1.25,
            "consumed": 2.5,
            "remaining": 0.0,
            "continue_with": "raise_owner_budget",
        },
        {
            "dimension": "owner_elapsed_seconds",
            "source": "max_elapsed_seconds",
            "limit": 60.0,
            "consumed": 61.5,
            "remaining": 0.0,
            "continue_with": "raise_owner_budget",
        },
    ],
)
def test_run_finished_fact_carries_typed_owner_budget_exhaustion(runtime, exhaustion):
    append(runtime, event("core.run_interrupted", stop_cause="limit_reached", budget_exhaustion=exhaustion, elapsed_ms=10))
    fact = runtime.fact_page("session", 0, 10).facts[0]
    assert isinstance(fact, RunFinishedFact)
    assert fact.status == "interrupted" and fact.stop_cause == "limit_reached"
    assert fact.budget_exhaustion is not None
    assert fact.budget_exhaustion.as_payload() == exhaustion


def test_budget_exhaustion_refuses_a_mismatched_owner_metric():
    with pytest.raises(ValueError, match="unknown dimension, source, or continuation"):
        BudgetExhaustion.from_payload(
            {
                "dimension": "owner_tokens",
                "source": "max_cost_usd",
                "limit": 1,
                "consumed": 1,
                "remaining": 0,
                "continue_with": "raise_owner_budget",
            }
        )


def test_wakeup_fact_retains_owner_identity_role_and_immutable_payload(runtime):
    from voidcode.sdk.hooks import WakeupFiredFact

    payload = {"text": "scheduled message", "nested": {"key": [1]}}
    append(runtime, event("core.wakeup_fired", owner="scheduler", wakeup_id="wake-1", role="user", payload=payload))
    fact = runtime.fact_page("session", 0, 200).facts[0]
    assert isinstance(fact, WakeupFiredFact)
    assert fact.owner == "scheduler" and fact.wakeup_id == "wake-1" and fact.role == "user"
    assert fact.payload["text"] == "scheduled message"
    payload["nested"]["key"].append(2)
    assert fact.payload["nested"]["key"] == (1,)
    with pytest.raises(TypeError):
        fact.payload["text"] = "changed"
    with pytest.raises(ValueError):
        WakeupFiredFact("s", "r", "v", "t", 1, "", "w", "user", {})


def test_run_requested_trigger_distinguishes_fresh_followup_continuation_and_resume(runtime) -> None:
    append(
        runtime,
        event("core.user_message", content="fresh"),
        event("core.user_message", content="queued", continuation_id="cid", trigger="followup"),
        event("core.user_message", content="automatic", continuation_id="cid-2"),
        event("core.run_resumed", max_steps=None),
    )
    facts = [fact for fact in runtime.fact_page("session", 0, 10).facts if isinstance(fact, RunRequestedFact)]
    assert [fact.trigger for fact in facts] == ["user", "followup", "continuation", "resume"]
    assert [fact.resumed for fact in facts] == [False, False, False, True]
