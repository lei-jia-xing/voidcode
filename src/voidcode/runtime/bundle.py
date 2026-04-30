"""Portable session bundle schema, redaction, and import/export helpers.

The bundle is intentionally an inert artifact:

- Importing it does not auto-resume execution; it only persists the session
  for ``sessions debug`` / ``sessions resume --dry-run`` style inspection.
- Default export redacts secrets, raw provider messages, full reasoning text,
  and oversized tool output. Opt-in flags can include them when the operator
  knows the destination is private.
- The schema is versioned (``voidcode.session.bundle.v1``); incompatible
  schemas fail fast on import with a clear migration message.

The on-disk format is either a JSON file or a zip archive containing a
single ``bundle.json`` entry. The zip wrapper exists so future revisions can
add bounded log files or screenshots without breaking the JSON entry.
"""

from __future__ import annotations

import hashlib
import io
import json
import platform
import re
import sys
import time
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final, Literal, cast, final

from .. import __version__ as VOIDCODE_VERSION
from .contracts import (
    RuntimeRequest,
    RuntimeResponse,
    UnknownSessionError,
    validate_session_id,
    validate_session_reference_id,
)
from .events import EventEnvelope, EventSource
from .session import SessionRef, SessionState, SessionStatus
from .storage import SessionStore
from .task import StoredBackgroundTaskSummary

SESSION_BUNDLE_SCHEMA_NAME: Final[str] = "voidcode.session.bundle.v1"
SESSION_BUNDLE_SCHEMA_VERSION: Final[int] = 1
SESSION_BUNDLE_FILE_NAME: Final[str] = "bundle.json"
SESSION_BUNDLE_DEFAULT_EXTENSION: Final[str] = ".vcsession.zip"
SESSION_BUNDLE_REDACTED_PLACEHOLDER: Final[str] = "<redacted>"

type SessionBundleFormat = Literal["zip", "json"]


_REDACTED_KEY_FRAGMENTS: Final[tuple[str, ...]] = (
    "api_key",
    "apikey",
    "auth",
    "authorization",
    "bearer",
    "cookie",
    "credential",
    "password",
    "secret",
    "session_token",
    "token",
)


_REDACTED_VALUE_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"sk-[A-Za-z0-9_\-]{6,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]{6,}", re.IGNORECASE),
    re.compile(r"(?i)(api[_-]?key|token|secret|password)=([^\s&]+)"),
)


_RAW_PROVIDER_EVENT_TYPES: Final[frozenset[str]] = frozenset(
    {
        "provider.request_payload",
        "provider.response_payload",
        "provider.raw_message",
        "provider.raw_messages",
    }
)


_REASONING_TEXT_KEYS: Final[frozenset[str]] = frozenset(
    {
        "reasoning",
        "reasoning_text",
        "reasoning_content",
        "thinking",
        "thinking_text",
        "thoughts",
    }
)


_TOOL_OUTPUT_KEYS: Final[frozenset[str]] = frozenset(
    {
        "content",
        "output",
        "stdout",
        "stderr",
        "result_text",
    }
)


_DEFAULT_TOOL_OUTPUT_PREVIEW_CHARS: Final[int] = 2_000


class SessionBundleError(ValueError):
    """Raised when a session bundle is malformed or incompatible."""


@dataclass(frozen=True, slots=True)
class SessionBundleOptions:
    """Operator-facing knobs that control export verbosity and redaction."""

    redact: bool = True
    include_tool_output: bool = False
    include_raw_provider_messages: bool = False
    include_reasoning_text: bool = False
    support_mode: bool = False
    tool_output_preview_chars: int = _DEFAULT_TOOL_OUTPUT_PREVIEW_CHARS

    @classmethod
    def support_artifact(cls) -> SessionBundleOptions:
        """Return an options preset tuned for bug-report style support bundles."""

        return cls(
            redact=True,
            include_tool_output=False,
            include_raw_provider_messages=False,
            include_reasoning_text=False,
            support_mode=True,
        )


@dataclass(frozen=True, slots=True)
class SessionBundleSessionPayload:
    id: str
    parent_id: str | None
    status: str
    turn: int
    prompt: str
    output: str | None
    metadata: dict[str, object]
    last_event_sequence: int
    events: tuple[dict[str, object], ...]


@dataclass(frozen=True, slots=True)
class SessionBundleBackgroundTaskPayload:
    task_id: str
    status: str
    parent_session_id: str | None
    child_session_id: str | None
    prompt: str
    error: str | None
    created_at: int
    updated_at: int


@dataclass(frozen=True, slots=True)
class SessionBundleDiagnostics:
    storage: dict[str, object] | None = None
    config_summary: dict[str, object] | None = None
    provider_summary: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class SessionBundleManifest:
    schema_version: int
    voidcode_version: str
    created_at: int
    workspace_hash: str
    platform: dict[str, object]
    redaction: dict[str, object]
    support_mode: bool
    session_count: int
    event_count: int
    background_task_count: int


@dataclass(frozen=True, slots=True)
class SessionBundle:
    manifest: SessionBundleManifest
    sessions: tuple[SessionBundleSessionPayload, ...]
    background_tasks: tuple[SessionBundleBackgroundTaskPayload, ...]
    diagnostics: SessionBundleDiagnostics

    def to_payload(self) -> dict[str, object]:
        """Return the canonical JSON-serializable bundle payload."""

        return {
            "schema": SESSION_BUNDLE_SCHEMA_NAME,
            "manifest": _manifest_payload(self.manifest),
            "sessions": [_session_payload(session) for session in self.sessions],
            "background_tasks": [_task_payload(task) for task in self.background_tasks],
            "diagnostics": _diagnostics_payload(self.diagnostics),
        }


@dataclass(frozen=True, slots=True)
class SessionBundleImportResult:
    schema: str
    schema_version: int
    voidcode_version: str
    created_at: int
    support_mode: bool
    redaction: dict[str, object]
    workspace_hash: str
    session_count: int
    event_count: int
    background_task_count: int
    imported_session_ids: tuple[str, ...]
    skipped_session_ids: tuple[str, ...]
    skipped_background_task_count: int
    dry_run: bool

    def to_payload(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "schema_version": self.schema_version,
            "voidcode_version": self.voidcode_version,
            "created_at": self.created_at,
            "support_mode": self.support_mode,
            "redaction": dict(self.redaction),
            "workspace_hash": self.workspace_hash,
            "session_count": self.session_count,
            "event_count": self.event_count,
            "background_task_count": self.background_task_count,
            "imported_session_ids": list(self.imported_session_ids),
            "skipped_session_ids": list(self.skipped_session_ids),
            "skipped_background_task_count": self.skipped_background_task_count,
            "dry_run": self.dry_run,
        }


def _manifest_payload(manifest: SessionBundleManifest) -> dict[str, object]:
    return {
        "schema_version": manifest.schema_version,
        "voidcode_version": manifest.voidcode_version,
        "created_at": manifest.created_at,
        "workspace_hash": manifest.workspace_hash,
        "platform": dict(manifest.platform),
        "redaction": dict(manifest.redaction),
        "support_mode": manifest.support_mode,
        "session_count": manifest.session_count,
        "event_count": manifest.event_count,
        "background_task_count": manifest.background_task_count,
    }


def _session_payload(session: SessionBundleSessionPayload) -> dict[str, object]:
    return {
        "id": session.id,
        "parent_id": session.parent_id,
        "status": session.status,
        "turn": session.turn,
        "prompt": session.prompt,
        "output": session.output,
        "metadata": session.metadata,
        "last_event_sequence": session.last_event_sequence,
        "events": [dict(event) for event in session.events],
    }


def _task_payload(task: SessionBundleBackgroundTaskPayload) -> dict[str, object]:
    return {
        "task_id": task.task_id,
        "status": task.status,
        "parent_session_id": task.parent_session_id,
        "child_session_id": task.child_session_id,
        "prompt": task.prompt,
        "error": task.error,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
    }


def _diagnostics_payload(diagnostics: SessionBundleDiagnostics) -> dict[str, object]:
    payload: dict[str, object] = {}
    if diagnostics.storage is not None:
        payload["storage"] = dict(diagnostics.storage)
    if diagnostics.config_summary is not None:
        payload["config_summary"] = dict(diagnostics.config_summary)
    if diagnostics.provider_summary is not None:
        payload["provider_summary"] = dict(diagnostics.provider_summary)
    return payload


def _looks_like_secret_key(key: str) -> bool:
    lowered = key.lower()
    return any(fragment in lowered for fragment in _REDACTED_KEY_FRAGMENTS)


def _scrub_secret_text(value: str) -> str:
    scrubbed = value
    for pattern in _REDACTED_VALUE_PATTERNS:
        scrubbed = pattern.sub(SESSION_BUNDLE_REDACTED_PLACEHOLDER, scrubbed)
    return scrubbed


def _redact_object(value: object) -> object:
    if isinstance(value, dict):
        result: dict[str, object] = {}
        for raw_key, raw_item in cast(Mapping[object, object], value).items():
            key = str(raw_key)
            if _looks_like_secret_key(key):
                result[key] = SESSION_BUNDLE_REDACTED_PLACEHOLDER
            else:
                result[key] = _redact_object(raw_item)
        return result
    if isinstance(value, list):
        return [_redact_object(item) for item in cast(list[object], value)]
    if isinstance(value, tuple):
        return tuple(_redact_object(item) for item in cast(tuple[object, ...], value))
    if isinstance(value, str):
        return _scrub_secret_text(value)
    return value


def _redact_dict(value: dict[str, object]) -> dict[str, object]:
    return cast(dict[str, object], _redact_object(value))


def _truncate_string(value: str, *, limit: int) -> str:
    if limit <= 0 or len(value) <= limit:
        return value
    suffix = f"\n... [truncated by session bundle: kept first {limit} of {len(value)} chars]"
    return value[:limit] + suffix


def _sanitize_export_text(
    value: str | None,
    *,
    options: SessionBundleOptions,
    truncate_when_tool_output_hidden: bool = False,
) -> str | None:
    if value is None:
        return None
    sanitized = _scrub_secret_text(value) if options.redact else value
    if truncate_when_tool_output_hidden and not options.include_tool_output:
        return _truncate_string(sanitized, limit=options.tool_output_preview_chars)
    return sanitized


def _strip_reasoning_payload(payload: dict[str, object]) -> dict[str, object]:
    cleaned: dict[str, object] = {}
    for key, value in payload.items():
        if key in _REASONING_TEXT_KEYS and isinstance(value, str):
            cleaned[key] = SESSION_BUNDLE_REDACTED_PLACEHOLDER
            continue
        if isinstance(value, dict):
            cleaned[key] = _strip_reasoning_payload(cast(dict[str, object], value))
        elif isinstance(value, list):
            cleaned_items: list[object] = []
            for item in cast(list[object], value):
                if isinstance(item, dict):
                    cleaned_items.append(_strip_reasoning_payload(cast(dict[str, object], item)))
                else:
                    cleaned_items.append(item)
            cleaned[key] = cleaned_items
        else:
            cleaned[key] = value
    return cleaned


def _truncate_tool_output_payload(
    payload: dict[str, object],
    *,
    limit: int,
) -> dict[str, object]:
    cleaned: dict[str, object] = {}
    for key, value in payload.items():
        if key in _TOOL_OUTPUT_KEYS and isinstance(value, str):
            cleaned[key] = _truncate_string(value, limit=limit)
            continue
        if isinstance(value, dict):
            cleaned[key] = _truncate_tool_output_payload(
                cast(dict[str, object], value), limit=limit
            )
        elif isinstance(value, list):
            cleaned_items: list[object] = []
            for item in cast(list[object], value):
                if isinstance(item, dict):
                    cleaned_items.append(
                        _truncate_tool_output_payload(cast(dict[str, object], item), limit=limit)
                    )
                else:
                    cleaned_items.append(item)
            cleaned[key] = cleaned_items
        else:
            cleaned[key] = value
    return cleaned


def _drop_raw_provider_event(event: EventEnvelope) -> bool:
    return event.event_type in _RAW_PROVIDER_EVENT_TYPES


def _apply_payload_options(
    payload: dict[str, object],
    *,
    options: SessionBundleOptions,
) -> dict[str, object]:
    cleaned = payload
    if not options.include_reasoning_text:
        cleaned = _strip_reasoning_payload(cleaned)
    if not options.include_tool_output:
        cleaned = _truncate_tool_output_payload(cleaned, limit=options.tool_output_preview_chars)
    if options.redact:
        cleaned = _redact_dict(cleaned)
    return cleaned


def _workspace_hash(workspace: Path) -> str:
    digest = hashlib.sha256(str(workspace.resolve()).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _platform_summary() -> dict[str, object]:
    return {
        "python_version": platform.python_version(),
        "system": platform.system(),
        "machine": platform.machine(),
        "implementation": sys.implementation.name,
    }


def _redaction_summary(options: SessionBundleOptions) -> dict[str, object]:
    notes: list[str] = []
    if options.redact:
        notes.append("Secret-looking keys and bearer tokens are masked.")
    if not options.include_raw_provider_messages:
        notes.append("Raw provider request/response payloads are dropped.")
    if not options.include_reasoning_text:
        notes.append("Reasoning/thinking text is redacted; metadata is kept.")
    if not options.include_tool_output:
        notes.append(
            f"Tool output text is truncated to {options.tool_output_preview_chars} characters."
        )
    return {
        "redacted": options.redact,
        "include_tool_output": options.include_tool_output,
        "include_raw_provider_messages": options.include_raw_provider_messages,
        "include_reasoning_text": options.include_reasoning_text,
        "tool_output_preview_chars": options.tool_output_preview_chars,
        "notes": notes,
    }


@final
class _SessionBundleBuilder:
    def __init__(
        self,
        *,
        session_store: SessionStore,
        workspace: Path,
        options: SessionBundleOptions,
        storage_diagnostics: dict[str, object] | None,
        config_summary: dict[str, object] | None,
        provider_summary: dict[str, object] | None,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self._session_store = session_store
        self._workspace = workspace
        self._options = options
        self._storage_diagnostics = storage_diagnostics
        self._config_summary = config_summary
        self._provider_summary = provider_summary
        self._clock = clock or (lambda: int(time.time() * 1000))

    def build(self, session_id: str) -> SessionBundle:
        validate_session_id(session_id)
        sessions, event_count = self._collect_sessions(session_id=session_id)
        background_tasks = self._collect_background_tasks(session_id=session_id)
        manifest = SessionBundleManifest(
            schema_version=SESSION_BUNDLE_SCHEMA_VERSION,
            voidcode_version=VOIDCODE_VERSION,
            created_at=self._clock(),
            workspace_hash=_workspace_hash(self._workspace),
            platform=_platform_summary(),
            redaction=_redaction_summary(self._options),
            support_mode=self._options.support_mode,
            session_count=len(sessions),
            event_count=event_count,
            background_task_count=len(background_tasks),
        )
        diagnostics = SessionBundleDiagnostics(
            storage=self._sanitize_diagnostics_block(self._storage_diagnostics),
            config_summary=self._sanitize_diagnostics_block(self._config_summary),
            provider_summary=self._sanitize_diagnostics_block(self._provider_summary),
        )
        return SessionBundle(
            manifest=manifest,
            sessions=sessions,
            background_tasks=background_tasks,
            diagnostics=diagnostics,
        )

    def _sanitize_diagnostics_block(
        self, payload: dict[str, object] | None
    ) -> dict[str, object] | None:
        if payload is None:
            return None
        if not self._options.redact:
            return dict(payload)
        return _redact_dict(payload)

    def _collect_sessions(
        self, *, session_id: str
    ) -> tuple[tuple[SessionBundleSessionPayload, ...], int]:
        primary = self._load_session_response(session_id=session_id)
        sessions: list[SessionBundleSessionPayload] = [self._build_session_payload(primary)]
        event_total = len(sessions[0].events)
        for child_id in self._child_session_ids(parent_session_id=session_id):
            try:
                child_response = self._load_session_response(session_id=child_id)
            except UnknownSessionError:
                continue
            child_payload = self._build_session_payload(child_response)
            sessions.append(child_payload)
            event_total += len(child_payload.events)
        return tuple(sessions), event_total

    def _load_session_response(self, *, session_id: str) -> RuntimeResponse:
        return self._session_store.load_session(
            workspace=self._workspace,
            session_id=session_id,
        )

    def _build_session_payload(self, response: RuntimeResponse) -> SessionBundleSessionPayload:
        prompt = _sanitize_export_text(
            self._session_prompt(session_id=response.session.session.id),
            options=self._options,
        )
        assert prompt is not None
        raw_events = tuple(self._build_event_payload(event) for event in response.events)
        events = tuple(payload for payload in raw_events if payload is not None)
        metadata = _apply_payload_options(response.session.metadata, options=self._options)
        output = _sanitize_export_text(
            response.output,
            options=self._options,
            truncate_when_tool_output_hidden=True,
        )
        return SessionBundleSessionPayload(
            id=response.session.session.id,
            parent_id=response.session.session.parent_id,
            status=response.session.status,
            turn=response.session.turn,
            prompt=prompt,
            output=output,
            metadata=metadata,
            last_event_sequence=(response.events[-1].sequence if response.events else 0),
            events=events,
        )

    def _session_prompt(self, *, session_id: str) -> str:
        try:
            result = self._session_store.load_session_result(
                workspace=self._workspace,
                session_id=session_id,
            )
        except UnknownSessionError:
            return ""
        return result.prompt

    def _build_event_payload(self, event: EventEnvelope) -> dict[str, object] | None:
        if not self._options.include_raw_provider_messages and _drop_raw_provider_event(event):
            return None
        payload = _apply_payload_options(event.payload, options=self._options)
        return {
            "sequence": event.sequence,
            "event_type": event.event_type,
            "source": event.source,
            "payload": payload,
        }

    def _child_session_ids(self, *, parent_session_id: str) -> tuple[str, ...]:
        try:
            tasks = self._session_store.list_background_tasks_by_parent_session(
                workspace=self._workspace,
                parent_session_id=parent_session_id,
            )
        except ValueError:
            return ()
        seen: set[str] = set()
        ordered: list[str] = []
        for task in tasks:
            child_id = task.session_id
            if child_id is None or child_id in seen:
                continue
            seen.add(child_id)
            ordered.append(child_id)
        return tuple(ordered)

    def _collect_background_tasks(
        self, *, session_id: str
    ) -> tuple[SessionBundleBackgroundTaskPayload, ...]:
        try:
            tasks = self._session_store.list_background_tasks_by_parent_session(
                workspace=self._workspace,
                parent_session_id=session_id,
            )
        except ValueError:
            return ()
        return tuple(self._build_task_payload(task) for task in tasks)

    def _build_task_payload(
        self, task: StoredBackgroundTaskSummary
    ) -> SessionBundleBackgroundTaskPayload:
        try:
            full = self._session_store.load_background_task(
                workspace=self._workspace,
                task_id=task.task.id,
            )
            parent_session_id: str | None = full.parent_session_id
        except (ValueError, UnknownSessionError):
            parent_session_id = None
        return SessionBundleBackgroundTaskPayload(
            task_id=task.task.id,
            status=task.status,
            parent_session_id=parent_session_id,
            child_session_id=task.session_id,
            prompt=_sanitize_export_text(task.prompt, options=self._options) or "",
            error=_sanitize_export_text(task.error, options=self._options),
            created_at=task.created_at,
            updated_at=task.updated_at,
        )


def build_session_bundle(
    *,
    session_store: SessionStore,
    workspace: Path,
    session_id: str,
    options: SessionBundleOptions | None = None,
    storage_diagnostics: dict[str, object] | None = None,
    config_summary: dict[str, object] | None = None,
    provider_summary: dict[str, object] | None = None,
    clock: Callable[[], int] | None = None,
) -> SessionBundle:
    """Build a redacted, schema-versioned session bundle for ``session_id``."""

    builder = _SessionBundleBuilder(
        session_store=session_store,
        workspace=workspace,
        options=options or SessionBundleOptions(),
        storage_diagnostics=storage_diagnostics,
        config_summary=config_summary,
        provider_summary=provider_summary,
        clock=clock,
    )
    return builder.build(session_id)


def _ensure_dict(value: object, *, where: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise SessionBundleError(f"session bundle {where} must be an object")
    return cast(dict[str, object], value)


def _ensure_list(value: object, *, where: str) -> list[object]:
    if not isinstance(value, list):
        raise SessionBundleError(f"session bundle {where} must be an array")
    return cast(list[object], value)


def _ensure_str(value: object, *, where: str) -> str:
    if not isinstance(value, str):
        raise SessionBundleError(f"session bundle {where} must be a string")
    return value


def _ensure_int(value: object, *, where: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise SessionBundleError(f"session bundle {where} must be an integer")
    return value


def parse_session_bundle(payload: object) -> SessionBundle:
    """Parse a JSON payload into a :class:`SessionBundle`, fail-fast on incompatible schemas."""

    root = _ensure_dict(payload, where="payload")
    schema = _ensure_str(root.get("schema"), where="schema")
    if schema != SESSION_BUNDLE_SCHEMA_NAME:
        raise SessionBundleError(
            f"unsupported session bundle schema: {schema!r}; "
            f"this build supports {SESSION_BUNDLE_SCHEMA_NAME!r}"
        )
    manifest_payload = _ensure_dict(root.get("manifest"), where="manifest")
    schema_version = _ensure_int(
        manifest_payload.get("schema_version"), where="manifest.schema_version"
    )
    if schema_version != SESSION_BUNDLE_SCHEMA_VERSION:
        raise SessionBundleError(
            f"session bundle schema version {schema_version} is not supported; "
            f"this build supports version {SESSION_BUNDLE_SCHEMA_VERSION}"
        )
    manifest = SessionBundleManifest(
        schema_version=schema_version,
        voidcode_version=_ensure_str(
            manifest_payload.get("voidcode_version"),
            where="manifest.voidcode_version",
        ),
        created_at=_ensure_int(manifest_payload.get("created_at"), where="manifest.created_at"),
        workspace_hash=_ensure_str(
            manifest_payload.get("workspace_hash"), where="manifest.workspace_hash"
        ),
        platform=dict(
            _ensure_dict(manifest_payload.get("platform", {}), where="manifest.platform")
        ),
        redaction=dict(
            _ensure_dict(manifest_payload.get("redaction", {}), where="manifest.redaction")
        ),
        support_mode=bool(manifest_payload.get("support_mode", False)),
        session_count=_ensure_int(
            manifest_payload.get("session_count", 0), where="manifest.session_count"
        ),
        event_count=_ensure_int(
            manifest_payload.get("event_count", 0), where="manifest.event_count"
        ),
        background_task_count=_ensure_int(
            manifest_payload.get("background_task_count", 0),
            where="manifest.background_task_count",
        ),
    )
    sessions_raw = _ensure_list(root.get("sessions", []), where="sessions")
    sessions: list[SessionBundleSessionPayload] = []
    for index, raw in enumerate(sessions_raw):
        session_dict = _ensure_dict(raw, where=f"sessions[{index}]")
        sessions.append(_parse_session_payload(session_dict, index=index))
    tasks_raw = _ensure_list(root.get("background_tasks", []), where="background_tasks")
    tasks: list[SessionBundleBackgroundTaskPayload] = []
    for index, raw in enumerate(tasks_raw):
        task_dict = _ensure_dict(raw, where=f"background_tasks[{index}]")
        tasks.append(_parse_task_payload(task_dict, index=index))
    diagnostics_payload = _ensure_dict(root.get("diagnostics", {}), where="diagnostics")
    diagnostics = SessionBundleDiagnostics(
        storage=_optional_dict(diagnostics_payload.get("storage")),
        config_summary=_optional_dict(diagnostics_payload.get("config_summary")),
        provider_summary=_optional_dict(diagnostics_payload.get("provider_summary")),
    )
    return SessionBundle(
        manifest=manifest,
        sessions=tuple(sessions),
        background_tasks=tuple(tasks),
        diagnostics=diagnostics,
    )


def _optional_dict(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise SessionBundleError("diagnostics blocks must be objects when present")
    return dict(cast(dict[str, object], value))


def _parse_session_payload(
    payload: dict[str, object], *, index: int
) -> SessionBundleSessionPayload:
    session_id = _validate_bundle_session_id(
        _ensure_str(payload.get("id"), where=f"sessions[{index}].id"),
        where=f"sessions[{index}].id",
    )
    parent_raw = payload.get("parent_id")
    parent_id = (
        _validate_bundle_parent_id(
            _ensure_str(parent_raw, where=f"sessions[{index}].parent_id"),
            where=f"sessions[{index}].parent_id",
        )
        if isinstance(parent_raw, str)
        else None
    )
    status = _ensure_str(payload.get("status"), where=f"sessions[{index}].status")
    turn = _ensure_int(payload.get("turn", 0), where=f"sessions[{index}].turn")
    prompt = _ensure_str(payload.get("prompt", ""), where=f"sessions[{index}].prompt")
    output_raw = payload.get("output")
    output = (
        _ensure_str(output_raw, where=f"sessions[{index}].output")
        if isinstance(output_raw, str)
        else None
    )
    metadata = dict(_ensure_dict(payload.get("metadata", {}), where=f"sessions[{index}].metadata"))
    last_event_sequence = _ensure_int(
        payload.get("last_event_sequence", 0),
        where=f"sessions[{index}].last_event_sequence",
    )
    events_raw = _ensure_list(payload.get("events", []), where=f"sessions[{index}].events")
    events: list[dict[str, object]] = []
    for event_index, raw_event in enumerate(events_raw):
        event_dict = _ensure_dict(raw_event, where=f"sessions[{index}].events[{event_index}]")
        events.append(
            _normalize_event_payload(event_dict, label=f"sessions[{index}].events[{event_index}]")
        )
    return SessionBundleSessionPayload(
        id=session_id,
        parent_id=parent_id,
        status=status,
        turn=turn,
        prompt=prompt,
        output=output,
        metadata=metadata,
        last_event_sequence=last_event_sequence,
        events=tuple(events),
    )


def _normalize_event_payload(event: dict[str, object], *, label: str) -> dict[str, object]:
    sequence = _ensure_int(event.get("sequence", 0), where=f"{label}.sequence")
    event_type = _ensure_str(event.get("event_type", ""), where=f"{label}.event_type")
    source = _ensure_str(event.get("source", "runtime"), where=f"{label}.source")
    payload = dict(_ensure_dict(event.get("payload", {}), where=f"{label}.payload"))
    return {
        "sequence": sequence,
        "event_type": event_type,
        "source": source,
        "payload": payload,
    }


def _validate_bundle_session_id(value: str, *, where: str) -> str:
    try:
        return validate_session_id(value)
    except ValueError as exc:
        raise SessionBundleError(f"session bundle {where} is invalid: {exc}") from exc


def _validate_bundle_parent_id(value: str, *, where: str) -> str:
    try:
        return validate_session_reference_id(value, field_name="parent_id")
    except ValueError as exc:
        raise SessionBundleError(f"session bundle {where} is invalid: {exc}") from exc


def _parse_task_payload(
    payload: dict[str, object], *, index: int
) -> SessionBundleBackgroundTaskPayload:
    task_id = _ensure_str(payload.get("task_id"), where=f"background_tasks[{index}].task_id")
    status = _ensure_str(payload.get("status"), where=f"background_tasks[{index}].status")
    raw_parent = payload.get("parent_session_id")
    parent_session_id = (
        _ensure_str(raw_parent, where=f"background_tasks[{index}].parent_session_id")
        if isinstance(raw_parent, str)
        else None
    )
    raw_child = payload.get("child_session_id")
    child_session_id = (
        _ensure_str(raw_child, where=f"background_tasks[{index}].child_session_id")
        if isinstance(raw_child, str)
        else None
    )
    prompt = _ensure_str(payload.get("prompt", ""), where=f"background_tasks[{index}].prompt")
    raw_error = payload.get("error")
    error = (
        _ensure_str(raw_error, where=f"background_tasks[{index}].error")
        if isinstance(raw_error, str)
        else None
    )
    created_at = _ensure_int(
        payload.get("created_at", 0), where=f"background_tasks[{index}].created_at"
    )
    updated_at = _ensure_int(
        payload.get("updated_at", 0), where=f"background_tasks[{index}].updated_at"
    )
    return SessionBundleBackgroundTaskPayload(
        task_id=task_id,
        status=status,
        parent_session_id=parent_session_id,
        child_session_id=child_session_id,
        prompt=prompt,
        error=error,
        created_at=created_at,
        updated_at=updated_at,
    )


def serialize_session_bundle(
    bundle: SessionBundle,
    *,
    fmt: SessionBundleFormat = "zip",
) -> bytes:
    """Return canonical bytes for ``bundle`` in either zip or json format."""

    json_bytes = (json.dumps(bundle.to_payload(), sort_keys=True, indent=2) + "\n").encode("utf-8")
    if fmt == "json":
        return json_bytes
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(SESSION_BUNDLE_FILE_NAME, json_bytes)
    return buffer.getvalue()


def write_session_bundle(
    bundle: SessionBundle,
    *,
    path: Path,
    fmt: SessionBundleFormat | None = None,
) -> Path:
    """Write ``bundle`` to ``path``, defaulting to zip when extension is ``.zip``."""

    resolved_format = fmt or _infer_bundle_format_from_path(path)
    payload = serialize_session_bundle(bundle, fmt=resolved_format)
    parent = path.parent
    if str(parent) and not parent.exists():
        parent.mkdir(parents=True, exist_ok=True)
    _ = path.write_bytes(payload)
    return path


def _infer_bundle_format_from_path(path: Path) -> SessionBundleFormat:
    suffix = path.suffix.lower()
    if suffix == ".zip":
        return "zip"
    if suffix == ".json":
        return "json"
    return "zip"


def read_session_bundle(path: Path) -> SessionBundle:
    """Load a bundle from disk, accepting both json and zip artifacts."""

    if not path.exists():
        raise SessionBundleError(f"session bundle does not exist: {path}")
    if not path.is_file():
        raise SessionBundleError(f"session bundle is not a regular file: {path}")
    raw = path.read_bytes()
    return read_session_bundle_bytes(raw)


def read_session_bundle_bytes(raw: bytes) -> SessionBundle:
    """Decode a bundle from raw bytes (zip or json)."""

    if raw.startswith(b"PK"):
        return _decode_zip_bundle(raw)
    return _decode_json_bundle(raw)


def _decode_zip_bundle(raw: bytes) -> SessionBundle:
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            try:
                payload_bytes = archive.read(SESSION_BUNDLE_FILE_NAME)
            except KeyError as exc:
                raise SessionBundleError(
                    f"session bundle archive missing entry {SESSION_BUNDLE_FILE_NAME!r}"
                ) from exc
    except zipfile.BadZipFile as exc:
        raise SessionBundleError("session bundle archive is not a valid zip file") from exc
    return _decode_json_bundle(payload_bytes)


def _decode_json_bundle(raw: bytes) -> SessionBundle:
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SessionBundleError("session bundle JSON payload is not valid UTF-8 JSON") from exc
    return parse_session_bundle(decoded)


def _validate_event_source(value: str) -> EventSource:
    allowed: set[str] = {"runtime", "graph", "tool"}
    if value not in allowed:
        raise SessionBundleError(
            f"unknown event source {value!r}; expected one of {sorted(allowed)}"
        )
    return cast(EventSource, value)


def _validate_session_status(value: str) -> SessionStatus:
    allowed: set[str] = {"idle", "running", "waiting", "completed", "failed"}
    if value not in allowed:
        raise SessionBundleError(
            f"unknown session status {value!r}; expected one of {sorted(allowed)}"
        )
    return cast(SessionStatus, value)


def _events_from_session_payload(
    session: SessionBundleSessionPayload,
) -> tuple[EventEnvelope, ...]:
    events: list[EventEnvelope] = []
    for raw_event in session.events:
        sequence = cast(int, raw_event.get("sequence", 0))
        events.append(
            EventEnvelope(
                session_id=session.id,
                sequence=sequence,
                event_type=cast(str, raw_event.get("event_type", "")),
                source=_validate_event_source(cast(str, raw_event.get("source", "runtime"))),
                payload=cast(dict[str, object], raw_event.get("payload", {})),
            )
        )
    return tuple(events)


def _runtime_response_for_session(
    session: SessionBundleSessionPayload,
    *,
    rebound_id: str,
    workspace: Path,
) -> RuntimeResponse:
    state = SessionState(
        session=SessionRef(id=rebound_id, parent_id=session.parent_id),
        status=_validate_session_status(session.status),
        turn=session.turn,
        metadata=_session_metadata_with_import_marker(
            session,
            rebound_id=rebound_id,
            workspace=workspace,
        ),
    )
    events = tuple(
        replace(event, session_id=rebound_id) for event in _events_from_session_payload(session)
    )
    return RuntimeResponse(session=state, events=events, output=session.output)


def _session_metadata_with_import_marker(
    session: SessionBundleSessionPayload,
    *,
    rebound_id: str,
    workspace: Path,
) -> dict[str, object]:
    metadata = dict(session.metadata)
    original_workspace = metadata.get("workspace")
    marker: dict[str, object] = {
        "version": 1,
        "original_session_id": session.id,
        "imported_at_session_id": rebound_id,
    }
    if isinstance(original_workspace, str):
        marker["original_workspace"] = original_workspace
    raw_existing = metadata.get("imported_bundle")
    if isinstance(raw_existing, dict):
        marker = {**cast(dict[str, object], raw_existing), **marker}
    metadata["workspace"] = str(workspace)
    metadata["imported_bundle"] = marker
    return metadata


def apply_session_bundle(
    bundle: SessionBundle,
    *,
    session_store: SessionStore,
    workspace: Path,
    dry_run: bool = False,
    session_id_resolver: Callable[[str], str] | None = None,
) -> SessionBundleImportResult:
    """Persist ``bundle`` into ``session_store``; never overwrites existing ids by default."""

    resolver = session_id_resolver or _default_id_collision_resolver(session_store, workspace)
    rebound_id_for = _resolve_import_session_ids(
        bundle.sessions,
        session_store=session_store,
        workspace=workspace,
        resolver=resolver,
    )
    imported_ids = tuple(rebound_id_for[session.id] for session in bundle.sessions)
    skipped_ids: tuple[str, ...] = ()
    for session in bundle.sessions:
        target_id = rebound_id_for[session.id]
        if dry_run:
            continue
        rebound_session = SessionBundleSessionPayload(
            id=session.id,
            parent_id=_remap_parent_id(session.parent_id, rebound_id_for),
            status=session.status,
            turn=session.turn,
            prompt=session.prompt,
            output=session.output,
            metadata=session.metadata,
            last_event_sequence=session.last_event_sequence,
            events=session.events,
        )
        request = RuntimeRequest(
            prompt=session.prompt,
            session_id=target_id,
            parent_session_id=rebound_session.parent_id,
        )
        response = _runtime_response_for_session(
            rebound_session,
            rebound_id=target_id,
            workspace=workspace,
        )
        session_store.save_run(workspace=workspace, request=request, response=response)
    skipped_tasks = sum(
        1
        for task in bundle.background_tasks
        if task.child_session_id is not None and task.child_session_id not in rebound_id_for
    )
    return SessionBundleImportResult(
        schema=SESSION_BUNDLE_SCHEMA_NAME,
        schema_version=bundle.manifest.schema_version,
        voidcode_version=bundle.manifest.voidcode_version,
        created_at=bundle.manifest.created_at,
        support_mode=bundle.manifest.support_mode,
        redaction=dict(bundle.manifest.redaction),
        workspace_hash=bundle.manifest.workspace_hash,
        session_count=bundle.manifest.session_count,
        event_count=bundle.manifest.event_count,
        background_task_count=bundle.manifest.background_task_count,
        imported_session_ids=imported_ids,
        skipped_session_ids=skipped_ids,
        skipped_background_task_count=skipped_tasks,
        dry_run=dry_run,
    )


def _remap_parent_id(
    parent_id: str | None,
    rebound: Mapping[str, str],
) -> str | None:
    if parent_id is None:
        return None
    return rebound.get(parent_id, parent_id)


def _default_id_collision_resolver(
    session_store: SessionStore, workspace: Path
) -> Callable[[str], str]:
    def resolve(original: str) -> str:
        candidate = f"{original}-imported"
        attempt = 1
        while session_store.has_session(workspace=workspace, session_id=candidate):
            attempt += 1
            candidate = f"{original}-imported-{attempt}"
        return candidate

    return resolve


def _resolve_import_session_ids(
    sessions: tuple[SessionBundleSessionPayload, ...],
    *,
    session_store: SessionStore,
    workspace: Path,
    resolver: Callable[[str], str],
) -> dict[str, str]:
    rebound: dict[str, str] = {}
    reserved: set[str] = set()
    for session in sessions:
        if session.id in rebound:
            raise SessionBundleError(f"duplicate session id in bundle: {session.id!r}")
        target_id = _resolve_target_id(
            session_store=session_store,
            workspace=workspace,
            bundle_session_id=session.id,
            resolver=resolver,
            reserved=reserved,
        )
        rebound[session.id] = target_id
        reserved.add(target_id)
    return rebound


def _resolve_target_id(
    *,
    session_store: SessionStore,
    workspace: Path,
    bundle_session_id: str,
    resolver: Callable[[str], str],
    reserved: set[str],
) -> str:
    validate_session_id(bundle_session_id)
    if bundle_session_id not in reserved and not session_store.has_session(
        workspace=workspace, session_id=bundle_session_id
    ):
        return bundle_session_id
    candidate = resolver(bundle_session_id)
    attempt = 1
    while candidate in reserved or session_store.has_session(
        workspace=workspace,
        session_id=candidate,
    ):
        attempt += 1
        candidate = f"{bundle_session_id}-imported-{attempt}"
    validate_session_id(candidate)
    return candidate


__all__ = [
    "SESSION_BUNDLE_DEFAULT_EXTENSION",
    "SESSION_BUNDLE_FILE_NAME",
    "SESSION_BUNDLE_REDACTED_PLACEHOLDER",
    "SESSION_BUNDLE_SCHEMA_NAME",
    "SESSION_BUNDLE_SCHEMA_VERSION",
    "SessionBundle",
    "SessionBundleBackgroundTaskPayload",
    "SessionBundleDiagnostics",
    "SessionBundleError",
    "SessionBundleFormat",
    "SessionBundleImportResult",
    "SessionBundleManifest",
    "SessionBundleOptions",
    "SessionBundleSessionPayload",
    "apply_session_bundle",
    "build_session_bundle",
    "parse_session_bundle",
    "read_session_bundle",
    "read_session_bundle_bytes",
    "serialize_session_bundle",
    "write_session_bundle",
]
