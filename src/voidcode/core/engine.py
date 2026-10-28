"""Governed turns over immutable revisions and durable native tool batches."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from threading import Event, Lock
from types import MappingProxyType
from typing import Any, Literal, cast
from uuid import uuid4

import jsonschema
from pydantic import BaseModel, ConfigDict, Field

from ..sdk.codec import (
    decode_provider_result,
    decode_tool_call,
    decode_tool_result,
    encode_message,
    encode_provider_result,
    encode_tool_call,
    encode_tool_definition,
    encode_tool_result,
)
from ..sdk.commands import (
    CommandCatalog,
    CommandDescriptor,
    CommandResult,
)
from ..sdk.hooks import RequestWindow, StopCause, WindowSegment
from ..sdk.icons import Icon
from ..sdk.invocation import ToolContext
from ..sdk.json_values import json_wire_object, own_json_object
from ..sdk.limits import CLIENT_PROMPT_MAX_BYTES
from ..sdk.observed_effects import MAX_OBSERVED_CHANGES, ObservedChange
from ..sdk.provider import ProviderExecutionError, ProviderMessage, ProviderTurnRequest, ProviderTurnResult
from ..sdk.services import (
    ChildOutcome,
    ChildSendReceipt,
    ContinuationResult,
    PluginBudget,
    SessionUsageReport,
)
from ..sdk.tools import (
    ResourceChange,
    TextOutput,
    ToolCall,
    ToolDefinition,
    ToolEffect,
    ToolFailure,
    ToolResult,
    ToolSuccess,
    denied_under_restriction,
    has_external_mutations,
)
from ..sdk.transcript import ContextHistory, TranscriptPage
from ..sdk.views import PluginViewDescriptor, ViewsSnapshot
from .activation import (
    Admission,
    ExecutionConfig,
    StateCopyPlan,
    load_private_snapshot,
    plan_state_copies,
    write_private_snapshot,
)
from .artifacts import ArtifactStore
from .coverage import _target_paths, canonical_path, model_result_text
from .delegation import (
    DELEGATION_SPAWN_BUDGET as DELEGATION_SPAWN_BUDGET,
)
from .delegation import (
    DelegationManager,
    _child_run_status,
    _terminal_run_result,
)
from .events import CHILD_MESSAGE_OWNER, EventType, child_completion_notice_id
from .facts import FACT_EVENT_TYPES as _FACT_EVENT_TYPES
from .facts import fact_for_event
from .plugins import LeaseContext, PluginRuntime, PluginSource, ToolInvocationOutcome
from .sandbox import SandboxBackend
from .storage.store import (
    Checkpoint,
    ContinuationRecord,
    EventDraft,
    ObserverReceiptRecord,
    RootBudgetExhausted,
    SequenceConflict,
    SessionRecord,
    SqliteStore,
    StagedBudgetUpdate,
    StagedCancellation,
    StagedConfirmation,
    StagedContinuationDraft,
    StagedStateUpdate,
    StoreConflict,
    StoredEvent,
    canonical_json,
)


class Cancellation:
    def __init__(self) -> None:
        self._event = Event()
        self._reason: str | None = None
        self._lock = threading.Lock()
        self._stop_cause: StopCause = "user_stop"

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str | None:
        with self._lock:
            return self._reason

    @property
    def stop_cause(self) -> StopCause:
        with self._lock:
            return self._stop_cause

    def set_cancelled(self, value: bool, *, reason: str | None = None, stop_cause: StopCause = "user_stop") -> None:
        with self._lock:
            self._reason = reason if value else None
            self._stop_cause = stop_cause if value else "user_stop"
            if value:
                self._event.set()
            else:
                self._event.clear()


class _EventAbortSignal:
    """A `ProviderAbortSignal` view over one background action's abort event.

    The driver owns the event; callbacks only observe it. `set_cancelled` mirrors into
    the shared event so cooperative tools can signal, but clearing is refused: an abort
    once observed must stay observed for every later invoke of the same action.
    """

    def __init__(self, event: threading.Event) -> None:
        self._event = event
        self._reason: str | None = None
        self._lock = threading.Lock()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str | None:
        with self._lock:
            return self._reason

    def set_cancelled(self, value: bool, *, reason: str | None = None) -> None:
        with self._lock:
            if value:
                self._reason = reason
                self._event.set()
            elif not self._event.is_set():
                self._reason = None


class _BackgroundContext:
    """The long-lived driver handle for one claimed background action.

    The driver keeps only this object across its whole run. Each `invoke` opens a fresh
    owner/revision-bound lease, runs the callback, and commits its staged work atomically —
    so progress is durable invoke by invoke, never at the end of the run. Commits never
    take the session run lock: the driver must not block (or be blocked by) model turns.
    """

    def __init__(
        self,
        core: Core,
        *,
        action_id: str,
        owner: str,
        session_id: str,
        action: Mapping[str, object],
        revision_id: str,
        abort: threading.Event,
    ) -> None:
        self._core = core
        self._action_id = action_id
        self._owner = owner
        self._session_id = session_id
        self._action = own_json_object(action)
        self._revision_id = revision_id
        self._abort = abort

    @property
    def action_id(self) -> str:
        return self._action_id

    @property
    def payload(self) -> Mapping[str, object]:
        return self._action

    @property
    def abort_signal(self) -> threading.Event:
        return self._abort

    def invoke[T](self, callback: Callable[[ToolContext, Any], T]) -> T:
        return self._core._invoke_background_action(
            self._owner,
            self._session_id,
            callback,
            action_id=self._action_id,
            revision_id=self._revision_id,
            abort=self._abort,
        )


class ActivationFenced(RuntimeError):
    """A dispatch or activation raced the activation fence; retry after it settles."""


def _wire_object(value: object, what: str) -> Mapping[str, object]:
    """Require a JSON object from a durable payload, failing visibly when malformed."""
    if not isinstance(value, Mapping):
        raise ValueError(f"{what} must be a JSON object")
    return json_wire_object(value)


_PARKED_INTERVENING_TYPES = frozenset(
    {
        EventType.COMMAND_COMPLETED,
        EventType.CONFIRMATION_REQUESTED,
        EventType.CONFIRMATION_DECIDED,
        EventType.CHILD_SPAWNED,
        EventType.CHILD_RESERVED,
        EventType.CHILD_FINISHED,
        EventType.MODEL_QUERY,
        EventType.TOOL_DEFERRED,
        EventType.TOOL_PROGRESS,
        EventType.PROVIDER_PROGRESS,
        EventType.OBSERVER_ERROR,
        EventType.WINDOW_PROJECTED,
        EventType.PROVIDER_INTERRUPTED,
        EventType.ACTIVATION_REQUESTED,
        EventType.ACTIVATION_COMMITTED,
        EventType.ACTIVATION_FAILED,
    }
)


_CORE_COMMAND_SPECS: tuple[tuple[str, str, Mapping[str, object], tuple[ToolEffect, ...], bool], ...] = (
    (
        "run",
        "Run an agent turn with a user prompt",
        {
            "type": "object",
            "properties": {"prompt": {"type": "string", "minLength": 1, "maxLength": 65536}},
            "required": ["prompt"],
            "additionalProperties": False,
        },
        (ToolEffect.EXECUTE, ToolEffect.READ, ToolEffect.WRITE),
        True,
    ),
    (
        "resume",
        "Resume a paused session (approval or interrupted)",
        {
            "type": "object",
            "properties": {
                "approval_request_id": {"type": "string", "minLength": 1},
                "decision": {"enum": ["allow", "deny"]},
            },
            "additionalProperties": False,
        },
        (ToolEffect.EXECUTE, ToolEffect.READ, ToolEffect.WRITE),
        True,
    ),
    (
        "show",
        "Show session state, checkpoint, and events",
        {"type": "object", "properties": {"events": {"type": "boolean"}}, "additionalProperties": False},
        (ToolEffect.READ,),
        False,
    ),
    ("status", "Show workspace status", {"type": "object", "additionalProperties": False}, (ToolEffect.READ,), False),
    ("help", "Show available commands and documentation", {"type": "object", "additionalProperties": False}, (ToolEffect.READ,), False),
)
_CORE_COMMAND_NAMES = frozenset(name for name, _d, _s, _e, _g in _CORE_COMMAND_SPECS)

#: Tools the core itself dispatches. A plugin can never shadow these names.
_CORE_TOOL_SPECS: tuple[tuple[str, str, Mapping[str, object], tuple[ToolEffect, ...], Icon], ...] = (
    (
        "core_reload",
        "Request that the configured plugins be re-admitted as a new capability revision. This only stages the "
        "request: the switch happens after the current tool batch finishes, and it needs user approval first.",
        {
            "type": "object",
            "properties": {"reason": {"type": "string", "maxLength": 500}},
            "additionalProperties": False,
        },
        (ToolEffect.EXECUTE,),
        Icon(glyph="\u21bb", fallback="R"),
    ),
)
CORE_TOOL_NAMES = frozenset(name for name, _d, _s, _e, _i in _CORE_TOOL_SPECS)


#: One truthful, actionable message for a providerless core. Launch, commands, tools, and
#: sessions stay available; only a model turn or governed query fails with this text.
MODEL_CONFIGURATION_GUIDANCE = "no model configured or registered"

#: At most this many supervisor-observed changes join one tool result at merge time. The
#: helper already caps its own report at MAX_OBSERVED_CHANGES; this only bounds multi-child
#: accumulation within one invocation.
MAX_MERGED_OBSERVATIONS = MAX_OBSERVED_CHANGES
#: Reserved core command that activates the configured sources on explicit request.
RELOAD_COMMAND_NAME = "core.reload"
_RELOAD_COMMAND_SPEC: tuple[str, str, Mapping[str, object], tuple[ToolEffect, ...], bool] = (
    RELOAD_COMMAND_NAME,
    "Re-admit the configured plugin sources and switch this workspace to the new capability revision.",
    {"type": "object", "properties": {}, "additionalProperties": False},
    (ToolEffect.EXECUTE, ToolEffect.READ, ToolEffect.WRITE),
    True,
)


def _core_command_descriptors(*, with_reload: bool) -> tuple[CommandDescriptor, ...]:
    """Reserved core command descriptors, captured together with the revision that admits them."""
    specs = (*_CORE_COMMAND_SPECS, _RELOAD_COMMAND_SPEC) if with_reload else _CORE_COMMAND_SPECS
    return tuple(
        CommandDescriptor(
            name=name,
            owner="core",
            description=description,
            argument_schema=dict(schema),
            effects=effects,
            busy_gate=busy_gate,
        )
        for name, description, schema, effects, busy_gate in specs
    )


def _core_tool_definitions() -> dict[str, ToolDefinition]:
    return {
        name: ToolDefinition(name=name, description=description, input_schema=dict(schema), effects=frozenset(effects), icon=icon)
        for name, description, schema, effects, icon in _CORE_TOOL_SPECS
    }


@dataclass(frozen=True, slots=True)
class _Snapshot:
    """One immutable capability generation. Every dispatch reads this single pointer."""

    revision_id: str
    config: ExecutionConfig
    admission: Admission | None
    plugin_runtime: PluginRuntime
    native_tools: Mapping[str, ToolDefinition]
    native_validators: Mapping[str, Any]
    native_commands: Mapping[str, CommandDescriptor]
    tools: tuple[ToolDefinition, ...]
    commands: tuple[CommandDescriptor, ...]

    @staticmethod
    def build(
        *,
        revision_id: str,
        config: ExecutionConfig,
        admission: Admission | None,
        plugin_runtime: PluginRuntime,
        with_reload: bool,
    ) -> _Snapshot:
        native_tools = MappingProxyType(_core_tool_definitions()) if with_reload else MappingProxyType({})
        for name in plugin_runtime.tool_definitions:
            if name in native_tools:
                raise ValueError(f"plugin tool {name!r} collides with a reserved tool")
        native_validators_dict = {}
        for name, definition in native_tools.items():
            if definition.input_schema:
                schema_json = json_wire_object(definition.input_schema)
                jsonschema.Draft202012Validator.check_schema(schema_json)
                native_validators_dict[name] = jsonschema.Draft202012Validator(schema_json)
        native_validators = MappingProxyType(native_validators_dict)
        native_commands = MappingProxyType({desc.name: desc for desc in _core_command_descriptors(with_reload=with_reload)})
        return _Snapshot(
            revision_id=revision_id,
            config=config,
            admission=admission,
            plugin_runtime=plugin_runtime,
            native_tools=native_tools,
            native_validators=native_validators,
            native_commands=native_commands,
            tools=(*plugin_runtime.tools, *native_tools.values()),
            commands=(*native_commands.values(), *plugin_runtime.commands),
        )

    def definition_of(self, name: str) -> ToolDefinition | None:
        return self.native_tools.get(name) or self.plugin_runtime.tool_definitions.get(name)

    def validator_of(self, name: str) -> Any | None:
        return self.native_validators.get(name) or self.plugin_runtime.tool_validators.get(name)

    def command_of(self, name: str) -> CommandDescriptor | None:
        return self.native_commands.get(name) or self.plugin_runtime.command_descriptors.get(name)

    def owner_of(self, tool_name: str) -> str:
        return "core" if tool_name in self.native_tools else self.plugin_runtime.owner(tool_name)


def _is_parked_intervening(event: StoredEvent) -> bool:
    """Whether an event may legitimately appear after a parked checkpoint."""
    return event.event_type in _PARKED_INTERVENING_TYPES or event.event_type.startswith("plugin.")


def _reserved_cancelled(events: list[StoredEvent]) -> bool:
    """Whether a cancel claimed this child before its first run ever started.

    A reserved child that never ran holds an owner-cancel interruption and no user
    message at all. Any user message proves a real run began, so a late driver must
    not mistake ordinary history for a pre-run cancellation.
    """
    cancelled = False
    for event in events:
        if event.event_type == EventType.USER_MESSAGE:
            return False
        if event.event_type == EventType.RUN_INTERRUPTED and event.payload.get("stop_cause") == "owner_cancel":
            cancelled = True
    return cancelled


def _tool_calls_from_content(content: str, *, provider_name: str, model_name: str) -> tuple[ToolCall, ...]:
    """Decode one whole-message tool content event into turn tool calls."""
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ProviderExecutionError(
            kind="provider_error",
            provider_name=provider_name,
            model_name=model_name,
            message="provider stream tool content was not JSON",
            retryable=False,
        ) from exc
    if isinstance(payload, Mapping) and isinstance(payload.get("tool_calls"), list):
        items = payload["tool_calls"]
    elif isinstance(payload, Mapping):
        items = [payload]
    else:
        raise ProviderExecutionError(
            kind="provider_error",
            provider_name=provider_name,
            model_name=model_name,
            message="provider stream tool content was not an object",
            retryable=False,
        )
    calls: list[ToolCall] = []
    for item in items:
        if not isinstance(item, Mapping) or not isinstance(item.get("tool_name"), str) or not item["tool_name"]:
            raise ProviderExecutionError(
                kind="provider_error",
                provider_name=provider_name,
                model_name=model_name,
                message="provider stream tool content was malformed",
                retryable=False,
            )
        arguments = item.get("arguments", {})
        if not isinstance(arguments, Mapping):
            raise ProviderExecutionError(
                kind="provider_error",
                provider_name=provider_name,
                model_name=model_name,
                message="provider stream tool content arguments were not an object",
                retryable=False,
            )
        call_id = item.get("tool_call_id")
        if call_id is not None and (not isinstance(call_id, str) or not call_id):
            raise ProviderExecutionError(
                kind="provider_error",
                provider_name=provider_name,
                model_name=model_name,
                message="provider stream tool content id was malformed",
                retryable=False,
            )
        calls.append(ToolCall(tool_name=item["tool_name"], arguments=dict(arguments), tool_call_id=call_id))
    return tuple(calls)


def _tool_call_from_entry(entry: Mapping[str, object]) -> ToolCall:
    """One aggregated lifecycle entry into its turn tool call; malformed entries fail the turn."""
    name = entry.get("tool_name")
    arguments = entry.get("arguments")
    call_id = entry.get("tool_call_id")
    if not isinstance(name, str) or not name or not isinstance(arguments, Mapping):
        raise ProviderExecutionError(
            kind="provider_error",
            provider_name="",
            model_name="",
            message="provider stream ended with an incomplete tool call",
            retryable=False,
        )
    if call_id is not None and (not isinstance(call_id, str) or not call_id):
        raise ProviderExecutionError(
            kind="provider_error",
            provider_name="",
            model_name="",
            message="provider stream tool call id was malformed",
            retryable=False,
        )
    return ToolCall(tool_name=name, arguments=dict(arguments), tool_call_id=call_id)


class _Cursor(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    batch_sequence: int | None = Field(default=None, ge=1)
    next_call: int = Field(default=0, ge=0)
    intent_sequence: int | None = Field(default=None, ge=1)
    approval_sequence: int | None = Field(default=None, ge=1)
    #: A plugin-requested call, executed only after its caller's callback has returned.
    deferred_sequence: int | None = Field(default=None, ge=1)
    deferred_depth: int = Field(default=0, ge=0)
    deferred_only: bool = False
    step: int = Field(default=0, ge=0)
    abandon_batch: bool = False
    continuation_id: str | None = None
    logical_turn: str = ""
    stop_cause: StopCause | None = None
    initial_root_budget: int | None = None
    root_id: str = ""
    #: Staged continuation this session owes itself: an activation request id or an upgrade pause id.
    resume_intent: str = ""


@dataclass(frozen=True, slots=True)
class CoreResult:
    session_id: str
    run_id: str
    revision_id: str
    status: str
    output: str | None = None
    error: str | None = None
    pending_approval: Mapping[str, object] | None = None
    stop_cause: StopCause | None = None

    def as_payload(self) -> dict[str, object]:
        payload = {
            "session_id": self.session_id,
            "run_id": self.run_id,
            "revision_id": self.revision_id,
            "status": self.status,
            "output": self.output,
            "error": self.error,
            "pending_approval": None if self.pending_approval is None else json_wire_object(self.pending_approval),
        }
        if self.stop_cause is not None:
            payload["stop_cause"] = self.stop_cause
        return payload


@dataclass(slots=True)
class _Execution:
    session: SessionRecord
    cursor: _Cursor
    events: list[StoredEvent]
    run_id: str
    lifecycle_started: bool = False


@dataclass(frozen=True, slots=True)
class _PreparedActivation:
    """A validated candidate generation, its state copies, and the generation it was prepared against."""

    admission: Admission
    plugin_runtime: PluginRuntime
    copies: StateCopyPlan
    source: str
    prepared_against: str


class Core:
    def __init__(
        self,
        *,
        workspace: Path,
        state_dir: Path,
        config: ExecutionConfig,
        plugins: tuple[PluginSource, ...],
        revision_id: str,
        revision_payload: Mapping[str, object],
        auto_drain: bool = True,
        admission: Admission | None = None,
        admits: Callable[..., Admission] | None = None,
        sandbox_backend: SandboxBackend | None = None,
    ) -> None:
        self.auto_drain = auto_drain
        self.workspace = workspace.resolve(strict=True)
        self._abort = Cancellation()
        self._closed = False
        # One run lock per session: independent sessions (delegated children, cron firings, a
        # user turn beside a background run) execute concurrently in the same workspace. The
        # activation barrier does not depend on this lock; it uses the dispatch fence and the
        # in-transaction blocker check.
        self._session_locks: dict[str, Lock] = {}
        self._session_locks_guard = Lock()
        self._session_aborts: dict[str, Cancellation] = {}
        self._aborts_lock = Lock()
        # Background action drivers: claimed-but-unsettled action ids this process drives.
        # Cross-process double-driving is impossible: construction holds an exclusive
        # workspace flock (`another process owns this workspace`), so a claimed row this
        # process does not drive is necessarily stale, never a live foreign claim.
        self._background_live: set[str] = set()
        self._background_aborts: dict[str, threading.Event] = {}
        self._background_cond = threading.Condition()
        self._background_identity = {"pid": os.getpid(), "core": uuid4().hex}
        # One pointer per capability generation, plus short-critical-section call accounting.
        # The activation fence rejects new dispatches and reports foreign in-flight calls as
        # busy instead of blocking any run, and no lock is ever held across an RPC.
        self._calls_lock = threading.Condition()
        self._active_calls: dict[str, str] = {}
        self._activating_thread: int | None = None
        # ponytail: bounded in-process cache of per-revision observed_facts; a large
        # multi-revision workspace could grow it, then key it by revision only.
        self._observer_eligibility: dict[tuple[str, str], tuple[str, ...]] = {}
        self._state_dir = state_dir
        self._admits = admits
        self._delegation = DelegationManager(self)
        self._artifacts = ArtifactStore(state_dir / "artifacts")
        with ExitStack() as resources:
            # Lock the directory inode, independent of the selected database path.
            # Opening/locking it does not create or rewrite workspace files.
            lease = os.open(self.workspace, os.O_RDONLY | os.O_DIRECTORY)
            resources.callback(os.close, lease)
            try:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("another process owns this workspace") from exc
            self._lease_fd = lease
            state_dir.mkdir(parents=True, exist_ok=True)
            self.store = SqliteStore(state_dir / "voidcode.db")
            resources.callback(self.store.close)
            runtime = PluginRuntime(
                plugins,
                workspace=self.workspace,
                revision_id=revision_id,
                store=self.store,
                usage_callback=self._get_session_usage,
                budget_callback=self._get_owner_budget,
                spawn_child_callback=lambda parent, prompt, label, max_steps, scope, readonly, notify, owner, key: self.spawn_child(
                    parent,
                    prompt,
                    label=label,
                    tool_allowlist=scope,
                    max_steps=max_steps,
                    readonly=readonly,
                    notify_on_completion=notify,
                    owner=owner,
                    idempotency_key=key,
                ),
                find_child_callback=self._delegation.find_keyed_child,
                join_child_callback=lambda parent, child, timeout: self.join_child(parent, child, timeout_seconds=timeout).as_payload(),
                send_child_callback=lambda parent, child, prompt, label, notify: self.send_child(
                    parent,
                    child,
                    prompt,
                    label=label,
                    notify_on_completion=notify,
                ).as_payload(),
                cancel_child_callback=self.cancel_child,
                cancel_background_action_callback=self.cancel_background_action,
                model_query_callback=self.governed_model_query,
                progress_callback=self._report_progress,
                tool_definition_callback=self._visible_tool_definition,
                artifacts_dir=self._state_dir / "artifacts",
                sandbox_backend=sandbox_backend,
                process_spool_dir=self._process_spool_dir(),
            )
            # A runtime whose registry cannot be published is closed here: nothing may outlive a
            # failed construction, and the late-bound cleanup callback is registered only after
            # the generation that owns this runtime is complete.
            try:
                if admission is not None and admission.validate is not None:
                    admission.validate(runtime.providers)
                self.store.put_revision(revision_payload, revision_id=revision_id)
                if admission is not None and admission.revision_id != revision_id:
                    raise ValueError("admission does not describe the generation it was opened with")
                self._snapshot = _Snapshot.build(
                    revision_id=revision_id,
                    config=config,
                    admission=admission,
                    plugin_runtime=runtime,
                    with_reload=admits is not None,
                )
                self._assert_state_schema_compatible()
            except BaseException:
                runtime.close()
                raise
            # Late-bound so the callback closes whatever generation is current at shutdown.
            resources.callback(self._close_runtime)
            # Initialization work still owns the resources: a failure here unwinds the stack
            # (runtime, store, workspace lease) instead of leaking an already-detached core.
            self._replay_pending_observers()
            self._background_started = False
            self._background_workers: list[threading.Thread] = []
            resources.callback(self._stop_background_work)
            if self.auto_drain:
                self._start_background_work()
            # Adopt the generation LAST: a failed cold start must leave the workspace unadopted, so
            # fixing the same authorized source and retrying works without deleting any database.
            self.store.record_workspace_revision(str(self.workspace), revision_id)
            self._resources = resources.pop_all()

    def _process_spool_dir(self) -> Path:
        """Stable runtime-owned spool ancestor for confined logs: state_dir/process-spool (0700)."""
        spool = self._state_dir / "process-spool"
        spool.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(spool, 0o700)
        except OSError:
            pass
        return spool

    def _assert_state_schema_compatible(self) -> None:
        """Refuse to reinterpret stored owner state under a different declared schema."""
        states = self._snapshot.plugin_runtime.state_metadata()
        scopes: list[tuple[str, str]] = [("workspace", str(self.workspace))]
        scopes.extend(("session", record.session_id) for record in self.store.sessions(workspace=str(self.workspace)))
        for scope, scope_key in scopes:
            for row in self.store.plugin_state_rows(scope, scope_key):
                state = states.get(row.owner)
                if state is None or state.version == row.schema_version:
                    continue
                raise ValueError(f"plugin {row.owner} state schema mismatch: stored v{row.schema_version}, declared v{state.version}")

    def _start_background_work(self) -> None:
        """Start the continuation driver, the wakeup ticker, and the background loop, exactly once."""
        with self._session_locks_guard:
            if self._closed or self._background_started:
                return
            self._background_started = True
            self._wire_background_runtime(self._snapshot.plugin_runtime)
            for target, name in (
                (self._drive_pending_activations, "CoreActivationDriver"),
                (self._wakeup_loop, "CoreWakeupTicker"),
                (self._background_loop, "CoreBackgroundLoop"),
            ):
                worker = threading.Thread(target=target, name=name, daemon=True)
                worker.start()
                self._background_workers.append(worker)

    def _wire_background_runtime(self, runtime: PluginRuntime) -> None:
        """Bind one runtime's background projections to this core's live driver registry."""
        runtime.background_action_notify_callback = self._notify_background_loop
        runtime.background_claim_live_callback = self._background_claim_live
        runtime.background_live_actions = set(self._background_live)

    def _notify_background_loop(self) -> None:
        with self._background_cond:
            self._background_cond.notify_all()

    def _background_claim_live(self, record: ContinuationRecord) -> bool:
        """Whether one claimed background row is driven by this process right now.

        Cross-process liveness needs no detection: construction holds the workspace's
        exclusive flock, so no second process can hold a claim on this database.
        """
        with self._background_cond:
            return record.continuation_id in self._background_live

    def _park_background_drivers(self, *, reason: str) -> None:
        """Signal every live driver to stop at its next invoke boundary, then wait for them.

        Drivers settle their own rows (cancelled); rows whose driver never reaches a
        boundary stay claimed-but-unattended and report `interrupted`. Nothing is
        auto-replayed: resume is always an explicit new action.
        """
        _ = reason
        with self._background_cond:
            aborts = list(self._background_aborts.values())
            for abort in aborts:
                abort.set()
            self._background_cond.notify_all()
        deadline = time.monotonic() + 30.0
        with self._background_cond:
            while self._background_live and time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                self._background_cond.wait(timeout=min(remaining, 0.2))

    def _stop_background_work(self) -> None:
        """Join coordination workers before runtime/store callbacks close their resources."""
        self.cancel("core shutdown", cause="shutdown")
        self._park_background_drivers(reason="core shutdown")
        with self._session_locks_guard:
            self._closed = True
            workers, self._background_workers = self._background_workers, []
        with self._background_cond:
            self._background_cond.notify_all()
        for worker in workers:
            worker.join()

    @contextmanager
    def _session_lock(self, session_id: str) -> Iterator[None]:
        """Serialize work of ONE session; different sessions never block each other."""
        with self._session_locks_guard:
            lock = self._session_locks.get(session_id)
            if lock is None:
                lock = Lock()
                self._session_locks[session_id] = lock
        with lock:
            yield

    def _drain_session_locks(self) -> None:
        """Wait for every in-flight session run to finish; callers have already set `_closed`."""
        with self._session_locks_guard:
            locks = list(self._session_locks.values())
        for lock in locks:
            with lock:
                continue

    def _close_runtime(self) -> None:
        self._snapshot.plugin_runtime.close()

    @property
    def plugin_runtime(self) -> PluginRuntime:
        return self._snapshot.plugin_runtime

    @property
    def config(self) -> ExecutionConfig:
        return self._snapshot.config

    @property
    def admission(self) -> Admission | None:
        return self._snapshot.admission

    @property
    def revision_id(self) -> str:
        return self._snapshot.revision_id

    @contextmanager
    def _dispatch(self, description: str) -> Iterator[_Snapshot]:
        """Capture the current generation and hold a call slot until the caller's result commits.

        The slot is what makes an activation safe: a switch is refused while any foreign
        dispatch is in flight, and refused-for-new once the fence is set, so no result can be
        committed under a generation other than the one that produced it.
        """
        call_id = uuid4().hex
        with self._calls_lock:
            if self._activating_thread is not None and self._activating_thread != threading.get_ident():
                raise ActivationFenced("capability activation is in progress; retry")
            snapshot = self._snapshot
            self._active_calls[call_id] = description
        try:
            yield snapshot
        finally:
            with self._calls_lock:
                self._active_calls.pop(call_id, None)
                self._calls_lock.notify_all()

    def __enter__(self) -> Core:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def cancel_session(self, session_id: str, reason: str = "user cancellation", *, cause: StopCause = "user_stop") -> bool:
        """Signal cancellation only to the currently executing run of a specific session."""
        self._ensure_open()
        # Ownership first: a session id from another workspace sharing this
        # database may never be signalled or settled.
        self.session(session_id)
        with self._aborts_lock:
            abort = self._session_aborts.get(session_id)
        if abort is not None and not abort.cancelled:
            abort.set_cancelled(True, reason=reason, stop_cause=cause)
            return True
        return self._settle_parked_cancel(session_id, reason, cause)

    def _settle_parked_cancel(self, session_id: str, reason: str, cause: StopCause) -> bool:
        """Settle parked work at its already-safe boundary.

        Parked work is an approval wait or a recoverable (non-terminal)
        interruption with no active abort in this process: an explicit user or
        owner cancellation ends it here instead of leaving a session that
        refuses fresh work AND cannot resume. The session must belong to this
        workspace and carry its own durable run identity, and claimed
        continuations are cancelled in the SAME transaction as the terminal
        checkpoint, so no crash can leave a cancelled claim next to a resumable
        session or vice versa. The run lock is deliberately not taken: an
        unrelated run may be executing in another thread, and the store's CAS on
        (sequence, run) is what keeps this settle atomic.
        """
        session = self.session(session_id)
        checkpoint = self.store.checkpoint(session_id)
        parked_run_id = self._parked_run_id(session, checkpoint)
        if parked_run_id is None:
            return False
        cursor = _Cursor()
        if checkpoint is not None and isinstance(checkpoint.payload, Mapping):
            cursor = _Cursor.model_validate(json_wire_object(checkpoint.payload))
        execution = _Execution(session, cursor, list(self.store.events(session_id)), parked_run_id)
        parked_claim = (
            next((c for c in self.store.claimed_continuations(session_id, limit=100) if c.continuation_id == cursor.continuation_id), None)
            if cursor.continuation_id is not None
            else None
        )
        try:
            if cursor.batch_sequence is not None or cursor.deferred_sequence is not None:
                # The parked batch is never replayed: report its members as interrupted first.
                self._interrupt_batch(execution)
            self._commit(
                execution,
                "interrupted",
                "terminal",
                EventDraft(
                    EventType.RUN_INTERRUPTED,
                    "kernel",
                    "kernel",
                    {
                        "reason": reason,
                        "stop_cause": cause,
                        "logical_turn": execution.cursor.logical_turn,
                        **self._measured_run_elapsed_fields(execution),
                    },
                ),
                staged_cancellations=(
                    (StagedCancellation(owner=parked_claim.owner, continuation_id=parked_claim.continuation_id),) if parked_claim is not None else ()
                ),
            )
        except StoreConflict:
            # The parked session moved on under another writer; settling is moot.
            return False
        return True

    def _parked_run_id(self, session: SessionRecord, checkpoint: Checkpoint | None) -> str | None:
        """The run identity of parked work this session may safely settle, else None.

        Parked work is an approval wait or a recoverable interruption, and it must
        carry the session's own durable run identity with a non-terminal checkpoint
        of that same run. A terminal checkpoint (or a finished session) holds
        nothing to settle, but a session that still claims parked work without an
        authentic identity is a corrupt state and fails visibly here - before any
        write - instead of being reported as an ordinary no-work cancellation.
        """
        if session.status not in ("waiting", "interrupted"):
            return None
        if checkpoint is not None and checkpoint.kind == "terminal":
            return None
        run_id = session.run_id
        if not run_id or checkpoint is None or checkpoint.run_id != run_id:
            raise ValueError(f"session {session.session_id!r} reports parked {session.status} work without its authentic run identity")
        return run_id

    def _admits_fresh_work(self, session: SessionRecord) -> bool:
        """Whether a session accepts a fresh user turn or auto-driven follow-up work.

        Idle, finished, and failed sessions are open, and so is a terminal
        interruption of the session's own run. Anything else still holds
        unfinished native work that only an explicit resume may re-enter.
        """
        if session.status in {"idle", "completed", "failed"}:
            return True
        checkpoint = self.store.checkpoint(session.session_id)
        return session.status == "interrupted" and checkpoint is not None and checkpoint.kind == "terminal" and checkpoint.run_id == session.run_id

    def cancel(self, reason: str = "user cancellation", *, cause: StopCause = "user_stop") -> None:
        self._abort.set_cancelled(True, reason=reason, stop_cause=cause)
        with self._aborts_lock:
            for ab in self._session_aborts.values():
                ab.set_cancelled(True, reason=reason, stop_cause=cause)

    def close(self) -> None:
        self.cancel("core shutdown", cause="shutdown")
        with self._session_locks_guard:
            if self._closed:
                return
            self._closed = True
        self._delegation.close()
        # No new session work can start (run/resume check `_closed` under their lock), so
        # draining every session lock now waits only for work that was already admitted.
        self._drain_session_locks()
        snapshot = self._snapshot
        if snapshot is not None:
            # The core's own shutdown is an owner shutdown: supervised background processes end
            # with it. A reload keeps them: the runtime swap is not the owner going away.
            snapshot.plugin_runtime.stop_background_processes()
        self._resources.close()

    def run(self, prompt: str, *, session_id: str | None = None, max_steps: int = 32) -> CoreResult:
        return self._run(prompt, session_id=session_id, max_steps=max_steps, prompt_max_bytes=CLIENT_PROMPT_MAX_BYTES)

    def _run(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        max_steps: int = 32,
        prompt_max_bytes: int,
        _refuse_reserved_cancelled: bool = False,
    ) -> CoreResult:
        # a core-driven child run carries a delegated payload (e.g. a review diff).
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode("utf-8")) > prompt_max_bytes:
            raise ValueError(f"prompt must be nonempty UTF-8 text within {prompt_max_bytes // 1024} KiB")
        self._validate_steps(max_steps)
        if session_id is not None and (not isinstance(session_id, str) or not session_id or len(session_id) > 128 or "\0" in session_id):
            raise ValueError("session_id must be nonempty text within 128 characters without NUL")
        session_id = uuid4().hex if session_id is None else session_id
        with self._session_lock(session_id):
            self._ensure_open()
            self._abort.set_cancelled(False)
            abort = Cancellation()
            with self._aborts_lock:
                self._session_aborts[session_id] = abort
            try:
                try:
                    session = self.store.session(session_id)
                except KeyError:
                    session = self._create_session_row(session_id)
                if not self._admits_fresh_work(session):
                    raise ValueError("unfinished session requires explicit resume")
                self._check_session(session)
                root_id = uuid4().hex
                self.store.ensure_root_budget(root_id, max_steps)
                execution = _Execution(
                    session,
                    _Cursor(root_id=root_id, initial_root_budget=max_steps),
                    list(self.store.events(session_id)),
                    uuid4().hex,
                )
                if _refuse_reserved_cancelled and _reserved_cancelled(execution.events):
                    # A cancel claimed this reserved child before its first run started
                    # (checked under the session lock, so a late driver can never slip
                    # past the claim): report the cancellation instead of starting work.
                    return CoreResult(
                        session_id, "", self.revision_id, "interrupted", error="child was cancelled before its first run", stop_cause="owner_cancel"
                    )
                if session.status == "interrupted":
                    # Finish the abandoned stopped prefix on its own cursor first,
                    # then start the new user turn with a fresh cursor: it must not
                    # inherit a stale continuation claim or a spent chain budget.
                    previous = self.store.checkpoint(session_id)
                    if previous is not None and isinstance(previous.payload, Mapping):
                        execution.cursor = _Cursor.model_validate(json_wire_object(previous.payload))
                        execution.cursor.step = 0
                        execution.cursor.stop_cause = None
                    if execution.cursor.batch_sequence is not None or execution.cursor.deferred_sequence is not None:
                        # The stopped batch is never replayed: report its members before the new turn.
                        self._interrupt_batch(execution)
                    # The new user turn owns its own fresh root budget: the stopped
                    # chain's exhausted root is never reused or reset.
                    execution.cursor = _Cursor(root_id=root_id, initial_root_budget=max_steps)
                self._commit(
                    execution, "running", "ready", EventDraft(EventType.USER_MESSAGE, "client", "user", {"content": prompt}), start_turn=True
                )
                result = self._drive(execution, max_steps=max_steps)
            finally:
                with self._aborts_lock:
                    self._session_aborts.pop(session_id, None)
        self._drive_pending(session_id, max_steps)
        return result

    def resume(
        self,
        session_id: str,
        *,
        approval_request_id: str | None = None,
        decision: Literal["allow", "deny"] | None = None,
        max_steps: int = 32,
    ) -> CoreResult:
        self._validate_steps(max_steps)
        with self._session_lock(session_id):
            self._ensure_open()
            self._abort.set_cancelled(False)
            abort = Cancellation()
            with self._aborts_lock:
                self._session_aborts[session_id] = abort
            resumed_notice: tuple[str, str] | None = None
            try:
                session = self.store.session(session_id)
                self._check_session(session)
                checkpoint = self.store.checkpoint(session_id)
                if checkpoint is None or checkpoint.kind == "terminal":
                    raise ValueError("session has no resumable checkpoint")
                if (
                    checkpoint.revision_id != self.revision_id
                    or checkpoint.run_id != session.run_id
                    or checkpoint.last_event_sequence > session.last_event_sequence
                ):
                    raise ValueError("checkpoint identity does not match the durable session")
                events_after_cp = self.store.events(session_id, after_sequence=checkpoint.last_event_sequence)
                for ev in events_after_cp:
                    if not _is_parked_intervening(ev):
                        raise ValueError(f"Intervening event {ev.event_type} violates parked checkpoint prefix")
                cursor = _Cursor.model_validate(json_wire_object(checkpoint.payload))
                execution = _Execution(session, cursor, list(self.store.events(session_id)), uuid4().hex)
                self._messages(execution, allow_pending=True)
                if cursor.intent_sequence is not None:
                    intent = self._event(execution, cursor.intent_sequence, EventType.TOOL_INTENT)
                    batch, call = self._pending(execution)
                    if intent.payload["batch_sequence"] != batch.sequence or json_wire_object(intent.payload)["call"] != encode_tool_call(call):
                        raise ValueError("pending intent does not match its native batch member")
                approval: StoredEvent | None = None
                if cursor.approval_sequence is not None:
                    approval = self._event(execution, cursor.approval_sequence, EventType.APPROVAL_REQUESTED)
                    if decision not in {"allow", "deny"} or approval_request_id != approval.payload["request_id"]:
                        raise ValueError("approval resume requires the exact pending request identity and decision")
                    batch, call = self._pending(execution)
                    if approval.payload["batch_sequence"] != batch.sequence or json_wire_object(approval.payload)["call"] != encode_tool_call(call):
                        raise ValueError("pending approval does not match its native batch member")
                elif approval_request_id is not None or decision is not None:
                    raise ValueError("session is not waiting for approval")
                self._commit(execution, "running", "ready", EventDraft(EventType.RUN_RESUMED, "kernel", "user", {}))
                if approval is not None:
                    cursor.approval_sequence = None
                    self._commit(
                        execution,
                        "running",
                        "ready",
                        EventDraft(EventType.APPROVAL_DECIDED, "client", "user", {"request_id": approval_request_id, "decision": decision}),
                    )
                    _, call = self._pending(execution)
                    if decision == "deny":
                        self._report(execution, call, ToolFailure(call.tool_name, "user denied this invocation"))
                    else:
                        definition = self._snapshot.definition_of(call.tool_name)
                        if definition is None:
                            raise ValueError("approved tool is not in the active capability revision")
                        resume_read_only = self._session_read_only(execution.session.session_id)
                        denial = self._denial(call, definition, read_only=resume_read_only)
                        if denial is None:
                            # The gate is re-asked on the approval path too: consent to run a
                            # tool never overrides a gate that refuses it.
                            denial = self._gate_denial(execution, self._snapshot, call, readonly=resume_read_only)
                        if denial:
                            self._report(execution, call, ToolFailure(call.tool_name, denial))
                        else:
                            stopped = self._invoke(execution, call, definition)
                            if stopped is not None:
                                return stopped
                elif cursor.abandon_batch:
                    self._interrupt_batch(execution)
                elif cursor.intent_sequence is not None:
                    intent = self._event(execution, cursor.intent_sequence, EventType.TOOL_INTENT)
                    _, call = self._pending(execution)
                    if json_wire_object(intent.payload)["call"] != encode_tool_call(call):
                        raise ValueError("pending intent does not match its native batch member")
                    if intent.payload["replay_policy"] == "never":
                        self._interrupt_batch(execution)
                    # A safe read reuses the durable invocation identity below.
                result = self._drive(execution, max_steps=max_steps)
                if session.parent_session_id is not None and _terminal_run_result(result):
                    child_now = self.store.session(session_id)
                    notify = self._delegation._notify_requested(child_now, self.store.events(session_id))
                    self._delegation._publish_child_terminal(
                        session.parent_session_id,
                        session_id,
                        run_id=result.run_id,
                        status=_child_run_status(result),
                        output=result.output,
                        error=result.error,
                        stop_cause=result.stop_cause,
                        label="",
                        notify=notify,
                        deliver=False,
                    )
                    if notify:
                        resumed_notice = (
                            session.parent_session_id,
                            child_completion_notice_id(session.parent_session_id, session_id, result.run_id),
                        )
            finally:
                with self._aborts_lock:
                    self._session_aborts.pop(session_id, None)
        if resumed_notice is not None:
            self._delegation._deliver_child_notice(*resumed_notice)
        self._drive_pending(session_id, max_steps)
        return result

    def _drive_pending(self, session_id: str, max_steps: int, *, limit: int = 32) -> None:
        """Drive owned queued follow-up work at a safe boundary, outside the run lock.

        Returns nothing on purpose: `run`/`resume` report the result of the
        operation the caller actually requested. Auto-driven run outcomes stay
        observable through durable events, continuation rows (status, output,
        error), and `drain_continuations`. Only "nothing left to claim" is
        absorbable here; malformed checkpoints and invalid payloads must raise.
        """
        if not self.auto_drain:
            return
        for _ in range(max(1, limit)):
            try:
                results = self.drain_continuations(session_id, limit=1, max_steps=max_steps)
            except StoreConflict:
                # No pending or claimed continuation remains: the chain is done.
                return
            if not results:
                return
            if results[-1].run_status not in ("completed", "idle"):
                return

    def _drain_deferred_requests(self, session_id: str) -> None:
        """A command's callback has returned; run its committed requests without a model call."""
        with self._session_lock(session_id):
            self._ensure_open()
            session = self.session(session_id)
            if not self._admits_fresh_work(session):
                return
            root_id = uuid4().hex
            execution = _Execution(
                session,
                _Cursor(root_id=root_id, initial_root_budget=32, deferred_only=True, logical_turn=uuid4().hex),
                list(self.store.events(session_id)),
                uuid4().hex,
            )
            if self._next_deferred(execution) is None:
                return
            self.store.ensure_root_budget(root_id, 32)
            abort = Cancellation()
            with self._aborts_lock:
                self._session_aborts[session_id] = abort
            try:
                self._commit(execution, "running", "ready")
                self._drive(execution, max_steps=32)
            finally:
                with self._aborts_lock:
                    self._session_aborts.pop(session_id, None)

    def command_catalog(self) -> CommandCatalog:
        """Command catalog captured together with the revision that admits it.

        Descriptor list and revision come from ONE immutable snapshot, so a client can
        bind a later action to exactly the generation it displayed.
        """
        self._ensure_open()
        snapshot = self._snapshot
        return CommandCatalog(revision_id=snapshot.revision_id, commands=snapshot.commands)

    def _core_result(self, res: CoreResult) -> CommandResult:
        """Map a native run result into the command variant contract.

        Waiting and interrupted are legitimate native outcomes: they are reported
        with their real status, output, and stop cause in `data`, and a failure
        always carries a non-empty error message instead of a fake null.
        """
        if res.status in ("completed", "idle", "waiting"):
            return CommandResult(status="ok", output=res.output, data=res.as_payload())
        return CommandResult(
            status="error",
            output=res.output,
            error=res.error or f"run {res.status}",
            data=res.as_payload(),
        )

    def _owner_cancel_claimed_work(
        self,
        session_id: str,
        cancellations: Sequence[StagedCancellation],
        reason: str,
    ) -> None:
        """Settle work whose own claimed continuation an owner just cancelled.

        A running chain is signalled with cause `owner_cancel` so it stops before
        its next native call; a parked chain is settled to a terminal checkpoint.
        Rows owned by anybody else are untouched (the store scopes cancellations
        by owner), and nothing here starts new work.
        """
        if not cancellations:
            return
        checkpoint = self.store.checkpoint(session_id)
        cursor = _Cursor()
        if checkpoint is not None and isinstance(checkpoint.payload, Mapping):
            cursor = _Cursor.model_validate(json_wire_object(checkpoint.payload))
        if cursor.continuation_id is None:
            return
        claim_owner = self.store.continuation_owner(cursor.continuation_id)
        if not any(self._cancels_claimed(cancellation, cursor.continuation_id, claim_owner) for cancellation in cancellations):
            return
        with self._aborts_lock:
            active = self._session_aborts.get(session_id)
        if active is not None and not active.cancelled:
            active.set_cancelled(True, reason=reason, stop_cause="owner_cancel")
            return
        self._settle_parked_cancel(session_id, reason, "owner_cancel")

    @staticmethod
    def _cancels_claimed(cancellation: StagedCancellation, claimed_id: str | None, claimed_owner: str | None) -> bool:
        """Whether one staged cancellation targets this claim under the claim's OWN owner.

        Cancellation scope is the pair (owner, id): a foreign owner naming this
        claim's id, and a foreign unscoped cancellation, never scope this claim -
        exactly like the store's owner-scoped update, so a foreign owner can never
        stop another owner's run or strip its durable claim.
        """
        if claimed_id is None or claimed_owner is None:
            return False
        return cancellation.owner == claimed_owner and (cancellation.continuation_id is None or cancellation.continuation_id == claimed_id)

    def create_session(self, session_id: str | None = None) -> SessionRecord:
        """Create a new durable session in idle state bound to workspace and active revision."""
        self._ensure_open()
        sid = uuid4().hex if session_id is None else session_id
        return self._create_session_row(sid)

    def _create_session_row(self, session_id: str) -> SessionRecord:
        """Create a session on the workspace's recorded revision, retrying if it moves first."""
        for _attempt in range(3):
            recorded = self.store.workspace_current(str(self.workspace)) or self.revision_id
            try:
                return self.store.create_session(session_id, str(self.workspace), recorded)
            except StoreConflict:
                continue
        raise StoreConflict(f"could not create session {session_id!r}: the workspace revision kept moving")

    def session(self, session_id: str) -> SessionRecord:
        """Fetch session metadata by ID, verifying workspace."""
        self._ensure_open()
        s = self.store.session(session_id)
        if s.workspace != str(self.workspace):
            raise ValueError("session belongs to another workspace")
        return s

    def sessions(self, *, limit: int = 100) -> tuple[SessionRecord, ...]:
        """Fetch sessions for this workspace, ordered by updated_at descending, up to limit."""
        self._ensure_open()
        return self.store.sessions(workspace=str(self.workspace), limit=limit)

    def show(self, session_id: str, *, events: bool = False) -> dict[str, object]:
        """Return session metadata, active checkpoint, and optional events as JSON-serializable dict."""
        self._ensure_open()
        s = self.session(session_id)
        checkpoint = self.store.checkpoint(session_id)
        payload: dict[str, object] = {
            "session_id": s.session_id,
            "status": s.status,
            "run_id": s.run_id,
            "revision_id": s.active_revision_id,
            "turn": s.turn,
            "last_event_sequence": s.last_event_sequence,
            "checkpoint": None if checkpoint is None else {"kind": checkpoint.kind, "payload": json_wire_object(checkpoint.payload)},
        }
        if events:
            payload["events"] = [
                {
                    "sequence": event.sequence,
                    "type": event.event_type,
                    "run_id": event.run_id,
                    "revision_id": event.revision_id,
                    "source": event.source,
                    "actor": event.actor,
                    "payload": json_wire_object(event.payload),
                }
                for event in self.store.events(session_id)
            ]
        return payload

    def events_after(self, session_id: str, *, after_sequence: int = 0, limit: int = 100) -> tuple[StoredEvent, ...]:
        """Bounded tail read of stored events for continuous SSE streaming."""
        self._ensure_open()
        self.session(session_id)
        return self.store.events_after(session_id, after_sequence=after_sequence, limit=limit)

    def transcript_page(self, session_id: str, *, after_sequence: int = 0, limit: int = 100) -> TranscriptPage:
        """One bounded transcript page; advance to next_sequence while has_more, even when empty."""
        self._ensure_open()
        self.session(session_id)
        if type(after_sequence) is not int or after_sequence < 0:
            raise ValueError("after_sequence must be a non-negative integer")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit must be an integer in 1..1000")
        return self.plugin_runtime.transcript_page(session_id, after_sequence, limit)

    def context_history(self, session_id: str, *, limit: int = 100) -> ContextHistory:
        """One bounded context prefix from the session's first record."""
        self._ensure_open()
        self.session(session_id)
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit must be an integer in 1..1000")
        return self.plugin_runtime.context_history(session_id, limit)

    def usage(self, session_id: str, *, since_sequence: int = 0, until_sequence: int | None = None) -> SessionUsageReport:
        """Calculate truthful token usage metrics strictly from immutable core facts."""
        self._ensure_open()
        s = self.session(session_id)
        return self._get_session_usage(session_id, since_sequence, until_sequence, session=s)

    def _get_session_usage(
        self,
        session_id: str,
        since_sequence: int = 0,
        until_sequence: int | None = None,
        *,
        session: SessionRecord | None = None,
    ) -> SessionUsageReport:
        if session is None:
            s = self.session(session_id)
        else:
            s = session

        all_events = self.store.events(session_id)
        dispatches_in_window: list[int] = []
        completions_by_dispatch: dict[int, StoredEvent] = {}

        for ev in all_events:
            if ev.sequence > s.last_event_sequence:
                # Only events already inside the captured session tip are reported,
                # so through_sequence never claims less than what was included.
                break
            if ev.event_type == EventType.PROVIDER_INTENT:
                seq = ev.sequence
                if seq > since_sequence and (until_sequence is None or seq <= until_sequence):
                    dispatches_in_window.append(seq)
            elif ev.event_type in (EventType.MODEL_TURN, EventType.PROVIDER_INTERRUPTED):
                disp_seq = ev.payload.get("dispatch_sequence")
                if disp_seq is not None and isinstance(disp_seq, (int, float)):
                    completions_by_dispatch[int(disp_seq)] = ev

        if not dispatches_in_window:
            return SessionUsageReport(
                session_id=session_id,
                input_tokens=0,
                output_tokens=0,
                cache_read_tokens=0,
                uncached_input_tokens=0,
                total_cost_usd=0.0,
                has_unknown_usage=False,
                turn_count=0,
                elapsed_seconds=0.0,
                since_sequence=since_sequence,
                until_sequence=until_sequence,
                through_sequence=s.last_event_sequence,
            )

        total_input = 0
        total_output = 0
        total_cache_read = 0
        total_uncached_input = 0
        total_cost = 0.0
        total_elapsed_ms = 0
        unknown_input = False
        unknown_output = False
        unknown_cache = False
        unknown_uncached = False
        unknown_cost = False
        unknown_elapsed = False
        turn_count = 0

        for disp_seq in dispatches_in_window:
            comp_ev = completions_by_dispatch.get(disp_seq)
            if comp_ev is None or comp_ev.event_type == EventType.PROVIDER_INTERRUPTED:
                unknown_input = True
                unknown_output = True
                unknown_cache = True
                unknown_uncached = True
                unknown_cost = True
                if comp_ev is not None:
                    ms = comp_ev.payload.get("elapsed_ms")
                    if ms is not None and isinstance(ms, (int, float)):
                        total_elapsed_ms += int(ms)
                    else:
                        unknown_elapsed = True
                else:
                    unknown_elapsed = True
            elif comp_ev.event_type == EventType.MODEL_TURN:
                turn_count += 1
                turn = decode_provider_result(_wire_object(comp_ev.payload["result"], "model turn"))
                u = turn.usage
                if u is not None and u.input_tokens is not None:
                    total_input += u.input_tokens
                else:
                    unknown_input = True

                if u is not None and u.output_tokens is not None:
                    total_output += u.output_tokens
                else:
                    unknown_output = True

                if u is not None and u.cache_read_tokens is not None:
                    total_cache_read += u.cache_read_tokens
                else:
                    unknown_cache = True

                if u is not None and u.uncached_input_tokens is not None:
                    total_uncached_input += u.uncached_input_tokens
                else:
                    unknown_uncached = True

                # One canonical usage DTO carries cost; the event envelope may also
                # carry it, and either source is truthful for this dispatch.
                cost = comp_ev.payload.get("cost_usd")
                if cost is None and u is not None and u.cost_usd is not None:
                    cost = u.cost_usd
                if cost is not None and isinstance(cost, (int, float)):
                    total_cost += float(cost)
                else:
                    unknown_cost = True

                ms = comp_ev.payload.get("elapsed_ms")
                if ms is not None and isinstance(ms, (int, float)):
                    total_elapsed_ms += int(ms)
                else:
                    unknown_elapsed = True

        through_seq = s.last_event_sequence
        return SessionUsageReport(
            session_id=session_id,
            input_tokens=None if unknown_input else total_input,
            output_tokens=None if unknown_output else total_output,
            cache_read_tokens=None if unknown_cache else total_cache_read,
            uncached_input_tokens=None if unknown_uncached else total_uncached_input,
            total_cost_usd=None if unknown_cost else total_cost,
            has_unknown_usage=unknown_input or unknown_output,
            turn_count=turn_count,
            elapsed_seconds=None if unknown_elapsed else (total_elapsed_ms / 1000.0),
            since_sequence=since_sequence,
            until_sequence=until_sequence,
            through_sequence=through_seq,
        )

    def _get_owner_budget(self, session_id: str, owner: str) -> PluginBudget | None:
        return self.store.owner_budget(session_id, owner)

    def invoke_command(
        self,
        session_id: str,
        name: str,
        arguments: Mapping[str, object],
        *,
        expected_revision: str,
    ) -> CommandResult:
        """Execute a command, handling core commands, policy checks, confirmation, and CAS commit.

        `expected_revision` is required and must be the revision the client displayed: a
        command advertised by one generation is never silently executed by another.
        """
        self._ensure_open()
        self._check_expected_revision(expected_revision)
        s = self.session(session_id)

        # Core reserved commands dispatch through the advertised typed contract.
        if name == RELOAD_COMMAND_NAME:
            return self._invoke_reload_command(session_id, arguments)
        if name in _CORE_COMMAND_NAMES:
            core_schema = next(spec[2] for spec in _CORE_COMMAND_SPECS if spec[0] == name)
            try:
                jsonschema.Draft202012Validator(json_wire_object(core_schema)).validate(json_wire_object(arguments))
            except jsonschema.ValidationError as val_err:
                return CommandResult(status="error", error=f"Command arguments failed schema: {val_err.message}")
            if name == "run":
                prompt = arguments["prompt"]
                if not isinstance(prompt, str):
                    return CommandResult(status="error", error="Command arguments failed schema: prompt must be a string")
                return self._core_result(self.run(prompt, session_id=session_id))
            if name == "resume":
                request_id = arguments.get("approval_request_id")
                decision_raw = arguments.get("decision")
                decision: Literal["allow", "deny"] | None = None
                if decision_raw is not None:
                    if decision_raw not in ("allow", "deny"):
                        return CommandResult(status="error", error="Command arguments failed schema: decision must be allow or deny")
                    decision = "allow" if decision_raw == "allow" else "deny"
                return self._core_result(
                    self.resume(
                        session_id,
                        approval_request_id=str(request_id) if request_id is not None else None,
                        decision=decision,
                    )
                )
            if name == "show":
                data = self.show(session_id, events=bool(arguments.get("events", False)))
                return CommandResult(status="ok", data=data)
            if name == "status":
                data = self.show(session_id, events=False)
                counts = self.store.event_type_counts(session_id)
                usage = self.usage(session_id)
                budgets = self.store.session_budgets(session_id)
                data["event_counts"] = counts
                data["usage"] = usage.as_payload()
                data["budgets"] = [budget.as_payload() for budget in budgets]
                model = self.config.model
                lines = [
                    f"session: {s.session_id}",
                    f"status: {s.status}",
                    f"workspace: {self.workspace}",
                    f"revision: {s.active_revision_id}",
                    f"model: {model}" if model is not None else "model: not configured",
                    f"run: {s.run_id or 'none'}",
                    f"turn: {s.turn}, last event: {s.last_event_sequence}",
                    f"turns: {usage.turn_count}, input tokens: {usage.input_tokens}, "
                    f"output tokens: {usage.output_tokens}, cost: {usage.total_cost_usd}",
                ]
                lines.extend("budget: " + ", ".join(f"{key}={value}" for key, value in budget.as_payload().items()) for budget in budgets)
                lines.extend(f"{event_type}: {count}" for event_type, count in sorted(counts.items()))
                return CommandResult(status="ok", output="\n".join(lines), data=data)
            if name == "help":
                payload = dict(self.command_catalog().as_payload())
                commands = payload.get("commands")
                lines = ["Available commands:"]
                if isinstance(commands, list):
                    for command in commands:
                        if isinstance(command, Mapping):
                            lines.append(f"  /{command.get('name', '')} — {command.get('description', '')}")
                return CommandResult(status="ok", output="\n".join(lines), data=payload)
            return CommandResult(status="ok", data=dict(self.command_catalog().as_payload()))

        cmd_desc = self._snapshot.command_of(name)
        if cmd_desc is None:
            raise ValueError(f"Unknown command: {name}")

        if cmd_desc.busy_gate and s.status not in {"idle", "completed", "failed"}:
            raise ValueError(f"Session {session_id} is busy and command '{name}' requires an idle session")

        # Core read-only policy check: external mutation only.
        self._check_session(s)
        if self._session_read_only(session_id) and denied_under_restriction(frozenset(cmd_desc.effects)):
            return CommandResult(status="error", error="read-only policy denies command write effects")

        if cmd_desc.argument_schema:
            try:
                jsonschema.Draft202012Validator(json_wire_object(cmd_desc.argument_schema)).validate(json_wire_object(arguments))
            except jsonschema.ValidationError as val_err:
                return CommandResult(status="error", error=f"Command arguments failed schema: {val_err.message}")

        run_id = s.run_id or uuid4().hex
        inv_id = uuid4().hex
        context = ToolContext(
            workspace=self.workspace,
            session_id=session_id,
            run_id=run_id,
            invocation_id=inv_id,
            revision_id=self.revision_id,
            actor="client",
            logical_turn=uuid4().hex,
        )
        with self._dispatch(f"command:{name}"):
            cmd_result, staged_state, staged_events, staged_continuations, staged_cancellations, staged_budget = self.plugin_runtime.invoke_command(
                session_id,
                name,
                arguments,
                context=context,
                readonly=self._session_read_only(session_id),
            )

            if cmd_result.status == "pending_confirmation" and cmd_result.confirmation is not None:
                req = cmd_result.confirmation
                latest = self.store.session(session_id)
                self.store.commit_staged(
                    session_id,
                    expected_sequence=latest.last_event_sequence,
                    expected_run_id=latest.run_id,
                    run_id=latest.run_id or run_id,
                    revision_id=self.revision_id,
                    events=(
                        EventDraft(
                            EventType.CONFIRMATION_REQUESTED,
                            f"plugin.{cmd_desc.owner}",
                            "client",
                            req.as_payload(),
                        ),
                    ),
                    status=latest.status,
                    checkpoint_kind=None,
                    checkpoint_payload=None,
                    staged_confirmations=(
                        StagedConfirmation(
                            request_id=req.request_id,
                            owner=cmd_desc.owner,
                            command_name=name,
                            arguments=req.arguments,
                            ui_type=req.ui_type,
                            ui_data=req.ui_data,
                            expected_state_version=req.expected_state_version,
                        ),
                    ),
                )
                return cmd_result

            # Handle budget update
            staged_budget_updates: list[StagedBudgetUpdate] = []
            if staged_budget == "clear":
                staged_budget_updates.append(StagedBudgetUpdate(owner=cmd_desc.owner, action="clear"))
            elif isinstance(staged_budget, PluginBudget):
                staged_budget_updates.append(StagedBudgetUpdate(owner=staged_budget.owner, action="set", budget=staged_budget))

            # Commit staged state/events if ok OR intentional error (e.g. goal blocked)
            if cmd_result.status in ("ok", "error"):
                latest_session = self.store.session(session_id)
                completion_event = EventDraft(
                    event_type=EventType.COMMAND_COMPLETED,
                    source=f"plugin.{cmd_desc.owner}",
                    actor=context.actor,
                    payload={"name": name, "result": cmd_result.as_payload()},
                )
                all_events = tuple(staged_events) + (completion_event,)
                self.store.commit_staged(
                    session_id,
                    expected_sequence=latest_session.last_event_sequence,
                    expected_run_id=latest_session.run_id,
                    run_id=latest_session.run_id or run_id,
                    revision_id=self.revision_id,
                    events=all_events,
                    status=latest_session.status,
                    checkpoint_kind=None,
                    checkpoint_payload=None,
                    staged_state=staged_state,
                    staged_continuations=staged_continuations,
                    staged_cancellations=staged_cancellations,
                    staged_budgets=tuple(staged_budget_updates),
                )
                if self.auto_drain and staged_continuations:
                    latest_sess = self.store.session(session_id)
                    if self._admits_fresh_work(latest_sess):
                        self.drain_continuations(session_id)
                if staged_cancellations:
                    self._owner_cancel_claimed_work(
                        session_id,
                        staged_cancellations,
                        f"owner {cmd_desc.owner} cancelled its durable work",
                    )

        if any(event.event_type == EventType.TOOL_DEFERRED for event in staged_events):
            self._drain_deferred_requests(session_id)
        return cmd_result

    def confirm_action(
        self,
        session_id: str,
        request_id: str,
        decision: Literal["allow", "deny"],
        *,
        expected_revision: str,
    ) -> CommandResult:
        """Resolve a pending confirmation request by exact ID and state version."""
        self._ensure_open()
        self._check_expected_revision(expected_revision)
        s = self.session(session_id)
        rec = self.store.confirmation(request_id)
        if rec is None or rec.session_id != session_id:
            raise ValueError(f"Confirmation request {request_id!r} not found for session {session_id!r}")
        if rec.status != "pending":
            raise ValueError(f"Confirmation request {request_id!r} is not pending (status: {rec.status})")
        if rec.revision_id != self.revision_id:
            raise ValueError("Confirmation request belongs to another revision")

        if rec.expected_state_version is not None:
            _, curr_ver = self.store.plugin_state(rec.owner, "session", session_id)
            if curr_ver != rec.expected_state_version:
                self.store.decide_confirmation(request_id, "stale")
                self._commit_confirmation_decision(session_id, request_id, rec.command_name, "stale")
                raise ValueError(f"Confirmation request is stale: expected state version {rec.expected_state_version}, got {curr_ver}")

        if decision == "deny":
            self.store.decide_confirmation(request_id, "denied")
            self._commit_confirmation_decision(session_id, request_id, rec.command_name, "deny")
            return CommandResult(status="ok", output="Action denied by user")

        cmd_desc = self._snapshot.command_of(rec.command_name)
        if cmd_desc is None:
            raise ValueError(f"Command '{rec.command_name}' is not admitted")
        if rec.owner != cmd_desc.owner:
            raise ValueError("Confirmation request owner does not match the admitted command owner")

        self._check_session(s)
        # The confirmation is provenance, not privilege: the command's declared effects are
        # re-checked against the session's CURRENT stance. A pending write approved after the
        # session moved to read-only (e.g. /plan) must not mutate anything.
        if self._session_read_only(session_id) and denied_under_restriction(frozenset(cmd_desc.effects)):
            self.store.decide_confirmation(request_id, "stale")
            self._commit_confirmation_decision(session_id, request_id, rec.command_name, "stale")
            return CommandResult(status="error", error="read-only policy denies command write effects")
        self.store.decide_confirmation(request_id, "allowed")
        run_id = s.run_id or uuid4().hex
        inv_id = uuid4().hex
        context = ToolContext(
            workspace=self.workspace,
            session_id=session_id,
            run_id=run_id,
            invocation_id=inv_id,
            revision_id=self.revision_id,
            actor="user",
            logical_turn=uuid4().hex,
        )

        with self._dispatch(f"command:{rec.command_name}"):
            cmd_result, staged_state, staged_events, staged_continuations, staged_cancellations, staged_budget = self.plugin_runtime.invoke_command(
                session_id,
                rec.command_name,
                rec.arguments,
                context=context,
                readonly=self._session_read_only(session_id),
            )

            staged_budget_updates = []
            if staged_budget == "clear":
                staged_budget_updates.append(StagedBudgetUpdate(owner=cmd_desc.owner, action="clear"))
            elif isinstance(staged_budget, PluginBudget):
                staged_budget_updates.append(StagedBudgetUpdate(owner=staged_budget.owner, action="set", budget=staged_budget))

            if cmd_result.status in ("ok", "error"):
                latest_session = self.store.session(session_id)
                completion_event = EventDraft(
                    event_type=EventType.COMMAND_COMPLETED,
                    source=f"plugin.{cmd_desc.owner}",
                    actor="user",
                    payload={"name": rec.command_name, "result": cmd_result.as_payload()},
                )
                all_events = tuple(staged_events) + (
                    EventDraft(
                        EventType.CONFIRMATION_DECIDED,
                        f"plugin.{cmd_desc.owner}",
                        "user",
                        {"request_id": request_id, "command_name": rec.command_name, "decision": "allow"},
                    ),
                    completion_event,
                )
                self.store.commit_staged(
                    session_id,
                    expected_sequence=latest_session.last_event_sequence,
                    expected_run_id=latest_session.run_id,
                    run_id=latest_session.run_id or run_id,
                    revision_id=self.revision_id,
                    events=all_events,
                    status=latest_session.status,
                    checkpoint_kind=None,
                    checkpoint_payload=None,
                    staged_state=staged_state,
                    staged_continuations=staged_continuations,
                    staged_cancellations=staged_cancellations,
                    staged_budgets=tuple(staged_budget_updates),
                )
                if self.auto_drain and staged_continuations:
                    latest_sess = self.store.session(session_id)
                    if self._admits_fresh_work(latest_sess):
                        self.drain_continuations(session_id)
                if staged_cancellations:
                    self._owner_cancel_claimed_work(
                        session_id,
                        staged_cancellations,
                        f"owner {cmd_desc.owner} cancelled its durable work",
                    )

        if any(event.event_type == EventType.TOOL_DEFERRED for event in staged_events):
            self._drain_deferred_requests(session_id)
        return cmd_result

    def _commit_confirmation_decision(self, session_id: str, request_id: str, command_name: str, decision: str) -> None:
        """Record one decided confirmation durably, so clients can reduce a pending request."""
        session = self.store.session(session_id)
        try:
            self.store.commit_staged(
                session_id,
                expected_sequence=session.last_event_sequence,
                expected_run_id=session.run_id,
                run_id=session.run_id or request_id,
                revision_id=self.revision_id,
                events=(
                    EventDraft(
                        EventType.CONFIRMATION_DECIDED,
                        "kernel",
                        "user",
                        {"request_id": request_id, "command_name": command_name, "decision": decision},
                    ),
                ),
                status=session.status,
                checkpoint_kind=None,
                checkpoint_payload=None,
            )
        except StoreConflict:
            # The decision itself is durable; a concurrent writer only costs the audit fact.
            return

    def plugin_views(
        self,
        session_id: str,
    ) -> ViewsSnapshot:
        """Render declarative views contributed by admitted plugins for the session.

        The whole set renders under ONE captured generation and carries that revision,
        so a late view from an older revision is never relabelled with the new one.
        """
        self._ensure_open()
        session = self.session(session_id)
        with self._dispatch("views") as snapshot:
            views: list[PluginViewDescriptor] = []
            for view_name in snapshot.plugin_runtime.views:
                try:
                    v = snapshot.plugin_runtime.render_view(session_id, view_name, run_id=session.run_id or "")
                    views.append(v)
                except Exception as exc:
                    owner = snapshot.plugin_runtime.view_owner(view_name)
                    views.append(
                        PluginViewDescriptor(
                            owner=owner,
                            name=view_name,
                            title=f"{view_name} (error)",
                            view_type="notice",
                            data={"message": f"View {view_name} failed: {type(exc).__name__}", "level": "error"},
                        )
                    )
            return ViewsSnapshot(revision_id=snapshot.revision_id, views=tuple(views))

    def drain_continuations(
        self,
        session_id: str,
        *,
        limit: int = 10,
        max_steps: int = 32,
        continuation_id: str | None = None,
    ) -> tuple[ContinuationResult, ...]:
        """Claim and execute pending continuations as governed runs with plugin provenance.

        `continuation_id` targets one exact durable message (used by startup recovery); a plain
        drain keeps the existing oldest-first behavior.
        """
        self._validate_steps(max_steps)
        results: list[ContinuationResult] = []
        parent_deliveries: list[tuple[str, str]] = []
        with self._session_lock(session_id):
            self._ensure_open()
            self._abort.set_cancelled(False)
            s = self.session(session_id)
            abort = Cancellation()
            with self._aborts_lock:
                self._session_aborts[session_id] = abort

            try:
                for _ in range(limit):
                    run_id = uuid4().hex
                    logical_turn = uuid4().hex
                    claimed = self.store.claimed_continuations(session_id, limit=100)
                    if continuation_id is not None:
                        claimed = tuple(record for record in claimed if record.continuation_id == continuation_id)
                    recovering = bool(claimed)
                    if recovering:
                        cont_record = claimed[0]
                        run_id = cont_record.run_id
                    else:
                        if s.status in ("running", "waiting"):
                            break
                        try:
                            cont_record, session_record = self.store.claim_and_start_continuation(
                                session_id,
                                run_id,
                                logical_turn,
                                revision_id=self.revision_id,
                                initial_root_budget=max_steps,
                                continuation_id=continuation_id,
                            )
                        except StoreConflict:
                            break

                    if recovering:
                        checkpoint = self.store.checkpoint(session_id)
                        if checkpoint is None or checkpoint.kind == "terminal":
                            self.store.complete_continuation(cont_record.continuation_id, status="failed")
                            results.append(
                                ContinuationResult(
                                    continuation_id=cont_record.continuation_id,
                                    owner=cont_record.owner,
                                    session_id=session_id,
                                    status="failed",
                                    payload=cont_record.payload,
                                    run_status="failed",
                                    run_id=run_id,
                                    error="claimed continuation has no resumable checkpoint",
                                    stop_cause="error",
                                )
                            )
                            break
                        cp = checkpoint.payload if isinstance(checkpoint.payload, Mapping) else {}
                        cursor = _Cursor.model_validate(json_wire_object(cp))
                        if str(checkpoint.run_id) != str(cont_record.run_id) or str(cp.get("continuation_id")) != cont_record.continuation_id:
                            self.store.complete_continuation(cont_record.continuation_id, status="failed")
                            results.append(
                                ContinuationResult(
                                    continuation_id=cont_record.continuation_id,
                                    owner=cont_record.owner,
                                    session_id=session_id,
                                    status="failed",
                                    payload=cont_record.payload,
                                    run_status="failed",
                                    run_id=cont_record.run_id,
                                    error="claimed continuation does not own the session checkpoint",
                                    stop_cause="error",
                                )
                            )
                            break
                        if not cursor.root_id:
                            cursor.root_id = cont_record.root_id or cont_record.continuation_id
                        self.store.ensure_root_budget(cursor.root_id, max_steps)
                        run_id = cont_record.run_id
                        session_record = self.store.session(session_id)
                    else:
                        cur_session = self.store.session(session_id)
                        root_id = cont_record.root_id or cont_record.continuation_id
                        self.store.ensure_root_budget(root_id, max_steps)
                        cursor = _Cursor(
                            continuation_id=cont_record.continuation_id,
                            root_id=root_id,
                            logical_turn=logical_turn,
                            initial_root_budget=max_steps,
                        )
                        session_record = cur_session

                    execution = _Execution(session_record, cursor, list(self.store.events(session_id)), run_id)
                    root_state = self.store.root_budget_state(cursor.root_id) if cursor.root_id else None
                    if root_state is not None and root_state[1] >= root_state[0]:
                        run_res = self._stop(execution, "root action budget exhausted", stop_cause="limit_reached")
                    else:
                        per_claim = max_steps
                        if root_state is not None:
                            per_claim = max(1, min(max_steps, root_state[0] - root_state[1]))
                        run_res = self._drive(execution, max_steps=per_claim)

                    status_str: Literal["pending", "claimed", "completed", "failed", "cancelled"] = "completed"
                    if run_res.status in ("completed", "idle"):
                        status_str = "completed"
                    elif run_res.status == "failed":
                        status_str = "failed"
                    elif run_res.status == "waiting":
                        status_str = "claimed"
                    elif run_res.status == "interrupted":
                        if run_res.stop_cause in ("user_stop", "owner_cancel"):
                            status_str = "cancelled"
                        else:
                            status_str = "claimed"

                    if status_str != "claimed":
                        self.store.complete_continuation(cont_record.continuation_id, status=status_str)

                    results.append(
                        ContinuationResult(
                            continuation_id=cont_record.continuation_id,
                            owner=cont_record.owner,
                            session_id=session_id,
                            status=status_str,
                            payload=cont_record.payload,
                            run_status=run_res.status,
                            run_id=run_res.run_id,
                            output=run_res.output,
                            error=run_res.error,
                            stop_cause=run_res.stop_cause,
                        )
                    )
                    if cont_record.owner == CHILD_MESSAGE_OWNER and cont_record.payload.get("kind") == "child_followup":
                        child = self.store.session(session_id)
                        parent_id = child.parent_session_id
                        notify = bool(cont_record.payload.get("notify_on_completion", True))
                        if parent_id is not None and _terminal_run_result(run_res):
                            self._delegation._publish_child_terminal(
                                parent_id,
                                session_id,
                                run_id=run_res.run_id,
                                status=_child_run_status(run_res),
                                output=run_res.output,
                                error=run_res.error,
                                stop_cause=run_res.stop_cause,
                                label=str(cont_record.payload.get("label", "")),
                                notify=notify,
                                deliver=False,
                            )
                            if notify:
                                parent_deliveries.append((parent_id, child_completion_notice_id(parent_id, session_id, run_res.run_id)))
                    if run_res.status in ("interrupted", "failed", "waiting"):
                        break
            finally:
                with self._aborts_lock:
                    self._session_aborts.pop(session_id, None)

        for parent_id, notice_id in dict.fromkeys(parent_deliveries):
            self._delegation._deliver_child_notice(parent_id, notice_id)
        return tuple(results)

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("engine is closed")

    def _check_session(self, session: SessionRecord) -> None:
        if session.workspace != str(self.workspace) or session.active_revision_id != self.revision_id:
            raise ValueError("session workspace or revision mismatch")

    def _check_expected_revision(self, expected_revision: str) -> None:
        """Bind a client action to the catalog generation it displayed."""
        if not isinstance(expected_revision, str) or not expected_revision.strip():
            raise ValueError("expected_revision must be the revision the client displayed")
        if expected_revision != self.revision_id:
            raise ValueError(f"displayed catalog revision {expected_revision} is stale; refresh the catalog")

    def _invoke_reload_command(self, session_id: str, arguments: Mapping[str, object]) -> CommandResult:
        """Reserved core command: activate the configured sources on explicit user request."""
        validator = jsonschema.Draft202012Validator(json_wire_object(_RELOAD_COMMAND_SPEC[2]))
        try:
            validator.validate(json_wire_object(arguments))
        except jsonschema.ValidationError as err:
            return CommandResult(status="error", error=f"Command arguments failed schema: {err.message}")
        if self._admits is None:
            return CommandResult(status="error", error="capability activation is unavailable")
        if self.config.policy.read_only:
            return CommandResult(status="error", error="read-only policy denies capability activation")
        session = self.session(session_id)
        if session.status not in {"idle", "completed", "failed", "interrupted"}:
            return CommandResult(status="error", error=f"session {session_id} is busy ({session.status}); activation requires an idle session")
        outcome = self.activate(session_id=session_id, source="client")
        if not outcome["activated"]:
            return CommandResult(status="error", error=str(outcome["error"]))
        return CommandResult(status="ok", data={key: value for key, value in outcome.items() if key != "error"})

    def activate(self, *, session_id: str, source: str, request_id: str = "", target_revision_id: str = "") -> dict[str, object]:
        """Run one capability activation for an already-safe boundary.

        With `target_revision_id` the activation uses exactly that pinned candidate (the one an
        approval was bound to); otherwise the live configuration is pinned now. Returns a
        JSON-safe outcome and never raises for a refused activation: the refusal is recorded
        durably on the session and the active revision stays untouched.
        """
        self._ensure_open()
        snapshot = self._snapshot
        session = self.session(session_id)
        request_id = request_id or uuid4().hex
        blockers = self.store.activation_blockers(str(self.workspace), session_id)
        if blockers:
            error = f"activation blocked: {'; '.join(blockers)}"
            self._record_activation_refusal(session_id, request_id, source, error, blockers)
            return {"activated": False, "error": error, "request_id": request_id}
        try:
            if target_revision_id:
                admission = self._pinned_target(target_revision_id)
            else:
                target_revision_id, admission = self._prepare_target()
            prepared = self._prepare_activation(snapshot, admission=admission, source=source)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            blocked = self.store.activation_blockers(str(self.workspace), session_id)
            self._record_activation_refusal(session_id, request_id, source, error, blocked)
            return {"activated": False, "error": error, "request_id": request_id}
        try:
            self._commit_activation(snapshot, session, request_id=request_id, source=source, prepared=prepared)
        except Exception as exc:
            self._clear_fence()
            prepared.plugin_runtime.close()
            if not isinstance(exc, StoreConflict):
                raise
            self._record_activation_refusal(session_id, request_id, source, str(exc), ())
            return {"activated": False, "error": str(exc), "request_id": request_id}
        return {
            "activated": True,
            "request_id": request_id,
            "from_revision_id": snapshot.revision_id,
            "to_revision_id": prepared.admission.revision_id,
            "deactivated_owners": list(prepared.copies.deactivated_owners),
        }

    def _bound_target(self, execution: _Execution, call: ToolCall) -> str | None:
        """The pinned candidate an approval was bound to, if one exists for this exact native call."""
        encoded = encode_tool_call(call)
        target: str | None = None
        for event in execution.events:
            if event.event_type != EventType.APPROVAL_REQUESTED:
                continue
            payload = event.payload if isinstance(event.payload, Mapping) else {}
            bound = payload.get("target_revision_id")
            bound_call = payload.get("call")
            if isinstance(bound, str) and isinstance(bound_call, Mapping) and json_wire_object(bound_call) == encoded:
                target = bound
        return target

    def _pending_activation_request(self, execution: _Execution) -> Mapping[str, object] | None:
        """The newest staged activation request of this run that no answer has consumed yet."""
        request: Mapping[str, object] | None = None
        answered: set[str] = set()
        for event in execution.events:
            payload = event.payload if isinstance(event.payload, Mapping) else {}
            request_id = payload.get("request_id")
            if not isinstance(request_id, str) or not request_id:
                continue
            if event.event_type == EventType.ACTIVATION_REQUESTED:
                request = payload
            elif event.event_type in {EventType.ACTIVATION_COMMITTED, EventType.ACTIVATION_FAILED}:
                answered.add(request_id)
        if request is None:
            return None
        return None if str(request.get("request_id", "")) in answered else request

    def _clear_fence(self) -> None:
        with self._calls_lock:
            self._activating_thread = None
            self._calls_lock.notify_all()

    def _record_activation_refusal(
        self,
        session_id: str,
        request_id: str,
        source: str,
        error: str,
        blockers: Sequence[str],
    ) -> None:
        """Record why an activation did not happen, on the acting session, without consuming the request."""
        session = self.store.session(session_id)
        cursor = _Cursor()
        checkpoint = self.store.checkpoint(session_id)
        if checkpoint is not None and isinstance(checkpoint.payload, Mapping):
            cursor = _Cursor.model_validate(json_wire_object(checkpoint.payload))
        execution = _Execution(session, cursor, list(self.store.events(session_id)), session.run_id or request_id)
        try:
            self._commit(
                execution,
                session.status,
                checkpoint.kind if checkpoint is not None else "ready",
                EventDraft(
                    EventType.ACTIVATION_FAILED,
                    "kernel",
                    "kernel",
                    {
                        "request_id": request_id,
                        "source": source,
                        "error": error,
                        "blockers": list(blockers),
                    },
                ),
            )
        except StoreConflict, ValueError:
            # The refusal is a diagnostic, never a reason to lose the durable request.
            pass

    def _prepare_target(self) -> tuple[str, Admission]:
        """Pin the candidate the live configuration currently describes; imports no user code.

        Called before an approval is requested, so what the user is asked about is exactly the
        immutable artifact and private configuration that a later activation will use.
        """
        admits = self._admits
        if admits is None:
            raise ValueError("capability activation is unavailable")
        admission = admits()
        # Only the private configuration is pinned here; the revision row is inserted atomically
        # with the activation commit, after the candidate runtime reported its metadata.
        write_private_snapshot(self._state_dir, admission.revision_id, admission.private_snapshot)
        return admission.revision_id, admission

    def _pinned_target(self, target_revision_id: str) -> Admission:
        """Rebuild a previously pinned target from durable records and its current plugin source."""
        snapshot = load_private_snapshot(self._state_dir, target_revision_id)
        admits = self._admits
        if admits is None:
            raise ValueError("capability activation is unavailable")
        return admits(snapshot=snapshot, revision_id=target_revision_id)

    def _prepare_activation(self, snapshot: _Snapshot, *, admission: Admission, source: str) -> _PreparedActivation:
        """Build and verify a candidate generation under the activation fence."""
        thread_id = threading.get_ident()
        with self._calls_lock:
            if self._activating_thread is not None:
                raise ActivationFenced("another capability activation is already in progress")
            if self._active_calls:
                busy = ", ".join(sorted(self._active_calls.values()))
                raise ActivationFenced(f"capability activation is blocked by in-flight work: {busy}")
            self._activating_thread = thread_id
        # Drivers hold no dispatch slot between invokes, so the busy check above cannot see
        # them: park them explicitly before the candidate builds, or an old-revision driver
        # would keep running (and its next invoke would only then be refused).
        self._park_background_drivers(reason="capability activation")
        try:
            return self._prepare_activation_locked(snapshot, admission=admission, source=source)
        except Exception:
            self._clear_fence()
            raise

    def _candidate_sandbox_backend(self, admission: Admission) -> SandboxBackend | None:
        """Rebuild the exec backend for a candidate generation (core-side assembly).

        Same neutral rule as bootstrap: the current backend's policy/helper/baseline plus
        freshly resolved protected paths (live config + admitted plugin roots + helper +
        state private/db). None stays None. No gate: new spawns use the current policy.
        """
        current = self._snapshot.plugin_runtime.sandbox_backend if self._snapshot is not None else None
        if current is None:
            return None
        protected = [str(self.workspace / "voidcode.json")]
        for source in admission.plugins:
            try:
                protected.append(str(source.root.resolve(strict=True)))
            except OSError:
                protected.append(str(source.root))
        if current.helper_path:
            protected.append(current.helper_path)
        protected.append(str(self._state_dir / "private"))
        protected.append(str(self._state_dir / "voidcode.db"))
        protected.append(str(self._state_dir / "process-spool"))
        return SandboxBackend(
            policy=current.policy,
            helper_path=current.helper_path,
            protected_paths=tuple(dict.fromkeys(protected)),
            env_baseline=current.env_baseline,
        )

    def _prepare_activation_locked(self, snapshot: _Snapshot, *, admission: Admission, source: str) -> _PreparedActivation:
        target_revision_id = admission.revision_id
        if target_revision_id == snapshot.revision_id:
            raise ValueError("the configured sources already produce the active revision")
        candidate_runtime: PluginRuntime | None = None
        try:
            candidate_runtime = PluginRuntime(
                admission.plugins,
                workspace=self.workspace,
                revision_id=target_revision_id,
                store=self.store,
                usage_callback=self._get_session_usage,
                spawn_child_callback=lambda parent, prompt, label, max_steps, scope, readonly, notify, owner, key: self.spawn_child(
                    parent,
                    prompt,
                    label=label,
                    tool_allowlist=scope,
                    max_steps=max_steps,
                    readonly=readonly,
                    notify_on_completion=notify,
                    owner=owner,
                    idempotency_key=key,
                ),
                find_child_callback=self._delegation.find_keyed_child,
                join_child_callback=lambda parent, child, timeout: self.join_child(parent, child, timeout_seconds=timeout).as_payload(),
                send_child_callback=lambda parent, child, prompt, label, notify: self.send_child(
                    parent,
                    child,
                    prompt,
                    label=label,
                    notify_on_completion=notify,
                ).as_payload(),
                cancel_child_callback=self.cancel_child,
                cancel_background_action_callback=self.cancel_background_action,
                model_query_callback=self.governed_model_query,
                progress_callback=self._report_progress,
                tool_definition_callback=self._visible_tool_definition,
                artifacts_dir=self._state_dir / "artifacts",
                sandbox_backend=self._candidate_sandbox_backend(admission),
                process_spool_dir=self._process_spool_dir(),
            )
            # Flush facts that belong to the OLD generation before its runtime and subscriptions go
            # away. An eligible owner the candidate re-registers receives its fact right after the
            # swap, so that fact is deferred instead of aborting the activation.
            self._replay_pending_observers(require_complete=True, defer_to_candidate=admission.plugin_ids)
            if admission.validate is not None:
                admission.validate(candidate_runtime.providers)
            session_ids = tuple(record.session_id for record in self.store.sessions(workspace=str(self.workspace)))
            old_states = snapshot.plugin_runtime.state_metadata()
            candidate_states = candidate_runtime.state_metadata()
            copies = plan_state_copies(
                store=self.store,
                workspace=str(self.workspace),
                session_ids=session_ids,
                old_states=old_states,
                candidate_states=candidate_states,
                deactivate=lambda owner, payload, scope, scope_key, tip: self._deactivate_state(
                    snapshot.plugin_runtime, owner, payload, scope, scope_key, tip
                ),
            )
        except Exception:
            if candidate_runtime is not None:
                candidate_runtime.close()
            self._clear_fence()
            raise
        return _PreparedActivation(
            admission=admission,
            plugin_runtime=candidate_runtime,
            copies=copies,
            source=source,
            prepared_against=snapshot.revision_id,
        )

    def spawn_child(
        self,
        parent_session_id: str,
        prompt: str,
        *,
        label: str = "",
        tool_allowlist: tuple[str, ...] | None = None,
        max_steps: int = 32,
        readonly: bool = False,
        notify_on_completion: bool = False,
        owner: str = "",
        idempotency_key: str | None = None,
    ) -> str:
        return self._delegation.spawn_child(
            parent_session_id,
            prompt,
            label=label,
            tool_allowlist=tool_allowlist,
            max_steps=max_steps,
            readonly=readonly,
            notify_on_completion=notify_on_completion,
            owner=owner,
            idempotency_key=idempotency_key,
        )

    def find_keyed_child(self, parent_session_id: str, owner: str, idempotency_key: str) -> str | None:
        """The child one parent spawned under one owner key; a read-only mapping lookup."""
        return self._delegation.find_keyed_child(parent_session_id, owner, idempotency_key)

    def join_child(self, parent_session_id: str, child_id: str, *, timeout_seconds: float = 300.0) -> ChildOutcome:
        return self._delegation.join_child(parent_session_id, child_id, timeout_seconds=timeout_seconds)

    def send_child(
        self,
        parent_session_id: str,
        child_id: str,
        prompt: str,
        *,
        label: str = "",
        notify_on_completion: bool = True,
    ) -> ChildSendReceipt:
        return self._delegation.send_child(
            parent_session_id,
            child_id,
            prompt,
            label=label,
            notify_on_completion=notify_on_completion,
        )

    def cancel_child(self, parent_session_id: str, child_id: str, reason: str = "owner cancellation") -> bool:
        return self._delegation.cancel_child(parent_session_id, child_id, reason=reason)

    def tick(self, now_ms: int | None = None) -> dict[str, object]:
        """Fire at most one due wakeup: claim it atomically with its run, then drive that run."""
        self._ensure_open()
        now = int(time.time() * 1000) if now_ms is None else now_ms
        if type(now) is not int or now < 0:
            raise ValueError("now_ms must be a non-negative integer")
        run_id = uuid4().hex
        logical_turn = uuid4().hex
        claimed = self.store.claim_and_start_wakeup(
            str(self.workspace),
            now,
            run_id=run_id,
            logical_turn=logical_turn,
            revision_id=self.revision_id,
            eligible_owners=self.plugin_runtime.observer_owners.get("wakeup_fired", ()),
        )
        if claimed is None:
            return {"fired": None}
        wakeup, session = claimed
        self.store.ensure_root_budget(wakeup.wakeup_id, 32)
        cursor = _Cursor(root_id=wakeup.wakeup_id, logical_turn=logical_turn, initial_root_budget=32)
        execution = _Execution(session, cursor, list(self.store.events(session.session_id)), run_id)
        with self._session_lock(session.session_id):
            self._deliver_facts(execution, (self._event(execution, session.last_event_sequence, EventType.WAKEUP_FIRED),))
            result = self._drive(execution, max_steps=32)
        if result.status in ("completed", "idle"):
            terminal: Literal["completed", "failed", "cancelled"] = "completed"
        elif result.status == "failed":
            terminal = "failed"
        elif result.stop_cause in ("user_stop", "owner_cancel"):
            terminal = "cancelled"
        else:
            terminal = "failed"
        self.store.complete_wakeup(wakeup.wakeup_id, terminal)
        return {"fired": wakeup.wakeup_id, "session_id": session.session_id, "status": result.status, "wakeup_status": terminal}

    def _wakeup_loop(self) -> None:
        """Core-owned tick: one due wakeup per interval, never replaying missed slots."""
        while not self._closed:
            try:
                if self.tick()["fired"] is not None:
                    continue
            except Exception:
                # A tick failure is visible to the next tick; it never kills the core.
                pass
            time.sleep(1.0)

    # --- Background actions: owner drivers outside any model turn ---

    def _background_loop(self) -> None:
        """Claim pending background actions and drive each on its own thread.

        The loop only claims and spawns; each driver runs independently, so one slow
        action never blocks another session's actions or any model turn. Claimed rows
        reported as `interrupted` — and are never auto-replayed.
        """
        while not self._closed:
            try:
                claimed = self.store.claim_background_action(workspace=str(self.workspace), claim_identity=self._background_identity)
            except StoreConflict:
                # Lost a claim race; back off and retry. Anything else is a real
                # store/contract failure and stays visible instead of idling as "no work".
                claimed = None
            if claimed is None:
                with self._background_cond:
                    self._background_cond.wait(timeout=1.0)
                continue
            abort = threading.Event()
            with self._background_cond:
                self._background_live.add(claimed.continuation_id)
                self._background_aborts[claimed.continuation_id] = abort
            self._snapshot.plugin_runtime.background_live_actions.add(claimed.continuation_id)
            worker = threading.Thread(
                target=self._drive_background_action,
                args=(claimed, abort),
                name=f"CoreBackground-{claimed.continuation_id[:8]}",
                daemon=True,
            )
            worker.start()

    def _drive_background_action(self, claimed: ContinuationRecord, abort: threading.Event) -> None:
        """Run one claimed action's owner driver, then settle the row terminally."""
        action_id = claimed.continuation_id
        terminal: Literal["completed", "failed", "cancelled"] = "completed"
        failure: str | None = None
        try:
            action = _wire_object(claimed.payload.get("action"), f"background action {action_id!r} envelope")
            context = _BackgroundContext(
                self,
                action_id=action_id,
                owner=claimed.owner,
                session_id=claimed.session_id,
                action=action,
                revision_id=claimed.revision_id,
                abort=abort,
            )
            driver = self._snapshot.plugin_runtime.background_driver(claimed.owner)
            if driver is None:
                raise RuntimeError(f"owner {claimed.owner!r} registers no background driver")
            driver(context)
            terminal = "cancelled" if abort.is_set() else "completed"
        except InterruptedError:
            terminal = "cancelled"
            failure = None
        except BaseException as exc:
            # Any driver exit — including SystemExit or an asyncio cancellation escaping
            # the script — settles failed with its real type recorded. This runs on the
            # worker thread only, so it can never swallow the main thread's Ctrl-C, and
            # the live registries below always clear in `finally`.
            terminal = "failed"
            failure = f"{type(exc).__name__}: {exc}"
        try:
            self.store.complete_background_action(action_id, status=terminal, error=failure)
        except StoreConflict:
            # The only provable race: a concurrent cancel settled the row first, so the
            # driver's outcome is moot against an already-terminal row. Anything else
            # propagates visibly out of the worker; cleanup still runs in `finally`.
            pass
        finally:
            with self._background_cond:
                self._background_live.discard(action_id)
                self._background_aborts.pop(action_id, None)
                self._background_cond.notify_all()
            self._snapshot.plugin_runtime.background_live_actions.discard(action_id)

    def _invoke_background_action[T](
        self,
        owner: str,
        session_id: str,
        callback: Callable[[ToolContext, Any], T],
        *,
        action_id: str,
        revision_id: str,
        abort: threading.Event,
    ) -> T:
        """Run one driver callback under a fresh lease and commit its staged work atomically.

        The callback runs exactly once: it is never replayed. Only a `SequenceConflict`
        retries, and then only by re-submitting the SAME staged proposal against the fresh
        tip (bounded attempts) — no callback re-execution, no external work replay. Any
        other conflict (state CAS, run id, revision) fails visibly so the driver sees the
        real contention instead of a silently retried mutation. The commit never takes the
        session run lock.
        """
        from .services import RuntimeServices

        if abort.is_set() or self._closed:
            raise InterruptedError(f"background action {action_id!r} was cancelled")
        with self._dispatch(f"background:{owner}") as snapshot:
            if snapshot.revision_id != revision_id:
                raise InterruptedError(f"background action {action_id!r} belongs to a retired revision")
            runtime = snapshot.plugin_runtime
            session = self.store.session(session_id)
            self._check_session(session)
            signal = _EventAbortSignal(abort)
            context = ToolContext(
                workspace=self.workspace,
                session_id=session_id,
                run_id=action_id,
                invocation_id=uuid4().hex,
                revision_id=snapshot.revision_id,
                actor="plugin",
                logical_turn=action_id,
                abort_signal=signal,
            )
            lease = runtime._open_lease(  # noqa: SLF001 - the core owns lease lifecycles
                owner=owner,
                session_id=session_id,
                run_id=action_id,
                invocation_id=context.invocation_id,
                readonly=self._session_read_only(session_id),
                actor="plugin",
                internal_work=True,
            )
            services = RuntimeServices(runtime, lease)
            try:
                result = callback(context, services)
            except BaseException:
                runtime._revoke_lease(lease)  # noqa: SLF001 - the core owns lease lifecycles
                raise
            staged = runtime._staged_changes(lease)  # noqa: SLF001 - the core owns lease lifecycles
            runtime._revoke_lease(lease)  # noqa: SLF001 - the core owns lease lifecycles
            if abort.is_set() or self._closed:
                raise InterruptedError(f"background action {action_id!r} was cancelled")
            staged_state, staged_events, staged_conts, staged_cancels, staged_budget = staged
            staged_budget_updates: list[StagedBudgetUpdate] = []
            if staged_budget == "clear":
                staged_budget_updates.append(StagedBudgetUpdate(owner=owner, action="clear"))
            elif isinstance(staged_budget, PluginBudget):
                staged_budget_updates.append(StagedBudgetUpdate(owner=staged_budget.owner, action="set", budget=staged_budget))
            for _attempt in range(8):
                if abort.is_set() or self._closed:
                    raise InterruptedError(f"background action {action_id!r} was cancelled")
                latest = self.store.session(session_id)
                try:
                    self.store.commit_staged(
                        session_id,
                        expected_sequence=latest.last_event_sequence,
                        expected_run_id=latest.run_id,
                        run_id=latest.run_id or action_id,
                        revision_id=snapshot.revision_id,
                        events=tuple(staged_events),
                        status=latest.status,
                        checkpoint_kind=None,
                        checkpoint_payload=None,
                        staged_state=staged_state,
                        staged_continuations=staged_conts,
                        staged_cancellations=staged_cancels,
                        staged_budgets=tuple(staged_budget_updates),
                    )
                except SequenceConflict:
                    continue
                if staged_cancels:
                    self._owner_cancel_claimed_work(
                        session_id,
                        staged_cancels,
                        f"owner {owner} cancelled its durable work",
                    )
                return result
            raise SequenceConflict(f"background action {action_id!r} could not commit its staged work")

    def cancel_background_action(self, session_id: str, owner: str, action_id: str, reason: str = "owner cancellation") -> bool:
        """Cancel one owner background action: signal its live driver and mark the row cancelled.

        Pending rows are marked cancelled without ever running. A claimed row driven by this
        process is signalled (the driver observes the abort and settles cancelled); a
        claimed-but-unattended row is marked cancelled outright. Foreign owners' rows and
        terminal rows are untouched; unknown ids return False.
        """
        _ = reason
        self._ensure_open()
        record = self.store.background_action(action_id)
        if record is None or record.owner != owner or record.session_id != session_id:
            return False
        if record.status in ("completed", "failed", "cancelled"):
            return False
        with self._background_cond:
            abort = self._background_aborts.get(action_id)
            live = action_id in self._background_live
        if live and abort is not None:
            abort.set()
            with self._background_cond:
                self._background_cond.notify_all()
            return True
        try:
            self.store.complete_background_action(action_id, status="cancelled")
        except StoreConflict:
            return False
        with self._background_cond:
            self._background_cond.notify_all()
        return True

    def wait_background_actions(
        self, session_id: str, *, action_id: str | None = None, timeout_seconds: float = 300.0
    ) -> tuple[dict[str, object], ...]:
        """Wait until this session's background actions settle; raise when one failed.

        With `action_id` the wait names one exact receipt: even when that action already
        settled before the wait starts, its terminal state is checked and a failure is
        raised — a pre-settled failure can never slip through as a green empty wait. The
        id must belong to this session; a foreign or unknown id raises visibly. Without
        an id the wait covers the session's currently active actions only, so a past
        failure never poisons a new run.

        Claimed-but-unattended (stale) rows are never waited on: they report `interrupted`
        and need an explicit new action, never an automatic replay. An explicitly waited
        stale row or a timeout is not success: the caller gets the honest remainder, and
        a failed action raises `RuntimeError` naming its id and recorded error.
        """
        self._ensure_open()
        self.session(session_id)
        if action_id is not None:
            return (self._wait_one_background_action(session_id, action_id, timeout_seconds=timeout_seconds),)
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        while True:
            pending = self.store.pending_background_actions(session_id)
            with self._background_cond:
                live = set(self._background_live)
            waited = [record for record in pending if record.status == "pending" or record.continuation_id in live]
            if not waited:
                # A claimed row outside the live set is stale — but the claim and the live
                # registration are two steps, so confirm across a beat before reporting it.
                time.sleep(0.2)
                pending = self.store.pending_background_actions(session_id)
                with self._background_cond:
                    live = set(self._background_live)
                waited = [record for record in pending if record.status == "pending" or record.continuation_id in live]
                if waited:
                    continue
                settled: list[dict[str, object]] = []
                for record in pending:
                    settled.append(self._stale_action_entry(record))
                return tuple(settled)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return tuple({"action_id": record.continuation_id, "status": record.status} for record in waited)
            with self._background_cond:
                self._background_cond.wait(timeout=min(remaining, 0.2))
            # One round settled (or was notified): surface failures before waiting further,
            # so a failed action never hides behind a longer-running sibling.
            for record in waited:
                current = self.store.background_action(record.continuation_id)
                if current is None or current.status != "failed":
                    continue
                raise RuntimeError(f"background action {current.continuation_id!r} failed" + self._action_error_suffix(current))

    def _wait_one_background_action(self, session_id: str, action_id: str, *, timeout_seconds: float) -> dict[str, object]:
        """Wait for one exact action receipt, however it settled — even before this call."""
        record = self.store.background_action(action_id)
        if record is None or record.session_id != session_id:
            raise ValueError(f"background action {action_id!r} does not belong to session {session_id!r}")
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        while True:
            current = self.store.background_action(action_id)
            assert current is not None  # background rows are never deleted
            if current.status in ("completed", "failed", "cancelled"):
                if current.status == "failed":
                    raise RuntimeError(f"background action {action_id!r} failed" + self._action_error_suffix(current))
                return {"action_id": action_id, "status": current.status}
            with self._background_cond:
                live = action_id in self._background_live
            if current.status == "claimed" and not live:
                time.sleep(0.2)
                reconfirmed = self.store.background_action(action_id)
                assert reconfirmed is not None
                with self._background_cond:
                    live = action_id in self._background_live
                if reconfirmed.status == "claimed" and not live:
                    return self._stale_action_entry(reconfirmed)
                continue
            if time.monotonic() >= deadline:
                return {"action_id": action_id, "status": current.status}
            with self._background_cond:
                self._background_cond.wait(timeout=0.2)

    @staticmethod
    def _stale_action_entry(record: ContinuationRecord) -> dict[str, object]:
        """The honest report for a claimed-but-unattended row: interrupted, never success."""
        error = record.payload.get("error")
        return {"action_id": record.continuation_id, "status": "interrupted", "error": error if isinstance(error, str) else None}

    @staticmethod
    def _action_error_suffix(record: ContinuationRecord) -> str:
        """The recorded driver error of one failed row, as a raise suffix (empty when absent)."""
        error = record.payload.get("error")
        if isinstance(error, str) and error:
            return f": {error}"
        return ""

    def governed_model_query(
        self,
        session_id: str,
        prompt: str,
        model: str | None = None,
        max_output_tokens: int | None = None,
        include_context: bool = False,
    ) -> str:
        """A tool-free side turn, optionally reading the current conversation window.

        Usage is recorded, but the question and answer never extend the conversation.
        """
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode("utf-8")) > 64 * 1024:
            raise ValueError("model query prompt must be nonempty UTF-8 text within 64 KiB")
        self._ensure_open()
        self.session(session_id)
        selected = model or self.config.model
        if selected is None:
            raise ValueError(MODEL_CONFIGURATION_GUIDANCE)
        provider_name, separator, model_name = selected.partition("/")
        if not separator or not model_name or provider_name not in self._snapshot.plugin_runtime.providers:
            raise ValueError("model must name a configured provider/model")
        metadata: dict[str, object] = {"session_id": session_id, "purpose": "plugin_query"}
        if max_output_tokens is not None:
            if type(max_output_tokens) is not int or max_output_tokens < 1:
                raise ValueError("max_output_tokens must be a positive integer")
            metadata["max_output_tokens"] = max_output_tokens
        context: tuple[ProviderMessage, ...] = ()
        if include_context:
            session = self.store.session(session_id)
            execution = _Execution(session, _Cursor(), list(self.store.events(session_id)), session.run_id or uuid4().hex)
            context = self._messages(execution, completed_only=True)
        request = ProviderTurnRequest(
            messages=(*context, ProviderMessage("user", prompt)),
            available_tools=(),
            raw_model=selected,
            provider_name=provider_name,
            model_name=model_name,
            reasoning_effort=self.config.reasoning_effort,
            metadata=metadata,
            abort_signal=self._abort,
        )
        with self._dispatch("model-query") as snapshot:
            turn = snapshot.plugin_runtime.propose_turn(request, provider=provider_name)
        if turn.tool_calls:
            raise RuntimeError("governed model query returned tool calls; none were offered")
        self._commit_control_event(
            session_id,
            EventDraft(
                EventType.MODEL_QUERY,
                "kernel",
                "plugin",
                {
                    "provider": provider_name,
                    "model": model_name,
                    "usage": None if turn.usage is None else turn.usage.metadata_payload(),
                    "cost_usd": None if turn.usage is None else turn.usage.cost_usd,
                },
            ),
        )
        return turn.output or ""

    def _commit_control_event(self, session_id: str, event: EventDraft) -> None:
        """Append one core control fact on a session without disturbing its run state."""
        try:
            session = self.store.session(session_id)
            self.store.commit_staged(
                session_id,
                expected_sequence=session.last_event_sequence,
                expected_run_id=session.run_id,
                run_id=session.run_id or uuid4().hex,
                revision_id=self.revision_id,
                events=self._stamp_fact_owners((event,)),
                status=session.status,
                checkpoint_kind=None,
                checkpoint_payload=None,
            )
        except Exception:
            # A control fact is diagnostic; losing it never blocks the owner's real work.
            return

    def _report_progress(self, lease: LeaseContext, payload: Mapping[str, object]) -> None:
        if len(canonical_json(payload).encode("utf-8")) > 16 * 1024:
            raise ValueError("progress report exceeds the 16 KiB bound")
        self._commit_control_event(
            lease.session_id,
            EventDraft(
                EventType.TOOL_PROGRESS,
                f"plugin.{lease.owner}",
                lease.actor,
                {
                    "invocation_id": lease.invocation_id,
                    "owner": lease.owner,
                    "run_id": lease.run_id,
                    "revision_id": lease.revision_id,
                    "progress": json_wire_object(payload),
                },
            ),
        )

    def _report_provider_progress(
        self,
        session_id: str,
        *,
        run_id: str,
        dispatch_id: str,
        dispatch_sequence: int,
        channel: str,
        text_delta: str | None,
    ) -> None:
        """Observational stream fragment on the existing control-event path; never raises."""
        if text_delta is None or not text_delta:
            return
        payload = {
            "run_id": run_id,
            "dispatch_id": dispatch_id,
            "dispatch_sequence": dispatch_sequence,
            "channel": channel,
            "text_delta": text_delta,
        }
        if len(canonical_json(payload).encode("utf-8")) > 16 * 1024:
            return
        self._commit_control_event(
            session_id,
            EventDraft(EventType.PROVIDER_PROGRESS, "provider", "model", payload),
        )

    def _stream_provider_turn(
        self,
        snapshot: _Snapshot,
        execution: _Execution,
        request: ProviderTurnRequest,
        *,
        provider_name: str,
        dispatch_id: str,
        dispatch_sequence: int,
    ) -> ProviderTurnResult:
        """Aggregate one provider stream into propose_turn's turn shape, with progress.

        Single-attempt by contract: once bytes flowed there is no safe resend,
        so this path never retries — the attempt is attempt 1 of the turn.
        """
        numbered = replace(request, attempt=1)
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        calls: dict[int, dict[str, object]] = {}
        contents: list[str] = []
        metadata: dict[str, object] = {}
        usage = None
        done_reason = "unknown"
        finish_reason_reported = False
        saw_done = False
        session_id = execution.session.session_id
        for event in snapshot.plugin_runtime.stream_turn(numbered, provider=provider_name):
            if event.kind == "delta" and event.text:
                if event.channel == "reasoning":
                    reasoning_parts.append(event.text)
                    self._report_provider_progress(
                        session_id,
                        run_id=execution.run_id,
                        dispatch_id=dispatch_id,
                        dispatch_sequence=dispatch_sequence,
                        channel="reasoning",
                        text_delta=event.text,
                    )
                else:
                    text_parts.append(event.text)
                    self._report_provider_progress(
                        session_id,
                        run_id=execution.run_id,
                        dispatch_id=dispatch_id,
                        dispatch_sequence=dispatch_sequence,
                        channel="text",
                        text_delta=event.text,
                    )
            elif event.kind == "tool_call_start":
                ordinal = event.tool_call_ordinal if event.tool_call_ordinal is not None else len(calls)
                calls[ordinal] = {
                    "tool_call_id": event.tool_call_id,
                    "tool_name": event.tool_name,
                    "arguments": {},
                }
            elif event.kind == "tool_call_end":
                ordinal = event.tool_call_ordinal if event.tool_call_ordinal is not None else len(calls)
                entry = calls.setdefault(ordinal, {})
                if event.tool_call_id is not None:
                    entry["tool_call_id"] = event.tool_call_id
                if event.tool_name is not None:
                    entry["tool_name"] = event.tool_name
                entry["arguments"] = dict(event.parsed_arguments or {})
            elif event.kind == "content" and event.channel == "tool" and event.text:
                contents.append(event.text)
            elif event.kind == "error":
                raise ProviderExecutionError(
                    kind=event.error_kind or "provider_error",
                    provider_name=provider_name,
                    model_name=request.model_name or "unknown",
                    message=event.error or "provider stream failed",
                    retryable=False,
                )
            elif event.kind == "done":
                saw_done = True
                if event.done_reason is not None:
                    done_reason = event.done_reason
                if event.usage is not None:
                    usage = event.usage
                if event.metadata:
                    metadata.update(event.metadata)
                    finish_reason_reported = bool(event.metadata.get("finish_reason_reported", finish_reason_reported))
        if not saw_done:
            raise ProviderExecutionError(
                kind="provider_error",
                provider_name=provider_name,
                model_name=request.model_name or "unknown",
                message="provider stream ended without a terminal event",
                retryable=False,
            )
        try:
            tool_calls = tuple(_tool_call_from_entry(entry) for _, entry in sorted(calls.items()))
        except ProviderExecutionError as exc:
            raise ProviderExecutionError(
                kind=exc.kind,
                provider_name=provider_name,
                model_name=request.model_name or "unknown",
                message=exc.message,
                retryable=False,
            ) from exc
        for content in contents:
            tool_calls += _tool_calls_from_content(content, provider_name=provider_name, model_name=request.model_name or "unknown")
        return ProviderTurnResult(
            tool_calls=tool_calls,
            output="".join(text_parts),
            reasoning="".join(reasoning_parts) or None,
            usage=usage,
            done_reason=cast(Any, done_reason),
            finish_reason_reported=finish_reason_reported,
            metadata=metadata or None,
        )

    def _deactivated_owners(self, snapshot: _Snapshot, admission: Admission) -> frozenset[str]:
        """Owners loaded before that the candidate does not admit: disabled, removed, or missing."""
        return frozenset(snapshot.plugin_runtime.plugin_ids) - admission.plugin_ids

    def _deactivate_state(
        self,
        runtime: PluginRuntime,
        owner: str,
        payload: Mapping[str, object],
        scope: str,
        scope_key: str,
        tip_sequence: int | None,
    ) -> Mapping[str, object]:
        try:
            return runtime.prepare_deactivate_owner_state(
                owner,
                payload,
                scope=scope,
                scope_key=scope_key,
                tip_sequence=tip_sequence,
            )
        except RuntimeError as exc:
            raise ValueError(f"state deactivation for {owner} failed: {exc}") from exc

    def _commit_activation(
        self,
        snapshot: _Snapshot,
        session: SessionRecord,
        *,
        request_id: str,
        source: str,
        prepared: _PreparedActivation,
    ) -> None:
        """One CAS transaction, then publish one new pointer and revoke the old generation."""
        admission = prepared.admission
        target_revision_id = admission.revision_id
        checkpoint = self.store.checkpoint(session.session_id)
        checkpoint_payload: Mapping[str, object] | None = None
        checkpoint_kind: Literal["ready", "approval_wait", "terminal", "interrupted"] | None = None
        if checkpoint is not None and isinstance(checkpoint.payload, Mapping):
            checkpoint_payload = self._activation_checkpoint(
                json_wire_object(checkpoint.payload),
                source=source,
                request_id=request_id,
            )
            checkpoint_kind = cast("Literal['ready', 'approval_wait', 'terminal', 'interrupted']", checkpoint.kind)
        disabled_owners = self._deactivated_owners(snapshot, admission)
        # The model's request was already recorded durably by its own tool commit; this
        # transaction only answers it (a client-initiated switch answers no staged request).
        events = (
            EventDraft(
                EventType.ACTIVATION_COMMITTED,
                "core",
                "core",
                {
                    "request_id": request_id,
                    "source": source,
                    "from_revision_id": snapshot.revision_id,
                    "to_revision_id": target_revision_id,
                    "deactivated_owners": list(prepared.copies.deactivated_owners),
                },
            ),
        )
        self.store.commit_activation(
            workspace=str(self.workspace),
            session_id=session.session_id,
            expected_sequence=session.last_event_sequence,
            expected_run_id=session.run_id,
            expected_revision_id=snapshot.revision_id,
            target_revision_id=target_revision_id,
            target_payload=admission.revision_payload,
            # Control facts carry the authentic operation identity, never an invented run.
            run_id=session.run_id or request_id,
            status=session.status,
            events=events,
            checkpoint_kind=checkpoint_kind,
            checkpoint_payload=checkpoint_payload,
            state_updates=prepared.copies.updates,
            cancelled_owners=tuple(sorted(disabled_owners)),
            cleared_budget_owners=tuple(sorted(disabled_owners)),
        )
        # Publication: one pointer swap, then revoke the old generation. Nothing can be
        # dispatched to the old runtime afterwards (it is closed and never respawns), and the
        # candidate runtime was already validated before the commit.
        with self._calls_lock:
            self._snapshot = _Snapshot.build(
                revision_id=target_revision_id,
                config=admission.config,
                admission=admission,
                plugin_runtime=prepared.plugin_runtime,
                with_reload=self._admits is not None,
            )
            self._activating_thread = None
            self._calls_lock.notify_all()
        # Deliver facts deferred to owners the candidate now registers, under the new runtime.
        self._replay_pending_observers()
        snapshot.plugin_runtime.close()
        self._wire_background_runtime(prepared.plugin_runtime)
        self._notify_background_loop()

    def _activation_checkpoint(
        self,
        checkpoint_payload: Mapping[str, object] | None,
        *,
        source: str,
        request_id: str,
    ) -> Mapping[str, object]:
        """Carry the session's own cursor across the switch, plus a real resume intent where owed.

        The intent is written only for a model-requested switch inside a live run: that run owes
        one automatic continuation, and a crash before the in-process continuation persists it
        lets a restart finish the job exactly once. A user-issued switch owes no such turn.
        """
        cursor = _Cursor()
        if checkpoint_payload is not None:
            cursor = _Cursor.model_validate(json_wire_object(checkpoint_payload))
        if source == "model":
            cursor.resume_intent = request_id
        return json_wire_object(cursor.model_dump())

    @staticmethod
    def _event(execution: _Execution, sequence: int, kind: str) -> StoredEvent:
        if sequence < 1 or sequence > len(execution.events):
            raise ValueError("checkpoint references a missing event")
        event = execution.events[sequence - 1]
        if event.sequence != sequence or event.event_type != kind:
            raise ValueError("checkpoint references the wrong event kind")
        return event

    def _settlement_candidates(self, session_id: str) -> list[StoredEvent]:
        """Events needing observer delivery: terminal facts plus unsettled dispatches.

        A dispatch intent with no completion event means the process died after
        requesting a turn; it must settle as an unknown-cost usage fact exactly
        once (receipts dedupe it) instead of leaving consumers with stale totals.
        """
        events = self.store.events(session_id)
        completed_dispatches: set[int] = set()
        for event in events:
            if event.event_type in (EventType.MODEL_TURN, EventType.PROVIDER_INTERRUPTED):
                dispatch_seq = event.payload.get("dispatch_sequence")
                if isinstance(dispatch_seq, (int, float)):
                    completed_dispatches.add(int(dispatch_seq))
        pending: list[StoredEvent] = []
        for event in events:
            if event.event_type == EventType.PROVIDER_INTENT:
                if event.sequence not in completed_dispatches:
                    pending.append(event)
            elif event.event_type in _FACT_EVENT_TYPES:
                pending.append(event)
        return pending

    def _replay_pending_observers(self, *, require_complete: bool = False, defer_to_candidate: frozenset[str] = frozenset()) -> None:
        """Deliver facts whose observer receipts never committed before accepting new work.

        Eligibility comes from each source event's OWN revision (its manifest declarations),
        not from whoever is subscribed now, so a reload can neither re-deliver an old fact to
        a new subscriber nor drop one an eligible owner already received. With
        `require_complete`, an eligible owner that is no longer registered aborts instead of
        being papered over: an activation must never mask an undelivered fact.
        """
        for session in self.store.sessions():
            if session.workspace != str(self.workspace) or session.active_revision_id != self.revision_id:
                continue
            pending = self._settlement_candidates(session.session_id)
            if not pending:
                continue
            checkpoint = self.store.checkpoint(session.session_id)
            cursor = (
                _Cursor.model_validate(json_wire_object(checkpoint.payload))
                if checkpoint is not None and isinstance(checkpoint.payload, Mapping)
                else _Cursor()
            )
            execution = _Execution(session, cursor, list(self.store.events(session.session_id)), session.run_id or uuid4().hex)
            settled = [self._record_unknown_dispatch(execution, ev) if ev.event_type == EventType.PROVIDER_INTENT else ev for ev in pending]
            self._deliver_facts(
                execution,
                sorted(settled, key=lambda event: event.sequence),
                require_complete=require_complete,
                defer_to_candidate=defer_to_candidate,
            )

    def _eligible_observers(self, event: StoredEvent) -> tuple[str, ...]:
        """Owners that subscribed this fact type when the event was produced.

        The list is frozen on the durable event, so a restart with changed plugin source can
        neither hand an old fact to a new subscriber nor drop an old one.
        """
        owners = event.payload.get("eligible_owners")
        if not isinstance(owners, (list, tuple)) or not all(isinstance(owner, str) for owner in owners):
            raise ValueError(
                f"cannot deliver event {event.sequence}: it carries no recorded eligible owners; "
                "the recorded fact does not satisfy the current contract"
            )
        return tuple(str(owner) for owner in owners)

    def _observer_gap_event(self, event: StoredEvent, missing: Sequence[str]) -> EventDraft:
        """A durable diagnostic for a fact that its own revision's owner can no longer receive."""
        return EventDraft(
            EventType.OBSERVER_ERROR,
            source="kernel",
            actor="kernel",
            payload={
                "owner": ", ".join(missing),
                "error": f"event {event.sequence} was produced by revision {event.revision_id} but its registered owner is not loaded",
                "event_sequence": event.sequence,
            },
        )

    def _deliver_fact_for_event(
        self,
        execution: _Execution,
        ev: StoredEvent,
        *,
        require_complete: bool = False,
        defer_to_candidate: frozenset[str] = frozenset(),
    ) -> None:
        """Notify exactly the owners this event's revision registered, then persist those receipts."""
        fact = self._fact_for(execution, ev)
        if fact is None:
            return
        fact_type, fact_payload = fact
        session_id = ev.session_id
        with self._dispatch(f"observer:{fact_type}") as snapshot:
            eligible = self._eligible_observers(ev)
            registered = snapshot.plugin_runtime.observer_owners.get(fact_type, ())
            missing = [owner for owner in eligible if owner not in registered and owner not in defer_to_candidate]
            if missing:
                if require_complete:
                    missing_list = ", ".join(missing)
                    raise ValueError(
                        f"cannot activate: event {ev.sequence} ({ev.event_type}) of session {session_id} is still undelivered for {missing_list}"
                    )
                self._commit_staged_rebased(
                    execution,
                    status=execution.session.status,
                    kind=None,
                    events=(self._observer_gap_event(ev, missing),),
                )
            active = [o for o in eligible if o in registered and not self.store.has_observer_receipt(session_id, ev.sequence, o)]
            if not active:
                return
            logical_turn = str(fact_payload.get("logical_turn", ""))
            obs_res = snapshot.plugin_runtime.notify_fact(fact_type, fact_payload, owners=tuple(active), logical_turn=logical_turn)
            receipts = tuple(ObserverReceiptRecord(event_sequence=ev.sequence, owner=o) for o in active)
            staged_events: list[EventDraft] = []
            staged_budget_updates: list[StagedBudgetUpdate] = []
            if obs_res:
                staged_events.extend(obs_res.staged_events)
                for owner, b_val in obs_res.staged_budgets:
                    if b_val == "clear":
                        staged_budget_updates.append(StagedBudgetUpdate(owner=owner, action="clear"))
                    elif isinstance(b_val, PluginBudget):
                        staged_budget_updates.append(StagedBudgetUpdate(owner=b_val.owner, action="set", budget=b_val))
                for err in obs_res.errors:
                    staged_events.append(
                        EventDraft(
                            EventType.OBSERVER_ERROR,
                            source=f"plugin.{err.get('owner', 'observer')}",
                            actor="plugin",
                            payload={"owner": err.get("owner"), "error": err.get("error")},
                        )
                    )
            self._commit_staged_rebased(
                execution,
                status=execution.session.status,
                kind=None,
                events=tuple(staged_events),
                staged_state=obs_res.staged_state if obs_res else (),
                staged_continuations=obs_res.staged_continuations if obs_res else (),
                staged_cancellations=obs_res.staged_cancellations if obs_res else (),
                staged_budgets=tuple(staged_budget_updates),
                observer_receipts=receipts,
            )

    def _deliver_facts(
        self,
        execution: _Execution,
        events: Sequence[StoredEvent],
        *,
        require_complete: bool = False,
        defer_to_candidate: frozenset[str] = frozenset(),
        tolerate_conflicts: bool = False,
    ) -> None:
        """One ordered delivery path for live commits, recovery and observer-produced facts."""
        pending = list(events)
        seen: set[int] = set()
        for event in pending:
            if event.sequence in seen or event.event_type not in _FACT_EVENT_TYPES:
                continue
            seen.add(event.sequence)
            before_delivery = len(execution.events)
            try:
                self._deliver_fact_for_event(
                    execution,
                    event,
                    require_complete=require_complete,
                    defer_to_candidate=defer_to_candidate,
                )
            except StoreConflict as exc:
                if not tolerate_conflicts:
                    raise
                # The outcome is durable. Leave the receipt absent for recovery,
                # and expose the rejected observer proposal without failing the run.
                try:
                    self._commit_staged_rebased(
                        execution,
                        status=execution.session.status,
                        kind=None,
                        events=(
                            EventDraft(
                                EventType.OBSERVER_ERROR,
                                "plugin.observer",
                                "plugin",
                                {"owner": None, "error": f"observer proposals rejected: {exc}"},
                            ),
                        ),
                    )
                except StoreConflict, ValueError:
                    pass
            pending.extend(execution.events[before_delivery:])

    def _record_unknown_dispatch(self, execution: _Execution, intent: StoredEvent) -> StoredEvent:
        committed = self._commit_staged_rebased(
            execution,
            status=execution.session.status,
            kind=None,
            events=(
                EventDraft(
                    EventType.PROVIDER_INTERRUPTED,
                    "kernel",
                    "kernel",
                    {
                        "dispatch_sequence": intent.sequence,
                        "logical_turn": intent.payload.get("logical_turn", ""),
                        "eligible_owners": list(self._eligible_observers(intent)),
                    },
                ),
            ),
        )
        return committed[0]

    def _settle_unsettled_dispatches(self, execution: _Execution) -> None:
        """Settle dispatches of this run that never produced a completion event."""
        completed: set[int] = set()
        for event in execution.events:
            if event.event_type in (EventType.MODEL_TURN, EventType.PROVIDER_INTERRUPTED):
                dispatch_seq = event.payload.get("dispatch_sequence")
                if isinstance(dispatch_seq, (int, float)):
                    completed.add(int(dispatch_seq))
        for event in execution.events:
            if event.event_type == EventType.PROVIDER_INTENT and event.sequence not in completed:
                settled = self._record_unknown_dispatch(execution, event)
                self._deliver_facts(execution, (settled,))

    @staticmethod
    def _assert_execution_prefix(execution: _Execution) -> int:
        """Fail closed unless the in-memory prefix still ends exactly at the session tip.

        Appended slices are validated sequence-by-sequence by the reconciler; this constant-time
        check protects every commit from a duplicated/holed tail without rescanning long runs.
        """
        known = len(execution.events)
        if execution.session.last_event_sequence != known:
            raise ValueError(f"execution session tip {execution.session.last_event_sequence} differs from in-memory prefix {known}")
        if execution.events and execution.events[-1].sequence != known:
            raise ValueError(f"execution event prefix ends at sequence {execution.events[-1].sequence}, expected {known}")
        return known

    def _reconcile_execution_events(
        self,
        execution: _Execution,
        *,
        upto: int,
        session: SessionRecord | None = None,
        parked: bool = False,
        allowed_sequences: frozenset[int] = frozenset(),
    ) -> None:
        """Append exactly the durable events missing from the in-memory prefix, never a tail.

        The execution projection is only valid as one unique contiguous prefix. Every
        concurrent publisher appends to SQLite outside the session lock, so all callers must
        reconcile against a bounded target tip and then adopt that same session snapshot.
        A parked-prefix reconcile accepts only control facts that may legally cross a
        checkpoint, plus explicitly claimed sequences from the same running run.
        """
        known = self._assert_execution_prefix(execution)
        if upto < known:
            raise ValueError(f"cannot reconcile backwards: session tip {upto} is below in-memory prefix {known}")
        if session is not None and session.last_event_sequence != upto:
            raise ValueError(f"reconcile target {upto} does not match its session snapshot {session.last_event_sequence}")
        if upto > known:
            events = self.store.events(execution.session.session_id, after_sequence=known, limit=upto - known)
            if len(events) != upto - known:
                raise ValueError(f"durable event prefix {known + 1}..{upto} is incomplete")
            for offset, event in enumerate(events, start=1):
                if event.sequence != known + offset:
                    raise ValueError(f"durable event prefix is not contiguous at sequence {event.sequence}")
                if parked and event.sequence not in allowed_sequences and not _is_parked_intervening(event):
                    raise ValueError(f"intervening event {event.event_type} violates the parked checkpoint prefix")
            execution.events.extend(events)
        if session is not None:
            execution.session = session

    def _commit_staged_rebased(
        self,
        execution: _Execution,
        *,
        status: str,
        kind: str | None,
        events: Sequence[EventDraft] = (),
        staged_state: Sequence[StagedStateUpdate] = (),
        staged_continuations: Sequence[StagedContinuationDraft] = (),
        staged_cancellations: Sequence[StagedCancellation] = (),
        staged_budgets: Sequence[StagedBudgetUpdate] = (),
        observer_receipts: Sequence[ObserverReceiptRecord] = (),
        completed_continuation_id: str | None = None,
        completed_continuation_status: Literal["completed", "failed", "cancelled"] = "completed",
        reserve_root: bool = False,
        start_turn: bool = False,
        checkpoint_bindings: Sequence[tuple[str, str]] = (),
    ) -> tuple[StoredEvent, ...]:
        """Commit against the fresh session tip, validating any intervening parked events first.

        Continuation drafts enqueued during this run inherit its root action chain,
        an optional root reservation happens inside the same transaction as the
        intent event it pays for, and bound checkpoint references adopt the actual
        appended sequences before any observer sees the new state.

        A SequenceConflict means the whole atomic transaction committed nothing, so the same
        payload is retried after reconciling to the fresh tip; no external invocation or
        plugin proposal is re-executed. Every other conflict is never replayed.
        """
        events = self._stamp_fact_owners(events)
        for attempt in range(16):
            current = self.store.session(execution.session.session_id)
            self._reconcile_execution_events(
                execution,
                upto=current.last_event_sequence,
                session=current,
                parked=True,
            )
            previous = current
            before = len(execution.events)
            root_id = execution.cursor.root_id
            root_reservation: tuple[str, int] | None = None
            if reserve_root and root_id:
                root_budget = execution.cursor.initial_root_budget
                root_reservation = (root_id, max(0, root_budget if root_budget is not None else 32))
            stamped_continuations = tuple(
                draft if draft.root_id is not None or not root_id else replace(draft, root_id=root_id) for draft in staged_continuations
            )
            try:
                committed = self.store.commit_staged(
                    previous.session_id,
                    expected_sequence=previous.last_event_sequence,
                    expected_run_id=previous.run_id,
                    run_id=execution.run_id,
                    revision_id=self.revision_id,
                    events=tuple(events),
                    status=status,
                    checkpoint_kind=kind,
                    checkpoint_payload=execution.cursor.model_dump() if kind is not None else None,
                    root_reservation=root_reservation,
                    staged_state=staged_state,
                    staged_continuations=stamped_continuations,
                    staged_cancellations=staged_cancellations,
                    staged_budgets=staged_budgets,
                    observer_receipts=observer_receipts,
                    completed_continuation_id=completed_continuation_id,
                    completed_continuation_status=completed_continuation_status,
                    start_turn=start_turn,
                    checkpoint_bindings=checkpoint_bindings,
                )
            except SequenceConflict:
                if attempt < 15:
                    continue
                raise
            self._reconcile_execution_events(execution, upto=committed.last_event_sequence, session=committed)
            new_events = tuple(execution.events[before:])
            self._bind_checkpoint_refs(execution.cursor, new_events, checkpoint_bindings)
            return new_events
        raise AssertionError("unreachable commit retry loop")

    @staticmethod
    def _bind_checkpoint_refs(
        cursor: _Cursor,
        events: Sequence[StoredEvent],
        bindings: Sequence[tuple[str, str]],
    ) -> None:
        """Adopt the durable event references this commit bound into the checkpoint.

        The store wrote exactly these sequences inside its transaction; the
        in-memory cursor must agree before any observer callback reads it.
        """
        for bound_field, bound_event_type in bindings:
            sequence = next((ev.sequence for ev in events if ev.event_type == bound_event_type), None)
            if sequence is None:
                raise ValueError(f"checkpoint binding {bound_field!r} has no committed {bound_event_type!r} event")
            setattr(cursor, bound_field, sequence)

    def _stamp_fact_owners(self, events: Sequence[EventDraft]) -> tuple[EventDraft, ...]:
        """Freeze the fact subscriptions of the producing generation onto each fact event.

        Delivery eligibility must survive a restart with changed plugin source: the owners that
        were registered when the fact was written stay its only eligible recipients, forever.
        """
        owners = self.plugin_runtime.observer_owners
        stamped: list[EventDraft] = []
        for event in events:
            fact_type = "usage" if event.event_type == EventType.PROVIDER_INTENT else _FACT_EVENT_TYPES.get(event.event_type)
            if fact_type is None or "eligible_owners" in event.payload:
                stamped.append(event)
                continue
            stamped.append(
                EventDraft(
                    event.event_type,
                    event.source,
                    event.actor,
                    {**event.payload, "eligible_owners": list(owners.get(fact_type, ()))},
                )
            )
        return tuple(stamped)

    def _fact_for(self, execution: _Execution, ev: StoredEvent) -> tuple[str, dict[str, object]] | None:
        return fact_for_event(ev, execution.events)

    def _measured_run_elapsed_fields(self, execution: _Execution) -> dict[str, int]:
        """Stamp the duration this process actually measured for the run, if it dispatched at all."""
        starts: list[float] = []
        for event in execution.events:
            if event.run_id != execution.run_id or event.event_type != EventType.PROVIDER_INTENT:
                continue
            start_ms = event.payload.get("start_time_ms")
            if isinstance(start_ms, (int, float)) and not isinstance(start_ms, bool):
                starts.append(float(start_ms))
        if not starts:
            return {}
        return {"elapsed_ms": max(0, int(time.time() * 1000 - min(starts)))}

    def _commit(
        self,
        execution: _Execution,
        status: str,
        kind: str,
        *events: EventDraft,
        start_turn: bool = False,
        staged_state: Sequence[StagedStateUpdate] = (),
        staged_continuations: Sequence[StagedContinuationDraft] = (),
        staged_cancellations: Sequence[StagedCancellation] = (),
        staged_budgets: Sequence[StagedBudgetUpdate] = (),
        completed_continuation_id: str | None = None,
        completed_continuation_status: Literal["completed", "failed", "cancelled"] = "completed",
        reserve_root: bool = False,
        checkpoint_bindings: Sequence[tuple[str, str]] = (),
    ) -> tuple[StoredEvent, ...]:
        known_before = len(execution.events)
        new_events = self._commit_staged_rebased(
            execution,
            status=status,
            kind=kind,
            events=events,
            reserve_root=reserve_root,
            checkpoint_bindings=checkpoint_bindings,
            staged_state=staged_state,
            staged_continuations=staged_continuations,
            staged_cancellations=staged_cancellations,
            staged_budgets=staged_budgets,
            completed_continuation_id=completed_continuation_id,
            completed_continuation_status=completed_continuation_status,
            start_turn=start_turn,
        )
        self._deliver_facts(execution, execution.events[known_before:], tolerate_conflicts=True)
        return new_events

    def _budget_admission(self, execution: _Execution) -> CoreResult | None:
        """Refuse dispatch when an active owner budget is already satisfied.

        Each metric is checked independently with its own unknown flag: a known
        input total with an unknown output total must still obey an input cap,
        and only the caps whose metric is genuinely unknown fail visibly.
        """
        for b in self.store.session_budgets(execution.session.session_id):
            u_report = self._get_session_usage(
                execution.session.session_id,
                since_sequence=b.since_sequence,
                session=execution.session,
            )
            if b.max_input_tokens is not None:
                if u_report.input_tokens is None:
                    return self._fail(execution, f"budget limit reached: input tokens unknown for owner {b.owner}")
                if u_report.input_tokens >= b.max_input_tokens:
                    return self._stop(
                        execution,
                        f"budget limit exceeded: input tokens {u_report.input_tokens} >= {b.max_input_tokens} for owner {b.owner}",
                        stop_cause="limit_reached",
                    )
            if b.max_output_tokens is not None:
                if u_report.output_tokens is None:
                    return self._fail(execution, f"budget limit reached: output tokens unknown for owner {b.owner}")
                if u_report.output_tokens >= b.max_output_tokens:
                    return self._stop(
                        execution,
                        f"budget limit exceeded: output tokens {u_report.output_tokens} >= {b.max_output_tokens} for owner {b.owner}",
                        stop_cause="limit_reached",
                    )
            if b.max_total_tokens is not None:
                if u_report.input_tokens is None or u_report.output_tokens is None:
                    return self._fail(execution, f"budget limit reached: total tokens unknown for owner {b.owner}")
                tot = (u_report.input_tokens or 0) + (u_report.output_tokens or 0)
                if tot >= b.max_total_tokens:
                    return self._stop(
                        execution,
                        f"budget limit exceeded: total tokens {tot} >= {b.max_total_tokens} for owner {b.owner}",
                        stop_cause="limit_reached",
                    )
            if b.max_elapsed_seconds is not None:
                if u_report.elapsed_seconds is None:
                    return self._fail(execution, f"budget limit reached: elapsed time unknown for owner {b.owner}")
                if u_report.elapsed_seconds >= b.max_elapsed_seconds:
                    return self._stop(
                        execution,
                        f"budget limit exceeded: elapsed time {u_report.elapsed_seconds}s >= {b.max_elapsed_seconds}s for owner {b.owner}",
                        stop_cause="limit_reached",
                    )
            if b.max_cost_usd is not None:
                if u_report.total_cost_usd is None:
                    return self._fail(execution, f"budget limit reached: cost unknown for owner {b.owner}")
                if u_report.total_cost_usd >= b.max_cost_usd:
                    return self._stop(
                        execution,
                        f"budget limit exceeded: cost ${u_report.total_cost_usd} >= ${b.max_cost_usd} for owner {b.owner}",
                        stop_cause="limit_reached",
                    )
        return None

    def _abort_stop(self, execution: _Execution, session_abort: Cancellation | None) -> CoreResult | None:
        """Stop the run when the core or its session was cancelled."""
        if session_abort is not None and session_abort.cancelled:
            return self._stop(execution, session_abort.reason or "cancelled", stop_cause=session_abort.stop_cause or "user_stop")
        if self._abort.cancelled:
            return self._stop(execution, self._abort.reason or "cancelled", stop_cause=self._abort.stop_cause or "shutdown")
        return None

    def _pending(self, execution: _Execution) -> tuple[StoredEvent, ToolCall]:
        cursor = execution.cursor
        if cursor.deferred_sequence is not None:
            event = self._event(execution, cursor.deferred_sequence, EventType.TOOL_DEFERRED)
            return event, decode_tool_call(_wire_object(event.payload["call"], "deferred tool call"))
        if cursor.batch_sequence is None:
            raise ValueError("checkpoint has no pending batch")
        event = self._event(execution, cursor.batch_sequence, EventType.MODEL_TURN)
        turn = decode_provider_result(_wire_object(event.payload["result"], "model turn"))
        if cursor.next_call >= len(turn.tool_calls):
            raise ValueError("checkpoint is outside its native batch")
        return event, turn.tool_calls[cursor.next_call]

    def _next_deferred(self, execution: _Execution) -> StoredEvent | None:
        """Unsettled requests of this chain, in commit order; no synthetic provider turns."""
        completed = {
            event.payload["batch_sequence"]
            for event in execution.events
            if event.event_type in {EventType.TOOL_COMPLETED, EventType.TOOL_INTERRUPTED} and event.payload.get("deferred") is True
        }
        return next(
            (
                event
                for event in execution.events
                if event.event_type == EventType.TOOL_DEFERRED
                and event.sequence not in completed
                and event.payload.get("root_id", "") in {"", execution.cursor.root_id}
            ),
            None,
        )

    def _post_tool_hooks(
        self,
        execution: _Execution,
        snapshot: _Snapshot,
        call: ToolCall,
        result: ToolResult,
    ) -> ToolResult:
        """Run declared post-tool hooks inside this invocation, before its result is durable.

        Only a successful result is offered: a hook observes what the tool actually did and
        appends one note (a hook reporting what it observed), never a rewritten verdict.
        A hook failure is recorded as a visible note on the same result, so a broken hook
        cannot silently disappear from the record the model and the events both carry.
        """
        if isinstance(result, ToolFailure):
            return result
        text = result.output.text if isinstance(result.output, TextOutput) else ""
        candidates = [hook for hook in snapshot.plugin_runtime.post_tool_hooks if call.tool_name in hook[1] or "*" in hook[1]]
        if not candidates:
            return result
        restricted = self._session_read_only(execution.session.session_id)
        notes: list[str] = []
        for owner, _match, mutating in candidates:
            if mutating and restricted:
                # A mutating hook cannot run under a restrictive stance: formatting writes files.
                notes.append(f"[hook {owner} skipped: read-only policy]")
                continue
            try:
                note = snapshot.plugin_runtime.post_tool(
                    owner,
                    session_id=execution.session.session_id,
                    run_id=execution.run_id,
                    tool_name=call.tool_name,
                    tool_call_id=call.tool_call_id,
                    status="ok",
                    text=text,
                    arguments=call.arguments,
                    readonly=restricted,
                    logical_turn=execution.cursor.logical_turn,
                )
            except Exception as exc:
                notes.append(f"[hook {owner} failed: {type(exc).__name__}: {exc}]")
                continue
            if note:
                notes.append(note)
        if not notes:
            return result
        return ToolSuccess(
            result.tool_name,
            TextOutput("\n\n".join((text, *notes)), bounds=result.output.bounds),
            result.body,
            result.coverage,
            result.changes,
        )

    @staticmethod
    def _request_window(
        messages: tuple[ProviderMessage, ...],
        tools: tuple[ToolDefinition, ...],
    ) -> RequestWindow:
        """Measure the request exactly as it is about to be sent (core truth, no estimates).

        Per-segment facts are character counts and digests of the literal strings, so a client
        panel derives its view from measurements; token counts stay provider-reported usage.
        """
        segments: list[WindowSegment] = []
        message_characters = 0
        for message in messages:
            content = message.content or ""
            characters = len(content)
            message_characters += characters
            segments.append(
                WindowSegment(
                    role=message.role,
                    characters=characters,
                    sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    tool_calls=len(message.tool_calls),
                    tool_name=message.tool_name,
                    reasoning_characters=len(message.reasoning or ""),
                )
            )
        tool_schema_characters = sum(len(canonical_json(encode_tool_definition(tool))) for tool in tools)
        return RequestWindow(
            message_characters=message_characters,
            tool_schema_characters=tool_schema_characters,
            segments=tuple(segments),
            tool_count=len(tools),
        )

    def _lifecycle_events(
        self,
        execution: _Execution,
        snapshot: _Snapshot,
        kind: str,
        *,
        status: str = "",
        error: str | None = None,
    ) -> tuple[EventDraft, ...]:
        """Notify declared lifecycle hooks."""
        hooks = [owner for owner, hook_kind in snapshot.plugin_runtime.lifecycle_hooks if hook_kind == kind]
        for owner in hooks:
            try:
                snapshot.plugin_runtime.run_lifecycle(
                    owner,
                    kind,
                    session_id=execution.session.session_id,
                    run_id=execution.run_id,
                    logical_turn=execution.cursor.logical_turn,
                    status=status,
                    error=error,
                )
            except Exception:
                pass
        return ()

    def _gate_denial(
        self,
        execution: _Execution,
        snapshot: _Snapshot,
        call: ToolCall,
        *,
        readonly: bool,
    ) -> str | None:
        """Ask every declared pre-tool gate; failure or a stated reason denies the call.

        Fail-closed by construction: a gate that errors, times out, or vanishes denies, because
        a gate exists to refuse work and a broken gate must never be read as permission.
        """
        gates = [hook for hook in snapshot.plugin_runtime.pre_tool_hooks if call.tool_name in hook[1] or "*" in hook[1]]
        for owner, _match, mutating in gates:
            if mutating and readonly:
                continue
            try:
                reason = snapshot.plugin_runtime.pre_tool(
                    owner,
                    session_id=execution.session.session_id,
                    run_id=execution.run_id,
                    tool_name=call.tool_name,
                    tool_call_id=call.tool_call_id,
                    arguments=call.arguments,
                    readonly=readonly,
                    logical_turn=execution.cursor.logical_turn,
                )
            except Exception as exc:
                return f"pre-tool gate {owner} failed: {type(exc).__name__}: {exc}"
            if reason:
                return f"pre-tool gate {owner} denied the call: {reason}"
        return None

    def _session_read_only(self, session_id: str) -> bool:
        """Effective read-only for one session: the frozen core policy OR owner overlays."""
        if self.config.policy.read_only:
            return True
        return bool(self.store.session_overlays(session_id))

    def _visible_tools(self, execution: _Execution, snapshot: _Snapshot) -> tuple[ToolDefinition, ...]:
        """The catalog this session may see: policy scope, then restrictive overlays."""
        tools = snapshot.tools
        scope = execution.session.tool_allowlist
        if scope is not None:
            tools = tuple(tool for tool in tools if tool.name in scope)
        if self._session_read_only(execution.session.session_id):
            tools = tuple(tool for tool in tools if not denied_under_restriction(tool.effects))
        return tools

    def _visible_tool_definition(self, session_id: str, name: str, revision_id: str) -> tuple[Mapping[str, object] | None, str]:
        """One definition from the active revision's dispatch snapshot, with this session's filters.

        The same source and the same predicate `_visible_tools` applies: the session's profile
        scope first, then the effective read-only stance (frozen policy or owner overlays). It
        answers `(None, reason)` instead of raising, so a documentation read reports the actual
        visibility instead of guessing it.
        """
        snapshot = self._snapshot
        if snapshot.revision_id != revision_id:
            raise ValueError("the lease's revision is no longer the active capability revision")
        definition = snapshot.definition_of(name)
        if definition is None:
            return None, "unknown"
        session = self.store.session(session_id)
        scope = session.tool_allowlist
        if scope is not None and name not in scope:
            return None, "scope"
        if self._session_read_only(session_id) and denied_under_restriction(definition.effects):
            return None, "read_only"
        return encode_tool_definition(definition), "visible"

    def _denial(self, call: ToolCall, definition: ToolDefinition, *, read_only: bool | None = None) -> str | None:
        validator = self._snapshot.validator_of(definition.name)
        if validator is not None:
            try:
                validator.validate(json_wire_object(call.arguments))
            except jsonschema.ValidationError:
                return "tool arguments failed the admitted JSON schema"
        # read_only denies EXTERNAL mutation only: reads and internal state
        # operations (SESSION) stay available in every mode. The session's effective
        # restriction includes its owner overlays (plan mode) as well as the frozen policy.
        restricted = self.config.policy.read_only if read_only is None else read_only
        if restricted and denied_under_restriction(definition.effects):
            return "read-only policy denies the declared tool effects"
        return None

    def _needs_approval(self, definition: ToolDefinition) -> bool:
        effects = definition.effects
        if not has_external_mutations(effects):
            # Reads and internal state operations (SESSION) mutate nothing
            # external, so no mode gates them behind a human decision.
            return False
        mode = self.config.policy.approval_mode
        if mode == "yolo":
            return False
        return not (mode == "write" and effects and effects <= {ToolEffect.READ, ToolEffect.WRITE})

    @staticmethod
    def _validate_steps(max_steps: int) -> None:
        if type(max_steps) is not int or max_steps < 1:
            raise ValueError("max_steps must be a positive integer")

    def _drive(self, execution: _Execution, *, max_steps: int) -> CoreResult:
        while True:
            session_abort = None
            with self._aborts_lock:
                session_abort = self._session_aborts.get(execution.session.session_id)
            if (session_abort and session_abort.cancelled) or self._abort.cancelled:
                cause = (session_abort.stop_cause if session_abort and session_abort.cancelled else "shutdown") or "user_stop"
                reason = (session_abort.reason if session_abort and session_abort.cancelled else self._abort.reason) or "cancelled"
                return self._stop(execution, reason, stop_cause=cause)
            snapshot = self._snapshot
            cursor = execution.cursor
            if cursor.batch_sequence is None and cursor.deferred_sequence is None:
                request = self._next_deferred(execution)
                if request is not None:
                    cursor.deferred_sequence = request.sequence
                    cursor.deferred_depth = cast(int, request.payload["depth"])
                    try:
                        self._commit(
                            execution,
                            "running",
                            "ready",
                            reserve_root=True,
                        )
                    except RootBudgetExhausted:
                        _, call = self._pending(execution)
                        self._report(execution, call, ToolFailure(call.tool_name, "root action budget exhausted"))
                        return self._stop(execution, "root action budget exhausted", stop_cause="limit_reached")
            if cursor.batch_sequence is None and cursor.deferred_sequence is None:
                # A staged activation applies only here: the whole previous native batch has been
                # reported and committed, so no result can mix generations, and the request is
                # consumed durably in the same transaction as the switch.
                pending_request = self._pending_activation_request(execution)
                if pending_request is not None:
                    outcome = self.activate(
                        session_id=execution.session.session_id,
                        source="model",
                        request_id=str(pending_request.get("request_id", "")),
                        target_revision_id=str(pending_request.get("target_revision_id", "")),
                    )
                    if outcome["activated"]:
                        # This run is the automatic continuation the activation implied; the
                        # next commit persists the consumed intent.
                        execution.cursor.resume_intent = ""
                    execution.session = self.store.session(execution.session.session_id)
                    execution.events = list(self.store.events(execution.session.session_id))
                    cursor = execution.cursor
                    snapshot = self._snapshot
                if cursor.deferred_only:
                    self._commit(
                        execution,
                        "completed",
                        "terminal",
                        EventDraft(
                            EventType.RUN_COMPLETED,
                            "kernel",
                            "plugin",
                            {"output": None, "logical_turn": cursor.logical_turn, **self._measured_run_elapsed_fields(execution)},
                        ),
                    )
                    return self._result(execution)
                if cursor.step >= max_steps:
                    return self._stop(execution, "provider turn budget exhausted", stop_cause="limit_reached")

                budget_stop = self._budget_admission(execution)
                if budget_stop is not None:
                    return budget_stop

                # Safe boundary: every member of the previous native batch is reported and
                # committed, so messages addressed to this running session may join THIS run
                # before the next provider dispatch. Never across an in-flight tool call, an
                # approval checkpoint, or a token stream.
                self._delegation._merge_child_messages(execution)

                with self._dispatch("provider") as snapshot:
                    if not execution.lifecycle_started:
                        self._lifecycle_events(execution, snapshot, "session_start")
                        execution.lifecycle_started = True
                    model = snapshot.config.model
                    if model is None:
                        return self._fail(execution, MODEL_CONFIGURATION_GUIDANCE)
                    provider_name, separator, model_name = model.partition("/")
                    if not separator or not model_name or provider_name not in snapshot.plugin_runtime.providers:
                        return self._fail(execution, "model must name a configured provider/model")
                    # Settle any earlier dispatch of this run that never completed BEFORE
                    # context assembly: observers see unknown usage exactly once, and any
                    # budget they set (even a zero cap) or cancellation they raise is
                    # already durable when the admission checks below read it.
                    self._settle_unsettled_dispatches(execution)
                    try:
                        messages = self._messages(execution)
                    except ValueError as exc:
                        return self._fail(execution, str(exc))
                    except (RuntimeError, TimeoutError) as exc:
                        # Critical context/admission failure: the run finishes durably
                        # failed before any intent is committed or provider is billed.
                        return self._fail(execution, f"context assembly failed: {type(exc).__name__}: {exc}")

                    effective_abort = session_abort or self._abort
                    request = ProviderTurnRequest(
                        messages=messages,
                        available_tools=self._visible_tools(execution, snapshot),
                        raw_model=model,
                        provider_name=provider_name,
                        model_name=model_name,
                        reasoning_effort=snapshot.config.reasoning_effort,
                        abort_signal=effective_abort,
                    )

                    dispatch_id = uuid4().hex
                    start_time_ms = int(time.time() * 1000)
                    # Final admission: context assembly may have taken a long RPC, so
                    # re-check cancellation and budgets immediately before the native
                    # call that reserves the root step and bills the provider.
                    late_abort = self._abort_stop(execution, session_abort)
                    if late_abort is not None:
                        return late_abort
                    late_budget = self._budget_admission(execution)
                    if late_budget is not None:
                        return late_budget
                    # A fresh provider dispatch is a new logical turn. The current
                    # native batch keeps this identity (checkpointed), so approvals,
                    # tool reports, and resumes all attribute to the same turn.
                    cursor.logical_turn = uuid4().hex
                    window = self._request_window(messages, request.available_tools)
                    try:
                        intent_events = self._commit(
                            execution,
                            "running",
                            "ready",
                            EventDraft(
                                EventType.PROVIDER_INTENT,
                                "kernel",
                                "kernel",
                                {
                                    "attempt": cursor.step,
                                    "provider": provider_name,
                                    "model": model_name,
                                    "dispatch_id": dispatch_id,
                                    "start_time_ms": start_time_ms,
                                    "logical_turn": execution.cursor.logical_turn,
                                    # Measured inventory of this exact request: a client
                                    # panel reads facts here instead of estimating tokens.
                                    "request_window": window.as_payload(),
                                },
                            ),
                            reserve_root=True,
                        )
                    except RootBudgetExhausted:
                        return self._stop(execution, "root action budget exhausted", stop_cause="limit_reached")
                    intent_seq = next(ev.sequence for ev in intent_events if ev.event_type == EventType.PROVIDER_INTENT)

                    t0 = time.monotonic()
                    try:
                        if snapshot.plugin_runtime.provider_supports_streaming(provider_name):
                            turn = self._stream_provider_turn(
                                snapshot,
                                execution,
                                request,
                                provider_name=provider_name,
                                dispatch_id=dispatch_id,
                                dispatch_sequence=intent_seq,
                            )
                        else:
                            turn = snapshot.plugin_runtime.propose_turn(request, provider=provider_name)
                        elapsed_ms = max(0, int((time.monotonic() - t0) * 1000))
                    except ProviderExecutionError as exc:
                        elapsed_ms = max(0, int((time.monotonic() - t0) * 1000))
                        self._commit(
                            execution,
                            "interrupted",
                            "interrupted",
                            EventDraft(
                                EventType.PROVIDER_INTERRUPTED,
                                "kernel",
                                "kernel",
                                {
                                    "error": str(exc),
                                    "unknown_usage": True,
                                    "dispatch_sequence": intent_seq,
                                    "dispatch_id": dispatch_id,
                                    "elapsed_ms": elapsed_ms,
                                    "provider": provider_name,
                                    "model": model_name,
                                },
                            ),
                        )
                        # Only a real cancellation may report a user decision; a provider
                        # failure is a service failure, never a user stop.
                        if effective_abort.cancelled:
                            return self._stop(
                                execution,
                                str(exc),
                                stop_cause=effective_abort.stop_cause or "user_stop",
                            )
                        return self._fail(execution, f"provider failed: {exc}")
                    except Exception as exc:
                        elapsed_ms = max(0, int((time.monotonic() - t0) * 1000))
                        self._commit(
                            execution,
                            "interrupted",
                            "interrupted",
                            EventDraft(
                                EventType.PROVIDER_INTERRUPTED,
                                "kernel",
                                "kernel",
                                {
                                    "error": str(exc),
                                    "unknown_usage": True,
                                    "dispatch_sequence": intent_seq,
                                    "dispatch_id": dispatch_id,
                                    "elapsed_ms": elapsed_ms,
                                    "provider": provider_name,
                                    "model": model_name,
                                },
                            ),
                        )
                        return self._fail(execution, f"provider failed: {type(exc).__name__}: {exc}")

                    cursor.step += 1

                    ids = [call.tool_call_id for call in turn.tool_calls]
                    has_duplicate_or_missing_ids = any(not identity for identity in ids) or len(ids) != len(set(ids))
                    invalid_done_reason = turn.done_reason not in {"stop", "tool_calls", "completed"}

                    model_turn_payload: dict[str, object] = {
                        "result": encode_provider_result(turn),
                        "dispatch_sequence": intent_seq,
                        "dispatch_id": dispatch_id,
                        "elapsed_ms": elapsed_ms,
                        "provider": provider_name,
                        "model": model_name,
                    }
                    if has_duplicate_or_missing_ids:
                        self._commit(
                            execution,
                            "failed",
                            "terminal",
                            EventDraft(EventType.MODEL_TURN, "provider", "model", model_turn_payload),
                            completed_continuation_id=execution.cursor.continuation_id,
                            completed_continuation_status="failed",
                        )
                        return self._fail(execution, "provider returned missing/duplicate native call identities")

                    if invalid_done_reason:
                        self._commit(
                            execution,
                            "failed",
                            "terminal",
                            EventDraft(EventType.MODEL_TURN, "provider", "model", model_turn_payload),
                            completed_continuation_id=execution.cursor.continuation_id,
                            completed_continuation_status="failed",
                        )
                        return self._fail(execution, f"provider did not complete its turn: {turn.done_reason}")

                    if turn.tool_calls:
                        # A native batch starts at its first member, and the checkpoint
                        # references this exact appended turn in the same transaction:
                        # a crash can never leave a batch event without its checkpoint ref.
                        cursor.next_call = 0
                        self._commit(
                            execution,
                            "running",
                            "ready",
                            EventDraft(EventType.MODEL_TURN, "provider", "model", model_turn_payload),
                            checkpoint_bindings=(("batch_sequence", EventType.MODEL_TURN),),
                        )
                    else:
                        self._commit(
                            execution,
                            "completed",
                            "terminal",
                            EventDraft(EventType.MODEL_TURN, "provider", "model", model_turn_payload),
                            EventDraft(
                                EventType.RUN_COMPLETED,
                                "kernel",
                                "kernel",
                                {"logical_turn": execution.cursor.logical_turn, **self._measured_run_elapsed_fields(execution)},
                            ),
                            *self._lifecycle_events(execution, snapshot, "session_end", status="completed"),
                            completed_continuation_id=execution.cursor.continuation_id,
                            completed_continuation_status="completed",
                        )
                        return self._result(execution, output=turn.output, stop_cause="completed")
                    if self._abort.cancelled or (session_abort and session_abort.cancelled):
                        cause = (session_abort.stop_cause if session_abort and session_abort.cancelled else "shutdown") or "user_stop"
                        reason = (session_abort.reason if session_abort and session_abort.cancelled else self._abort.reason) or "cancelled"
                        return self._stop(execution, reason, stop_cause=cause)

            batch, call = self._pending(execution)
            if cursor.deferred_sequence is not None and batch.payload["expected_revision"] != snapshot.revision_id:
                self._report(execution, call, ToolFailure(call.tool_name, "requested tool belongs to a different capability revision"))
                continue
            scope = execution.session.tool_allowlist
            if scope is not None and call.tool_name not in scope:
                self._report(
                    execution,
                    call,
                    ToolFailure(call.tool_name, "tool is outside this session's profile scope"),
                )
                continue
            definition = self._snapshot.definition_of(call.tool_name)
            if definition is None:
                self._report(execution, call, ToolFailure(call.tool_name, "tool is not in the active capability revision"))
                continue
            effective_read_only = self._session_read_only(execution.session.session_id)
            denial = self._denial(call, definition, read_only=effective_read_only)
            if denial is None:
                denial = self._gate_denial(execution, snapshot, call, readonly=effective_read_only)
            if denial:
                self._report(execution, call, ToolFailure(call.tool_name, denial))
                continue
            if cursor.intent_sequence is None and self._needs_approval(definition):
                payload = {
                    "request_id": uuid4().hex,
                    "batch_sequence": batch.sequence,
                    "call": encode_tool_call(call),
                    "owner": snapshot.owner_of(call.tool_name),
                }
                if call.tool_name in snapshot.native_tools:
                    # Pin the exact candidate this approval is about, before the user is asked:
                    # admission imports nothing, and after approval the already-pinned artifact
                    # is activated, so editing the workspace source in between cannot change it.
                    target_revision_id, _admission = self._prepare_target()
                    payload["target_revision_id"] = target_revision_id
                    payload["activation_pending"] = True
                self._commit(
                    execution,
                    "waiting",
                    "approval_wait",
                    EventDraft(EventType.APPROVAL_REQUESTED, "kernel", "plugin" if cursor.deferred_sequence is not None else "model", payload),
                    checkpoint_bindings=(("approval_sequence", EventType.APPROVAL_REQUESTED),),
                )
                return self._result(execution, pending_approval=payload)
            stopped = self._invoke(execution, call, definition)
            if stopped is not None:
                return stopped

    def _invoke(self, execution: _Execution, call: ToolCall, definition: ToolDefinition) -> CoreResult | None:
        with self._dispatch(f"tool:{call.tool_name}") as snapshot:
            cursor = execution.cursor
            if cursor.intent_sequence is None:
                invocation_id = uuid4().hex
                self._commit(
                    execution,
                    "running",
                    "ready",
                    EventDraft(
                        EventType.TOOL_INTENT,
                        "kernel",
                        "plugin" if cursor.deferred_sequence is not None else "model",
                        {
                            "invocation_id": invocation_id,
                            "batch_sequence": cursor.deferred_sequence or cursor.batch_sequence,
                            "call": encode_tool_call(call),
                            "owner": snapshot.owner_of(call.tool_name),
                            "effects": sorted(effect.value for effect in definition.effects),
                            "semantics": [],
                            "replay_policy": definition.effective_replay_policy,
                            "icon": {"glyph": definition.icon.glyph, "fallback": definition.icon.fallback} if definition.icon is not None else None,
                            "logical_turn": execution.cursor.logical_turn,
                            "depth": cursor.deferred_depth,
                        },
                    ),
                    checkpoint_bindings=(("intent_sequence", EventType.TOOL_INTENT),),
                )
            else:
                intent = self._event(execution, cursor.intent_sequence, EventType.TOOL_INTENT)
                invocation_id = str(intent.payload["invocation_id"])

            session_abort = None
            with self._aborts_lock:
                session_abort = self._session_aborts.get(execution.session.session_id)
            effective_abort = session_abort or self._abort

            if call.tool_name in self._snapshot.native_tools:
                # A core-owned tool only stages a durable, approved request; the switch itself
                # happens at the next batch boundary, never inside this callback's stack.
                request_id = uuid4().hex
                target = str(self._bound_target(execution, call) or "")
                result: ToolResult = ToolSuccess(
                    call.tool_name,
                    TextOutput("activation request recorded; it is applied after this tool batch finishes"),
                )
                self._report(
                    execution,
                    call,
                    result,
                    staged_events=(
                        EventDraft(
                            EventType.ACTIVATION_REQUESTED,
                            "kernel",
                            "model",
                            {
                                "request_id": request_id,
                                "source": "model",
                                "run_id": execution.run_id,
                                "logical_turn": execution.cursor.logical_turn,
                                "reason": str(call.arguments.get("reason", "")),
                                "target_revision_id": target,
                            },
                        ),
                    ),
                )
                return None
            context = ToolContext(
                workspace=self.workspace,
                session_id=execution.session.session_id,
                run_id=execution.run_id,
                invocation_id=invocation_id,
                revision_id=snapshot.revision_id,
                actor="plugin" if cursor.deferred_sequence is not None else "model",
                logical_turn=execution.cursor.logical_turn,
                abort_signal=effective_abort,
                invocation_depth=cursor.deferred_depth,
            )
            try:
                outcome = snapshot.plugin_runtime.invoke(
                    call,
                    context=context,
                    return_outcome=True,
                    readonly=self._session_read_only(execution.session.session_id),
                )
                if not isinstance(outcome, ToolInvocationOutcome):
                    raise TypeError("plugin runtime returned a bare tool result for an outcome request")
                result = outcome.result
                staged_state = outcome.staged_state
                staged_events = outcome.staged_events
                staged_conts = outcome.staged_continuations
                staged_cancels = outcome.staged_cancellations
                staged_budget = outcome.staged_budget
                staged_observations = outcome.staged_observations
            except ActivationFenced as exc:
                if effective_abort.cancelled:
                    return self._stop(execution, str(exc), stop_cause=effective_abort.stop_cause or "user_stop")
                return self._stop(execution, str(exc), stop_cause="error")
            except Exception as exc:
                result = ToolFailure(call.tool_name, f"tool failed: {type(exc).__name__}: {exc}")
                staged_state = ()
                staged_events = ()
                staged_conts = ()
                staged_cancels = ()
                staged_budget = None
                staged_observations = ()

            if result.tool_name != call.tool_name:
                return self._stop(execution, "tool result identity differs from the dispatched native call", stop_cause="error")

            if effective_abort.cancelled:
                # A cancelled call's self-report is unverifiable: it may have been killed
                # mid-write, so the batch stays parked and recovery reports unknown effects.
                return self._stop(execution, "tool invocation was cancelled", stop_cause=effective_abort.stop_cause or "user_stop")

            result = self._post_tool_hooks(execution, snapshot, call, result)

            self._report(
                execution,
                call,
                result,
                staged_state=staged_state,
                staged_events=staged_events,
                staged_conts=staged_conts,
                staged_cancels=staged_cancels,
                staged_budget=staged_budget,
                staged_observations=staged_observations,
            )
            claim_id = execution.cursor.continuation_id
            claim_owner = self.store.continuation_owner(claim_id) if claim_id is not None else None
            if any(self._cancels_claimed(cancellation, claim_id, claim_owner) for cancellation in staged_cancels):
                # The tool cancelled its own claimed work: the effect and audit above are
                # durable, and this chain stops here with the owner cause it stated.
                return self._stop(execution, "tool cancelled its own durable work", stop_cause="owner_cancel")
            return None

    def _report(
        self,
        execution: _Execution,
        call: ToolCall,
        result: ToolResult,
        *,
        interrupted: bool = False,
        staged_state: tuple[StagedStateUpdate, ...] = (),
        staged_events: tuple[EventDraft, ...] = (),
        staged_conts: tuple[StagedContinuationDraft, ...] = (),
        staged_cancels: tuple[StagedCancellation, ...] = (),
        staged_budget: PluginBudget | None | Literal["clear"] = None,
        staged_observations: tuple[ObservedChange, ...] = (),
    ) -> None:
        # Transparent backend merge: supervisor-observed changes join the tool's self-report
        # (success AND failure — a failed invocation may have committed a prefix). Deduped by
        # (path, operation, old_path); the tool's own entries win ties. Capped: at most
        # MAX_MERGED_OBSERVATIONS supervisor entries join one result; the helper already caps
        # its own report at 256, so this only bounds multi-child accumulation per invocation.
        if staged_observations:
            seen = {(item.path, item.operation, item.old_path) for item in result.changes}
            extra: list[ResourceChange] = []
            for observed in staged_observations:
                key = (observed.path, observed.operation, observed.old_path)
                if key in seen:
                    continue
                seen.add(key)
                extra.append(ResourceChange(path=observed.path, operation=observed.operation, old_path=observed.old_path))
                if len(extra) >= MAX_MERGED_OBSERVATIONS:
                    break
            if extra:
                result = replace(result, changes=(*result.changes, *extra))
        cursor = execution.cursor
        deferred = cursor.deferred_sequence is not None
        batch_sequence = cursor.deferred_sequence or cursor.batch_sequence
        invocation_id: str | None = None
        intent: StoredEvent | None = None
        if cursor.intent_sequence is not None:
            intent = self._event(execution, cursor.intent_sequence, EventType.TOOL_INTENT)
            raw_invocation = intent.payload.get("invocation_id")
            invocation_id = raw_invocation if isinstance(raw_invocation, str) else None
        cursor.intent_sequence = None
        cursor.approval_sequence = None
        definition = self._snapshot.definition_of(call.tool_name)
        declaration = intent.payload if intent is not None else {}
        diagnostic = result.diagnostics if isinstance(result, ToolFailure) else None
        _, projection_clipped = model_result_text(result)
        model_event = next(
            (event for event in reversed(execution.events) if event.sequence == batch_sequence and event.event_type == EventType.MODEL_TURN), None
        )
        metadata: dict[str, object] = {
            "semantics": declaration.get("semantics", []) if intent is not None else [],
            "model": model_event.payload.get("model") if model_event is not None else None,
            "argument_bytes": len(canonical_json(dict(call.arguments)).encode("utf-8")),
            "result_bytes": len(canonical_json(encode_tool_result(result)).encode("utf-8")),
            "truncated": result.output.bounds.truncated or projection_clipped,
            "partial": result.output.bounds.partial,
            "error_kind": diagnostic.kind if diagnostic else None,
            "has_retry_guidance": bool(diagnostic and diagnostic.guidance),
            "reads": [],
            "changes": [],
            "resource_paths": [],
            "owner": declaration["owner"] if intent is not None else self._snapshot.owner_of(call.tool_name) if definition else "",
            "effects": declaration["effects"] if intent is not None else sorted(item.value for item in definition.effects) if definition else [],
        }
        paths = [item.path for item in result.coverage] if isinstance(result, ToolSuccess) else []
        paths.extend(item.path for item in result.changes)
        paths.extend(item.old_path for item in result.changes if item.old_path is not None)
        if definition is not None:
            paths.extend(_target_paths(self.workspace, call.arguments, definition.path_argument_keys))
        if isinstance(result, ToolSuccess):
            metadata["reads"] = [
                {
                    **item.as_payload(),
                    "path": canonical_path(self.workspace, item.path),
                    "clipped": item.clipped or projection_clipped,
                }
                for item in result.coverage
            ]
        metadata["changes"] = [
            {
                **asdict(item),
                "path": canonical_path(self.workspace, item.path),
                "old_path": canonical_path(self.workspace, item.old_path) if item.old_path is not None else None,
            }
            for item in result.changes
        ]
        metadata["resource_paths"] = sorted({str((self.workspace / path).resolve()) for path in paths if "://" not in path})
        if deferred:
            cursor.deferred_sequence = None
            cursor.deferred_depth = 0
            cursor.abandon_batch = False
        else:
            cursor.next_call += 1
            turn = decode_provider_result(
                _wire_object(self._event(execution, batch_sequence or 0, EventType.MODEL_TURN).payload["result"], "model turn")
            )
            if cursor.next_call == len(turn.tool_calls):
                cursor.batch_sequence = None
                cursor.next_call = 0
                cursor.abandon_batch = False
        payload: dict[str, object] = {
            "batch_sequence": batch_sequence,
            "call": encode_tool_call(call),
            "result": encode_tool_result(result),
            "fact_metadata": metadata,
            "icon": declaration.get("icon")
            if intent is not None
            else ({"glyph": definition.icon.glyph, "fallback": definition.icon.fallback} if definition and definition.icon else None),
            "logical_turn": execution.cursor.logical_turn,
        }
        if deferred:
            request = self._event(execution, batch_sequence or 0, EventType.TOOL_DEFERRED)
            payload["deferred"] = True
            payload["request_id"] = request.payload["request_id"]
            payload["depth"] = request.payload["depth"]
        if invocation_id is not None:
            payload["invocation_id"] = invocation_id
        report_event = EventDraft(
            EventType.TOOL_INTERRUPTED if interrupted else EventType.TOOL_COMPLETED,
            "kernel" if interrupted else "tool",
            "plugin" if deferred else "model",
            payload,
        )
        all_events = (report_event,) + staged_events

        staged_budget_updates: list[StagedBudgetUpdate] = []
        if staged_budget == "clear":
            owner = self._snapshot.owner_of(call.tool_name)
            staged_budget_updates.append(StagedBudgetUpdate(owner=owner, action="clear"))
        elif isinstance(staged_budget, PluginBudget):
            staged_budget_updates.append(StagedBudgetUpdate(owner=staged_budget.owner, action="set", budget=staged_budget))

        self._commit(
            execution,
            "running",
            "ready",
            *all_events,
            staged_state=staged_state,
            staged_continuations=staged_conts,
            staged_cancellations=staged_cancels,
            staged_budgets=tuple(staged_budget_updates),
        )

    def _interrupt_batch(self, execution: _Execution) -> None:
        first = execution.cursor.intent_sequence is not None
        execution.cursor.abandon_batch = True
        while execution.cursor.batch_sequence is not None or execution.cursor.deferred_sequence is not None:
            _, call = self._pending(execution)
            message = (
                "execution was interrupted; external effects are unknown; not replayed"
                if first
                else "not executed: prior batch member was interrupted"
            )
            self._report(execution, call, ToolFailure(call.tool_name, message), interrupted=True)
            first = False

    def _messages(self, execution: _Execution, *, allow_pending: bool = False, completed_only: bool = False) -> tuple[ProviderMessage, ...]:
        messages: list[ProviderMessage] = []
        pending_calls: tuple[ToolCall, ...] = ()
        pending_index = 0
        batch_start = 0
        batch_sequence: int | None = None
        # A verified projection replaces a committed prefix with its owner's summary, so a long
        # session keeps a bounded window without rewriting a single historical event.
        projection = self.store.window_projection(execution.session.session_id)
        projected_through = 0
        if projection is not None:
            projected_through = projection.covered_to_sequence
            messages.append(
                ProviderMessage(
                    "system",
                    f"[window summary by {projection.owner}; {projection.covered_to_sequence} covered events "
                    f"({projection.covered_bytes} bytes -> {projection.dropped_bytes} dropped)]\n{projection.replacement}",
                )
            )
        for event in execution.events:
            if event.sequence <= projected_through:
                continue
            if event.event_type == EventType.USER_MESSAGE:
                if pending_calls:
                    raise ValueError("user message crossed an unfinished native batch")
                messages.append(ProviderMessage("user", str(event.payload["content"])))
            elif event.event_type == EventType.CONTINUATION_STARTED:
                if pending_calls:
                    raise ValueError("continuation crossed an unfinished native batch")
                p_payload = event.payload.get("payload")
                p_map = p_payload if isinstance(p_payload, Mapping) else {}
                payload_str = json.dumps(json_wire_object(p_map), ensure_ascii=False)
                if event.payload.get("role") == "user":
                    # The owner declared a user-role message: its text (or payload) is verbatim.
                    text = p_map.get("text")
                    messages.append(ProviderMessage("user", text if isinstance(text, str) else payload_str))
                else:
                    messages.append(ProviderMessage("system", f"[Durable continuation from {event.source}]\n{payload_str}"))
            elif event.event_type == EventType.WAKEUP_FIRED:
                if pending_calls:
                    raise ValueError("wakeup crossed an unfinished native batch")
                w_payload = event.payload.get("payload")
                w_map = w_payload if isinstance(w_payload, Mapping) else {}
                payload_str = json.dumps(json_wire_object(w_map), ensure_ascii=False)
                if event.payload.get("role") == "user":
                    text = w_map.get("text")
                    messages.append(ProviderMessage("user", text if isinstance(text, str) else payload_str))
                else:
                    owner_label = str(event.payload.get("owner", event.source))
                    messages.append(ProviderMessage("system", f"[Durable wakeup from {owner_label}]\n{payload_str}"))
            elif event.event_type == EventType.MODEL_TURN:
                if pending_calls:
                    raise ValueError("provider turn crossed an unfinished native batch")
                turn = decode_provider_result(_wire_object(event.payload["result"], "model turn"))
                batch_start = len(messages)
                messages.append(
                    ProviderMessage("assistant", turn.output, reasoning=turn.reasoning, tool_calls=turn.tool_calls, metadata=turn.metadata or {})
                )
                if turn.tool_calls:
                    pending_calls = turn.tool_calls
                    pending_index = 0
                    batch_sequence = event.sequence
            elif event.event_type in {EventType.TOOL_COMPLETED, EventType.TOOL_INTERRUPTED}:
                if event.payload.get("deferred") is True:
                    request = self._event(execution, cast(int, event.payload["batch_sequence"]), EventType.TOOL_DEFERRED)
                    if event.payload["call"] != request.payload["call"]:
                        raise ValueError("deferred tool result does not match its committed request")
                    result = decode_tool_result(_wire_object(event.payload["result"], "tool result"))
                    text, _clipped = model_result_text(result)
                    messages.append(
                        ProviderMessage(
                            "user",
                            f"[Untrusted deferred tool result: {result.tool_name}; "
                            f"invocation {event.payload.get('invocation_id', 'not-dispatched')}; "
                            f"request {request.payload['request_id']}; caller {request.payload['caller_invocation_id']}]\n{text or ''}",
                        )
                    )
                    continue
                if not pending_calls or event.payload["batch_sequence"] != batch_sequence:
                    raise ValueError("reported tool result crossed an unfinished native batch")
                call = pending_calls[pending_index]
                if _wire_object(event.payload["call"], "tool call") != json_wire_object(encode_tool_call(call)):
                    raise ValueError("reported tool result does not match the active batch call")
                result = decode_tool_result(_wire_object(event.payload["result"], "tool result"))
                text, _clipped = model_result_text(result)
                messages.append(
                    ProviderMessage("tool", text, tool_call_id=call.tool_call_id, tool_name=call.tool_name, is_error=isinstance(result, ToolFailure))
                )
                pending_index += 1
                if pending_index == len(pending_calls):
                    pending_calls = ()
        if pending_calls and completed_only:
            del messages[batch_start:]
        elif pending_calls:
            if not allow_pending:
                raise ValueError("cannot call the provider before completing the pending native batch")
            if execution.cursor.batch_sequence != batch_sequence or execution.cursor.next_call != pending_index:
                raise ValueError("checkpoint is not the authentic completed batch prefix")
        elif execution.cursor.batch_sequence is not None or execution.cursor.next_call != 0:
            raise ValueError("checkpoint references an already completed batch")

        # Contributed context from plugins only when not allow_pending. A context
        # failure is never suppressed or re-wrapped here: the drive boundary
        # finishes the run failed before any provider charge.
        if not allow_pending:
            with self._dispatch("context") as snapshot:
                contributions = snapshot.plugin_runtime.contribute_context(
                    execution.session.session_id,
                    run_id=execution.run_id,
                    logical_turn=execution.cursor.logical_turn,
                )
                for c in contributions:
                    if c.get("type") != "text":
                        continue
                    owner = str(c.get("owner", "plugin"))
                    content = str(c.get("content", ""))
                    if c.get("role") == "user":
                        # Declarative role: verbatim, never re-wrapped as core system prose.
                        messages.append(ProviderMessage("user", content))
                    else:
                        messages.append(ProviderMessage("system", f"[{owner} context]\n{content}"))

            activation_note = self._activation_outcome_note(execution)
            if activation_note is not None:
                messages.append(ProviderMessage("system", activation_note))

            # Hard bound check strictly AFTER contributions
            if sum(len(json.dumps(encode_message(message), allow_nan=False).encode("utf-8")) for message in messages) > 256 * 1024:
                raise ValueError("context exceeded hard bound; explicit compaction is required")

        return tuple(messages)

    def _drive_pending_activations(self) -> None:
        """Finish committed command-tool work and staged continuations left across restart."""
        try:
            for session in self.store.sessions(workspace=str(self.workspace)):
                if self._closed:
                    return
                if self._admits_fresh_work(session):
                    self._drain_deferred_requests(session.session_id)
                checkpoint = self.store.checkpoint(session.session_id)
                if checkpoint is None or not isinstance(checkpoint.payload, Mapping):
                    continue
                cursor = _Cursor.model_validate(json_wire_object(checkpoint.payload))
                if not cursor.resume_intent:
                    continue
                try:
                    self._consume_resume_intent(session.session_id)
                except Exception:
                    # The intent stays durable, so the next process retries it; never crash the core.
                    continue
        except Exception:
            return
        if not self.auto_drain or self._closed:
            return
        # A crash can land between a child's terminal run commit and the parent fact that
        # publishes it. Rebuild the outcome from durable events; never rerun child work.
        try:
            notices = self._delegation._recover_child_publications()
        except Exception:
            notices = []
        for parent_id, notice_id in notices:
            if self._closed:
                return
            self._delegation._deliver_child_notice(parent_id, notice_id)
        # Queued follow-ups and notices are durable messages: deliver each exact identity once.
        for session in self.store.sessions(workspace=str(self.workspace)):
            if self._closed:
                return
            if not self._admits_fresh_work(session):
                continue
            for _ in range(8):
                message = self._delegation._first_child_message(session.session_id)
                if message is None:
                    break
                try:
                    self.drain_continuations(
                        session.session_id,
                        limit=1,
                        continuation_id=message.continuation_id,
                    )
                except Exception:
                    break

    def _first_child_message(self, session_id: str) -> ContinuationRecord | None:
        """The oldest core child message that is claimed or still pending, if any."""
        return self._delegation._first_child_message(session_id)

    def _consume_resume_intent(self, session_id: str) -> None:
        """Claim and drive one owed continuation, exactly once, on its own captured generation.

        The intent is either an activation request id or an upgrade pause id; the consumption event
        names the real staged fact it came from, never a forged cause.
        """
        with self._session_lock(session_id):
            self._ensure_open()
            with self._aborts_lock:
                if session_id in self._session_aborts:
                    return
            checkpoint = self.store.checkpoint(session_id)
            if checkpoint is None or not isinstance(checkpoint.payload, Mapping):
                return
            cursor = _Cursor.model_validate(json_wire_object(checkpoint.payload))
            intent = cursor.resume_intent
            if not intent:
                return
            session = self.store.session(session_id)
            if checkpoint.revision_id != self.revision_id or session.active_revision_id != self.revision_id:
                return
            self._abort.set_cancelled(False)
            cursor.resume_intent = ""
            execution = _Execution(session, cursor, list(self.store.events(session_id)), session.run_id or intent)
            try:
                self._commit(
                    execution,
                    "running",
                    "ready",
                )
            except StoreConflict:
                return
            budget = 32
            root_state = self.store.root_budget_state(cursor.root_id) if cursor.root_id else None
            if root_state is not None:
                remaining = root_state[0] - root_state[1]
                if remaining <= 0:
                    self._stop(execution, "root action budget exhausted", stop_cause="limit_reached")
                    return
                budget = max(1, min(32, remaining))
            abort = Cancellation()
            with self._aborts_lock:
                self._session_aborts[session_id] = abort
            try:
                self._drive(execution, max_steps=budget)
            finally:
                with self._aborts_lock:
                    self._session_aborts.pop(session_id, None)

    def _activation_outcome_note(self, execution: _Execution) -> str | None:
        """Tell the next prompt what happened to a staged activation this run asked for."""
        request_id: str | None = None
        outcome: tuple[str, str] | None = None
        for event in execution.events:
            payload = event.payload if isinstance(event.payload, Mapping) else {}
            candidate = payload.get("request_id")
            if not isinstance(candidate, str) or not candidate:
                continue
            if event.run_id != execution.run_id:
                # Only the run that asked for the switch is told about it; later turns are not
                # re-fed an old decision as if it were news.
                continue
            if event.event_type == EventType.ACTIVATION_REQUESTED:
                request_id = candidate
                outcome = None
            elif event.event_type == EventType.ACTIVATION_COMMITTED and candidate == request_id:
                outcome = ("committed", str(payload.get("to_revision_id", "")))
            elif outcome is None and candidate == request_id and event.event_type == EventType.ACTIVATION_FAILED:
                outcome = ("refused", str(payload.get("error", "")))
        if request_id is None or outcome is None:
            return None
        kind, detail = outcome
        if kind == "committed":
            return f"[core] capability activation {request_id} committed the revision {detail}."
        return f"[core] capability activation {request_id} was refused and the previous revision is still active: {detail}"

    def _stop(self, execution: _Execution, message: str, *, stop_cause: StopCause = "user_stop") -> CoreResult:
        """Stop a run. A deliberate user/owner stop is terminal for the session (no replay of the
        abandoned native batch); crashes and shutdown keep their recoverable checkpoint."""
        execution.cursor.stop_cause = stop_cause
        deliberate = stop_cause in ("user_stop", "owner_cancel")
        checkpoint_kind = "terminal" if deliberate else "interrupted"
        stop_event = EventDraft(
            EventType.RUN_INTERRUPTED,
            "kernel",
            "kernel",
            {
                "reason": message,
                "stop_cause": stop_cause,
                "logical_turn": execution.cursor.logical_turn,
                **self._measured_run_elapsed_fields(execution),
            },
        )
        if deliberate and execution.cursor.continuation_id is not None:
            self._commit(
                execution,
                "interrupted",
                checkpoint_kind,
                stop_event,
                completed_continuation_id=execution.cursor.continuation_id,
                completed_continuation_status="cancelled",
            )
            return self._result(execution, error=message, stop_cause=stop_cause)
        self._commit(execution, "interrupted", checkpoint_kind, stop_event)
        return self._result(execution, error=message, stop_cause=stop_cause)

    def _fail(self, execution: _Execution, message: str) -> CoreResult:
        execution.cursor.stop_cause = "error"
        self._commit(
            execution,
            "failed",
            "terminal",
            EventDraft(
                EventType.RUN_FAILED,
                "kernel",
                "kernel",
                {"error": message, "logical_turn": execution.cursor.logical_turn, **self._measured_run_elapsed_fields(execution)},
            ),
            *self._lifecycle_events(execution, self._snapshot, "session_end", status="failed", error=message),
            completed_continuation_id=execution.cursor.continuation_id,
            completed_continuation_status="failed",
        )
        return self._result(execution, error=message, stop_cause="error")

    def _result(
        self,
        execution: _Execution,
        *,
        output: str | None = None,
        error: str | None = None,
        pending_approval: Mapping[str, object] | None = None,
        stop_cause: StopCause | None = None,
    ) -> CoreResult:
        return CoreResult(
            execution.session.session_id,
            execution.run_id,
            self.revision_id,
            execution.session.status,
            output=output,
            error=error,
            pending_approval=pending_approval,
            stop_cause=stop_cause,
        )
