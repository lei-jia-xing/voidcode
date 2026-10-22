"""Lossless transcript paging, explicit malformed history, and scoped service access."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from voidcode.core.plugins import LeaseContext, PluginRuntime
from voidcode.core.services import RuntimeServices
from voidcode.core.storage.store import StoredEvent
from voidcode.core.transcript import transcript_entry
from voidcode.sdk.provider import ProviderMessage
from voidcode.sdk.transcript import TranscriptPage


def _event(sequence, event_type="kernel.user_message", payload=None):
    return StoredEvent("session", sequence, "run", event_type, "owner", "user", "revision", {"content": "hello"} if payload is None else payload)


class _Store:
    def __init__(self, events):
        self.source = events

    def events(self, session_id, *, after_sequence, limit):
        assert session_id == "session"
        return tuple(event for event in self.source if event.sequence > after_sequence)[:limit]


def _runtime(events):
    return SimpleNamespace(require_store=lambda: _Store(events), closed=False)


def test_pages_preserve_oversized_unicode_records_and_resume_after_empty_pages():
    text = "完整正文" * 30_000
    events = (_event(1, "plugin.probe.tick", {}), _event(2, payload={"content": text}), _event(3))
    runtime = _runtime(events)
    first = PluginRuntime.transcript_page(runtime, "session", 0, 200)
    assert first == TranscriptPage((), 1, True, 1)
    second = PluginRuntime.transcript_page(runtime, "session", first.next_sequence, 200)
    assert second.next_sequence == 2 and second.has_more and second.source_events == 1
    assert second.entries[0].content == ProviderMessage("user", text)
    third = PluginRuntime.transcript_page(runtime, "session", second.next_sequence, 200)
    assert third.next_sequence == 3 and not third.has_more
    assert [entry.sequence for entry in (*first.entries, *second.entries, *third.entries)] == [2, 3]
    assert PluginRuntime.transcript_page(runtime, "session", 3, 200) == TranscriptPage((), 3, False, 0)


@pytest.mark.parametrize("event_type", ["kernel.continuation_started", "kernel.wakeup_fired"])
@pytest.mark.parametrize("role", ["user", "system"])
def test_owner_messages_retain_role_and_verbatim_text(event_type, role):
    event = _event(7, event_type, {"owner": "owner", "role": role, "payload": {"text": "do this"}})
    entry = transcript_entry(event)
    assert entry.sequence == 7 and entry.run_id == "run" and entry.revision_id == "revision"
    assert isinstance(entry.content, ProviderMessage)
    assert entry.content.role == role
    if role == "user":
        assert entry.content.content == "do this"
    else:
        assert '"text": "do this"' in entry.content.content
        assert "owner" in entry.content.content


def test_malformed_conversation_is_an_error_instead_of_a_missing_message():
    with pytest.raises(ValueError, match="content must be a string"):
        transcript_entry(_event(1, payload={"content": None}))
    with pytest.raises(KeyError):
        transcript_entry(_event(2, "kernel.model_turn", {}))


@pytest.mark.parametrize("after,limit", [(-1, 1), (True, 1), (0, 0), (0, 201), (0, True)])
def test_transcript_service_validates_ingress(tmp_path, after, limit):
    lease = LeaseContext("token", "owner", "session", tmp_path, "run", "revision", "invoke", True)
    services = RuntimeServices(_runtime(()), lease)
    with pytest.raises(ValueError):
        services.read_transcript(after_sequence=after, limit=limit)


def test_transcript_service_is_session_scoped_readonly_and_revocable(tmp_path):
    lease = LeaseContext("token", "owner", "session", tmp_path, "run", "revision", "invoke", True, hook=True)
    runtime = _runtime(())
    runtime.transcript_page = lambda session_id, after, limit: (session_id, after, limit)
    services = RuntimeServices(runtime, lease)
    assert services.read_transcript(after_sequence=7, limit=3) == ("session", 7, 3)
    lease.revoked = True
    with pytest.raises(PermissionError, match="revoked"):
        services.read_transcript()
    runtime.closed = True
    with pytest.raises(PermissionError, match="revoked"):
        RuntimeServices(runtime, replace(lease, revoked=False)).read_transcript()


def test_pending_dispatch_is_visible_without_fabricating_a_completed_turn():
    from voidcode.sdk.hooks import RequestWindow
    from voidcode.sdk.transcript import TranscriptDispatch

    measured = RequestWindow(message_characters=42, tool_schema_characters=7, tool_count=1, segments=())
    event = _event(10, "kernel.provider_intent", {"provider": "provider", "model": "model", "request_window": measured.as_payload()})
    runtime = _runtime((event,))
    page = PluginRuntime.transcript_page(runtime, "session", 0, 200)
    assert page.entries[0].content == TranscriptDispatch("provider", "model", measured)
    assert len(page.entries) == 1 and not page.has_more
    with pytest.raises(ValueError, match="identities"):
        transcript_entry(replace(event, payload={**event.payload, "model": None}))


def _batch():
    from voidcode.sdk.codec import encode_provider_result, encode_tool_call, encode_tool_result
    from voidcode.sdk.provider import ProviderTurnResult
    from voidcode.sdk.tools import TextOutput, ToolCall, ToolSuccess

    calls = (ToolCall("lookup", {}, "one"), ToolCall("replace", {}, "two"))
    return (
        _event(1, payload={"content": "long prompt " * 500}),
        _event(2, "kernel.model_turn", {"dispatch_sequence": 1, "result": encode_provider_result(ProviderTurnResult(tool_calls=calls))}),
        *(
            _event(
                index + 3,
                "kernel.tool_completed",
                {"batch_sequence": 2, "call": encode_tool_call(call), "result": encode_tool_result(ToolSuccess(call.tool_name, TextOutput("done")))},
            )
            for index, call in enumerate(calls)
        ),
    )


def test_certified_context_prefix_requires_every_matching_native_result():
    from voidcode.core.transcript import context_records

    batch = _batch()
    records = context_records(batch)
    assert [record.safe_boundary for record in records] == [True, False, False, True]
    assert records[1].entry.dispatch_sequence == 1
    assert records[2].content.batch_sequence == 2
    assert records[2].content.call.tool_call_id == "one"
    short = PluginRuntime.context_history(_runtime(batch), "session", 3)
    assert len(short.records) == 3 and not short.reached_end
    assert not short.records[-1].safe_boundary
    assert PluginRuntime.context_history(_runtime(batch), "session", 4).reached_end


@pytest.mark.parametrize("corruption", ["batch", "call", "duplicate"])
def test_mismatched_results_cannot_certify_a_prefix_even_when_counts_match(corruption):
    from voidcode.core.transcript import context_records

    batch = list(_batch())
    payload = dict(batch[-1].payload)
    if corruption == "batch":
        payload["batch_sequence"] = 1
    elif corruption == "call":
        payload["call"] = {**payload["call"], "tool_call_id": "wrong"}
    else:
        payload = dict(batch[-2].payload)
    batch[-1] = replace(batch[-1], payload=payload)
    assert not context_records(batch)[-1].safe_boundary


def test_deferred_outcome_does_not_answer_a_native_call_or_unbalance_a_completed_batch():
    from voidcode.core.transcript import context_records

    batch = _batch()
    deferred = replace(batch[-1], sequence=5, payload={**batch[-1].payload, "deferred": True, "batch_sequence": 99})
    assert not context_records((*batch[:3], deferred))[-1].safe_boundary
    assert context_records((*batch, deferred))[-1].safe_boundary


@pytest.mark.parametrize("limit", [0, -1, 2001, True])
def test_context_history_validates_source_limit(tmp_path, limit):
    lease = LeaseContext("token", "owner", "session", tmp_path, "run", "revision", "invoke", True)
    with pytest.raises(ValueError, match="limit"):
        RuntimeServices(_runtime(()), lease).read_context_history(limit=limit)


def test_projection_admission_rechecks_ids_and_accepts_completed_deferred_work(tmp_path):
    from voidcode.core.storage.store import EventDraft, SqliteStore

    batch = _batch()
    for corrupt in (False, True):
        with SqliteStore(":memory:") as store:
            revision = store.put_revision({})
            store.record_workspace_revision(str(tmp_path), revision)
            store.create_session("session", str(tmp_path), revision)
            payload = dict(batch[-1].payload)
            if corrupt:
                payload["batch_sequence"] = 1
            source = (*batch[:-1], replace(batch[-1], payload=payload))
            deferred = replace(batch[-1], sequence=5, payload={**batch[-1].payload, "deferred": True, "batch_sequence": 99})
            store.commit_staged(
                "session",
                run_id="run",
                revision_id=revision,
                events=tuple(EventDraft(e.event_type, e.source, e.actor, e.payload) for e in (*source, deferred)),
                status="completed",
            )
            with PluginRuntime((), workspace=tmp_path, revision_id=revision, store=store) as runtime:
                lease = LeaseContext("token", "compressor", "session", tmp_path, "run", revision, "invoke", False)
                if corrupt:
                    with pytest.raises(ValueError, match="whole native tool batches"):
                        runtime.store_window_projection(lease, 1, 5, "summary")
                else:
                    runtime.store_window_projection(lease, 1, 5, "summary")
                    assert len(lease.staged_events) == 1
                    certified = runtime.context_history("session", 5)
                    assert certified.reached_end and certified.records[-1].safe_boundary
                    assert lease.staged_events[0].payload["covered_bytes"] == sum(record.payload_bytes for record in certified.records)


def test_context_history_service_is_readonly_session_scoped_and_revocable(tmp_path):
    lease = LeaseContext("token", "owner", "session", tmp_path, "run", "revision", "invoke", True, hook=True)
    runtime = _runtime(())
    runtime.context_history = lambda session_id, limit: (session_id, limit)
    services = RuntimeServices(runtime, lease)
    assert services.owner == "owner"
    assert services.read_context_history(limit=42) == ("session", 42)
    lease.revoked = True
    with pytest.raises(PermissionError, match="revoked"):
        services.read_context_history()
    with pytest.raises(PermissionError, match="revoked"):
        _ = services.owner
