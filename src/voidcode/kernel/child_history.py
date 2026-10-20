"""Immutable child-history snapshots.

Every terminal child run leaves a readable, content-addressed snapshot of the whole child
session as of that point. The SQLite event stream remains the only mutable truth; this module
only projects it, so a snapshot is never a second journal and can never be edited. The spill is
streamed block by block and never size-clipped: the store's own artifact bound is the only
ceiling, and a failure there is reported as a visible history error instead of a lossy snapshot.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable

from ..sdk.history import iter_event_blocks
from .artifacts import ArtifactStore
from .storage.store import StoredEvent

MEDIA_TYPE = "text/markdown; charset=utf-8"
#: The artifact store's owner label for kernel-generated snapshots; it scopes unfinished spills.
SPILL_OWNER = "kernel"
_HASH_NAME = "kernel.child_history"


def snapshot_id(child_session_id: str, run_id: str) -> str:
    """Deterministic naming hint for logs/tests; the artifact path is still its digest."""
    return hashlib.sha256("\0".join((_HASH_NAME, child_session_id, run_id)).encode("utf-8")).hexdigest()


def spill_history(
    store: ArtifactStore,
    *,
    child_session_id: str,
    run_id: str,
    status: str,
    label: str,
    events: Iterable[StoredEvent],
) -> dict[str, object]:
    """Stream one complete readable snapshot and return its durable reference facts."""
    items = tuple(events)
    header = (
        f"# Child session {child_session_id}\n\n"
        f"- run: {run_id or '(none)'}\n"
        f"- status: {status}\n"
        f"- label: {label or '(none)'}\n"
        f"- events: {len(items)}\n"
        f"- live view (paged): voidcode://transcript/{child_session_id}\n\n"
    )
    spill_id, spill = store.start_spill(MEDIA_TYPE, owner=SPILL_OWNER)
    try:
        spill.append(header.encode("utf-8"))
        for index, block in enumerate(iter_event_blocks(items)):
            separator = "\n\n" if index else ""
            spill.append(f"{separator}{block}".encode())
        sha256, size, media_type = store.finish(spill_id)
    except Exception:
        store.discard(spill_id)
        raise
    return {
        "sha256": sha256,
        "bytes": size,
        "media_type": media_type,
        "events": len(items),
    }
