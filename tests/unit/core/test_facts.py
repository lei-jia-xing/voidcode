"""Facts use frozen declarations, source cursors, and atomic core-owned projections."""

from dataclasses import asdict
from sqlite3 import IntegrityError

import pytest

from voidcode.core.facts import fact_for_event
from voidcode.core.plugins import LeaseContext, PluginRuntime
from voidcode.core.storage.store import EventDraft, SqliteStore
from voidcode.sdk.codec import decode_tool_result, encode_tool_result
from voidcode.sdk.hooks import ContextProjectedFact, InvocationFinishedFact, UsageFact
from voidcode.sdk.tools import ReadObservation, ResourceChange, TextOutput, ToolSuccess


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
        status="completed",
    )


def event(kind, **payload):
    return EventDraft(kind, "kernel", "kernel", payload)


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
                "changes": [asdict(ResourceChange("a")), asdict(ResourceChange("b"))],
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
    assert fact.dispatch_sequence == 1 and fact.event_sequence == 2 and fact.elapsed_seconds is None


def test_resource_changes_roundtrip_and_failures_cannot_claim_successful_changes():
    result = ToolSuccess("substitute", TextOutput("done"), coverage=(ReadObservation("a", "a" * 64),), changes=(ResourceChange("b", "move", "a"),))
    assert decode_tool_result(encode_tool_result(result)) == result
    payload = encode_tool_result(result)
    payload.update(status="error", error="failed")
    with pytest.raises(ValueError):
        decode_tool_result(payload)
    with pytest.raises(ValueError):
        ResourceChange("b", "move")


def test_projection_and_fact_commit_or_rollback_together(runtime):
    append(runtime, event("core.user_message", content="large input " * 100))
    lease = LeaseContext("token", "compressor", "session", runtime.workspace, "run", runtime.revision_id, "inv", False)
    runtime.store_window_projection(lease, 1, 1, "summary")
    store = runtime.require_store()
    assert store.window_projection("session") is None
    append(runtime, *lease.staged_events)
    projection = store.window_projection("session")
    assert projection is not None and projection.replacement == "summary"
    fact = runtime.fact_page("session", 1, 10).facts[0]
    assert isinstance(fact, ContextProjectedFact)
    assert fact.owner == "compressor" and fact.dropped_bytes == projection.dropped_bytes
    bad = event("core.window_projected", **{**lease.staged_events[0].payload, "replacement": "different"})
    with pytest.raises(IntegrityError):
        # An invalid event insert rolls back the projection upsert in the same transaction.
        append(runtime, bad, EventDraft(None, "kernel", "kernel", {}))
    assert store.window_projection("session") == projection
    assert len(store.events("session")) == 2


@pytest.mark.parametrize("payload", [{}, {"call": {}, "result": {"status": "ok"}}, {"call": {"tool_name": "read"}, "result": {}}])
def test_corrupt_history_never_becomes_a_successful_or_anonymous_invocation(runtime, payload):
    append(runtime, event("core.tool_completed", **payload))
    with pytest.raises(ValueError):
        runtime.fact_page("session", 0, 10)


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
