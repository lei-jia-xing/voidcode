"""Lossless transcript paging, explicit malformed history, and scoped service access."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from voidcode.core.plugins import LeaseContext, LeaseIdentity, PluginRuntime
from voidcode.core.services import RuntimeServices
from voidcode.core.storage.store import SessionStatus, StoredEvent, canonical_json
from voidcode.core.transcript import transcript_entry
from voidcode.sdk.provider import ProviderMessage
from voidcode.sdk.transcript import TranscriptPage


def _event(sequence, event_type="core.user_message", payload=None):
    return StoredEvent("session", sequence, "run", event_type, "owner", "user", "revision", {"content": "hello"} if payload is None else payload)


class _Store:
    def __init__(self, events):
        self.source = events

    def events(self, session_id, *, after_sequence, limit):
        assert session_id == "session"
        return tuple(event for event in self.source if event.sequence > after_sequence)[:limit]

    def event_payload_bytes(self, session_id, *, after_sequence=0, limit=None):
        assert session_id == "session"
        rows = tuple(event for event in self.source if event.sequence > after_sequence)
        if limit is not None:
            rows = rows[: max(1, limit)]
        return {event.sequence: len(canonical_json(event.payload).encode("utf-8")) for event in rows}


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


@pytest.mark.parametrize("event_type", ["core.continuation_started", "core.wakeup_fired"])
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
        transcript_entry(_event(2, "core.model_turn", {}))


@pytest.mark.parametrize("after,limit", [(-1, 1), (True, 1), (0, 0), (0, 201), (0, True)])
def test_transcript_service_validates_ingress(tmp_path, after, limit):
    lease = LeaseContext(LeaseIdentity("token", "owner", "session", tmp_path, "run", "revision", "invoke", True))
    services = RuntimeServices(_runtime(()), lease)
    with pytest.raises(ValueError):
        services.read_transcript(after_sequence=after, limit=limit)


def test_transcript_service_is_session_scoped_readonly_and_revocable(tmp_path):
    lease = LeaseContext(LeaseIdentity("token", "owner", "session", tmp_path, "run", "revision", "invoke", True, hook=True))
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
    event = _event(10, "core.provider_intent", {"provider": "provider", "model": "model", "request_window": measured.as_payload()})
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
        _event(2, "core.model_turn", {"dispatch_sequence": 1, "result": encode_provider_result(ProviderTurnResult(tool_calls=calls))}),
        *(
            _event(
                index + 3,
                "core.tool_completed",
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
    lease = LeaseContext(LeaseIdentity("token", "owner", "session", tmp_path, "run", "revision", "invoke", True))
    with pytest.raises(ValueError, match="limit"):
        RuntimeServices(_runtime(()), lease).read_context_history(limit=limit)


@pytest.mark.parametrize("after", [-1, "0", True])
def test_context_history_validates_after_sequence(tmp_path, after):
    lease = LeaseContext(LeaseIdentity("token", "owner", "session", tmp_path, "run", "revision", "invoke", True))
    with pytest.raises(ValueError, match="after_sequence"):
        RuntimeServices(_runtime(()), lease).read_context_history(after_sequence=after)


def test_context_history_pages_are_prefix_certified_and_cover_the_whole_session():
    events = tuple(_event(sequence, "plugin.probe.tick", {}) for sequence in range(1, 602))
    runtime = _runtime(events)
    first = PluginRuntime.context_history(runtime, "session", 600)
    assert [record.sequence for record in first.records] == list(range(1, 601))
    assert not first.reached_end
    assert all(record.safe_boundary for record in first.records)
    second = PluginRuntime.context_history(runtime, "session", 600, 600)
    assert [record.sequence for record in second.records] == [601]
    assert second.reached_end
    assert second.records[0].safe_boundary
    beyond = PluginRuntime.context_history(runtime, "session", 600, 601)
    assert beyond.records == () and beyond.reached_end


def test_context_history_real_store_pages_601_events_without_gaps(tmp_path):
    from voidcode.core.storage.store import EventDraft, SqliteStore

    with SqliteStore(":memory:") as store:
        revision = store.put_revision({})
        store.record_workspace_revision(str(tmp_path), revision)
        store.create_session("session", str(tmp_path), revision)
        store.commit_staged(
            "session",
            run_id="run",
            revision_id=revision,
            events=tuple(EventDraft("plugin.probe.tick", "core", "core", {}) for _ in range(601)),
            status=SessionStatus.COMPLETED,
        )
        with PluginRuntime((), workspace=tmp_path, revision_id=revision, store=store) as runtime:
            first = runtime.context_history("session", 600)
            assert [record.sequence for record in first.records] == list(range(1, 601))
            assert not first.reached_end
            second = runtime.context_history("session", 600, 600)
            assert [record.sequence for record in second.records] == [601]
            assert second.reached_end
            assert first.records[-1].safe_boundary and second.records[0].safe_boundary


def test_context_history_page_boundary_never_certifies_a_page_suffix():
    batch = _batch()
    first = PluginRuntime.context_history(_runtime(batch), "session", 3)
    assert [record.sequence for record in first.records] == [1, 2, 3]
    assert not first.reached_end
    assert not first.records[-1].safe_boundary
    second = PluginRuntime.context_history(_runtime(batch), "session", 1, 3)
    assert [record.sequence for record in second.records] == [4]
    assert second.reached_end
    assert second.records[0].safe_boundary


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
                status=SessionStatus.COMPLETED,
            )
            with PluginRuntime((), workspace=tmp_path, revision_id=revision, store=store) as runtime:
                lease = LeaseContext(LeaseIdentity("token", "compressor", "session", tmp_path, "run", revision, "invoke", False))
                if corrupt:
                    with pytest.raises(ValueError, match="whole native tool batches"):
                        runtime.store_window_projection(lease, 1, 5, "summary")
                else:
                    runtime.store_window_projection(lease, 1, 5, "summary")
                    assert len(lease.staging.events) == 1
                    certified = runtime.context_history("session", 5)
                    assert certified.reached_end and certified.records[-1].safe_boundary
                    assert lease.staging.events[0].payload["covered_bytes"] == sum(record.payload_bytes for record in certified.records)


def test_context_history_service_is_readonly_session_scoped_and_revocable(tmp_path):
    lease = LeaseContext(LeaseIdentity("token", "owner", "session", tmp_path, "run", "revision", "invoke", True, hook=True))
    runtime = _runtime(())
    runtime.context_history = lambda session_id, limit, after_sequence=0: (session_id, limit, after_sequence)
    services = RuntimeServices(runtime, lease)
    assert services.owner == "owner"
    assert services.read_context_history(after_sequence=7, limit=42) == ("session", 42, 7)
    lease.revoked = True
    with pytest.raises(PermissionError, match="revoked"):
        services.read_context_history()
    with pytest.raises(PermissionError, match="revoked"):
        _ = services.owner


def test_tool_icon_absence_stays_generic_and_malformed_icon_fails_visibly():
    from voidcode.sdk.transcript import TranscriptTool

    call = {"tool_name": "read", "arguments": {}, "tool_call_id": "c1"}
    result = {"status": "ok", "tool_name": "read", "output": {"type": "text", "text": "hi"}}
    old = transcript_entry(_event(10, "core.tool_completed", {"call": call, "result": result, "batch_sequence": 4}))
    assert isinstance(old.content, TranscriptTool)
    assert old.content.icon is None
    with pytest.raises(ValueError):
        transcript_entry(_event(11, "core.tool_completed", {"call": call, "result": result, "batch_sequence": 4, "icon": {"glyph": 1}}))
