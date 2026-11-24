"""Shared projections from core records to client-visible shapes.

The HTTP transport and the in-process TUI client must render one identical
surface: the same session payloads, event frames, health body, and error
envelope. This module owns those shapes so neither client can drift from the
other; the wire framing (JSON-RPC messages) stays in ``http.py``.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Any, Final

from voidcode import __version__
from voidcode.core.artifacts import ArtifactError
from voidcode.core.storage.store import (
    InvalidRevision,
    SessionRecord,
    StaleRevision,
    StoredEvent,
    UnknownConfirmation,
    UnknownContinuation,
    UnknownSession,
    UnknownWorkspace,
)
from voidcode.core.transcript import transcript_entry
from voidcode.sdk.json_values import json_wire_object
from voidcode.sdk.transcript import encode_transcript_entry

from .models import SESSION_ID_MAX_LENGTH, SESSION_ID_PATTERN

LAUNCH_PROTOCOL_VERSION = 1

FOLLOW_TAIL_LIMIT = 100


def session_payload(
    record: SessionRecord,
    *,
    summary: str | None = None,
    created_at: int | None = None,
    updated_at: int | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "session_id": record.session_id,
        "workspace": record.workspace,
        "status": record.status.value,
        "active_revision_id": record.active_revision_id,
        "run_id": record.run_id,
        "turn": record.turn,
        "last_event_sequence": record.last_event_sequence,
    }
    if summary is not None:
        payload["summary"] = summary
    if created_at is not None:
        payload["created_at"] = created_at
    if updated_at is not None:
        payload["updated_at"] = updated_at
    return payload


def event_payload(session_id: str, event: StoredEvent) -> dict[str, object]:
    """One stored event as session event frames carry it; malformed conversation records fail visibly."""
    entry = transcript_entry(event)
    return {
        "session_id": session_id,
        "sequence": event.sequence,
        "type": event.event_type,
        "run_id": event.run_id,
        "revision_id": event.revision_id,
        "source": event.source,
        "actor": event.actor,
        "payload": json_wire_object(event.payload),
        "transcript": None if entry is None else encode_transcript_entry(entry),
    }


def safe_projection_error(event: StoredEvent, error: Exception) -> str:
    """Sequence + event type + exception class only; never str(exc), which leaks inputs."""
    return f"projection failed: sequence={event.sequence} type={event.event_type} error={type(error).__name__}"


def model_payload(*, raw: str | None, configured_effort: str | int | None) -> dict[str, object]:
    """The health ``model`` section: admitted identity plus supported thinking levels.

    Levels come from the shipped catalog row for ``provider/model``: its own
    ``supported_effort_levels``, else the thinking rule's budget-tier names for a
    known budget-backed row. ``off`` joins only when the row explicitly advertises
    ``supports_reasoning_off``. ``reasoning_effort`` is the configured level
    clamped to them. Unknown/non-reasoning/unset/numeric configs hide the effort.
    """
    from voidcode.capabilities.providers.native.model_catalog import static_catalog_metadata
    from voidcode.capabilities.providers.native.reasoning_effort import CANONICAL_EFFORTS, clamp_effort_to_supported, normalize_reasoning_effort
    from voidcode.capabilities.providers.native.thinking_rules import thinking_rule_for

    provider: str | None = None
    model: str | None = None
    if raw is not None:
        provider_segment, separator, model_segment = raw.partition("/")
        if separator and provider_segment and model_segment:
            provider, model = provider_segment, model_segment
    levels: list[str] = []
    effort: str | None = None
    context_window: int | None = None
    if provider is not None and model is not None:
        metadata = static_catalog_metadata(provider, model)
        if metadata is not None:
            context_window = metadata.context_window
        if metadata is not None and metadata.supports_reasoning is True:
            row_levels = metadata.supported_effort_levels
            if row_levels:
                levels = [level for level in CANONICAL_EFFORTS if level in row_levels]
            elif metadata.supports_thinking_budget is True:
                budgets = thinking_rule_for(provider, model).budgets
                levels = [level for level in CANONICAL_EFFORTS if level in budgets]
            if levels and metadata.supports_reasoning_off is True:
                levels = ["off", *levels]
            if levels and not isinstance(configured_effort, bool) and isinstance(configured_effort, str):
                try:
                    normalized = normalize_reasoning_effort(configured_effort)
                except ValueError:
                    normalized = None
                if normalized == "off":
                    effort = "off" if "off" in levels else None
                elif isinstance(normalized, str):
                    effort = clamp_effort_to_supported(normalized, tuple(level for level in levels if level != "off"))
    return {
        "raw": raw,
        "provider": provider,
        "model": model,
        "reasoning_effort": effort,
        "reasoning_levels": levels,
        "context_window": context_window,
    }


def health_payload(
    *,
    revision_id: str,
    stage: str,
    workspace: str,
    generation: int = 1,
    upgrade_id: str | None = None,
    model: Mapping[str, object] | None = None,
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
        "model": dict(model) if model is not None else None,
    }


_SESSION_ID_RE: Final = re.compile(SESSION_ID_PATTERN)


def validated_session_id(value: str) -> str:
    if not isinstance(value, str) or len(value) > SESSION_ID_MAX_LENGTH or _SESSION_ID_RE.fullmatch(value) is None:
        raise ValueError("session_id must be a non-empty string without '/' or NUL within 128 characters")
    return value


def core_error_status(error: Exception) -> tuple[int, str | None]:
    """Map a core failure onto the transport's ``(status, code)`` refusal.

    Mirrors the HTTP transport's error envelope: stale displayed revisions
    are 409 ``stale_revision``, unknown sessions are 404, anything else the
    core raises visibly is 400, and the rest is a 500 with no invented detail.
    """
    # A client acting on a stale or missing displayed revision is refused before
    # any dispatch: the refusal is visible and never retried automatically.
    if isinstance(error, StaleRevision):
        return 409, "stale_revision"
    if isinstance(error, InvalidRevision):
        return 400, "invalid_params"
    if isinstance(error, FileNotFoundError):
        return 404, "not_found"
    if isinstance(error, ArtifactError):
        return 400, "invalid_params"
    if isinstance(error, (KeyError, UnknownSession, UnknownConfirmation, UnknownContinuation, UnknownWorkspace)):
        return 404, None
    if isinstance(error, PermissionError):
        return 403, None
    if isinstance(error, (ValueError, RuntimeError)):
        return 400, None
    return 500, None
