"""Exact CURRENT bundle3 row snapshots; import never dispatches copied tasks.

Signed composition bodies stay on their canonical owner rows. JSON and ZIP use
one closed schema, and every row is checked before the single storage transaction.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import platform
import re
import time
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Final, Literal, cast

from .. import __version__ as VOIDCODE_VERSION
from ..tools.output import (
    _ARTIFACT_ID_PATTERN,
    _artifact_path_from_metadata,
    _is_text_artifact_mime,
    _safe_artifact_segment,
    _suffix_for_mime,
    iter_event_artifact_references,
)
from .agent_capability import validate_agent_capability_snapshot
from .composition import CompositionRef, FrozenComposition, SessionCompositionOwner, TaskCompositionOwner
from .contracts import validate_id
from .events import EventEnvelope, EventSource, model_turn_batch_id
from .execution.resume_checkpoint import tool_results_from_checkpoint, validated_resume_checkpoint_envelope
from .fact_codec import decode_fact
from .paths import session_artifacts_dir
from .permissions.policy import PendingApproval
from .storage import BackgroundTaskRepository, SessionEventRepository, SessionRecoveryRepository, SessionRepository, SessionRunWriter
from .storage.shared import _pending_operation_class, _pending_path_scope, _pending_permission_decision

SESSION_BUNDLE_SCHEMA_NAME: Final[str] = "voidcode.session.bundle.v3"
SESSION_BUNDLE_SCHEMA_VERSION: Final[int] = 3
SESSION_BUNDLE_FILE_NAME: Final[str] = "bundle.json"
SESSION_BUNDLE_DEFAULT_EXTENSION: Final[str] = ".vcsession.zip"
type SessionBundleFormat = Literal["zip", "json"]


class SessionBundleError(ValueError):
    """Malformed, incomplete, or unsupported CURRENT bundle."""


@dataclass(frozen=True, slots=True)
class SessionBundleOptions:
    """Exact snapshots cannot be redacted or have transcript rows removed."""

    redact: bool = False
    include_tool_output: bool = True
    include_raw_provider_messages: bool = True
    include_reasoning_text: bool = True


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
class SessionBundleArtifactPayload:
    artifact_id: str
    session_id: str
    tool_call_id: str | None
    tool_name: str
    kind: str
    mime: str
    byte_count: int
    line_count: int | None
    sha256: str
    created_at: int | None
    data_base64: str | None
    status: Literal["available", "missing"] = "available"

    def as_payload(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "session_id": self.session_id,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "kind": self.kind,
            "mime": self.mime,
            "byte_count": self.byte_count,
            "line_count": self.line_count,
            "sha256": self.sha256,
            "created_at": self.created_at,
            "data_base64": self.data_base64,
            "status": self.status,
        }


@dataclass(frozen=True, slots=True)
class SessionBundle:
    manifest: SessionBundleManifest
    sessions: tuple[SessionBundleSessionPayload, ...]
    background_tasks: tuple[SessionBundleBackgroundTaskPayload, ...]
    diagnostics: SessionBundleDiagnostics
    deliveries: tuple[dict[str, object], ...]
    artifacts: tuple[SessionBundleArtifactPayload, ...] = ()

    def to_payload(self) -> dict[str, object]:
        return {
            "schema": SESSION_BUNDLE_SCHEMA_NAME,
            "manifest": asdict(self.manifest),
            "sessions": [session.row for session in self.sessions],
            "events": [event for session in self.sessions for event in session.events],
            "background_tasks": [task.row for task in self.background_tasks],
            "deliveries": list(self.deliveries),
            "diagnostics": asdict(self.diagnostics),
            "artifacts": [artifact.as_payload() for artifact in self.artifacts],
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


def _str(value: object, where: str) -> str:
    if type(value) is not str:
        raise SessionBundleError(f"{where} must be a string")
    return value


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
            prompt_fields = {"question", "header", "multiple", "options"}
            if set(prompt) not in (prompt_fields, prompt_fields | {"allow_freeform"}):
                raise SessionBundleError("invalid question prompt fields")
            allow_freeform = prompt.get("allow_freeform", False)
            if not isinstance(allow_freeform, bool):
                raise SessionBundleError("invalid pending prompt freeform flag")
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
        if event["workspace_id"] != workspace or identity not in by_session or sequence <= 0 or event["source"] not in ("runtime", "core", "tool"):
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
        if type(manifest["schema_version"]) is not int or manifest["schema_version"] != SESSION_BUNDLE_SCHEMA_VERSION:
            raise SessionBundleError("unsupported session bundle schema version")
        for name in ("created_at", "session_count", "event_count", "background_task_count", "artifact_count"):
            if type(manifest[name]) is not int or cast(int, manifest[name]) < 0:
                raise SessionBundleError("invalid manifest count/timestamp")
        if type(manifest["voidcode_version"]) is not str or not manifest["voidcode_version"]:
            raise SessionBundleError("invalid manifest version")
        workspace_hash = manifest["workspace_hash"]
        if type(workspace_hash) is not str or re.fullmatch(r"sha256:[0-9a-f]{64}", workspace_hash) is None:
            raise SessionBundleError("invalid manifest workspace hash")
        platform_info = _object(manifest["platform"], "manifest platform")
        _exact(platform_info, frozenset({"system", "machine"}), "manifest platform")
        if any(type(platform_info[name]) is not str or not cast(str, platform_info[name]) for name in ("system", "machine")):
            raise SessionBundleError("invalid manifest platform")
        redaction = _object(manifest["redaction"], "manifest redaction")
        _exact(redaction, frozenset({"exact"}), "manifest redaction")
        if redaction["exact"] is not True or manifest["support_mode"] is not False:
            raise SessionBundleError("unsupported non-exact/artifact bundle scope")
        sessions = _rows(root["sessions"], "sessions")
        session_id_set = {validate_id(_str(session["session_id"], "session_id")) for session in sessions}
        artifacts = _rows(root["artifacts"], "artifacts")
        parsed_artifacts: list[SessionBundleArtifactPayload] = []
        seen_artifact_ids: set[str] = set()
        valid_statuses = frozenset({"available", "missing"})
        for art in artifacts:
            _exact(
                art,
                frozenset(
                    {
                        "artifact_id",
                        "session_id",
                        "tool_call_id",
                        "tool_name",
                        "kind",
                        "mime",
                        "byte_count",
                        "line_count",
                        "sha256",
                        "created_at",
                        "data_base64",
                        "status",
                    }
                ),
                "artifact entry",
            )
            art_id = art.get("artifact_id")
            if type(art_id) is not str or _ARTIFACT_ID_PATTERN.fullmatch(art_id) is None or art_id in seen_artifact_ids:
                raise SessionBundleError(f"invalid or duplicate artifact_id in bundle: {art_id}")
            seen_artifact_ids.add(art_id)
            art_session_id = art.get("session_id")
            if type(art_session_id) is not str:
                raise SessionBundleError("artifact entry missing session_id")
            validate_id(art_session_id)
            if art_session_id not in session_id_set:
                raise SessionBundleError(f"artifact session_id '{art_session_id}' not found in bundle sessions")
            tool_call_id = art.get("tool_call_id")
            if tool_call_id is not None and (type(tool_call_id) is not str or not tool_call_id):
                raise SessionBundleError("invalid artifact tool_call_id")
            for name in ("tool_name", "kind", "mime"):
                if type(art[name]) is not str or not cast(str, art[name]):
                    raise SessionBundleError(f"invalid artifact {name}")
            status = art.get("status")
            if type(status) is not str or status not in valid_statuses:
                raise SessionBundleError(f"invalid artifact status: {status}")
            byte_count = art.get("byte_count")
            if type(byte_count) is not int or byte_count < 0:
                raise SessionBundleError("invalid artifact byte_count")
            line_count = art.get("line_count")
            if line_count is not None and (type(line_count) is not int or line_count < 0):
                raise SessionBundleError("invalid artifact line_count")
            created_at = art.get("created_at")
            if created_at is not None and (type(created_at) is not int or created_at < 0):
                raise SessionBundleError("invalid artifact created_at")
            sha256_val = art.get("sha256")
            if type(sha256_val) is not str or re.fullmatch(r"[0-9a-f]{64}", sha256_val) is None:
                raise SessionBundleError("invalid artifact sha256 checksum")
            data_b64 = art.get("data_base64")
            if status == "available":
                if type(data_b64) is not str:
                    raise SessionBundleError("available artifact missing data_base64 string")
                try:
                    decoded = base64.b64decode(data_b64, validate=True)
                except Exception as exc:
                    raise SessionBundleError(f"corrupted base64 for artifact {art_id}") from exc
                if len(decoded) != byte_count:
                    raise SessionBundleError(f"artifact byte count mismatch for {art_id}")
                if hashlib.sha256(decoded).hexdigest() != sha256_val:
                    raise SessionBundleError(f"artifact checksum mismatch for {art_id}")
            elif data_b64 is not None:
                raise SessionBundleError(f"missing artifact must not contain data_base64: {art_id}")
            parsed_artifacts.append(
                SessionBundleArtifactPayload(
                    artifact_id=art_id,
                    session_id=art_session_id,
                    tool_call_id=cast(str | None, tool_call_id),
                    tool_name=cast(str, art["tool_name"]),
                    kind=cast(str, art["kind"]),
                    mime=cast(str, art["mime"]),
                    byte_count=byte_count,
                    line_count=line_count,
                    sha256=sha256_val,
                    created_at=created_at,
                    data_base64=data_b64,
                    status=cast(Literal["available", "missing"], status),
                )
            )
        events = _rows(root["events"], "events")
        tasks = _rows(root["background_tasks"], "background_tasks")
        deliveries = _rows(root["deliveries"], "deliveries")
        source = _validate_rows(sessions, events, tasks, deliveries)
        if manifest["workspace_hash"] != _workspace_hash(Path(source)):
            raise SessionBundleError("source workspace hash mismatch")
        if (manifest["session_count"], manifest["event_count"], manifest["background_task_count"], manifest["artifact_count"]) != (
            len(sessions),
            len(events),
            len(tasks),
            len(parsed_artifacts),
        ):
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
            artifacts=tuple(parsed_artifacts),
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
    if options.redact is not False or any(
        value is not True for value in (options.include_tool_output, options.include_raw_provider_messages, options.include_reasoning_text)
    ):
        raise SessionBundleError("CURRENT bundle3 requires exact unredacted export options")
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
    collected_artifacts: list[dict[str, object]] = []
    seen_artifact_ids: set[str] = set()

    artifact_sources: list[tuple[str, Mapping[str, object]]] = []
    for event in events:
        payload = event.get("payload_json") or event.get("payload")
        if isinstance(payload, Mapping):
            artifact_sources.append((cast(str, event.get("session_id", session_id)), payload))
    for session_row in session_rows:
        checkpoint = session_row.get("resume_checkpoint_json")
        if isinstance(checkpoint, Mapping):
            raw_results = checkpoint.get("tool_results")
            if isinstance(raw_results, list):
                owner_session_id = cast(str, session_row["session_id"])
                artifact_sources.extend((owner_session_id, report) for report in raw_results if isinstance(report, Mapping))
    for event_session_id, payload in artifact_sources:
        for art_meta in iter_event_artifact_references(payload):
            if art_meta.get("producer") != "voidcode.tool_output.v1":
                continue
            target_art_id = art_meta.get("artifact_id")
            if type(target_art_id) is not str or _ARTIFACT_ID_PATTERN.fullmatch(target_art_id) is None:
                raise SessionBundleError("invalid artifact reference in event")
            if target_art_id in seen_artifact_ids:
                continue
            seen_artifact_ids.add(target_art_id)
            if art_meta.get("session_id") != event_session_id:
                raise SessionBundleError(f"artifact session owner mismatch: {target_art_id}")
            raw_path = art_meta.get("path")
            art_file = _artifact_path_from_metadata(art_meta)
            if isinstance(raw_path, str) and raw_path and art_file is None:
                raise SessionBundleError(f"artifact path escapes its session owner: {target_art_id}")
            raw_bytes: bytes | None = None
            if art_file is not None and art_file.is_file():
                raw_bytes = art_file.read_bytes()
            expected_count = art_meta.get("byte_count")
            expected_sha = art_meta.get("sha256")
            if type(expected_count) is not int or expected_count < 0:
                raise SessionBundleError(f"invalid artifact byte count: {target_art_id}")
            if type(expected_sha) is not str or re.fullmatch(r"[0-9a-f]{64}", expected_sha) is None:
                raise SessionBundleError(f"invalid artifact checksum: {target_art_id}")
            if raw_bytes is None:
                raise SessionBundleError(f"referenced artifact bytes are missing: {target_art_id}")
            if len(raw_bytes) != expected_count or hashlib.sha256(raw_bytes).hexdigest() != expected_sha:
                raise SessionBundleError(f"artifact integrity check failed: {target_art_id}")
            status = "available"
            collected_artifacts.append(
                {
                    "artifact_id": target_art_id,
                    "session_id": event_session_id,
                    "tool_call_id": art_meta.get("tool_call_id"),
                    "tool_name": art_meta.get("tool_name"),
                    "kind": art_meta.get("kind"),
                    "mime": art_meta.get("mime"),
                    "byte_count": expected_count,
                    "line_count": art_meta.get("line_count"),
                    "sha256": expected_sha,
                    "created_at": art_meta.get("created_at"),
                    "data_base64": base64.b64encode(raw_bytes).decode("ascii") if raw_bytes is not None else None,
                    "status": status,
                }
            )
    manifest = SessionBundleManifest(
        SESSION_BUNDLE_SCHEMA_VERSION,
        VOIDCODE_VERSION,
        (clock or (lambda: int(time.time() * 1000)))(),
        _workspace_hash(workspace),
        {"system": platform.system(), "machine": platform.machine()},
        {"exact": True},
        False,
        len(session_rows),
        len(events),
        len(task_rows),
        len(collected_artifacts),
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
            "artifacts": collected_artifacts,
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


def _relocate_turn_batch(
    metadata: dict[str, object],
    *,
    source_session_id: str,
    destination_session_id: str,
    batch_ids: Mapping[str, str],
) -> None:
    runtime_state = metadata.get("runtime_state")
    if not isinstance(runtime_state, dict):
        return
    batch = runtime_state.get("turn_batch")
    if batch is None:
        return
    if not isinstance(batch, dict) or batch.get("session_id") != source_session_id:
        raise SessionBundleError("native batch checkpoint does not match its source session")
    source_batch_id = batch.get("tool_batch_id")
    destination_batch_id = batch_ids.get(source_batch_id) if isinstance(source_batch_id, str) else None
    if destination_batch_id is None:
        raise SessionBundleError("native batch checkpoint has no matching copied model-turn event")
    batch["session_id"] = destination_session_id
    batch["tool_batch_id"] = destination_batch_id


def _relocate_composition_turn(metadata: dict[str, object], *, source_session_id: str, destination_session_id: str) -> None:
    runtime_state = metadata.get("runtime_state")
    if not isinstance(runtime_state, dict):
        return
    turn = runtime_state.get("composition_turn")
    if turn is None:
        return
    if not isinstance(turn, dict) or turn.get("session_id") != source_session_id:
        raise SessionBundleError("composition turn does not match its source session")
    run_id = turn.get("run_id")
    occurrence = turn.get("occurrence")
    if (
        not isinstance(run_id, str)
        or not run_id.strip()
        or type(occurrence) is not int
        or occurrence < 1
        or turn.get("scope_id") != f"{source_session_id}:{run_id}:{occurrence}"
    ):
        raise SessionBundleError("composition turn has invalid source ownership")
    turn["session_id"] = destination_session_id
    turn["scope_id"] = f"{destination_session_id}:{run_id}:{occurrence}"


def _relocate_checkpoint_tool_results(checkpoint: dict[str, object], *, batch_ids: Mapping[str, str]) -> None:
    tool_results = checkpoint.get("tool_results")
    if not isinstance(tool_results, list):
        return
    for result in tool_results:
        if not isinstance(result, dict):
            raise SessionBundleError("checkpoint tool result must be an object")
        source_batch_id = result.get("tool_batch_id")
        if not isinstance(source_batch_id, str) or not source_batch_id.strip():
            raise SessionBundleError("checkpoint tool result requires its native batch identity")
        destination_batch_id = batch_ids.get(source_batch_id)
        if destination_batch_id is None:
            raise SessionBundleError("checkpoint tool result has no matching copied model-turn event")
        result["tool_batch_id"] = destination_batch_id


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
    if admit_composition is None and not dry_run:
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
            if not dry_run:
                assert admit_composition is not None
                admit_composition(frozen)
            admitted.add(identity)
    rows = json.loads(json.dumps(checked.to_payload()))
    source = cast(str, rows["sessions"][0]["workspace_id"])
    session_ids: dict[str, str] = {}
    task_ids: dict[str, str] = {}
    original_owner_ids = {
        "session": tuple(cast(str, row["session_id"]) for row in rows["sessions"]),
        "task": tuple(cast(str, row["task_id"]) for row in rows["background_tasks"]),
    }
    occupied_rows = session_repository.export_session_bundle_rows(
        workspace=workspace,
        session_ids=original_owner_ids["session"],
        task_ids=original_owner_ids["task"],
    )
    occupied_ids = {
        "session": {cast(str, row["session_id"]) for row in cast(tuple[dict[str, object], ...], occupied_rows["sessions"])},
        "task": {cast(str, row["task_id"]) for row in cast(tuple[dict[str, object], ...], occupied_rows["tasks"])},
    }
    occupied_cache = {(kind, identity): identity in occupied_ids[kind] for kind, identities in original_owner_ids.items() for identity in identities}
    for kind, collection, rebound, resolver in (
        ("session", rows["sessions"], session_ids, session_id_resolver),
        ("task", rows["background_tasks"], task_ids, task_id_resolver),
    ):
        reserved: set[str] = set()

        def occupied(identity: str, check_kind: str = kind) -> bool:
            key = check_kind, identity
            if key not in occupied_cache:
                existing = session_repository.export_session_bundle_rows(
                    workspace=workspace,
                    session_ids=(identity,) if check_kind == "session" else (),
                    task_ids=(identity,) if check_kind == "task" else (),
                )
                occupied_cache[key] = bool(existing["sessions" if check_kind == "session" else "tasks"])
            return occupied_cache[key]

        for row in collection:
            original = cast(str, row[f"{kind}_id"])
            candidate = original

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
    batch_id_maps: dict[str, dict[str, str]] = {}
    for event in rows["events"]:
        original_session_id = cast(str, event["session_id"])
        if event["event_type"] != "core.model_turn":
            continue
        sequence = cast(int, event["sequence"])
        destination_session_id = session_ids[original_session_id]
        batch_id_maps.setdefault(original_session_id, {})[model_turn_batch_id(original_session_id, sequence)] = model_turn_batch_id(
            destination_session_id, sequence
        )
    for row in rows["sessions"]:
        original = cast(str, row["session_id"])
        destination = session_ids[original]
        row["session_id"] = destination
        row["workspace_id"] = str(workspace)
        for name in ("parent_session_id", "forked_from_session_id"):
            if row[name] is not None:
                row[name] = session_ids[row[name]]
        batch_ids = batch_id_maps.get(original, {})
        metadata = row["metadata_json"]
        _relocate_metadata(metadata, workspace=str(workspace), session_ids=session_ids, task_ids=task_ids)
        _relocate_turn_batch(
            metadata,
            source_session_id=original,
            destination_session_id=destination,
            batch_ids=batch_ids,
        )
        _relocate_composition_turn(metadata, source_session_id=original, destination_session_id=destination)
        metadata["bundle_import_provenance"] = {
            "source_workspace": source,
            "source_session_id": original,
            "status": row["status"],
            "runnable": True,
        }
        checkpoint = row["resume_checkpoint_json"]
        if checkpoint is not None:
            checkpoint_metadata = checkpoint["session_metadata"]
            _relocate_metadata(checkpoint_metadata, workspace=str(workspace), session_ids=session_ids, task_ids=task_ids)
            _relocate_turn_batch(
                checkpoint_metadata,
                source_session_id=original,
                destination_session_id=destination,
                batch_ids=batch_ids,
            )
            _relocate_composition_turn(checkpoint_metadata, source_session_id=original, destination_session_id=destination)
            checkpoint_metadata["bundle_import_provenance"] = metadata["bundle_import_provenance"]
            _relocate_checkpoint_tool_results(checkpoint, batch_ids=batch_ids)
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
    artifact_by_id = {artifact.artifact_id: artifact for artifact in checked.artifacts}
    destination_paths: dict[str, Path] = {}
    dest_artifacts: list[tuple[Path, bytes]] = []

    for art in checked.artifacts:
        if art.session_id not in session_ids:
            raise SessionBundleError(f"foreign artifact session {art.session_id} cannot be rebound")
        target_session_id = session_ids[art.session_id]
        raw_root = session_artifacts_dir(target_session_id).parent
        raw_dir = session_artifacts_dir(target_session_id)
        try:
            if raw_root.is_symlink() or raw_dir.is_symlink():
                raise SessionBundleError(f"artifact directory symlink escape detected: {raw_dir}")
            durable_root = raw_root.resolve()
            dest_dir = raw_dir.resolve()
        except (OSError, RuntimeError) as exc:
            raise SessionBundleError(f"cannot resolve artifact destination: {raw_dir}") from exc
        if durable_root.parent != raw_root.parent.resolve() or dest_dir.parent != durable_root or dest_dir.name != target_session_id:
            raise SessionBundleError(f"artifact directory escapes its destination owner: {dest_dir}")
        call_segment = _safe_artifact_segment(art.tool_call_id, fallback="unknown-tool-call")
        tool_segment = _safe_artifact_segment(art.tool_name, fallback="unknown-tool")
        suffix = ".txt" if _is_text_artifact_mime(art.mime) else _suffix_for_mime(art.mime)
        dest_path = dest_dir / f"{call_segment}-{tool_segment}-{art.artifact_id}{suffix}"
        if dest_path.is_symlink():
            raise SessionBundleError(f"artifact destination is a symlink: {dest_path}")
        destination_paths[art.artifact_id] = dest_path
        if art.data_base64 is None:
            if dest_path.exists():
                raise SessionBundleError(f"missing artifact collides with an existing file: {dest_path}")
            continue
        raw_bytes = base64.b64decode(art.data_base64, validate=True)
        if dest_path.exists():
            if not dest_path.is_file():
                raise SessionBundleError(f"artifact destination is not a regular file: {dest_path}")
            existing_bytes = dest_path.read_bytes()
            if len(existing_bytes) != art.byte_count or hashlib.sha256(existing_bytes).hexdigest() != art.sha256:
                raise SessionBundleError(f"cannot overwrite existing user file with conflicting artifact: {dest_path}")
        else:
            dest_artifacts.append((dest_path, raw_bytes))

    def rebind_artifact_references(payload: object) -> None:
        if not isinstance(payload, Mapping):
            return
        for reference in iter_event_artifact_references(payload):
            artifact_id = reference.get("artifact_id")
            artifact = artifact_by_id.get(artifact_id) if isinstance(artifact_id, str) else None
            if artifact is None:
                if reference.get("producer") == "voidcode.tool_output.v1":
                    raise SessionBundleError(f"runtime artifact reference is missing from bundle: {artifact_id}")
                continue
            if reference.get("session_id") not in (None, artifact.session_id):
                raise SessionBundleError(f"artifact reference owner mismatch: {artifact_id}")
            if isinstance(reference, dict):
                reference["session_id"] = session_ids[artifact.session_id]
                reference["path"] = str(destination_paths[artifact.artifact_id])

    for event in rows["events"]:
        event["session_id"] = session_ids[event["session_id"]]
        event["workspace_id"] = str(workspace)
        payload = event.get("payload_json") or event.get("payload")
        if isinstance(payload, dict):
            for name, mapping in (
                ("owner_session_id", session_ids),
                ("owner_parent_session_id", session_ids),
                ("delegated_task_id", task_ids),
            ):
                if payload.get(name) in mapping:
                    payload[name] = mapping[payload[name]]
        rebind_artifact_references(payload)
    for row in rows["sessions"]:
        checkpoint = row["resume_checkpoint_json"]
        if isinstance(checkpoint, dict):
            tool_results = checkpoint.get("tool_results")
            if isinstance(tool_results, list):
                for report in tool_results:
                    rebind_artifact_references(report)
    for delivery in rows["deliveries"]:
        delivery["session_id"] = session_ids[delivery["session_id"]]
        delivery["workspace_id"] = str(workspace)
    _validate_rows(tuple(rows["sessions"]), tuple(rows["events"]), tuple(rows["background_tasks"]), tuple(rows["deliveries"]))

    if dry_run:
        return SessionBundleImportResult(
            SESSION_BUNDLE_SCHEMA_NAME,
            SESSION_BUNDLE_SCHEMA_VERSION,
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
            True,
        )

    # Stage artifact writes atomically before DB commit
    staged_files: list[Path] = []
    staged_dirs: list[Path] = []
    try:
        for dest_path, raw_bytes in dest_artifacts:
            parent_dir = dest_path.parent
            if not parent_dir.exists():
                parent_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                staged_dirs.append(parent_dir)
            if platform.system() != "Windows":
                try:
                    parent_dir.chmod(0o700)
                except OSError as exc:
                    raise SessionBundleError(f"failed to secure artifact directory: {exc}") from exc
            fd = os.open(dest_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            staged_files.append(dest_path)
            with os.fdopen(fd, "wb") as f:
                f.write(raw_bytes)
    except Exception as exc:
        for f in reversed(staged_files):
            try:
                f.unlink(missing_ok=True)
            except OSError:
                pass
        for d in reversed(staged_dirs):
            try:
                d.rmdir()
            except OSError:
                pass
        if isinstance(exc, SessionBundleError):
            raise
        raise SessionBundleError(f"failed to write imported artifacts: {exc}") from exc

    try:
        session_repository.import_session_bundle_rows(
            workspace=workspace,
            sessions=tuple(rows["sessions"]),
            events=tuple(rows["events"]),
            tasks=tuple(rows["background_tasks"]),
            deliveries=tuple(rows["deliveries"]),
        )
    except Exception:
        for f in reversed(staged_files):
            try:
                f.unlink(missing_ok=True)
            except OSError:
                pass
        for d in reversed(staged_dirs):
            try:
                d.rmdir()
            except OSError:
                pass
        raise
    return SessionBundleImportResult(
        SESSION_BUNDLE_SCHEMA_NAME,
        SESSION_BUNDLE_SCHEMA_VERSION,
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
    raw = serialize_session_bundle(bundle, fmt=fmt or ("json" if path.suffix.lower() == ".json" else "zip"))
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(raw)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    return path


def read_session_bundle(path: Path) -> SessionBundle:
    try:
        return read_session_bundle_bytes(path.read_bytes())
    except OSError as error:
        raise SessionBundleError(str(error)) from error


def preview_session_bundle_import_file(bundle_path: Path, *, workspace: Path) -> SessionBundleImportResult:
    """Validate an import without runtime startup or filesystem writes."""
    from .storage import SqliteSessionStore

    store = SqliteSessionStore()
    return apply_session_bundle(
        read_session_bundle(bundle_path),
        session_repository=store,
        events=store,
        recovery=store,
        run_writer=store,
        workspace=workspace,
        dry_run=True,
    )


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
                if SESSION_BUNDLE_FILE_NAME not in archive.namelist():
                    raise SessionBundleError("bundle archive must contain bundle.json")
                raw = archive.read(SESSION_BUNDLE_FILE_NAME)
        return parse_session_bundle(json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_json_object))
    except (UnicodeDecodeError, json.JSONDecodeError, zipfile.BadZipFile) as error:
        raise SessionBundleError("invalid bundle encoding") from error
