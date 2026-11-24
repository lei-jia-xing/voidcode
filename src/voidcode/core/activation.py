"""Core capability generation, activation, state copies, and private snapshot storage."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

import jsonschema

from ..sdk.json_values import json_wire_object, own_json_object
from ..sdk.plugin import StateSchema, StateScope
from ..sdk.provider import ProviderReasoningEffort
from .plugins import PluginSource
from .storage.store import (
    PluginStateRow,
    SessionRecord,
    StagedStateUpdate,
    canonical_json,
)

__all__ = [
    "Admission",
    "ExecutionConfig",
    "ExecutionPolicy",
    "StateCopyPlan",
    "StateCopySource",
    "load_private_snapshot",
    "plan_state_copies",
    "snapshot_path",
    "validate_state_payload",
    "write_private_snapshot",
]

PRIVATE_DIR_NAME = "private"


@dataclass(frozen=True, slots=True)
class ExecutionPolicy:
    """Execution policy governing approval and read-only constraints."""

    approval_mode: Literal["ask", "write", "yolo"] = "ask"
    read_only: bool = False


@dataclass(frozen=True, slots=True)
class SandboxPolicy:
    """Neutral confinement for registry children. None (absent) means unconfined host exec.

    No reload gate: new spawns use the current policy; old jobs keep the enforcement argv
    fixed at their spawn. Model-editing-config plus yolo reload is out of threat model.
    """

    mode: Literal["workspace-write", "read-only", "danger-full-access"] = "workspace-write"
    network: bool = False
    writable_roots: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "writable_roots", tuple(self.writable_roots))


@dataclass(frozen=True, slots=True)
class ExecutionConfig:
    """Core execution options for model selection and policy."""

    model: str | None = None
    reasoning_effort: ProviderReasoningEffort | None = None
    policy: ExecutionPolicy = ExecutionPolicy()
    #: How many child sessions one chain root may hold; the single governed spawn quota.
    max_root_children: int = 4

    def __post_init__(self) -> None:
        if type(self.max_root_children) is not int or not 1 <= self.max_root_children <= 64:
            raise ValueError("max_root_children must be an integer within 1..64")


@dataclass(frozen=True, slots=True)
class Admission:
    """One immutable capability generation: opaque id, runtime inputs, payload, private configuration."""

    revision_id: str
    plugins: tuple[PluginSource, ...]
    revision_payload: Mapping[str, object]
    private_snapshot: Mapping[str, object]
    config: ExecutionConfig
    validate: Callable[[Sequence[str]], None] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.revision_id, str) or not self.revision_id:
            raise ValueError("admission needs a non-empty generation id")
        # Own read-only snapshots: callers cannot mutate the generation they were handed.
        object.__setattr__(self, "revision_payload", own_json_object(self.revision_payload))
        object.__setattr__(self, "private_snapshot", own_json_object(self.private_snapshot))

    @property
    def plugin_ids(self) -> frozenset[str]:
        return frozenset(plugin.plugin_id for plugin in self.plugins)


def snapshot_path(state_dir: Path, revision_id: str) -> Path:
    return state_dir / PRIVATE_DIR_NAME / f"{revision_id}.json"


def write_private_snapshot(state_dir: Path, revision_id: str, snapshot: Mapping[str, object]) -> Path:
    """Persist credential-bearing configuration once per revision identity, never overwriting one."""
    directory = state_dir / PRIVATE_DIR_NAME
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    target = snapshot_path(state_dir, revision_id)
    text = canonical_json(snapshot)
    if target.exists():
        if target.read_text(encoding="utf-8") != text:
            raise ValueError(f"private configuration snapshot for revision {revision_id} already exists with other content")
        return target
    temporary = directory / f".{revision_id}.{os.getpid()}.tmp"
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
        try:
            os.link(temporary, target)
        except FileExistsError as exc:
            if target.read_text(encoding="utf-8") != text:
                raise ValueError(f"private configuration snapshot for revision {revision_id} already exists with other content") from exc
    finally:
        temporary.unlink(missing_ok=True)
    return target


def load_private_snapshot(state_dir: Path, revision_id: str) -> Mapping[str, object]:
    """Load the private configuration a stored revision was admitted with, or fail visibly."""
    path = snapshot_path(state_dir, revision_id)
    if not path.is_file():
        raise FileNotFoundError(
            f"private configuration snapshot is missing for the active revision {revision_id} ({path}); "
            "restore it from backup or start a fresh state directory"
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"private configuration snapshot {path} is not an object")
    return json_wire_object(payload)


def validate_state_payload(payload: Mapping[str, object], state: StateSchema | None) -> Mapping[str, object]:
    """Require an owner state copy to be a JSON object, and match its declared schema when it has one."""
    owned = json_wire_object(payload)
    if state is not None and state.schema is not None:
        try:
            jsonschema.Draft202012Validator(json_wire_object(state.schema)).validate(owned)
        except jsonschema.ValidationError as exc:
            raise ValueError(f"state copy failed its declared state schema: {exc.message}") from exc
    return owned


@dataclass(frozen=True, slots=True)
class StateCopyPlan:
    """The owner state copies one activation transaction will write, and who they belong to."""

    updates: tuple[StagedStateUpdate, ...]
    deactivated_owners: tuple[str, ...] = ()


class StateCopySource(Protocol):
    """The two store reads the state-copy planner needs."""

    def plugin_state_rows(self, scope: StateScope, scope_key: str) -> tuple[PluginStateRow, ...]: ...
    def session(self, session_id: str) -> SessionRecord: ...


def plan_state_copies(
    *,
    store: StateCopySource,
    workspace: str,
    session_ids: Sequence[str],
    old_states: Mapping[str, StateSchema],
    candidate_states: Mapping[str, StateSchema],
    deactivate: Callable[[str, Mapping[str, object], StateScope, str, int | None], Mapping[str, object] | None],
) -> StateCopyPlan:
    """Validate or deactivate owner state for the candidate generation.

    An owner admitted by the candidate must match the candidate's declared state schema version
    and payload schema (version mismatch is explicitly rejected). An owner the candidate does not
    admit is deactivated by the runtime that still holds its schema. A dropped owner that declares
    no deactivation transform has no state contract: its historic rows are preserved verbatim and
    ignored rather than blocking the switch.
    """
    updates: list[StagedStateUpdate] = []
    deactivated: list[str] = []
    deactivate_owners = frozenset(old_states) - frozenset(candidate_states)
    scopes: list[tuple[StateScope, str]] = [("session", session_id) for session_id in session_ids]
    scopes.append(("workspace", workspace))
    for scope, scope_key in scopes:
        tip = store.session(scope_key).last_event_sequence if scope == "session" else None
        for row in store.plugin_state_rows(scope, scope_key):
            candidate = candidate_states.get(row.owner)
            if candidate is not None:
                target_version = candidate.version
                if target_version != row.schema_version:
                    raise ValueError(f"plugin {row.owner} state schema mismatch: stored v{row.schema_version}, target v{target_version}")
                validate_state_payload(row.payload, candidate)
                continue
            elif row.owner in deactivate_owners:
                prepared = deactivate(row.owner, row.payload, scope, scope_key, tip)
                if prepared is None:
                    deactivated.append(row.owner)
                    continue
                payload = validate_state_payload(prepared, old_states[row.owner])
                updates.append(
                    StagedStateUpdate(
                        owner=row.owner,
                        scope=scope,
                        scope_key=scope_key,
                        payload=payload,
                        expected_version=row.version,
                        schema_version=row.schema_version,
                    )
                )
                deactivated.append(row.owner)
    return StateCopyPlan(updates=tuple(updates), deactivated_owners=tuple(sorted(set(deactivated))))
