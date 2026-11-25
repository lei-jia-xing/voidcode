"""State reads, projections, and owner-state seams over one runtime generation."""

from __future__ import annotations

from collections.abc import Mapping

from voidcode.sdk.json_values import json_wire_object
from voidcode.sdk.plugin import StateSchema, StateScope
from voidcode.sdk.services import (
    EventView,
)
from voidcode.sdk.transcript import ContextHistory, TranscriptEntry, TranscriptPage

from ..events import EventType
from ..storage.store import (
    EventDraft,
    SessionRecord,
)
from ..transcript import context_records, transcript_entry
from ._base import PluginRuntimeState
from ._contracts import LeaseContext


class PluginState(PluginRuntimeState):
    """State reads, projections, and owner-state seams over one runtime generation."""

    def state_metadata(self) -> Mapping[str, StateSchema]:
        """The owner-state contract each loaded plugin declared at runtime."""
        return dict(self._plugin_states)

    def state_schema_version(self, owner: str) -> int:
        """The declared state schema version of one loaded owner (1 when it declares none)."""
        state = self._plugin_states.get(owner)
        return 1 if state is None else state.version

    def store_window_projection(self, lease: LeaseContext, covered_from_sequence: int, covered_to_sequence: int, replacement: str) -> None:
        store = self.require_store()
        if not replacement.strip():
            raise ValueError("a window projection must carry non-empty replacement content")
        events = store.events(lease.identity.session_id)
        if not events:
            raise ValueError("session has no events to project")
        last_sequence = events[-1].sequence
        if covered_from_sequence != events[0].sequence:
            raise ValueError("a window projection must start at the session's first event")
        if covered_to_sequence < covered_from_sequence or covered_to_sequence > last_sequence:
            raise ValueError(f"covered_to_sequence must be within 1..{last_sequence}")
        covered = [event for event in events if event.sequence <= covered_to_sequence]
        # Integrity: the replaced prefix must be batch-complete, so every assistant tool call it
        # covers is answered inside it and the surviving tail still starts at a message boundary.
        sizes = store.event_payload_bytes(lease.identity.session_id, limit=covered_to_sequence)
        records = context_records(covered, sizes=sizes)
        if not records[-1].safe_boundary:
            raise ValueError("a window projection must cover whole native tool batches")
        covered_bytes = sum(record.payload_bytes for record in records)
        replacement_bytes = len(replacement.encode("utf-8"))
        if replacement_bytes >= covered_bytes:
            raise ValueError("a window projection must be smaller than the window it replaces")
        lease.staging.events.append(
            EventDraft(
                EventType.WINDOW_PROJECTED,
                "core",
                "plugin",
                {
                    "owner": lease.identity.owner,
                    "covered_from_sequence": covered_from_sequence,
                    "covered_to_sequence": covered_to_sequence,
                    "replacement": replacement,
                    "covered_bytes": covered_bytes,
                    "dropped_bytes": covered_bytes - replacement_bytes,
                },
            )
        )

    def context_history(self, session_id: str, limit: int, after_sequence: int = 0) -> ContextHistory:
        """One page of the certified prefix; safe boundaries are computed from record 1.

        The fetch starts at the session's first event and ends one record past this page,
        so a page suffix is never certified on its own. The slice is taken only after the
        full fetched prefix has been validated.
        """
        if type(after_sequence) is not int or after_sequence < 0 or type(limit) is not int or not 1 <= limit <= 2000:
            raise ValueError("after_sequence must be >= 0 and limit within 1..2000")
        store = self.require_store()
        source_limit = after_sequence + limit + 1
        source = store.events(session_id, after_sequence=0, limit=source_limit)
        sizes = store.event_payload_bytes(session_id, after_sequence=0, limit=source_limit)
        prefix = context_records(source, sizes=sizes)
        start = 0
        while start < len(prefix) and prefix[start].sequence <= after_sequence:
            start += 1
        page = prefix[start : start + limit]
        return ContextHistory(page, len(prefix) <= start + limit)

    def transcript_page(self, session_id: str, after: int, limit: int) -> TranscriptPage:
        source = self.require_store().events(session_id, after_sequence=after, limit=limit + 1)
        entries: list[TranscriptEntry] = []
        budget = 64 * 1024
        consumed = 0
        cursor = after
        sizes = self.require_store().event_payload_bytes(session_id, after_sequence=after, limit=limit + 1)
        for event in source[:limit]:
            size = sizes[event.sequence]
            if consumed and size > budget:
                break
            entry = transcript_entry(event)
            if entry is not None:
                entries.append(entry)
            budget -= size
            consumed += 1
            cursor = event.sequence
        return TranscriptPage(tuple(entries), cursor, len(source) > consumed, consumed)

    def event_views(self, session_id: str, after: int, limit: int, include_payload: bool) -> tuple[EventView, ...]:
        """One bounded page of a session's events; the payload budget is shared by the page."""
        from voidcode.sdk.services import EventView as View

        store = self.require_store()
        views: list[EventView] = []
        budget = 64 * 1024
        sizes = store.event_payload_bytes(session_id, after_sequence=after, limit=limit)
        for event in store.events(session_id, after_sequence=after, limit=limit):
            payload_wire: dict[str, object] | None = None
            truncated = False
            if include_payload:
                original_bytes = sizes[event.sequence]
                if original_bytes > budget:
                    truncated = True
                    payload_wire = {"truncated": True, "original_bytes": original_bytes}
                else:
                    budget -= original_bytes
                    payload_wire = json_wire_object(event.payload)
            views.append(
                View(
                    sequence=event.sequence,
                    event_type=event.event_type,
                    run_id=event.run_id,
                    revision_id=event.revision_id,
                    source=event.source,
                    actor=event.actor,
                    payload=payload_wire,
                    payload_truncated=truncated,
                )
            )
        return tuple(views)

    def is_descendant(self, child: SessionRecord, caller_id: str) -> bool:
        """Whether the caller sits on the child's own parent chain (never a peer)."""
        store = self.require_store()
        current: SessionRecord | None = child
        while current is not None and current.parent_session_id is not None:
            if current.parent_session_id == caller_id:
                return True
            try:
                current = store.session(current.parent_session_id)
            except KeyError:
                return False
        return False

    def has_state_deactivator(self, owner: str) -> bool:
        """Whether one loaded owner declares a pure deactivation transform."""
        return owner in self._state_deactivators

    def prepare_deactivate_owner_state(
        self,
        owner: str,
        payload: Mapping[str, object],
        *,
        scope: StateScope,
        scope_key: str,
        tip_sequence: int | None,
    ) -> dict[str, object]:
        """Run one owner's pure deactivation transform in the runtime that still holds its schema."""
        if scope not in ("session", "workspace"):
            raise RuntimeError("scope must be 'session' or 'workspace'")
        if scope == "session" and (isinstance(tip_sequence, bool) or not isinstance(tip_sequence, int) or tip_sequence < 0):
            raise RuntimeError("session-scope deactivation requires the owning session's event tip as a non-negative integer")
        if scope == "workspace" and tip_sequence is not None:
            raise RuntimeError("workspace-scope deactivation has no single session event tip")
        transform = self._state_deactivators.get(owner)
        if transform is None:
            raise RuntimeError(f"plugin {owner} declares no prepare_deactivate transform")
        try:
            prepared = transform(payload, scope, scope_key, tip_sequence)
        except Exception as exc:
            raise RuntimeError(f"state deactivation of {owner} ({scope}:{scope_key}) failed: {exc}") from exc
        if not isinstance(prepared, Mapping):
            raise RuntimeError("prepare_deactivate must return a JSON object")
        return json_wire_object(prepared)
