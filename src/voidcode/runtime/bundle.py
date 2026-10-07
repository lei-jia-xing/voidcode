"""Exact CURRENT bundle2 row snapshots; import never dispatches copied tasks.

Signed composition bodies stay on their canonical owner rows. JSON and ZIP use
one closed schema, and every row is checked before the single storage transaction.
"""

from __future__ import annotations

import hashlib
import io
import json
import platform
import time
import zipfile
from collections.abc import Callable
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Final, Literal, cast

from .. import __version__ as VOIDCODE_VERSION
from .agent_capability import validate_agent_capability_snapshot
from .composition import CompositionRef, FrozenComposition, SessionCompositionOwner, TaskCompositionOwner
from .contracts import validate_id
from .events import EventEnvelope, EventSource
from .execution.resume_checkpoint import tool_results_from_checkpoint, validated_resume_checkpoint_envelope
from .fact_codec import decode_fact
from .permission import PendingApproval
from .storage import BackgroundTaskRepository, SessionEventRepository, SessionRecoveryRepository, SessionRepository, SessionRunWriter
from .storage.shared import _pending_operation_class, _pending_path_scope, _pending_permission_decision

SESSION_BUNDLE_SCHEMA_NAME: Final[str] = "voidcode.session.bundle.v2"
SESSION_BUNDLE_SCHEMA_VERSION: Final[int] = 2
SESSION_BUNDLE_FILE_NAME: Final[str] = "bundle.json"
SESSION_BUNDLE_DEFAULT_EXTENSION: Final[str] = ".vcsession.zip"
type SessionBundleFormat = Literal["zip", "json"]


class SessionBundleError(ValueError):
    """Malformed, incomplete, or unsupported CURRENT bundle."""


@dataclass(frozen=True, slots=True)
class SessionBundleOptions:
    """Exact snapshots cannot be redacted or have transcript rows removed."""

    redact: bool = True
    include_tool_output: bool = False
    include_raw_provider_messages: bool = False
    include_reasoning_text: bool = False
    support_mode: bool = False
    tool_output_preview_chars: int = 16_000

    @classmethod
    def support_artifact(cls) -> SessionBundleOptions:
        return cls(redact=True, support_mode=True)


@dataclass(frozen=True, slots=True)
class SessionBundleSessionPayload:
    row: dict[str, object]
    events: tuple[dict[str, object], ...]

    @property
    def id(self) -> str:
        return cast(str, self.row["session_id"])

    @property
    def metadata(self) -> dict[str, object]:
        return cast(dict[str, object], self.row["metadata_json"])


@dataclass(frozen=True, slots=True)
class SessionBundleBackgroundTaskPayload:
    row: dict[str, object]

    @property
    def task_id(self) -> str:
        return cast(str, self.row["task_id"])


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
    artifact_count: int = 0


@dataclass(frozen=True, slots=True)
class SessionBundle:
    manifest: SessionBundleManifest
    sessions: tuple[SessionBundleSessionPayload, ...]
    background_tasks: tuple[SessionBundleBackgroundTaskPayload, ...]
    diagnostics: SessionBundleDiagnostics
    deliveries: tuple[dict[str, object], ...]

    def to_payload(self) -> dict[str, object]:
        return {
            "schema": SESSION_BUNDLE_SCHEMA_NAME,
            "manifest": asdict(self.manifest),
            "sessions": [session.row for session in self.sessions],
            "events": [event for session in self.sessions for event in session.events],
            "background_tasks": [task.row for task in self.background_tasks],
            "deliveries": list(self.deliveries),
            "diagnostics": asdict(self.diagnostics),
            "artifacts": [],
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
    skipped_background_task_count: int
    dry_run: bool

    def to_payload(self) -> dict[str, object]:
        payload = asdict(self)
        payload["imported_session_ids"] = list(self.imported_session_ids)
        return payload


# These are the existing physical2 columns, with JSON columns decoded, not a
# generic repository/schema adapter. Unknown columns require a format cutover.
_SESSION_STRINGS = frozenset({"session_id", "workspace_id", "status", "prompt"})
_SESSION_NULL_STRINGS = frozenset({"parent_session_id", "output", "title", "forked_from_session_id"})
_SESSION_INTS = frozenset({"turn", "created_at", "updated_at", "last_event_sequence"})
_SESSION_NULL_INTS = frozenset({"leaf_sequence", "created_at_unix_ms", "forked_at_sequence"})
_SESSION_JSON = frozenset({"metadata_json"})
_SESSION_NULL_JSON = frozenset({"pending_approval_json", "pending_question_json", "resume_checkpoint_json"})
_TASK_STRINGS = frozenset({"task_id", "workspace_id", "status", "prompt", "schema_mode"})
_TASK_NULL_STRINGS = frozenset(
    {
        "request_session_id",
        "request_parent_session_id",
        "requested_child_session_id",
        "routing_mode",
        "routing_subagent_type",
        "routing_description",
        "routing_command",
        "approval_request_id",
        "question_request_id",
        "cancellation_cause",
        "session_id",
        "error",
        "steer_prompt",
    }
)
_TASK_INTS = frozenset({"result_available", "allocate_session_id", "created_at", "updated_at", "keep_alive"})
_TASK_NULL_INTS = frozenset({"cancel_requested_at", "started_at", "finished_at", "created_at_unix_ms", "started_at_unix_ms", "finished_at_unix_ms"})
_TASK_JSON = frozenset({"request_metadata_json"})
_TASK_NULL_JSON = frozenset({"delegated_reminder_json", "output_schema_json", "structured_output_json", "schema_validation_json"})
_EVENT_STRINGS = frozenset({"workspace_id", "session_id", "event_type", "source"})
_EVENT_INTS = frozenset({"sequence"})
_DELIVERY_STRINGS = frozenset({"workspace_id", "session_id", "dedupe_key"})
_DELIVERY_INTS = frozenset({"delivered_at", "event_sequence"})
_SESSION_STATUSES = frozenset({"idle", "running", "waiting", "completed", "failed", "interrupted"})
_TASK_STATUSES = frozenset({"queued", "running", "idle", "completed", "failed", "cancelled", "interrupted"})
_CHECKPOINT_KINDS = frozenset({"approval_wait", "question_wait", "provider_failure_retryable", "terminal", "interrupted"})


def _object(value: object, where: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise SessionBundleError(f"{where} must be an object")
    return cast(dict[str, object], value)


def _exact(value: dict[str, object], expected: frozenset[str], where: str) -> None:
    if frozenset(value) != expected:
        raise SessionBundleError(f"{where} has unsupported fields")


def _rows(value: object, where: str) -> tuple[dict[str, object], ...]:
    if not isinstance(value, (list, tuple)):
        raise SessionBundleError(f"{where} must be an array")
    return tuple(_object(item, where) for item in value)


def _row_types(
    row: dict[str, object],
    *,
    strings: frozenset[str],
    ints: frozenset[str],
    objects: frozenset[str] = frozenset(),
    nullable_strings: frozenset[str] = frozenset(),
    nullable_ints: frozenset[str] = frozenset(),
    nullable_objects: frozenset[str] = frozenset(),
    where: str,
) -> None:
    _exact(row, strings | ints | objects | nullable_strings | nullable_ints | nullable_objects, where)
    for names, kind, nullable in (
        (strings, str, False),
        (ints, int, False),
        (objects, dict, False),
        (nullable_strings, str, True),
        (nullable_ints, int, True),
        (nullable_objects, dict, True),
    ):
        for name in names:
            value = row[name]
            if nullable and value is None:
                continue
            if type(value) is not kind:
                raise SessionBundleError(f"{where}.{name} has invalid type")


def _ref(metadata: dict[str, object], *, task: bool = False) -> CompositionRef:
    _ = task
    ref = CompositionRef.model_validate(metadata.get("composition_ref"))
    if "agent_capability_snapshot" in metadata:
        snapshot = _object(metadata["agent_capability_snapshot"], "agent_capability_snapshot")
        validate_agent_capability_snapshot(snapshot)
        if CompositionRef.model_validate(snapshot["composition_ref"]) != ref:
            raise SessionBundleError("capability snapshot differs from canonical metadata ref")
    return ref


def _owner_key(ref: CompositionRef) -> tuple[str, str]:
    if isinstance(ref.owner, SessionCompositionOwner):
        return ("session", ref.owner.session_id)
    return ("task", ref.owner.task_id)


def _workspace_hash(workspace: Path) -> str:
    return "sha256:" + hashlib.sha256(str(workspace).encode()).hexdigest()


def _validate_pending(row: dict[str, object], event_sequences: set[int]) -> None:
    metadata = _object(row["metadata_json"], "session metadata")
    checkpoint = row["resume_checkpoint_json"]
    if checkpoint is not None:
        cp = _object(checkpoint, "checkpoint")
        kind = cp.get("kind")
        if kind not in _CHECKPOINT_KINDS:
            raise SessionBundleError("unsupported checkpoint kind")
        validated_resume_checkpoint_envelope(checkpoint=cp, expected_kind=cast(str, kind))
        if not isinstance(cp.get("prompt"), str) or not isinstance(cp.get("session_metadata"), dict):
            raise SessionBundleError("invalid checkpoint metadata/prompt")
        cp_metadata = _object(cp["session_metadata"], "checkpoint metadata")
        if "execution_composition" in cp_metadata or _ref(cp_metadata) != _ref(metadata):
            raise SessionBundleError("checkpoint composition ref differs from session")
        results = cp.get("tool_results")
        if not isinstance(results, list):
            raise SessionBundleError("checkpoint tool_results must be an array")
        tool_results_from_checkpoint(results, version=2)
        watermark = cp.get("last_event_sequence")
        if type(watermark) is not int or watermark < 0 or watermark > cast(int, row["last_event_sequence"]):
            raise SessionBundleError("checkpoint watermark outside stored events")
    approval = row["pending_approval_json"]
    question = row["pending_question_json"]
    if approval is not None and question is not None:
        raise SessionBundleError("session has both pending approval and question")
    if approval is not None:
        pending = _object(approval, "pending approval")
        expected = frozenset(field.name for field in fields(PendingApproval))
        _exact(pending, expected | ({"resolution_claimed"} if "resolution_claimed" in pending else set()), "pending approval")
        for key in ("request_id", "tool_name", "target_summary", "reason", "policy_mode"):
            if not isinstance(pending[key], str):
                raise SessionBundleError("invalid pending approval string")
        _object(pending["arguments"], "pending approval arguments")
        _pending_permission_decision(pending["policy_mode"])
        for name in ("owner_session_id", "owner_parent_session_id", "delegated_task_id", "canonical_path", "matched_rule", "policy_surface"):
            if pending[name] is not None and not isinstance(pending[name], str):
                raise SessionBundleError("invalid pending approval owner/policy string")
        if pending["path_scope"] is not None and _pending_path_scope(pending["path_scope"]) is None:
            raise SessionBundleError("invalid pending approval path scope")
        if pending["operation_class"] is not None and _pending_operation_class(pending["operation_class"]) is None:
            raise SessionBundleError("invalid pending approval operation class")
        if "resolution_claimed" in pending and type(pending["resolution_claimed"]) is not bool:
            raise SessionBundleError("invalid approval claim")
        sequence = pending["request_event_sequence"]
        if sequence is not None and (type(sequence) is not int or sequence not in event_sequences):
            raise SessionBundleError("pending approval request edge missing")
        if checkpoint is not None and _object(checkpoint, "checkpoint").get("pending_approval_request_id") != pending["request_id"]:
            raise SessionBundleError("pending approval checkpoint request mismatch")
    if question is not None:
        pending = _object(question, "pending question")
        _exact(pending, frozenset({"request_id", "tool_name", "arguments", "prompts"}), "pending question")
        if not isinstance(pending["request_id"], str) or not isinstance(pending["tool_name"], str):
            raise SessionBundleError("invalid pending question identity")
        _object(pending["arguments"], "pending question arguments")
        prompts = _rows(pending["prompts"], "pending prompts")
        for prompt in prompts:
            _exact(prompt, frozenset({"question", "header", "multiple", "options"}), "question prompt")
            if not isinstance(prompt["question"], str) or not isinstance(prompt["header"], str) or type(prompt["multiple"]) is not bool:
                raise SessionBundleError("invalid pending prompt")
            for option in _rows(prompt["options"], "question options"):
                _exact(option, frozenset({"label", "description"}), "question option")
                if not all(isinstance(value, str) for value in option.values()):
                    raise SessionBundleError("invalid question option")
        if checkpoint is not None and _object(checkpoint, "checkpoint").get("pending_question_request_id") != pending["request_id"]:
            raise SessionBundleError("pending question checkpoint request mismatch")


def _validate_rows(
    sessions: tuple[dict[str, object], ...],
    events: tuple[dict[str, object], ...],
    tasks: tuple[dict[str, object], ...],
    deliveries: tuple[dict[str, object], ...],
) -> str:
    owners: dict[tuple[str, str], tuple[dict[str, object], CompositionRef]] = {}
    source_workspaces: set[str] = set()
    for kind, rows in (("session", sessions), ("task", tasks)):
        for row in rows:
            if kind == "session":
                _row_types(
                    row,
                    strings=_SESSION_STRINGS,
                    ints=_SESSION_INTS,
                    objects=_SESSION_JSON,
                    nullable_strings=_SESSION_NULL_STRINGS,
                    nullable_ints=_SESSION_NULL_INTS,
                    nullable_objects=_SESSION_NULL_JSON,
                    where="session row",
                )
                metadata = _object(row["metadata_json"], "session metadata")
                statuses = _SESSION_STATUSES
            else:
                _row_types(
                    row,
                    strings=_TASK_STRINGS,
                    ints=_TASK_INTS,
                    objects=_TASK_JSON,
                    nullable_strings=_TASK_NULL_STRINGS,
                    nullable_ints=_TASK_NULL_INTS,
                    nullable_objects=_TASK_NULL_JSON,
                    where="task row",
                )
                metadata = _object(row["request_metadata_json"], "task metadata")
                statuses = _TASK_STATUSES
                for name in ("allocate_session_id", "result_available", "keep_alive"):
                    if row[name] not in (0, 1):
                        raise SessionBundleError("invalid task boolean column")
                if row["schema_mode"] not in ("permissive", "strict"):
                    raise SessionBundleError("unsupported task schema mode")
            if row["status"] not in statuses:
                raise SessionBundleError(f"unknown {kind} status")
            identity = validate_id(cast(str, row[f"{kind}_id"]))
            key = (kind, identity)
            if key in owners:
                raise SessionBundleError(f"duplicate {kind} id")
            ref = _ref(metadata, task=kind == "task")
            if ref.workspace != row["workspace_id"]:
                raise SessionBundleError("composition ref workspace differs from row")
            source_workspaces.add(ref.workspace)
            owners[key] = metadata, ref
    if not sessions or len(source_workspaces) != 1:
        raise SessionBundleError("bundle requires one source workspace and actual sessions")
    for key, (metadata, ref) in owners.items():
        owner = owners.get(_owner_key(ref))
        if owner is None:
            raise SessionBundleError("composition owner closure missing")
        owner_metadata, owner_ref = owner
        if owner_ref != ref:
            raise SessionBundleError("composition owner ref mismatch")
        frozen = FrozenComposition.from_payload(owner_metadata.get("execution_composition"))
        if frozen.binding.binding_id != ref.binding_id or frozen.plan.plan_id != ref.plan_id:
            raise SessionBundleError("composition body/ref hash mismatch")
        if key != _owner_key(ref) and "execution_composition" in metadata:
            raise SessionBundleError("non-owner contains composition body")
    session_ids = {cast(str, row["session_id"]) for row in sessions}
    workspace = next(iter(source_workspaces))
    by_session: dict[str, dict[int, dict[str, object]]] = {identity: {} for identity in session_ids}
    for event in events:
        _row_types(
            event,
            strings=_EVENT_STRINGS,
            ints=_EVENT_INTS,
            nullable_ints=frozenset({"parent_sequence"}),
            objects=frozenset({"payload_json"}),
            where="event row",
        )
        identity = cast(str, event["session_id"])
        sequence = cast(int, event["sequence"])
        if event["workspace_id"] != workspace or identity not in by_session or sequence <= 0 or event["source"] not in ("runtime", "graph", "tool"):
            raise SessionBundleError("invalid event scope/identity/source")
        if sequence in by_session[identity]:
            raise SessionBundleError("duplicate event sequence")
        decode_fact(
            EventEnvelope(
                session_id=identity,
                sequence=sequence,
                event_type=cast(str, event["event_type"]),
                source=cast(EventSource, event["source"]),
                payload=cast(dict[str, object], event["payload_json"]),
            )
        )
        by_session[identity][sequence] = event
    for row in sessions:
        identity = cast(str, row["session_id"])
        event_map = by_session[identity]
        for sequence, event in event_map.items():
            parent = event["parent_sequence"]
            if parent is not None and (parent not in event_map or cast(int, parent) >= sequence):
                raise SessionBundleError("event parent edge missing or cyclic")
        if row["last_event_sequence"] != max(event_map, default=0) or cast(int, row["turn"]) < 0:
            raise SessionBundleError("session watermark differs from complete event log")
        if row["leaf_sequence"] is not None and row["leaf_sequence"] not in event_map:
            raise SessionBundleError("session leaf edge missing")
        for name in ("parent_session_id", "forked_from_session_id"):
            if row[name] is not None and row[name] not in session_ids:
                raise SessionBundleError("session parent/fork closure missing")
        fork_source = row["forked_from_session_id"]
        fork_sequence = row["forked_at_sequence"]
        if (fork_source is None) != (fork_sequence is None):
            raise SessionBundleError("incomplete fork provenance")
        if fork_source is not None and fork_sequence != 0 and fork_sequence not in by_session[cast(str, fork_source)]:
            raise SessionBundleError("fork sequence missing in source")
        _validate_pending(row, set(event_map))
    for row in tasks:
        for name in ("request_session_id", "request_parent_session_id", "requested_child_session_id", "session_id"):
            # Requested IDs can refer to an as-yet uncreated child; actual
            # parent and result references must resolve to physical rows.
            if name in ("request_parent_session_id", "session_id") and row[name] is not None and row[name] not in session_ids:
                raise SessionBundleError("task parent/result closure missing")
    seen_deliveries: set[tuple[str, str]] = set()
    for delivery in deliveries:
        _row_types(delivery, strings=_DELIVERY_STRINGS, ints=_DELIVERY_INTS, where="delivery row")
        identity = cast(str, delivery["session_id"])
        key = identity, cast(str, delivery["dedupe_key"])
        if (
            delivery["workspace_id"] != workspace
            or identity not in by_session
            or delivery["event_sequence"] not in by_session[identity]
            or key in seen_deliveries
        ):
            raise SessionBundleError("invalid delivery dedupe edge")
        seen_deliveries.add(key)
    return workspace


def parse_session_bundle(payload: object) -> SessionBundle:
    try:
        root = _object(payload, "bundle")
        json.dumps(root, allow_nan=False)
        if root.get("schema") != SESSION_BUNDLE_SCHEMA_NAME:
            raise SessionBundleError("unsupported session bundle schema")
        _exact(root, frozenset({"schema", "manifest", "sessions", "events", "background_tasks", "deliveries", "diagnostics", "artifacts"}), "bundle")
        manifest = _object(root["manifest"], "manifest")
        _exact(manifest, frozenset(field.name for field in fields(SessionBundleManifest)), "manifest")
        if type(manifest["schema_version"]) is not int or manifest["schema_version"] != 2:
            raise SessionBundleError("unsupported session bundle schema version")
        for name in ("created_at", "session_count", "event_count", "background_task_count", "artifact_count"):
            if type(manifest[name]) is not int or cast(int, manifest[name]) < 0:
                raise SessionBundleError("invalid manifest count/timestamp")
        for name in ("voidcode_version", "workspace_hash"):
            if not isinstance(manifest[name], str):
                raise SessionBundleError("invalid manifest string")
        _object(manifest["platform"], "manifest platform")
        if (
            manifest["redaction"] != {"exact": True}
            or manifest["support_mode"] is not False
            or root["artifacts"] != []
            or manifest["artifact_count"] != 0
        ):
            raise SessionBundleError("unsupported non-exact/artifact bundle scope")
        sessions = _rows(root["sessions"], "sessions")
        events = _rows(root["events"], "events")
        tasks = _rows(root["background_tasks"], "background_tasks")
        deliveries = _rows(root["deliveries"], "deliveries")
        source = _validate_rows(sessions, events, tasks, deliveries)
        if manifest["workspace_hash"] != _workspace_hash(Path(source)):
            raise SessionBundleError("source workspace hash mismatch")
        if (manifest["session_count"], manifest["event_count"], manifest["background_task_count"]) != (len(sessions), len(events), len(tasks)):
            raise SessionBundleError("manifest counts differ from complete rows")
        diagnostics = _object(root["diagnostics"], "diagnostics")
        _exact(diagnostics, frozenset({"storage", "config_summary", "provider_summary"}), "diagnostics")
        for value in diagnostics.values():
            if value is not None:
                _object(value, "diagnostic section")
        return SessionBundle(
            manifest=SessionBundleManifest(**cast(dict[str, Any], manifest)),
            sessions=tuple(
                SessionBundleSessionPayload(row, tuple(event for event in events if event["session_id"] == row["session_id"])) for row in sessions
            ),
            background_tasks=tuple(SessionBundleBackgroundTaskPayload(row) for row in tasks),
            diagnostics=SessionBundleDiagnostics(**cast(dict[str, Any], diagnostics)),
            deliveries=deliveries,
        )
    except (ValueError, TypeError, KeyError) as error:
        if isinstance(error, SessionBundleError):
            raise
        raise SessionBundleError(str(error)) from error


def build_session_bundle(
    *,
    sessions: SessionRepository,
    tasks: BackgroundTaskRepository,
    workspace: Path,
    session_id: str,
    options: SessionBundleOptions | None = None,
    storage_diagnostics: dict[str, object] | None = None,
    config_summary: dict[str, object] | None = None,
    provider_summary: dict[str, object] | None = None,
    clock: Callable[[], int] | None = None,
) -> SessionBundle:
    options = options or SessionBundleOptions()
    if (
        options.redact
        or options.support_mode
        or not all((options.include_tool_output, options.include_raw_provider_messages, options.include_reasoning_text))
    ):
        raise SessionBundleError("CURRENT bundle2 requires an exact unredacted snapshot; support summaries are not importable")
    validate_id(session_id)
    session_ids = {session_id}
    task_ids: set[str] = set()
    while True:
        rows = sessions.export_session_bundle_rows(workspace=workspace, session_ids=tuple(sorted(session_ids)), task_ids=tuple(sorted(task_ids)))
        session_rows = _rows(rows["sessions"], "export sessions")
        task_rows = _rows(rows["tasks"], "export tasks")
        actual_sessions = {cast(str, row["session_id"]) for row in session_rows}
        actual_tasks = {cast(str, row["task_id"]) for row in task_rows}
        if not session_ids <= actual_sessions or not task_ids <= actual_tasks:
            raise SessionBundleError("source owner/parent/fork row is missing")
        before = (len(session_ids), len(task_ids))
        for row in session_rows:
            for name in ("parent_session_id", "forked_from_session_id"):
                if isinstance(row[name], str):
                    session_ids.add(cast(str, row[name]))
            for summary in tasks.list_background_tasks_by_parent_session(workspace=workspace, parent_session_id=cast(str, row["session_id"])):
                task_ids.add(summary.task.id)
        for kind, collected in (("session", session_rows), ("task", task_rows)):
            for row in collected:
                metadata = _object(row["metadata_json" if kind == "session" else "request_metadata_json"], "owner metadata")
                ref = _ref(metadata, task=kind == "task")
                if ref.workspace != str(workspace):
                    raise SessionBundleError("cross-workspace owner closure is unsupported")
                owner_kind, identity = _owner_key(ref)
                (session_ids if owner_kind == "session" else task_ids).add(identity)
                if kind == "task":
                    for name in ("request_parent_session_id", "session_id"):
                        if isinstance(row[name], str):
                            session_ids.add(cast(str, row[name]))
        if before == (len(session_ids), len(task_ids)):
            break
    events = _rows(rows["events"], "export events")
    deliveries = _rows(rows["deliveries"], "export deliveries")
    manifest = SessionBundleManifest(
        2,
        VOIDCODE_VERSION,
        (clock or (lambda: int(time.time() * 1000)))(),
        _workspace_hash(workspace),
        {"system": platform.system(), "machine": platform.machine()},
        {"exact": True},
        False,
        len(session_rows),
        len(events),
        len(task_rows),
    )
    return parse_session_bundle(
        {
            "schema": SESSION_BUNDLE_SCHEMA_NAME,
            "manifest": asdict(manifest),
            "sessions": list(session_rows),
            "events": list(events),
            "background_tasks": list(task_rows),
            "deliveries": list(deliveries),
            "diagnostics": asdict(SessionBundleDiagnostics(storage_diagnostics, config_summary, provider_summary)),
            "artifacts": [],
        }
    )


def _relocate_metadata(metadata: dict[str, object], *, workspace: str, session_ids: dict[str, str], task_ids: dict[str, str]) -> None:
    # Rewrite only recognized outer refs/owner IDs; never walk arbitrary signed
    # configuration, report arguments, or source-identity strings.
    if "agent_capability_snapshot" in metadata:
        snapshot = _object(metadata["agent_capability_snapshot"], "capability snapshot")
        snapshot["composition_ref"] = _relocate_ref(_ref(metadata), workspace, session_ids, task_ids)
    if "composition_ref" in metadata:
        metadata["composition_ref"] = _relocate_ref(CompositionRef.model_validate(metadata["composition_ref"]), workspace, session_ids, task_ids)
    if "workspace" in metadata:
        metadata["workspace"] = workspace


def _relocate_ref(ref: CompositionRef, workspace: str, session_ids: dict[str, str], task_ids: dict[str, str]) -> dict[str, object]:
    owner = (
        SessionCompositionOwner(kind="session", session_id=session_ids[ref.owner.session_id])
        if isinstance(ref.owner, SessionCompositionOwner)
        else TaskCompositionOwner(kind="task", task_id=task_ids[ref.owner.task_id])
    )
    return CompositionRef(workspace=workspace, owner=owner, binding_id=ref.binding_id, plan_id=ref.plan_id).model_dump(mode="json")


def apply_session_bundle(
    bundle: SessionBundle,
    *,
    session_repository: SessionRepository,
    events: SessionEventRepository,
    recovery: SessionRecoveryRepository,
    run_writer: SessionRunWriter,
    workspace: Path,
    admit_composition: Callable[[FrozenComposition], None] | None = None,
    dry_run: bool = False,
    session_id_resolver: Callable[[str], str] | None = None,
    task_id_resolver: Callable[[str], str] | None = None,
) -> SessionBundleImportResult:
    _ = (events, recovery, run_writer)
    # Reparse even in-process objects: nested row dictionaries remain mutable.
    checked = parse_session_bundle(bundle.to_payload())
    if admit_composition is None:
        raise SessionBundleError("bundle import requires destination composition admission before writes")
    admitted: set[tuple[str, str]] = set()
    for metadata in [session.metadata for session in checked.sessions] + [
        cast(dict[str, object], task.row["request_metadata_json"]) for task in checked.background_tasks
    ]:
        if "execution_composition" not in metadata:
            continue
        frozen = FrozenComposition.from_payload(metadata["execution_composition"])
        identity = frozen.binding.binding_id, frozen.plan.plan_id
        if identity not in admitted:
            admit_composition(frozen)
            admitted.add(identity)
    rows = json.loads(json.dumps(checked.to_payload()))
    source = cast(str, rows["sessions"][0]["workspace_id"])
    session_ids: dict[str, str] = {}
    task_ids: dict[str, str] = {}
    for kind, collection, rebound, resolver in (
        ("session", rows["sessions"], session_ids, session_id_resolver),
        ("task", rows["background_tasks"], task_ids, task_id_resolver),
    ):
        reserved: set[str] = set()
        for row in collection:
            original = cast(str, row[f"{kind}_id"])
            candidate = original

            def occupied(identity: str, check_kind: str = kind) -> bool:
                existing = session_repository.export_session_bundle_rows(
                    workspace=workspace,
                    session_ids=(identity,) if check_kind == "session" else (),
                    task_ids=(identity,) if check_kind == "task" else (),
                )
                return bool(existing["sessions" if check_kind == "session" else "tasks"])

            if occupied(candidate) or candidate in reserved:
                if resolver is not None:
                    candidate = resolver(original)
                    validate_id(candidate)
                    if occupied(candidate) or candidate in reserved:
                        raise SessionBundleError("collision resolver returned an occupied owner identity")
                else:
                    candidate = original + "-imported"
                    attempt = 1
                    while occupied(candidate) or candidate in reserved:
                        attempt += 1
                        candidate = f"{original}-imported-{attempt}"
            validate_id(candidate)
            rebound[original] = candidate
            reserved.add(candidate)
    for row in rows["sessions"]:
        original = row["session_id"]
        row["session_id"] = session_ids[original]
        row["workspace_id"] = str(workspace)
        for name in ("parent_session_id", "forked_from_session_id"):
            if row[name] is not None:
                row[name] = session_ids[row[name]]
        metadata = row["metadata_json"]
        _relocate_metadata(metadata, workspace=str(workspace), session_ids=session_ids, task_ids=task_ids)
        metadata["bundle_import_provenance"] = {"source_workspace": source, "source_session_id": original, "status": row["status"], "runnable": True}
        checkpoint = row["resume_checkpoint_json"]
        if checkpoint is not None:
            _relocate_metadata(checkpoint["session_metadata"], workspace=str(workspace), session_ids=session_ids, task_ids=task_ids)
            for name, mapping in (
                ("pending_approval_owner_session_id", session_ids),
                ("pending_approval_owner_parent_session_id", session_ids),
                ("pending_approval_delegated_task_id", task_ids),
            ):
                if checkpoint.get(name) in mapping:
                    checkpoint[name] = mapping[checkpoint[name]]
        pending = row["pending_approval_json"]
        if pending is not None:
            for name, mapping in (("owner_session_id", session_ids), ("owner_parent_session_id", session_ids), ("delegated_task_id", task_ids)):
                if pending.get(name) in mapping:
                    pending[name] = mapping[pending[name]]
    for row in rows["background_tasks"]:
        original = row["task_id"]
        row["task_id"] = task_ids[original]
        row["workspace_id"] = str(workspace)
        for name in ("request_session_id", "request_parent_session_id", "requested_child_session_id", "session_id"):
            if row[name] in session_ids:
                row[name] = session_ids[row[name]]
        metadata = row["request_metadata_json"]
        source_ref = metadata["composition_ref"]
        _relocate_metadata(metadata, workspace=str(workspace), session_ids=session_ids, task_ids=task_ids)
        metadata["bundle_import_provenance"] = {
            "source_workspace": source,
            "source_task_id": original,
            "status": row["status"],
            "source_composition_ref": source_ref,
            "runnable": False,
        }
    for row in rows["events"] + rows["deliveries"]:
        row["workspace_id"] = str(workspace)
        row["session_id"] = session_ids[row["session_id"]]
    _validate_rows(tuple(rows["sessions"]), tuple(rows["events"]), tuple(rows["background_tasks"]), tuple(rows["deliveries"]))
    if not dry_run:
        session_repository.import_session_bundle_rows(
            workspace=workspace,
            sessions=tuple(rows["sessions"]),
            events=tuple(rows["events"]),
            tasks=tuple(rows["background_tasks"]),
            deliveries=tuple(rows["deliveries"]),
        )
    return SessionBundleImportResult(
        SESSION_BUNDLE_SCHEMA_NAME,
        2,
        checked.manifest.voidcode_version,
        checked.manifest.created_at,
        False,
        {"exact": True},
        checked.manifest.workspace_hash,
        len(checked.sessions),
        checked.manifest.event_count,
        len(checked.background_tasks),
        tuple(session_ids.values()),
        0,
        dry_run,
    )


def serialize_session_bundle(bundle: SessionBundle, *, fmt: SessionBundleFormat = "zip") -> bytes:
    raw = (json.dumps(bundle.to_payload(), sort_keys=True, indent=2, allow_nan=False) + "\n").encode()
    if fmt == "json":
        return raw
    if fmt != "zip":
        raise SessionBundleError("unsupported bundle encoding")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(SESSION_BUNDLE_FILE_NAME, raw)
    return buffer.getvalue()


def write_session_bundle(bundle: SessionBundle, *, path: Path, fmt: SessionBundleFormat | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(serialize_session_bundle(bundle, fmt=fmt or ("json" if path.suffix.lower() == ".json" else "zip")))
    return path


def read_session_bundle(path: Path) -> SessionBundle:
    try:
        return read_session_bundle_bytes(path.read_bytes())
    except OSError as error:
        raise SessionBundleError(str(error)) from error


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SessionBundleError("duplicate JSON object key")
        result[key] = value
    return result


def read_session_bundle_bytes(raw: bytes) -> SessionBundle:
    try:
        if raw.startswith(b"PK"):
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                if archive.namelist() != [SESSION_BUNDLE_FILE_NAME]:
                    raise SessionBundleError("bundle archive must contain exactly bundle.json")
                raw = archive.read(SESSION_BUNDLE_FILE_NAME)
        return parse_session_bundle(json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_json_object))
    except (UnicodeDecodeError, json.JSONDecodeError, zipfile.BadZipFile) as error:
        raise SessionBundleError("invalid bundle encoding") from error
