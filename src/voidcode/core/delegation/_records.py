"""Durable child identity reads: spawn facts, birth records, and ownership."""

from __future__ import annotations

from ..events import EventType
from ..storage.store import (
    SessionRecord,
    SqliteStore,
    StoreConflict,
)
from ._base import DelegationBase


class _DelegationRecords(DelegationBase):
    """Durable child identity reads: spawn facts, birth records, and ownership."""

    def _spawned_fact(self, parent_session_id: str, child_id: str) -> dict[str, object] | None:
        """The durable spawn fact for one child, if the crash window already committed it."""
        spawns = self.core.store.events_by_types(parent_session_id, (EventType.CHILD_SPAWNED,))
        for event in reversed(spawns):
            if event.event_type == EventType.CHILD_SPAWNED and event.payload.get("child_session_id") == child_id:
                return dict(event.payload)
        return None

    def _birth_record(self, child_id: str) -> dict[str, object] | None:
        """The child's own atomic birth record (sequence 1), or None when it is missing.

        None covers both a child that predates birth records and a session id with no
        row at all: a first keyed spawn probes its deterministic id before creating it.
        The birth record commits in the same transaction as the child row, so it is the
        crash-window truth when the parent fact never landed. It is read-only here.
        """
        try:
            first = self.core.store.events(child_id, limit=1)
        except KeyError:
            return None
        for event in first:
            if event.sequence == 1 and event.event_type == EventType.CHILD_RESERVED:
                return dict(event.payload)
        return None

    def _captured_child_root_id(self, child_id: str) -> str:
        """Return the immutable root action identity committed at child reservation."""
        birth = self._birth_record(child_id)
        root_id = None if birth is None else birth.get("root_id")
        if not isinstance(root_id, str) or not root_id:
            raise StoreConflict(f"child {child_id!r} has no captured root action identity")
        return root_id

    def _root_action_id_for_send(self, child: SessionRecord) -> str:
        """Child sends retain the root authorization captured at the child's birth."""
        return self._captured_child_root_id(child.session_id)

    def _child_label(self, parent_session_id: str, child_id: str) -> str:
        spawns = self.core.store.events_by_types(parent_session_id, (EventType.CHILD_SPAWNED,))
        for event in reversed(spawns):
            if event.event_type == EventType.CHILD_SPAWNED and event.payload.get("child_session_id") == child_id:
                label = event.payload.get("label")
                return label if isinstance(label, str) else ""
        return ""

    def _owned_child(self, parent_session_id: str, child_id: str) -> SessionRecord:
        child = self.core.session(child_id)
        if child.parent_session_id != parent_session_id:
            raise PermissionError("child session does not belong to this parent session")
        return child

    def _fence_holds(self, chain: frozenset[str], child_id: str | None = None) -> str | None:
        """The active fence blocking spawn/send through this chain, if any (caller holds the lifecycle lock)."""
        fences = self._cancel_fences
        if child_id is not None and child_id in fences:
            return child_id
        for fenced in fences:
            if fenced in chain:
                return fenced
        return None

    def _chain_of(self, session_id: str) -> frozenset[str]:
        """One session's ancestor chain snapshot (outside any store transaction)."""
        try:
            record = self.core.store.session(session_id)
        except KeyError:
            return frozenset((session_id,))
        return frozenset((session_id, *self._parent_chain(record)))

    def _guard_fence(self, chain: frozenset[str], child_id: str | None) -> str | None:
        """Store-transaction fence check: in-memory fence set plus a captured chain."""
        with self._lifecycle_lock:
            return self._fence_holds(chain, child_id)

    def attach_store(self, store: SqliteStore) -> None:
        """Install the in-transaction fence guard on the owning store (called once by Core)."""
        store._cancel_fence_guard = self._guard_fence  # noqa: SLF001 - core owns the store seam

    def _parent_chain(self, record: SessionRecord) -> tuple[str, ...]:
        chain: list[str] = []
        current = record.parent_session_id
        seen = {record.session_id}
        while current is not None and current not in seen:
            chain.append(current)
            seen.add(current)
            try:
                current = self.core.store.session(current).parent_session_id
            except KeyError:
                break
        return tuple(chain)
