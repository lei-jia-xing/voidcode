from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from .events import EventEnvelope
from .mode import backfill_runtime_policy_mode, runtime_mode_from_metadata, runtime_read_only_from_metadata
from .policy import runtime_policy_snapshot_from_session_metadata

if TYPE_CHECKING:
    from .storage import SessionStore

type SessionStatus = Literal["idle", "running", "waiting", "completed", "failed", "interrupted"]
type SessionKind = Literal["top_level", "child"]

# Statuses after which the session's persisted truth is sealed against late
# events. ``completed``/``failed`` are the storage-level seal written by
# ``save_run``; ``interrupted`` is the row status of a run that ended without a
# terminal seal (cancel, overlap, shutdown). An ``interrupted`` row is sealed
# against *late* events from the dead run and only re-opens through explicit
# re-entry: a fresh run / follow-up (``save_interrupted_checkpoint`` with
# ``create_if_missing``) or an explicit resume (``resume_stream``). The runtime
# guard in ``VoidCodeRuntime._sealed_session_status`` couples this with the
# active-run registry so a live run's own appends are never misclassified as
# late events.
SESSION_TERMINAL_STATUSES: frozenset[SessionStatus] = frozenset({"completed", "failed", "interrupted"})
# Narrow half of the terminal-seal split: rows in these statuses are sealed at
# the storage layer (``storage._assert_terminal_session_events_allowed``) and
# reject late events. ``interrupted`` rows are the live in-flight state of a
# running session and are sealed only by the runtime-level guard
# (``VoidCodeRuntime._sealed_session_status``), never by the storage layer.
SESSION_STORAGE_SEAL_STATUSES: frozenset[SessionStatus] = frozenset({"completed", "failed"})


def is_session_status_terminal(status: SessionStatus) -> bool:
    """Return whether ``status`` is a terminal status for session sealing.

    Single source of truth for the runtime's terminal-seal guard. Storage-level
    appends use the narrower ``{completed, failed}`` seal (the row status of a
    live run is ``interrupted``); the runtime-level guard additionally treats
    ``interrupted`` as sealed when no run is active on the session.
    """
    return status in SESSION_TERMINAL_STATUSES


_PERSISTED_STRING_LIMIT = 1_000
_PERSISTED_LIST_LIMIT = 50
_PERSISTED_DICT_LIMIT = 100
_REDACTED = "<redacted>"
_SECRET_KEY_FRAGMENTS = (
    "api_key",
    "apikey",
    "authorization",
    "bearer",
    "credential",
    "password",
    "secret",
    "session_token",
)
_SECRET_VALUE_PATTERNS = (
    re.compile(r"(?<![A-Za-z0-9_\-])sk-[A-Za-z0-9_\-]{6,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]{6,}", re.IGNORECASE),
    re.compile(r"(?i)(api[_-]?key|token|secret|password)=([^\s&]+)"),
)
_REDACTED_ENV_VALUE_KEYS = frozenset(
    {
        "env",
        "environment",
        "injected_env",
        "injected_env_values",
        "env_values",
    }
)

# Summary fields are long by construction and already bounded by context
# compaction, so they are redacted but never truncated: a resume must see the
# full projection rather than the metadata length cap's half-summary.


def _looks_secret_key(key: str) -> bool:
    lowered = key.lower()
    return any(fragment in lowered for fragment in _SECRET_KEY_FRAGMENTS)


def _redact_text(value: str) -> str:
    """Apply the secret-value patterns without the persistence length cap.

    Used for summary fields that are long by construction and already bounded by
    context compaction, so truncating them at the metadata cap would leave the
    model a half-summary after resume.
    """
    redacted = value
    for pattern in _SECRET_VALUE_PATTERNS:
        redacted = pattern.sub(_REDACTED, redacted)
    return redacted


def _scrub_text(value: str) -> str:
    scrubbed = _redact_text(value)
    if len(scrubbed) <= _PERSISTED_STRING_LIMIT:
        return scrubbed
    return scrubbed[:_PERSISTED_STRING_LIMIT] + (
        f"\n... [truncated by runtime metadata: kept first {_PERSISTED_STRING_LIMIT} of {len(scrubbed)} chars]"
    )


def _bounded_redacted_mapping(value: Mapping[str, object]) -> dict[str, object]:
    """The bounded, redacted copy of one top-level mapping: a mapping in, a mapping out.

    ``_bounded_redacted`` returns ``object`` (it also bounds lists, passes scalars
    through, and replaces a value under a secret key with ``_REDACTED``), so a
    caller holding a mapping would otherwise have to cast the result back. The
    walk stays in one place: this is its mapping arm, reached only when the
    caller's own key is not a secret.
    """
    result: dict[str, object] = {}
    for index, (raw_key, item) in enumerate(value.items()):
        if index >= _PERSISTED_DICT_LIMIT:
            result["__truncated__"] = True
            break
        item_key = str(raw_key)
        result[item_key] = _bounded_redacted(item, key=item_key)
    return result


def _bounded_redacted(value: object, *, key: str | None = None) -> object:
    if key is not None and (_looks_secret_key(key) or key.lower() in _REDACTED_ENV_VALUE_KEYS):
        return _REDACTED
    if isinstance(value, dict):
        return _bounded_redacted_mapping(value)
    if isinstance(value, list):
        result = [_bounded_redacted(item) for item in value[:_PERSISTED_LIST_LIMIT]]
        if len(value) > _PERSISTED_LIST_LIMIT:
            result.append({"__truncated__": True, "original_length": len(value)})
        return result
    if isinstance(value, tuple):
        items = list(value)
        result = [_bounded_redacted(item) for item in items[:_PERSISTED_LIST_LIMIT]]
        if len(items) > _PERSISTED_LIST_LIMIT:
            result.append({"__truncated__": True, "original_length": len(items)})
        return result
    if isinstance(value, str):
        if key == "summary_text":
            return _redact_text(value)
        return _scrub_text(value)
    return value


def session_metadata_for_replay(metadata: dict[str, object]) -> dict[str, object]:
    """Return session metadata projected for replay/resume without run-local markers."""

    projected = dict(metadata)
    raw_runtime_policy = projected.get("runtime_policy")
    if not isinstance(raw_runtime_policy, dict):
        return projected
    runtime_policy: dict[str, object] = dict(raw_runtime_policy)
    raw_prompt_activation = runtime_policy.get("prompt_activation")
    if isinstance(raw_prompt_activation, dict):
        runtime_policy["prompt_activation"] = {
            **raw_prompt_activation,
            "activated_this_turn": False,
        }
    projected["runtime_policy"] = runtime_policy
    return projected


def normalize_persisted_session_metadata(metadata: dict[str, object]) -> dict[str, object]:
    """Validate persisted top-level runtime mode metadata."""
    return backfill_runtime_policy_mode(metadata)


def _policy_observations(events: tuple[EventEnvelope, ...]) -> dict[str, object]:
    tool_policy_denial: dict[str, object] | None = None
    shell_policy_events: list[dict[str, object]] = []
    for event in events:
        event_type = event.event_type
        payload = event.payload
        raw_tool_policy = payload.get("tool_policy")
        if payload.get("kind") == "runtime_tool_policy_denied" and isinstance(raw_tool_policy, dict):
            tool_policy_denial = {
                "event_sequence": event.sequence,
                **_bounded_redacted_mapping(raw_tool_policy),
            }
        if payload.get("policy_surface") == "shell_policy":
            shell_policy_events.append(
                {
                    "event_sequence": event.sequence,
                    "event_type": event_type,
                    "tool": payload.get("tool"),
                    "path_scope": payload.get("path_scope"),
                    "operation_class": payload.get("operation_class"),
                    "matched_rule": _bounded_redacted(payload.get("matched_rule")),
                    "policy_surface": "shell_policy",
                }
            )
        if payload.get("tool") == "shell_exec" and "injected_env_keys" in payload:
            shell_policy_events.append(
                {
                    "event_sequence": event.sequence,
                    "event_type": event_type,
                    "tool": "shell_exec",
                    "injected_env_keys": _bounded_redacted(payload.get("injected_env_keys")),
                }
            )
    observations: dict[str, object] = {}
    if tool_policy_denial is not None:
        observations["tool_policy_denial"] = tool_policy_denial
    if shell_policy_events:
        observations["shell_policy"] = shell_policy_events[-_PERSISTED_LIST_LIMIT:]
    return observations


def session_metadata_for_persistence(
    metadata: dict[str, object],
    *,
    events: tuple[EventEnvelope, ...] = (),
) -> dict[str, object]:
    """Return bounded, redacted session metadata safe for durable storage.

    Boundary: this applies safety bounds (secret scrubbing, string-length caps,
    dict/list depth limits) — NOT context compaction. Context window projection
    is owned by ``context/window.py``. The bounds here prevent unbounded metadata
    bloat in the sessions row.
    """

    persisted = _bounded_redacted_mapping(metadata)
    mode = runtime_mode_from_metadata(persisted)
    read_only = runtime_read_only_from_metadata(persisted)
    observations = _policy_observations(events)
    raw_persisted_runtime_policy = persisted.get("runtime_policy")
    if isinstance(raw_persisted_runtime_policy, dict):
        persisted["runtime_policy"] = runtime_policy_snapshot_from_session_metadata(persisted)
    has_policy_truth = (
        "mode" in metadata
        or "read_only" in metadata
        or "delegation" in metadata
        or "prompt_stack" in metadata
        or bool(observations)
        or mode != "normal"
        or read_only
    )
    if not has_policy_truth:
        return persisted

    persisted["mode"] = mode
    persisted["read_only"] = read_only

    if "runtime_policy" in persisted:
        raw_runtime_policy = persisted.get("runtime_policy")
        if observations and isinstance(raw_runtime_policy, dict):
            runtime_policy_metadata: dict[str, object] = raw_runtime_policy
            persisted["runtime_policy"] = {
                **runtime_policy_metadata,
                **observations,
            }
    elif observations:
        persisted["policy_observations"] = observations
    return persisted


@dataclass(frozen=True, slots=True)
class SessionRef:
    id: str
    parent_id: str | None = None

    @property
    def kind(self) -> SessionKind:
        return "child" if self.parent_id is not None else "top_level"


@dataclass(frozen=True, slots=True)
class SessionState:
    session: SessionRef
    status: SessionStatus = "idle"
    turn: int = 0
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class StoredSessionSummary:
    session: SessionRef
    status: SessionStatus
    turn: int
    prompt: str
    updated_at: int
    title: str | None = None
    #: Fork provenance: the session this one copied an event-log prefix from,
    #: and the copied boundary. ``None`` for a non-forked session. Deliberately
    #: not ``parent_session_id``, which means *delegated child* to every reader.
    forked_from_session_id: str | None = None
    forked_at_sequence: int | None = None


@dataclass(frozen=True, slots=True)
class StoredSessionLineageEntry:
    """One row of a fork lineage walk (read-only provenance projection)."""

    session_id: str
    forked_from_session_id: str | None
    forked_at_sequence: int | None


@dataclass(frozen=True, slots=True)
class StoredSessionForestEntry:
    """One node of the workspace fork forest, in display order (parent first).

    Carries the same provenance as :class:`StoredSessionLineageEntry` plus the
    topological ``depth`` a tree renderer indents by: 0 for a root (no
    provenance, or a ``forked_from_session_id`` that is not part of the
    returned forest — a fork of a deleted/disabled session is still a root).
    """

    session_id: str
    forked_from_session_id: str | None
    forked_at_sequence: int | None
    depth: int


def validate_session_workspace(
    session: SessionState,
    *,
    session_id: str,
    workspace: Path,
) -> None:
    session_workspace = session.metadata.get("workspace")
    if session_workspace is None:
        return
    if session_workspace != str(workspace):
        raise ValueError(f"session {session_id} does not belong to workspace {workspace}")


def reload_persisted_session(
    store: SessionStore,
    workspace: Path,
    *,
    session_id: str,
) -> SessionState:
    response = store.load_session(
        workspace=workspace,
        session_id=session_id,
    )
    validate_session_workspace(response.session, session_id=session_id, workspace=workspace)
    return response.session
