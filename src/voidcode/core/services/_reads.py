"""Bounded read services: facts, transcripts, events, history pages, and usage."""

from __future__ import annotations

from dataclasses import replace

from voidcode.sdk.hooks import FactPage
from voidcode.sdk.services import EventView, SessionInfo, SessionUsageReport
from voidcode.sdk.transcript import ContextHistory, TranscriptEntry, TranscriptPage

from ..history_renderer import PAGE_BYTES, HistoryPage, render_history_page
from ..transcript import transcript_entry
from ._lease import ServiceLease


class ReadServices(ServiceLease):
    """Lease-scoped reads over committed session truth, with the same page bounds."""

    def read_facts(self, *, after_sequence: int = 0, limit: int = 100) -> FactPage:
        lease = self._active_lease()
        if type(after_sequence) is not int or after_sequence < 0 or type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("after_sequence must be >= 0 and limit within 1..200")
        return self._runtime.fact_page(lease.identity.session_id, after_sequence, limit)

    def read_context_history(self, *, after_sequence: int = 0, limit: int = 600) -> ContextHistory:
        lease = self._active_lease()
        if type(after_sequence) is not int or after_sequence < 0 or type(limit) is not int or not 1 <= limit <= 2000:
            raise ValueError("after_sequence must be >= 0 and limit within 1..2000")
        return self._runtime.context_history(lease.identity.session_id, limit, after_sequence)

    def read_transcript(self, *, after_sequence: int = 0, limit: int = 100) -> TranscriptPage:
        lease = self._active_lease()
        if type(after_sequence) is not int or after_sequence < 0 or type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("after_sequence must be >= 0 and limit within 1..200")
        return self._runtime.transcript_page(lease.identity.session_id, after_sequence, limit)

    def latest_event(self, event_type: str, run_id: str | None = None) -> TranscriptEntry | None:
        lease = self._active_lease()
        if not isinstance(event_type, str) or not event_type:
            raise ValueError("event_type must be a non-empty string")
        if run_id is not None and (not isinstance(run_id, str) or not run_id):
            raise ValueError("run_id must be a non-empty string or None")
        stored = self._runtime.require_store().latest_event(lease.identity.session_id, event_type, run_id)
        if stored is None:
            return None
        return transcript_entry(stored)

    def read_events(
        self,
        *,
        after_sequence: int = 0,
        limit: int = 100,
        include_payload: bool = True,
    ) -> tuple[EventView, ...]:
        lease = self._active_lease()
        if type(after_sequence) is not int or after_sequence < 0 or type(limit) is not int or limit < 1 or limit > 200:
            raise ValueError("after_sequence must be >= 0 and limit within 1..200")
        if type(include_payload) is not bool:
            raise ValueError("include_payload must be a boolean")
        return self._runtime.event_views(lease.identity.session_id, after_sequence, limit, include_payload)

    def session_info(self) -> SessionInfo:
        lease = self._active_lease()
        return self._runtime.require_store().session_info(lease.identity.session_id, lease.identity.owner)

    def read_child_history(
        self,
        child_session_id: str,
        *,
        after_sequence: int = 0,
        offset: int = 0,
        limit: int = 100,
    ) -> HistoryPage:
        lease = self._active_lease()
        if not isinstance(child_session_id, str) or not child_session_id or child_session_id == lease.identity.session_id:
            raise ValueError("child_session_id must name another session")
        return self.read_session_history(child_session_id, after_sequence=after_sequence, offset=offset, limit=limit)

    def read_session_history(
        self,
        session_id: str,
        *,
        after_sequence: int = 0,
        offset: int = 0,
        limit: int = 100,
        page_bytes: int = PAGE_BYTES,
    ) -> HistoryPage:
        lease = self._active_lease()
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session_id must be a non-empty string")
        if type(after_sequence) is not int or after_sequence < 0 or type(offset) is not int or offset < 0:
            raise ValueError("after_sequence and offset must be non-negative integers")
        if type(page_bytes) is not int or page_bytes < 1 or page_bytes > PAGE_BYTES:
            raise ValueError(f"page_bytes must be an integer within 1..{PAGE_BYTES}")
        limit = max(1, min(limit, 200))
        store = self._runtime.require_store()
        if session_id != lease.identity.session_id:
            caller = store.session(lease.identity.session_id)
            target = store.session(session_id)
            if target.workspace != caller.workspace or not self._runtime.is_descendant(target, caller.session_id):
                raise PermissionError("session history is readable only for this session or its own descendants")
        events = store.events(session_id, after_sequence=after_sequence, limit=limit + 1)
        page = render_history_page(events[:limit], after_sequence=after_sequence, offset=offset, page_bytes=page_bytes)
        return replace(page, truncated=page.truncated or len(events) > limit)

    def get_usage(self, *, since_sequence: int = 0, until_sequence: int | None = None) -> SessionUsageReport:
        lease = self._active_lease()
        if type(since_sequence) is not int:
            raise ValueError("since_sequence must be an integer")
        if until_sequence is not None and type(until_sequence) is not int:
            raise ValueError("until_sequence must be an integer or null")
        callback = self._runtime.usage_callback
        if callback is None:
            raise RuntimeError("usage service is unavailable in this runtime")
        return callback(lease.identity.session_id, since_sequence, until_sequence)
