"""Persisted workflow run/node records, owner-state store, and the per-session commit lock."""

from __future__ import annotations

import hashlib
import json
import threading
import time
import weakref
from collections.abc import Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Literal, TypeGuard

from voidcode.capabilities.plugin_state import PluginStateStore
from voidcode.sdk.child_schema import canonical_result_schema
from voidcode.sdk.json_values import json_wire_object, json_wire_value, own_json_object, own_json_value
from voidcode.sdk.services import BudgetExhaustion, StateServices, StructuredQueryParseError

from .constants import (
    _NODE_KEYS,
    _NODE_REQUIRED_KEYS,
    _NODE_STATUSES,
    _RUN_KEYS,
    _RUN_STATUSES,
    SCHEMA_VERSION,
    NodeStatus,
    RunStatus,
)

_STATE_LOCKS: weakref.WeakValueDictionary[str, threading.RLock] = weakref.WeakValueDictionary()
_STATE_LOCKS_GUARD = threading.Lock()


def _state_lock(session_id: str) -> threading.RLock:
    """The per-session workflow-state lock; process-wide so commands and drivers agree."""
    with _STATE_LOCKS_GUARD:
        lock = _STATE_LOCKS.get(session_id)
        if lock is None:
            lock = threading.RLock()
            _STATE_LOCKS[session_id] = lock
        return lock


def _is_node_status(status: object) -> TypeGuard[NodeStatus]:
    return status in _NODE_STATUSES


def _is_run_status(status: object) -> TypeGuard[RunStatus]:
    return status in _RUN_STATUSES


def _node_optional_id(raw: Mapping[str, object], field: str, run_id: str, key: str, label: str) -> str | None:
    val = raw[field]
    if val is not None and (not isinstance(val, str) or not val):
        raise ValueError(f"workflow run '{run_id}' node '{key}' {field} must be {label} or null")
    return val


def _node_optional_sha256(raw: Mapping[str, object], field: str, run_id: str, key: str) -> str | None:
    val = raw[field]
    if val is not None and (not isinstance(val, str) or len(val) != 64):
        raise ValueError(f"workflow run '{run_id}' node '{key}' {field} must be a sha256 or null")
    return val


def _node_optional_text(val: object, *, field: str, run_id: str, key: str) -> str | None:
    if val is not None and not isinstance(val, str):
        raise ValueError(f"workflow run '{run_id}' node '{key}' {field} must be text or null")
    return val


def _run_text(val: object, *, field: str, run_id: str) -> str:
    if not isinstance(val, str) or not val:
        raise ValueError(f"workflow run '{run_id}' {field} must be a non-empty string")
    return val


def _run_sha256(val: object, *, field: str, run_id: str) -> str:
    if not isinstance(val, str) or len(val) != 64:
        raise ValueError(f"workflow run '{run_id}' {field} must be a sha256 hex digest")
    return val


def _structured_parse_error_from_payload(value: object, *, run_id: str, key: str) -> StructuredQueryParseError | None:
    """Parse a persisted structured parse error; absent stays absent."""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError(f"workflow run '{run_id}' node '{key}' parse_error must be an object or null")
    kind = value.get("kind")
    message = value.get("message")
    path = value.get("path")
    if kind != "invalid_json" and kind != "schema_violation":
        raise ValueError(f"workflow run '{run_id}' node '{key}' parse_error has an unknown kind: {kind!r}")
    if not isinstance(message, str) or not message:
        raise ValueError(f"workflow run '{run_id}' node '{key}' parse_error requires a non-empty message")
    if path is not None and not isinstance(path, str):
        raise ValueError(f"workflow run '{run_id}' node '{key}' parse_error path must be a string or null")
    error_kind: Literal["invalid_json", "schema_violation"] = "invalid_json" if kind == "invalid_json" else "schema_violation"
    return StructuredQueryParseError(kind=error_kind, message=message, path=path)


@dataclass(frozen=True, slots=True)
class WorkflowNode:
    key: str
    status: NodeStatus = "pending"
    depends_on: tuple[str, ...] = ()
    agent: str | None = None
    child_session_id: str | None = None
    run_id: str | None = None
    effective_input_sha256: str | None = None
    output: str | None = None
    output_sha256: str | None = None
    output_bytes: int | None = None
    #: Bounded preview of a terminal output whose full bytes were not captured; never a completeness claim.
    output_preview: str | None = None
    #: Committed capture cause when the exact run's large output could not be spilled; None is no committed failure.
    output_error: str | None = None
    #: Committed capture cause when the exact run's history snapshot could not be spilled; None is no committed failure.
    history_error: str | None = None
    error: str | None = None
    budget_exhaustion: BudgetExhaustion | None = None
    #: Declared result schema for structured delivery; None means undeclared (unknown, never valid).
    result_schema: Mapping[str, object] | None = None
    #: Validated structured delivery (exactly one of data/parse_error) when a schema was
    #: declared and the node completed with full text captured; absent means undeclared,
    #: undelivered, or uncaptured — never valid, never a guess.
    data: Mapping[str, object] | None = None
    parse_error: StructuredQueryParseError | None = None

    def __post_init__(self) -> None:
        if self.data is not None:
            object.__setattr__(self, "data", own_json_object(self.data))

    @classmethod
    def from_payload(cls, raw: Mapping[str, object], *, run_id: str) -> WorkflowNode:
        unknown = sorted(str(key) for key in raw if key not in _NODE_KEYS)
        if unknown:
            raise ValueError(f"workflow run '{run_id}' node field '{unknown[0]}' is not supported")
        missing = sorted(_NODE_REQUIRED_KEYS - raw.keys())
        if missing:
            raise ValueError(f"workflow run '{run_id}' node is missing field(s): " + ", ".join(missing))
        key = raw["key"]
        if not isinstance(key, str) or not key:
            raise ValueError(f"workflow run '{run_id}' node key must be a non-empty string")
        status = raw["status"]
        if not _is_node_status(status):
            raise ValueError(f"workflow run '{run_id}' node '{key}' has an unknown status: {status!r}")
        depends_on = raw["depends_on"]
        if not isinstance(depends_on, (list, tuple)) or any(not isinstance(dep, str) or not dep for dep in depends_on):
            raise ValueError(f"workflow run '{run_id}' node '{key}' depends_on must list non-empty keys")
        agent = _node_optional_id(raw, "agent", run_id, key, "a name")
        child_session_id = _node_optional_id(raw, "child_session_id", run_id, key, "an id")
        run_id_value = _node_optional_id(raw, "run_id", run_id, key, "an id")
        effective_input_sha256 = _node_optional_sha256(raw, "effective_input_sha256", run_id, key)
        output = _node_optional_text(raw["output"], field="output", run_id=run_id, key=key)
        output_sha256 = _node_optional_sha256(raw, "output_sha256", run_id, key)
        output_bytes = raw["output_bytes"]
        if output_bytes is not None and (not isinstance(output_bytes, int) or isinstance(output_bytes, bool) or output_bytes < 0):
            raise ValueError(f"workflow run '{run_id}' node '{key}' output_bytes must be a byte count or null")
        output_preview = _node_optional_text(raw.get("output_preview"), field="output_preview", run_id=run_id, key=key)
        output_error = _node_optional_text(raw.get("output_error"), field="output_error", run_id=run_id, key=key)
        history_error = _node_optional_text(raw.get("history_error"), field="history_error", run_id=run_id, key=key)
        error = _node_optional_text(raw["error"], field="error", run_id=run_id, key=key)
        budget_exhaustion = BudgetExhaustion.from_payload(raw.get("budget_exhaustion"))
        schema_raw = raw.get("result_schema")
        result_schema: Mapping[str, object] | None = None
        if schema_raw is not None:
            if not isinstance(schema_raw, Mapping):
                raise ValueError(f"workflow run '{run_id}' node '{key}' result_schema must be an object or null")
            try:
                result_schema = canonical_result_schema(schema_raw)
            except ValueError as exc:
                raise ValueError(f"workflow run '{run_id}' node '{key}' {exc}") from exc
        data_raw = raw.get("data")
        data: Mapping[str, object] | None = None
        if data_raw is not None:
            if not isinstance(data_raw, Mapping):
                raise ValueError(f"workflow run '{run_id}' node '{key}' data must be an object or null")
            try:
                data = own_json_object(data_raw)
            except ValueError as exc:
                raise ValueError(f"workflow run '{run_id}' node '{key}' data is not JSON: {exc}") from exc
        parse_error = _structured_parse_error_from_payload(raw.get("parse_error"), run_id=run_id, key=key)
        return cls(
            key=key,
            status=status,
            depends_on=tuple(depends_on),
            agent=agent,
            child_session_id=child_session_id,
            run_id=run_id_value,
            effective_input_sha256=effective_input_sha256,
            output=output,
            output_sha256=output_sha256,
            output_bytes=output_bytes,
            output_preview=output_preview,
            output_error=output_error,
            history_error=history_error,
            error=error,
            budget_exhaustion=budget_exhaustion,
            result_schema=result_schema,
            data=data,
            parse_error=parse_error,
        )

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "key": self.key,
            "status": self.status,
            "depends_on": list(self.depends_on),
            "agent": self.agent,
            "child_session_id": self.child_session_id,
            "run_id": self.run_id,
            "effective_input_sha256": self.effective_input_sha256,
            "output": self.output,
            "output_sha256": self.output_sha256,
            "output_bytes": self.output_bytes,
            "error": self.error,
        }
        if self.output_preview is not None:
            payload["output_preview"] = self.output_preview
        if self.output_error is not None:
            payload["output_error"] = self.output_error
        if self.history_error is not None:
            payload["history_error"] = self.history_error
        if self.budget_exhaustion is not None:
            payload["budget_exhaustion"] = self.budget_exhaustion.as_payload()
        if self.result_schema is not None:
            payload["result_schema"] = self.result_schema
        if self.data is not None:
            payload["data"] = json_wire_object(self.data)
        if self.parse_error is not None:
            payload["parse_error"] = self.parse_error.as_payload()
        return payload


@dataclass(frozen=True, slots=True)
class WorkflowRun:
    run_id: str
    session_id: str
    workflow: str
    source_path: str
    source_sha256: str
    task: object
    input_sha256: str
    notify_on_completion: bool = False
    status: RunStatus = "running"
    error: str | None = None
    action_id: str | None = None
    nodes: tuple[WorkflowNode, ...] = ()
    result: str | None = None
    result_sha256: str | None = None
    result_bytes: int | None = None
    created_at: int = 0
    updated_at: int = 0
    schema_version: int = SCHEMA_VERSION

    def node(self, key: str) -> WorkflowNode | None:
        for node in self.nodes:
            if node.key == key:
                return node
        return None

    def with_node(self, updated: WorkflowNode) -> WorkflowRun:
        return replace(self, nodes=tuple(updated if node.key == updated.key else node for node in self.nodes), updated_at=_clock_ms())

    def with_status(self, status: RunStatus, error: str | None = None) -> WorkflowRun:
        return replace(self, status=status, error=error, updated_at=_clock_ms())

    @classmethod
    def from_payload(cls, raw: Mapping[str, object]) -> WorkflowRun:
        unknown = sorted(str(key) for key in raw if key not in _RUN_KEYS)
        if unknown:
            raise ValueError(f"workflow run field '{unknown[0]}' is not supported")
        missing = sorted(_RUN_KEYS - raw.keys())
        if missing:
            raise ValueError("workflow run is missing field(s): " + ", ".join(missing))
        schema_version = raw["schema_version"]
        if schema_version != SCHEMA_VERSION:
            raise ValueError(f"workflow run schema_version must be {SCHEMA_VERSION}, got {schema_version!r}")
        run_id = raw["run_id"]
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("workflow run run_id must be a non-empty string")
        session_id = _run_text(raw["session_id"], field="session_id", run_id=run_id)
        workflow = _run_text(raw["workflow"], field="workflow", run_id=run_id)
        source_path = _run_text(raw["source_path"], field="source_path", run_id=run_id)
        source_sha256 = _run_sha256(raw["source_sha256"], field="source_sha256", run_id=run_id)
        task = raw["task"]
        try:
            owned_task: object = own_json_value(task)
        except ValueError:
            raise ValueError(f"workflow run '{run_id}' task must be a JSON value") from None
        input_sha256 = _run_sha256(raw["input_sha256"], field="input_sha256", run_id=run_id)
        notify_on_completion = raw["notify_on_completion"]
        if not isinstance(notify_on_completion, bool):
            raise ValueError(f"workflow run '{run_id}' notify_on_completion must be a boolean")
        status = raw["status"]
        if not _is_run_status(status):
            raise ValueError(f"workflow run '{run_id}' has an unknown status: {status!r}")
        error = raw["error"]
        if error is not None and not isinstance(error, str):
            raise ValueError(f"workflow run '{run_id}' error must be text or null")
        action_id = raw["action_id"]
        if action_id is not None and (not isinstance(action_id, str) or not action_id):
            raise ValueError(f"workflow run '{run_id}' action_id must be an id or null")
        nodes = raw["nodes"]
        if not isinstance(nodes, (list, tuple)):
            raise ValueError(f"workflow run '{run_id}' nodes must be a list")
        parsed = tuple(WorkflowNode.from_payload(item, run_id=run_id) if isinstance(item, Mapping) else _reject_node(run_id) for item in nodes)
        keys = [node.key for node in parsed]
        if len(set(keys)) != len(keys):
            raise ValueError(f"workflow run '{run_id}' has duplicate node keys")
        for node in parsed:
            for dep in node.depends_on:
                if dep not in keys:
                    raise ValueError(f"workflow run '{run_id}' node '{node.key}' depends on unknown key '{dep}'")
        result = raw["result"]
        if result is not None and not isinstance(result, str):
            raise ValueError(f"workflow run '{run_id}' result must be text or null")
        result_sha256 = raw["result_sha256"]
        if result_sha256 is not None and (not isinstance(result_sha256, str) or len(result_sha256) != 64):
            raise ValueError(f"workflow run '{run_id}' result_sha256 must be a sha256 or null")
        result_bytes = raw["result_bytes"]
        if result_bytes is not None and (not isinstance(result_bytes, int) or isinstance(result_bytes, bool) or result_bytes < 0):
            raise ValueError(f"workflow run '{run_id}' result_bytes must be a byte count or null")
        created_at = raw["created_at"]
        updated_at = raw["updated_at"]
        if (
            not isinstance(created_at, int)
            or isinstance(created_at, bool)
            or created_at < 0
            or not isinstance(updated_at, int)
            or isinstance(updated_at, bool)
            or updated_at < 0
        ):
            raise ValueError(f"workflow run '{run_id}' created_at/updated_at must be non-negative integers")
        return cls(
            run_id=run_id,
            session_id=session_id,
            workflow=workflow,
            source_path=source_path,
            source_sha256=source_sha256,
            task=owned_task,
            input_sha256=input_sha256,
            notify_on_completion=notify_on_completion,
            status=status,
            error=error,
            action_id=action_id,
            nodes=parsed,
            result=result,
            result_sha256=result_sha256,
            result_bytes=result_bytes,
            created_at=created_at,
            updated_at=updated_at,
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "session_id": self.session_id,
            "workflow": self.workflow,
            "source_path": self.source_path,
            "source_sha256": self.source_sha256,
            "task": json_wire_value(self.task),
            "input_sha256": self.input_sha256,
            "notify_on_completion": self.notify_on_completion,
            "status": self.status,
            "error": self.error,
            "action_id": self.action_id,
            "nodes": [node.to_payload() for node in self.nodes],
            "result": self.result,
            "result_sha256": self.result_sha256,
            "result_bytes": self.result_bytes,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def _reject_node(run_id: str) -> WorkflowNode:
    raise ValueError(f"workflow run '{run_id}' nodes must be objects")


def _clock_ms() -> int:
    return int(time.time() * 1000)


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(json_wire_value(own_json_value(value)), sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


# --- State document ---


@dataclass(frozen=True, slots=True)
class WorkflowState:
    runs: Mapping[str, WorkflowRun]

    @classmethod
    def from_payload(cls, raw: Mapping[str, object]) -> WorkflowState:
        if raw["schema_version"] != SCHEMA_VERSION:
            raise ValueError(f"workflows state schema_version must be {SCHEMA_VERSION}")
        runs = raw["runs"]
        if not isinstance(runs, Mapping):
            raise ValueError("workflows state 'runs' must be an object")
        parsed: dict[str, WorkflowRun] = {}
        for run_id, item in runs.items():
            if not isinstance(run_id, str) or not run_id:
                raise ValueError("workflow run ids must be non-empty strings")
            if not isinstance(item, Mapping):
                raise ValueError(f"workflow run '{run_id}' must be an object")
            run = WorkflowRun.from_payload(item)
            if run.run_id != run_id:
                raise ValueError(f"workflow run id mismatch: {run_id!r} != {run.run_id!r}")
            parsed[run_id] = run
        return cls(MappingProxyType(parsed))

    def to_payload(self) -> dict[str, object]:
        return {"schema_version": SCHEMA_VERSION, "runs": {run_id: run.to_payload() for run_id, run in self.runs.items()}}

    def with_run(self, run: WorkflowRun) -> WorkflowState:
        runs = dict(self.runs)
        runs[run.run_id] = run
        return WorkflowState(MappingProxyType(runs))


def _store(services: StateServices) -> PluginStateStore[WorkflowState]:
    """This plugin's session-state store: strict keys, typed parse, CAS save."""
    return PluginStateStore(
        services,
        keys=frozenset({"schema_version", "runs"}),
        parse=WorkflowState.from_payload,
        serialize=WorkflowState.to_payload,
        empty=lambda: WorkflowState(MappingProxyType({})),
        unknown=lambda extra: f"workflows state field '{extra[0]}' is not supported",
    )
