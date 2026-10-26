"""Shared projections from core records to client-visible shapes.

The HTTP transport and the in-process TUI client must render one identical
surface: the same session payloads, event frames, health body, and error
envelope. This module owns those shapes so neither client can drift from the
other; the wire framing (JSON bytes, SSE bytes) stays in ``http.py``.
"""

from __future__ import annotations

import os
from typing import Any

from voidcode import __version__
from voidcode.core.storage.store import SessionRecord, StoredEvent
from voidcode.core.transcript import transcript_entry
from voidcode.sdk.codec import _wire
from voidcode.sdk.json_values import json_wire_object

LAUNCH_PROTOCOL_VERSION = 1

#: Follow cadence for live event streams. Measured 2026-10-26: at 1.0 s a
#: committed progress event waited 0.1-0.9 s (mean ~0.5 s) before delivery,
#: which makes token streaming feel dead. 0.1 s bounds that wait at ~50 ms
#: mean for one cheap indexed tail read per idle stream — no wake-up channel
#: needed until stream counts grow.
FOLLOW_POLL_SECONDS = 0.1
FOLLOW_TAIL_LIMIT = 100


def session_payload(record: SessionRecord) -> dict[str, object]:
    return {
        "session_id": record.session_id,
        "workspace": record.workspace,
        "status": record.status,
        "active_revision_id": record.active_revision_id,
        "run_id": record.run_id,
        "turn": record.turn,
        "last_event_sequence": record.last_event_sequence,
    }


def event_payload(session_id: str, event: StoredEvent) -> dict[str, object]:
    try:
        entry = transcript_entry(event)
    except Exception:
        entry = None
    return {
        "session_id": session_id,
        "sequence": event.sequence,
        "type": event.event_type,
        "run_id": event.run_id,
        "revision_id": event.revision_id,
        "source": event.source,
        "actor": event.actor,
        "payload": json_wire_object(event.payload),
        "transcript": _wire(entry) if entry is not None else None,
    }


def health_payload(
    *,
    revision_id: str,
    stage: str,
    workspace: str,
    generation: int,
    upgrade_id: str | None,
) -> dict[str, Any]:
    return {
        "status": "ok",
        "stage": stage,
        "pid": os.getpid(),
        "workspace": workspace,
        "revision_id": revision_id,
        "launch_protocol": LAUNCH_PROTOCOL_VERSION,
        "core_version": __version__,
        "web_bundle": None,
        "generation": generation,
        "active_upgrade": None if upgrade_id is None else {"upgrade_id": upgrade_id, "stage": stage},
    }


def validated_session_id(value: str) -> str:
    if not value or "/" in value or "\0" in value or len(value) > 128:
        raise ValueError("session_id must be a non-empty string without '/' or NUL within 128 characters")
    return value


def core_error_status(error: Exception) -> tuple[int, str | None]:
    """Map a core failure onto the transport's ``(status, code)`` refusal.

    Mirrors the HTTP transport's error envelope: stale displayed revisions
    are 409 ``stale_revision``, unknown sessions are 404, anything else the
    core raises visibly is 400, and the rest is a 500 with no invented detail.
    """
    message = str(error)
    # A client acting on a stale or missing displayed revision is refused before
    # any dispatch: the refusal is visible and never retried automatically.
    if "is stale; refresh the catalog" in message:
        return 409, "stale_revision"
    if "expected_revision must be the revision the client displayed" in message:
        return 400, "invalid_params"
    if isinstance(error, KeyError) or "unknown session" in message or "does not exist" in message:
        return 404, None
    if isinstance(error, PermissionError):
        return 403, None
    if isinstance(error, (ValueError, RuntimeError)):
        return 400, None
    return 500, None
