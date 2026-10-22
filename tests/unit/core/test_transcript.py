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
