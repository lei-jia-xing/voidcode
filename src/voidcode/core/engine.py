"""Governed turns over immutable revisions and durable native tool batches."""

from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import logging
import os
import threading
import time
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from threading import Lock
from types import MappingProxyType
from typing import Any, Literal, cast
from uuid import uuid4

import jsonschema
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..sdk.ask import AskAnswerStatus, AskError, AskRequest, AskRequestDraft, AskResultBody, validate_ask_answer
from ..sdk.client import CLIENT_PROMPT_MAX_BYTES, BudgetExhaustion
from ..sdk.codec import (
    decode_provider_result,
    decode_tool_call,
    decode_tool_result,
    encode_provider_result,
    encode_tool_call,
    encode_tool_definition,
    encode_tool_result,
)
from ..sdk.command_help import grouped_help_entries
from ..sdk.commands import (
    CommandCatalog,
    CommandDescriptor,
    CommandInputSnapshot,
    CommandResult,
)
from ..sdk.hooks import RequestWindow, StopCause, WindowSegment
from ..sdk.icons import Icon
from ..sdk.invocation import ToolContext
from ..sdk.json_values import json_wire_object, json_wire_value, own_json_object
from ..sdk.observed_effects import MAX_OBSERVED_CHANGES
from ..sdk.provider import ImageAttachment, ProviderExecutionError, ProviderMessage, ProviderTurnRequest, ProviderTurnResult
from ..sdk.services import (
    ArtifactPage,
    ArtifactReference,
    CancelSubtreeReceipt,
    ChildOutcome,
    ChildSendReceipt,
    ContinuationResult,
    DescendantNode,
    PluginBudget,
    SessionUsageReport,
    StructuredModelResult,
    StructuredQueryParseError,
)
from ..sdk.suspend import ParkRevisionError
from ..sdk.tools import (
    TOOL_OUTPUT_BYTES,
    Change,
    TextOutput,
    ToolCall,
    ToolDefinition,
    ToolEffect,
    ToolFailure,
    ToolResult,
    ToolSuccess,
    UnconfirmedStop,
    denied_under_restriction,
    has_external_mutations,
)
from ..sdk.transcript import ContextHistory, TranscriptPage
from ..sdk.views import PluginViewDescriptor, ViewsSnapshot
from .activation import Admission, ExecutionConfig
from .activation_coordinator import ActivationCoordinator, ActivationFenced, ActivationSnapshot, PreparedActivation
from .artifacts import SHA256_PATTERN, ArtifactStore
from .background_actions import BackgroundActionRegistry
from .background_execution import BackgroundExecution
from .cancellation import Cancellation
from .child_contracts import ChildSpawnRequest
from .coverage import (
    ProjectionAllocation,
    _target_paths,
    allocate_tool_text_budget,
    canonical_path,
    measured_tokens_per_character,
    project_tool_result,
    read_artifact_content,
)
from .delegation import (
    DelegationManager,
    _child_run_status,
    _terminal_run_result,
)
from .events import CHILD_MESSAGE_OWNER, EventType, child_completion_notice_id
from .facts import FACT_EVENT_TYPES as _FACT_EVENT_TYPES
from .facts import fact_for_event
from .output_projection import ResultProjector
from .plugins import CommandInvocationOutcome, LeaseContext, PluginRuntime, PluginSource, ToolInvocationOutcome, ToolSuspension
from .sandbox import SandboxBackend
from .storage.store import (
    DATABASE_FILENAME,
    AskAnswerConflict,
    Checkpoint,
    ContinuationRecord,
    EventDraft,
    InvalidRevision,
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
    StaleRevision,
    StoreConflict,
    StoredEvent,
    WaitingAskPark,
    canonical_json,
)
from .usage import UsageProjector

logger = logging.getLogger(__name__)


class _UnboundRun:
    """Sentinel for the legacy unbound cancel path; distinct from a bound null run."""


_UNBOUND_RUN = _UnboundRun()


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
        Icon(glyph="↻"),
    ),
)


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
    ask_sequence: int | None = Field(default=None, ge=1)
    ask_request_id: str | None = None
    deferred_sequence: int | None = Field(default=None, ge=1)
    deferred_depth: int = Field(default=0, ge=0)
    deferred_action_id: str | None = None
    deferred_origin_tools: tuple[str, ...] | None = None
    deferred_origin_readonly: bool | None = None
    deferred_authority_missing: bool = False
    deferred_only: bool = False
    #: Optional provider-turn cap for this logical run; independent of shared root actions.
    max_steps: int | None = None
    step: int = Field(default=0, ge=0)
    abandon_batch: bool = False
    continuation_id: str | None = None
    logical_turn: str = ""
    stop_cause: StopCause | None = None
    initial_root_budget: int | None = None
    root_id: str = ""
    #: Staged continuation this session owes itself: an activation request id or an upgrade pause id.
    resume_intent: str = ""

    @field_validator("deferred_origin_tools", mode="before")
    @classmethod
    def _decode_deferred_origin_tools(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value


@dataclass(frozen=True, slots=True)
class CoreResult:
    session_id: str
    run_id: str
    revision_id: str
    status: str
    output: str | None = None
    error: str | None = None
    pending_approval: Mapping[str, object] | None = None
    pending_ask: Mapping[str, object] | None = None
    stop_cause: StopCause | None = None
    budget_exhaustion: BudgetExhaustion | None = None

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
        if self.pending_ask is not None:
            payload["pending_ask"] = json_wire_object(self.pending_ask)
        if self.stop_cause is not None:
            payload["stop_cause"] = self.stop_cause
        if self.budget_exhaustion is not None:
            payload["budget_exhaustion"] = asdict(self.budget_exhaustion)
        return payload


@dataclass(slots=True)
class _ShrinkableToolProjection:
    """One ceiling-rendered tool message that adaptive sizing may re-project smaller."""

    index: int
    result: ToolResult
    label: str | None


@dataclass(slots=True)
class _Execution:
    session: SessionRecord
    cursor: _Cursor
    events: list[StoredEvent]
    run_id: str
    lifecycle_started: bool = False
    ephemeral_steering: list[str] = field(default_factory=list)
    projection_allocation: ProjectionAllocation | None = None
    allocation_ineffective_tools: int = 0
    #: Request-scoped interaction declaration for this drive (D3): None means
    #: interactive. Unattended drives refuse ask and approval parks at the
    #: park-commit site with a typed ToolFailure and continue. Ephemeral:
    #: never checkpointed, re-declared by every run/resume entry.
    interaction: Literal["interactive", "unattended"] | None = None


@dataclass(frozen=True, slots=True)
class _AskBinding:
    """One validated live ask park: its committed request plus park-time facts."""

    event: StoredEvent
    request: AskRequest
    parked_at_ms: int
    idempotency_key: str | None


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
        # Product-owned config filename (e.g. voidcode.json): the product layer
        # passes its own name in; the core never spells it, only guards it.
        config_filename: str = "voidcode.json",
    ) -> None:
        self.auto_drain = auto_drain
        self._config_filename = config_filename
        self.workspace = workspace.resolve(strict=True)
        self._abort = Cancellation()
        self._closed = False
        # One run lock per session: independent sessions (delegated children, cron firings, a
        # user turn beside a background run) execute concurrently in the same workspace. The
        # activation barrier does not depend on this lock; it uses the dispatch fence and the
        # in-transaction blocker check.
        self._session_locks: dict[str, Lock] = {}
        self._session_locks_guard = Lock()
        self._session_aborts: dict[str, tuple[str | None, Cancellation]] = {}
        self._command_aborts: dict[str, dict[str, Cancellation]] = {}
        self._aborts_lock = Lock()
        # Background action drivers: claimed-but-unsettled action ids this process drives.
        # Cross-process double-driving is impossible: construction holds an exclusive
        # workspace flock (`another process owns this workspace`), so a claimed row this
        # process does not drive is necessarily stale, never a live foreign claim.
        self._background = BackgroundActionRegistry(self)
        self._background_execution = BackgroundExecution(self, self._background)
        # Commit clock for event streams: bumped under this condition after every
        # committed session-events write (see notify_commit); waiters re-read cursors.
        self._commit_cond = threading.Condition()
        self._commit_generation = 0
        # One pointer per capability generation, plus short-critical-section call accounting.
        # The activation fence rejects new dispatches and reports foreign in-flight calls as
        # busy instead of blocking any run, and no lock is ever held across an RPC.
        self._calls_lock = threading.Condition()
        self._active_calls: dict[str, str] = {}
        self._active_dispatch_threads: dict[int, int] = {}
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
            self.store = SqliteStore(state_dir / DATABASE_FILENAME)
            self._delegation.attach_store(self.store)
            self._activation = ActivationCoordinator(self)
            self._usage = UsageProjector(self.store)
            self._projection = ResultProjector(self._artifacts)
            resources.callback(self.store.close)
            runtime = self._build_runtime(plugins, revision_id=revision_id, sandbox_backend=sandbox_backend)
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
            self._background.wire(self._snapshot.plugin_runtime)
            for target, name in (
                (self._drive_pending_activations, "CoreActivationDriver"),
                (self._wakeup_loop, "CoreWakeupTicker"),
            ):
                worker = threading.Thread(target=target, name=name, daemon=True)
                worker.start()
                self._background_workers.append(worker)
            self._background_workers.append(self._background.start())

    def _wire_background_runtime(self, runtime: PluginRuntime) -> None:
        """Bind one runtime's background projections to this core's live driver registry."""
        self._background.wire(runtime)

    def _stop_background_work(self) -> None:
        """Join coordination workers before runtime/store callbacks close their resources."""
        self.cancel("core shutdown", cause="shutdown")
        self._background.park("core shutdown")
        with self._session_locks_guard:
            self._closed = True
            workers, self._background_workers = self._background_workers, []
        self._background.notify()
        for worker in workers:
            worker.join()

    @contextmanager
    def _session_lock(self, session_id: str) -> Generator[None]:
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

    @property
    def commit_generation(self) -> int:
        """Current store-commit generation; event streams wait for it to advance."""
        with self._commit_cond:
            return self._commit_generation

    def notify_commit(self) -> None:
        """Record one committed session-events write and wake event-stream waiters.

        Call after every committed session-events write affecting sessions (all commit_staged /
        commit_activation / claim paths in engine and delegation). Level-triggered:
        waiters record a generation, wait for ANY change, then re-read their event
        cursor — no wakeup is ever lost (a check after a bump sees the new value),
        and spurious wakes or timeouts just re-read an unchanged cursor.
        """
        with self._commit_cond:
            self._commit_generation += 1
            self._commit_cond.notify_all()

    def wait_for_commit(self, after_generation: int, timeout: float) -> int:
        """Block until a commit lands past ``after_generation`` (or timeout); return current generation."""
        with self._commit_cond:
            self._commit_cond.wait_for(lambda: self._commit_generation != after_generation, timeout=timeout)
            return self._commit_generation

    @contextmanager
    def dispatch(self, description: str) -> Generator[_Snapshot]:
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
            thread_id = threading.get_ident()
            self._active_calls[call_id] = description
            self._active_dispatch_threads[thread_id] = self._active_dispatch_threads.get(thread_id, 0) + 1
        try:
            yield snapshot
        finally:
            with self._calls_lock:
                self._active_calls.pop(call_id, None)
                dispatch_count = self._active_dispatch_threads.get(thread_id, 0)
                if dispatch_count <= 1:
                    self._active_dispatch_threads.pop(thread_id, None)
                else:
                    self._active_dispatch_threads[thread_id] = dispatch_count - 1
                self._calls_lock.notify_all()

    def assert_background_wait_unleased(self) -> None:
        """Refuse waits from any active plugin lease or Core dispatch on this thread."""
        thread_id = threading.get_ident()
        if PluginRuntime.current_thread_has_active_lease():
            raise RuntimeError("background tool wait is unavailable inside an active plugin callback")
        with self._calls_lock:
            if self._active_dispatch_threads.get(thread_id, 0) or self._activating_thread == thread_id:
                raise RuntimeError("background tool wait is unavailable inside an active Core dispatch")

    def __enter__(self) -> Core:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def cancel_session(self, session_id: str, reason: str = "user cancellation", *, cause: StopCause = "user_stop") -> bool:
        """Signal the currently executing run and command invocations of one session."""
        return self._cancel_session_run(session_id, reason, cause=cause, expected_run_id=_UNBOUND_RUN)

    def _cancel_session_run(self, session_id: str, reason: str, *, cause: StopCause, expected_run_id: str | None | _UnboundRun) -> bool:
        """Signal one session only when its live run matches ``expected_run_id``.

        ``expected_run_id=None`` is a REAL binding (a reserved, never-started
        run); the unbound public path passes the ``_UNBOUND_RUN`` sentinel.
        Lock order is aborts -> store only; the target's long-held session
        lock is never taken, so a blocked provider run stays signallable.
        Signals fire only after the durable run and the live abort entry both
        match the bound run under one aborts-lock critical section; a stale
        request touches no signal, including beside-running commands.
        """
        self.ensure_open()
        # Ownership first: a session id from another workspace sharing this
        # database may never be signalled or settled.
        self.session(session_id)
        is_bound = expected_run_id is not _UNBOUND_RUN
        bound = cast("str | None", None if not is_bound else expected_run_id)
        with self._aborts_lock:
            if is_bound and (self.store.session(session_id).run_id or None) != bound:
                return False
            entry = self._session_aborts.get(session_id)
            abort: Cancellation | None = None
            if entry is not None:
                entry_run, candidate = entry
                if not is_bound or entry_run == bound:
                    abort = candidate
            if is_bound and entry is not None and abort is None:
                # A live abort for a different run owns this session: touch
                # nothing, including beside-running commands of the new run.
                return False
            signals = list(self._command_aborts.get(session_id, {}).values())
            if abort is not None:
                signals.append(abort)
            elif is_bound:
                # No live run abort: only parked settling is in scope; beside
                # commands belong to no bound run and stay untouched.
                signals = []
            for signal in signals:
                signal.set_cancelled(True, reason=reason, stop_cause=cause)
        self.store.cancel_pending_continuations(session_id, "core")
        self._delegation.notify_child_state_changed()
        # A bound call with no live abort for the bound run falls through to
        # the pinned parked settle; an unbound call keeps legacy behavior.
        settled = self._settle_parked_cancel(session_id, reason, cause, expected_run_id=bound, is_bound=is_bound) if abort is None else False
        return bool(signals) or settled

    def _settle_parked_cancel(
        self, session_id: str, reason: str, cause: StopCause, *, expected_run_id: str | None = None, is_bound: bool = False
    ) -> bool:
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
        (sequence, run) is what keeps this settle atomic. A bound call pins the
        whole settle (including the parked-batch reports) to the expected run
        and never rebases onto a newer run.
        """
        session = self.session(session_id)
        if is_bound and (session.run_id or None) != expected_run_id:
            return False
        checkpoint = self.store.checkpoint(session_id)
        parked_run_id = self._parked_run_id(session, checkpoint)
        if parked_run_id is None:
            return False
        if is_bound and parked_run_id != expected_run_id:
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
                self._interrupt_batch(execution, pinned_run_id=expected_run_id if is_bound else None, enforce_pin=is_bound)
            if is_bound and execution.run_id != expected_run_id:
                return False
            self._commit(
                execution,
                "interrupted",
                "terminal",
                EventDraft(
                    EventType.RUN_INTERRUPTED,
                    "core",
                    "core",
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
                pinned_run_id=expected_run_id if is_bound else None,
                enforce_pin=is_bound,
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

    def admits_fresh_work(self, session: SessionRecord) -> bool:
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
            for session_id in list(self._session_aborts.keys()):
                self.store.cancel_pending_continuations(session_id, "core")
            for _, abort in self._session_aborts.values():
                abort.set_cancelled(True, reason=reason, stop_cause=cause)
            for commands in self._command_aborts.values():
                for abort in commands.values():
                    abort.set_cancelled(True, reason=reason, stop_cause=cause)
        self._delegation.notify_child_state_changed()

    def close(self) -> None:
        self.cancel("core shutdown", cause="shutdown")
        with self._session_locks_guard:
            if self._closed:
                return
            self._closed = True
        self._background.signal_stop("core shutdown")
        self._delegation.close()
        # No new session work can start (run/resume check `_closed` under their lock), so
        # draining every session lock now waits only for work that was already admitted.
        self._drain_session_locks()
        self._background.quiesce("core shutdown")
        snapshot = self._snapshot
        if snapshot is not None:
            # The core's own shutdown is an owner shutdown: supervised background processes end
            # with it. A reload keeps them: the runtime swap is not the owner going away.
            snapshot.plugin_runtime.stop_background_processes()
        self._resources.close()

    def read_artifact(self, sha256: str, *, offset: int = 0, limit: int = 65536) -> ArtifactPage:
        """Read immutable workspace artifact bytes, never a caller-supplied path."""
        self.ensure_open()
        return self._projection.read(sha256, offset=offset, limit=limit)

    def run(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        max_steps: int | None = None,
        root_action_budget: int | None = None,
        images: tuple[ImageAttachment, ...] = (),
        interaction: Literal["interactive", "unattended"] | None = None,
    ) -> CoreResult:
        return self._run(
            prompt,
            session_id=session_id,
            max_steps=max_steps,
            root_action_budget=root_action_budget,
            prompt_max_bytes=CLIENT_PROMPT_MAX_BYTES,
            images=images,
            interaction=interaction,
        )

    def _run(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        max_steps: int | None = None,
        root_action_budget: int | None = None,
        prompt_max_bytes: int,
        images: tuple[ImageAttachment, ...] = (),
        _root_id: str | None = None,
        _refuse_reserved_cancelled: bool = False,
        interaction: Literal["interactive", "unattended"] | None = None,
    ) -> CoreResult:
        # A core-driven child run carries a delegated payload (e.g. a review diff).
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode("utf-8")) > prompt_max_bytes:
            raise ValueError(f"prompt must be nonempty UTF-8 text within {prompt_max_bytes // 1024} KiB")
        self._validate_steps(max_steps)
        self._validate_interaction(interaction)
        self._validate_root_action_budget(root_action_budget)
        if _root_id is not None and (not isinstance(_root_id, str) or not _root_id or "\0" in _root_id):
            raise ValueError("inherited root action id must be nonempty text without NUL")
        if _root_id is not None and root_action_budget is not None:
            raise ValueError("a child cannot assign a new root action budget")
        if session_id is not None and (not isinstance(session_id, str) or not session_id or len(session_id) > 128 or "\0" in session_id):
            raise ValueError("session_id must be nonempty text within 128 characters without NUL")
        if session_id is not None:
            try:
                active_sess = self.store.session(session_id)
                if active_sess.status in ("running", "waiting"):
                    if root_action_budget is not None:
                        raise ValueError("a queued follow-up cannot assign a new root action budget; resume with an explicit extension")
                    return self._queue_followup(active_sess, prompt, max_steps, images=images)
            except KeyError:
                pass
        session_id = uuid4().hex if session_id is None else session_id
        with self._session_lock(session_id):
            self.ensure_open()
            self._abort.set_cancelled(False)
            abort = Cancellation()
            run_abort_id = uuid4().hex
            with self._aborts_lock:
                self._session_aborts[session_id] = (run_abort_id, abort)
            try:
                try:
                    session = self.store.session(session_id)
                except KeyError:
                    session = self._create_session_row(session_id)
                if session.status in ("running", "waiting"):
                    if root_action_budget is not None:
                        raise ValueError("a queued follow-up cannot assign a new root action budget; resume with an explicit extension")
                    return self._queue_followup(session, prompt, max_steps, images=images)
                if not self.admits_fresh_work(session):
                    raise ValueError("unfinished session requires explicit resume")
                self.check_session(session)
                root_id = _root_id if _root_id is not None else uuid4().hex
                if session.parent_session_id is not None:
                    if _root_id is None:
                        raise ValueError("child sessions cannot start a new root action; use a parent-authorized child send")
                    birth_root_id = self._delegation._captured_child_root_id(session_id)
                    if birth_root_id != _root_id:
                        raise ValueError("child run root action identity does not match its captured birth authorization")
                elif _root_id is not None:
                    raise ValueError("an inherited root action identity is only valid for a child session")
                initial_root_budget = root_action_budget
                if _root_id is None:
                    self.store.ensure_root_budget(root_id, root_action_budget)
                else:
                    birth = self._delegation._birth_record(session_id)
                    if birth is None or birth.get("root_id") != root_id:
                        raise StoreConflict(f"child {session_id!r} is missing its captured root authorization")
                    if "root_action_budget" not in birth:
                        raise StoreConflict(f"child {session_id!r} has no captured root budget state")
                    birth_budget = birth.get("root_action_budget")
                    if birth_budget is not None and (type(birth_budget) is not int or birth_budget < 0):
                        raise StoreConflict(f"child {session_id!r} has an invalid captured root budget")
                    root_state = self.store.root_budget_state(root_id)
                    if birth_budget is None:
                        if root_state is not None:
                            raise StoreConflict("captured uncapped root authorization conflicts with a finite budget row")
                    elif root_state is None or root_state[0] < birth_budget:
                        raise StoreConflict("captured finite root authorization has no matching durable budget row")
                    initial_root_budget = birth_budget
                execution = _Execution(
                    session,
                    _Cursor(root_id=root_id, initial_root_budget=initial_root_budget, max_steps=max_steps),
                    list(self.store.events(session_id)),
                    run_abort_id,
                    interaction=interaction,
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
                    # The new user turn has its own root identity and optional authorization.
                    execution.cursor = _Cursor(root_id=root_id, initial_root_budget=initial_root_budget, max_steps=max_steps)
                user_payload: dict[str, object] = {"content": prompt}
                if max_steps is not None:
                    user_payload["max_steps"] = max_steps
                if root_action_budget is not None:
                    user_payload["root_action_budget"] = root_action_budget
                if images:
                    user_payload["images"] = [img.as_payload() for img in images]
                self._commit(
                    execution,
                    "running",
                    "ready",
                    EventDraft(
                        EventType.USER_MESSAGE,
                        "client",
                        "user",
                        user_payload,
                    ),
                    start_turn=True,
                )
                result = self._drive(execution)
            finally:
                with self._aborts_lock:
                    self._session_aborts.pop(session_id, None)
        self._drive_pending(session_id)
        return result

    def _spill_followup_images(self, images: tuple[ImageAttachment, ...]) -> list[dict[str, object]]:
        """Spill queued follow-up image bytes to the artifact store, returning references.

        Each reference is `{"media_type", "sha256", "bytes"}` — the same content
        address the tool-result spill path publishes, never a second blob store.
        The continuation payload carries only these small references, so a large
        image no longer blows the continuation bound; delivery rehydrates to the
        same base64 bytes the direct path commits. Invalid base64 fails here,
        visibly, before any row is staged.
        """
        decoded: list[bytes] = []
        for index, img in enumerate(images):
            try:
                decoded.append(base64.b64decode(img.data, validate=True))
            except ValueError as exc:
                raise ValueError(f"queued follow-up image {index} has invalid base64 data") from exc
        refs: list[dict[str, object]] = []
        for img, raw in zip(images, decoded, strict=True):
            spill_id, spill = self._artifacts.start_spill(img.media_type, owner="core")
            try:
                spill.append(raw)
                digest, size, _media_type = self._artifacts.finish(spill_id)
            except BaseException:
                self._artifacts.discard(spill_id)
                raise
            refs.append({"media_type": img.media_type, "sha256": digest, "bytes": size})
        return refs

    def _rehydrate_followup_images(self, raw_images: list[dict[str, object]], continuation_id: str) -> tuple[ImageAttachment, ...]:
        """Rebuild provider-visible images from continuation references.

        Reference entries (`{"media_type", "sha256", "bytes"}`) read back through
        the shared artifact pager with digest+size verification. Missing,
        unreadable, or malformed references raise `StoreConflict` — the caller
        completes the continuation as failed first, so delivery never falls back
        to text-only pretending completeness.
        """
        if not raw_images:
            return ()
        rehydrated: list[ImageAttachment] = []
        for index, entry in enumerate(raw_images):
            if not isinstance(entry, Mapping):
                raise StoreConflict(f"queued follow-up {continuation_id!r} image {index} is malformed")
            media_type = entry.get("media_type")
            sha = entry.get("sha256")
            size = entry.get("bytes")
            if not isinstance(media_type, str) or not media_type:
                raise StoreConflict(f"queued follow-up {continuation_id!r} image {index} has no media type")
            if not isinstance(sha, str) or SHA256_PATTERN.fullmatch(sha) is None:
                raise StoreConflict(f"queued follow-up {continuation_id!r} image {index} has an invalid artifact reference")
            if type(size) is not int or size < 0:
                raise StoreConflict(f"queued follow-up {continuation_id!r} image {index} has an invalid artifact size")
            try:
                content = read_artifact_content(ArtifactReference(sha, size, media_type), self.artifact_page)
            except (ValueError, OSError) as exc:
                raise StoreConflict(f"queued follow-up {continuation_id!r} image {index} references missing or unreadable artifact {sha}") from exc
            rehydrated.append(ImageAttachment(media_type, base64.b64encode(content).decode("ascii"), sha))
        return tuple(rehydrated)

    def _queue_followup(
        self,
        session: SessionRecord,
        prompt: str,
        max_steps: int | None,
        images: tuple[ImageAttachment, ...] = (),
    ) -> CoreResult:
        checkpoint = self.store.checkpoint(session.session_id)
        if checkpoint is None or not isinstance(checkpoint.payload, Mapping):
            raise StoreConflict(f"running session {session.session_id!r} has no captured root authorization")
        root_id = checkpoint.payload.get("root_id")
        if not isinstance(root_id, str) or not root_id:
            raise StoreConflict(f"running session {session.session_id!r} has no captured root action identity")
        if "initial_root_budget" not in checkpoint.payload:
            raise StoreConflict(f"running session {session.session_id!r} has no captured root budget state")
        root_budget = checkpoint.payload.get("initial_root_budget")
        if root_budget is not None and (type(root_budget) is not int or root_budget < 0):
            raise StoreConflict(f"running session {session.session_id!r} has an invalid captured root budget")
        cid = uuid4().hex
        payload: dict[str, object] = {"prompt": prompt, "root_action_budget": root_budget}
        if max_steps is not None:
            payload["max_steps"] = max_steps
        if images:
            payload["images"] = self._spill_followup_images(images)
        self.store.enqueue_continuation(
            cid,
            "core",
            session.session_id,
            "user_message",
            session.run_id or "",
            self.revision_id,
            payload,
            root_id=root_id,
            role="user",
        )
        self._delegation.notify_child_state_changed()
        return CoreResult(
            session_id=session.session_id,
            run_id=session.run_id or "",
            revision_id=self.revision_id,
            status="queued",
        )

    def _execute_user_followup(
        self,
        session_id: str,
        continuation_id: str,
        prompt: str,
        max_steps: int | None,
        root_id: str | None,
        root_action_budget: int | None,
        images: tuple[ImageAttachment, ...] = (),
    ) -> CoreResult:
        if not isinstance(root_id, str) or not root_id:
            self.store.complete_continuation(continuation_id, status="failed")
            return CoreResult(
                session_id,
                "",
                self.revision_id,
                "failed",
                error="queued follow-up has no captured root action identity; refusing to create a new budget",
                stop_cause="error",
            )
        if root_action_budget is not None and (type(root_action_budget) is not int or root_action_budget < 0):
            self.store.complete_continuation(continuation_id, status="failed")
            return CoreResult(
                session_id,
                "",
                self.revision_id,
                "failed",
                error="queued follow-up has an invalid captured root budget",
                stop_cause="error",
            )
        with self._session_lock(session_id):
            self.ensure_open()
            self._abort.set_cancelled(False)
            abort = Cancellation()
            run_abort_id = uuid4().hex
            with self._aborts_lock:
                self._session_aborts[session_id] = (run_abort_id, abort)
            try:
                session = self.store.session(session_id)
                if not self.admits_fresh_work(session):
                    self.store.complete_continuation(continuation_id, status="failed")
                    return CoreResult(session_id, "", self.revision_id, "failed", error="session does not admit fresh work")
                self.check_session(session)
                root_state = self.store.root_budget_state(root_id)
                if root_action_budget is None and root_state is not None:
                    self.store.complete_continuation(continuation_id, status="failed")
                    return CoreResult(
                        session_id,
                        "",
                        self.revision_id,
                        "failed",
                        error="queued follow-up captured an uncapped root with a finite budget row",
                        stop_cause="error",
                    )
                if root_action_budget is not None and (root_state is None or root_state[0] < root_action_budget):
                    self.store.complete_continuation(continuation_id, status="failed")
                    return CoreResult(
                        session_id,
                        "",
                        self.revision_id,
                        "failed",
                        error="queued follow-up lost its captured finite root authorization",
                        stop_cause="error",
                    )
                execution = _Execution(
                    session,
                    _Cursor(
                        root_id=root_id,
                        initial_root_budget=root_action_budget,
                        max_steps=max_steps,
                        continuation_id=continuation_id,
                    ),
                    list(self.store.events(session_id)),
                    run_abort_id,
                )
                if session.status == "interrupted":
                    previous = self.store.checkpoint(session_id)
                    if previous is not None and isinstance(previous.payload, Mapping):
                        execution.cursor = _Cursor.model_validate(json_wire_object(previous.payload))
                        execution.cursor.step = 0
                        execution.cursor.stop_cause = None
                    if execution.cursor.batch_sequence is not None or execution.cursor.deferred_sequence is not None:
                        self._interrupt_batch(execution)
                    execution.cursor = _Cursor(
                        root_id=root_id,
                        initial_root_budget=root_action_budget,
                        max_steps=max_steps,
                        continuation_id=continuation_id,
                    )
                user_payload: dict[str, object] = {"content": prompt}
                if max_steps is not None:
                    user_payload["max_steps"] = max_steps
                if images:
                    user_payload["images"] = [img.as_payload() for img in images]
                self._commit(
                    execution,
                    "running",
                    "ready",
                    EventDraft(EventType.USER_MESSAGE, "client", "user", user_payload),
                    start_turn=True,
                )
                self.store.set_continuation_run_id(continuation_id, execution.run_id)
                result = self._drive(execution)
                status: Literal["completed", "failed", "cancelled"] = "completed"
                if result.status in ("completed", "idle"):
                    status = "completed"
                elif result.status == "failed":
                    status = "failed"
                elif result.status == "interrupted":
                    if result.stop_cause in ("user_stop", "owner_cancel"):
                        status = "cancelled"
                    else:
                        return result
                elif result.status == "waiting":
                    return result
                self.store.complete_continuation(continuation_id, status=status)
                return result
            finally:
                with self._aborts_lock:
                    self._session_aborts.pop(session_id, None)

    def resume(
        self,
        session_id: str,
        *,
        approval_request_id: str | None = None,
        decision: Literal["allow", "deny"] | None = None,
        max_steps: int | None = None,
        extend_root_actions: int | None = None,
        root_action_grant_id: str | None = None,
        interaction: Literal["interactive", "unattended"] | None = None,
    ) -> CoreResult:
        self._validate_steps(max_steps)
        self._validate_interaction(interaction)
        with self._session_lock(session_id):
            self.ensure_open()
            self._abort.set_cancelled(False)
            abort = Cancellation()
            run_abort_id = uuid4().hex
            with self._aborts_lock:
                self._session_aborts[session_id] = (run_abort_id, abort)
            resumed_notice: tuple[str, str] | None = None
            stopped_result: CoreResult | None = None
            try:
                session = self.store.session(session_id)
                self.check_session(session)
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
                if "initial_root_budget" not in checkpoint.payload:
                    raise ValueError("checkpoint has no captured root budget state")
                cursor = _Cursor.model_validate(json_wire_object(checkpoint.payload))
                if not isinstance(cursor.root_id, str) or not cursor.root_id:
                    raise ValueError("checkpoint has no captured root action identity")
                if cursor.deferred_action_id is not None:
                    action_abort = self._background.abort_event(cursor.deferred_action_id)
                    if action_abort is not None:
                        abort = Cancellation(
                            action_abort,
                            linked_reason="background action stopped",
                            linked_stop_cause_provider=lambda action_id=cursor.deferred_action_id: self._background.stop_cause(action_id),
                        )
                        with self._aborts_lock:
                            self._session_aborts[session_id] = (run_abort_id, abort)
                root_state = self.store.root_budget_state(cursor.root_id)
                if cursor.initial_root_budget is None:
                    if root_state is not None:
                        raise StoreConflict("checkpoint captured an uncapped root with a finite budget row")
                elif root_state is None or root_state[0] < cursor.initial_root_budget:
                    raise StoreConflict("checkpoint lost its captured finite root authorization")
                if session.parent_session_id is not None:
                    self._delegation._captured_child_root_id(session_id)
                if max_steps is not None:
                    cursor.max_steps = max_steps
                execution = _Execution(session, cursor, list(self.store.events(session_id)), run_abort_id, interaction=interaction)
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
                if cursor.ask_sequence is not None or cursor.ask_request_id is not None:
                    # An ask park resumes only through answer_ask/cancel_ask: a plain
                    # resume would re-invoke the parked tool. Refuse while live; when
                    # the caller-declared deadline passed, settle the expiry here and
                    # fall through so RUN_RESUMED plus _drive continue the run.
                    if approval_request_id is not None or decision is not None:
                        raise ValueError("session is not waiting for approval")
                    binding = self._ask_binding(execution, cursor)
                    if not self._ask_binding_expired(binding):
                        raise ValueError(f"session is waiting for ask {cursor.ask_request_id!r}; answer via ask.answer or ask.cancel")
                    self._adopt_ask_terminal(execution, binding, status="expired", values={})
                resume_payload: dict[str, object] = {"max_steps": max_steps}
                root_budget_grant: tuple[str, int, str] | None = None
                if extend_root_actions is not None:
                    if not cursor.root_id:
                        raise ValueError("checkpoint has no root action identity to extend")
                    resume_payload.update(
                        {
                            "root_id": cursor.root_id,
                            "extend_root_actions": extend_root_actions,
                            "root_action_grant_id": root_action_grant_id,
                        }
                    )
                    root_budget_grant = (cursor.root_id, extend_root_actions, cast(str, root_action_grant_id))
                self._commit(
                    execution,
                    "running",
                    "ready",
                    EventDraft(EventType.RUN_RESUMED, "core", "user", resume_payload),
                    root_budget_grant=root_budget_grant,
                )
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
                        resume_read_only = self.session_read_only(execution.session.session_id) or cursor.deferred_origin_readonly is True
                        visible = {tool.name for tool in self._visible_tools(execution, self._snapshot)}
                        denial = (
                            "tool is outside the current or captured origin scope"
                            if call.tool_name not in visible
                            else self._denial(call, definition, read_only=resume_read_only)
                        )
                        if denial is None:
                            # Approval is not authority: the current tool gate is re-asked.
                            denial = self._gate_denial(execution, self._snapshot, call, readonly=resume_read_only)
                        if denial:
                            self._report(execution, call, ToolFailure(call.tool_name, denial))
                        else:
                            stopped = self._invoke(execution, call, definition)
                            stopped_result = stopped
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
                result = stopped_result if stopped_result is not None else self._drive(execution)
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
                        budget_exhaustion=result.budget_exhaustion,
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
        self.drain_deferred_requests(session_id)
        self._background_execution.drain_pending(session_id)
        self._drive_pending(session_id)
        if session.parent_session_id is not None:
            self._delegation.notify_child_state_changed()
        return result

    def _ask_binding(self, execution: _Execution, cursor: _Cursor) -> _AskBinding:
        """Validate the cursor's live ask binding against its committed request event."""
        if cursor.ask_sequence is None or cursor.ask_request_id is None:
            raise ValueError("session holds no live ask binding")
        event = self._event(execution, cursor.ask_sequence, EventType.ASK_REQUESTED)
        request = AskRequest.from_payload(_wire_object(event.payload.get("request"), "ask request"))
        if request.request_id != cursor.ask_request_id:
            raise ValueError("parked ask binding does not match its committed request")
        parked_at = event.payload.get("parked_at_ms")
        if type(parked_at) is not int or parked_at < 0:
            raise ValueError("parked ask request has no honest park time")
        raw_key = event.payload.get("idempotency_key")
        return _AskBinding(event=event, request=request, parked_at_ms=parked_at, idempotency_key=raw_key if isinstance(raw_key, str) else None)

    @staticmethod
    def _ask_binding_expired(binding: _AskBinding) -> bool:
        """Whether the caller-declared deadline passed; None waits indefinitely like approvals."""
        timeout = binding.request.timeout_ms
        if timeout is None:
            return False
        return int(time.time() * 1000) > binding.parked_at_ms + timeout

    @staticmethod
    def _render_ask_answers(request: AskRequest, values: Mapping[str, object]) -> str:
        """Render every answer as model-projected text; bodies are omitted from projection."""
        lines = [f"Ask: {request.title}"]
        for question in request.questions:
            label = question.label or question.name
            if question.name in values:
                value = values[question.name]
                rendered = value if isinstance(value, str) else json.dumps(json_wire_value(value), ensure_ascii=False)
                lines.append(f"{label}: {rendered}")
            else:
                lines.append(f"{label}: (no answer)")
        return "\n".join(lines)

    def _adopt_ask_terminal(self, execution: _Execution, binding: _AskBinding, *, status: AskAnswerStatus, values: Mapping[str, object]) -> None:
        """Adopt one ask terminal without re-invoking the parked tool, exactly once.

        Commits ASK_ANSWERED plus the parked call's TOOL_COMPLETED in one guarded
        transaction (the guard raises AskAnswerConflict when a terminal already
        exists) and clears the park binding via _report.
        """
        request = binding.request
        if execution.cursor.intent_sequence is None:
            raise ValueError("ask adoption requires its parked tool intent")
        _, call = self._pending(execution)
        intent = self._event(execution, execution.cursor.intent_sequence, EventType.TOOL_INTENT)
        if json_wire_object(intent.payload)["call"] != encode_tool_call(call):
            raise ValueError("parked ask intent does not match its native batch member")
        if status == "answered":
            result: ToolResult = ToolSuccess(
                call.tool_name,
                TextOutput(text=self._render_ask_answers(request, values)),
                body=AskResultBody(answers=dict(values), status="answered"),
            )
            source, actor = "client", "user"
        elif status == "cancelled":
            result = ToolFailure(call.tool_name, f"ask {request.request_id} was cancelled by the user")
            source, actor = "client", "user"
        else:
            result = ToolFailure(call.tool_name, f"ask {request.request_id} expired without an answer")
            source, actor = "core", "core"
        answered = EventDraft(
            EventType.ASK_ANSWERED, source, actor, {"request_id": request.request_id, "status": status, "values": json_wire_object(values)}
        )
        try:
            self._report(execution, call, result, prefix_events=(answered,), ask_answer_guard=request.request_id)
        except AskAnswerConflict as exc:
            raise AskError(f"ask {request.request_id!r} was already answered") from exc

    def _ask_resume_prelude(self, session_id: str, request_id: str, run_abort_id: str, abort: Cancellation) -> tuple[_Execution, _AskBinding]:
        """Rebuild the parked ask's execution under the session lock, resume-style.

        Validates session, checkpoint identity, parked prefix, root budget, and
        the ask binding; the named request must be the live one and must have
        no terminal answer yet. Callers hold the session lock and registered
        the abort; adoption plus _drive continue from here.
        """
        session = self.store.session(session_id)
        self.check_session(session)
        for ev in self.store.events(session_id):
            if ev.event_type == EventType.ASK_ANSWERED and ev.payload.get("request_id") == request_id:
                raise AskError(f"ask {request_id!r} was already answered (status: {ev.payload.get('status')})")
        if session.status != "waiting":
            raise AskError(f"session {session_id!r} holds no live ask park (status: {session.status})")
        checkpoint = self.store.checkpoint(session_id)
        if checkpoint is None or checkpoint.kind == "terminal":
            raise AskError(f"session {session_id!r} holds no live ask park")
        if (
            checkpoint.revision_id != self.revision_id
            or checkpoint.run_id != session.run_id
            or checkpoint.last_event_sequence > session.last_event_sequence
        ):
            raise ParkRevisionError("ask checkpoint identity does not match the durable session")
        events_after_cp = self.store.events(session_id, after_sequence=checkpoint.last_event_sequence)
        for ev in events_after_cp:
            if not _is_parked_intervening(ev):
                raise ValueError(f"Intervening event {ev.event_type} violates parked checkpoint prefix")
        if "initial_root_budget" not in checkpoint.payload:
            raise ValueError("checkpoint has no captured root budget state")
        cursor = _Cursor.model_validate(json_wire_object(checkpoint.payload))
        if not isinstance(cursor.root_id, str) or not cursor.root_id:
            raise ValueError("checkpoint has no captured root action identity")
        if cursor.deferred_action_id is not None:
            action_abort = self._background.abort_event(cursor.deferred_action_id)
            if action_abort is not None:
                abort = Cancellation(
                    action_abort,
                    linked_reason="background action stopped",
                    linked_stop_cause_provider=lambda action_id=cursor.deferred_action_id: self._background.stop_cause(action_id),
                )
                with self._aborts_lock:
                    self._session_aborts[session_id] = (run_abort_id, abort)
        root_state = self.store.root_budget_state(cursor.root_id)
        if cursor.initial_root_budget is None:
            if root_state is not None:
                raise StoreConflict("checkpoint captured an uncapped root with a finite budget row")
        elif root_state is None or root_state[0] < cursor.initial_root_budget:
            raise StoreConflict("checkpoint lost its captured finite root authorization")
        if session.parent_session_id is not None:
            self._delegation._captured_child_root_id(session_id)
        execution = _Execution(session, cursor, list(self.store.events(session_id)), run_abort_id)
        self._messages(execution, allow_pending=True)
        binding = self._ask_binding(execution, cursor)
        if binding.request.request_id != request_id:
            raise AskError(f"ask {request_id!r} is not the live parked ask of session {session_id!r}")
        if binding.request.revision_id != self.revision_id:
            raise ParkRevisionError(f"ask {request_id!r} belongs to a retired revision")
        return execution, binding

    def answer_ask(
        self,
        session_id: str,
        request_id: str,
        values: Mapping[str, object],
        *,
        expected_revision: str,
    ) -> CoreResult:
        """Adopt one client's answer for the session's live ask and continue the run.

        Session-locked and resume-like: the request must be live and unexpired,
        values are validated by the shared strict validator, and the parked
        tool is adopted without re-invoke. A past-deadline park settles as
        expired first and the answer is refused as already terminal.
        """
        self._check_expected_revision(expected_revision)
        with self._session_lock(session_id):
            self.ensure_open()
            self._abort.set_cancelled(False)
            abort = Cancellation()
            run_abort_id = uuid4().hex
            with self._aborts_lock:
                self._session_aborts[session_id] = (run_abort_id, abort)
            resumed_notice: tuple[str, str] | None = None
            try:
                execution, binding = self._ask_resume_prelude(session_id, request_id, run_abort_id, abort)
                session = execution.session
                expired = self._ask_binding_expired(binding)
                if expired:
                    self._adopt_ask_terminal(execution, binding, status="expired", values={})
                else:
                    validated = validate_ask_answer(binding.request, values)
                    self._adopt_ask_terminal(execution, binding, status="answered", values=validated)
                result = self._drive(execution)
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
                        budget_exhaustion=result.budget_exhaustion,
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
        self.drain_deferred_requests(session_id)
        self._background_execution.drain_pending(session_id)
        self._drive_pending(session_id)
        if session.parent_session_id is not None:
            self._delegation.notify_child_state_changed()
        if expired:
            raise AskError(f"ask {request_id!r} expired without an answer; the run continued with the expiry failure")
        return result

    def cancel_ask(self, session_id: str, request_id: str, *, expected_revision: str) -> CoreResult:
        """Adopt one client's cancellation for the session's live ask and continue the run.

        Session-locked and resume-like: the request must be live and unexpired.
        A past-deadline park settles as expired first and the cancellation is
        refused as already terminal.
        """
        self._check_expected_revision(expected_revision)
        with self._session_lock(session_id):
            self.ensure_open()
            self._abort.set_cancelled(False)
            abort = Cancellation()
            run_abort_id = uuid4().hex
            with self._aborts_lock:
                self._session_aborts[session_id] = (run_abort_id, abort)
            resumed_notice: tuple[str, str] | None = None
            try:
                execution, binding = self._ask_resume_prelude(session_id, request_id, run_abort_id, abort)
                session = execution.session
                expired = self._ask_binding_expired(binding)
                if expired:
                    self._adopt_ask_terminal(execution, binding, status="expired", values={})
                else:
                    self._adopt_ask_terminal(execution, binding, status="cancelled", values={})
                result = self._drive(execution)
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
                        budget_exhaustion=result.budget_exhaustion,
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
        self.drain_deferred_requests(session_id)
        self._background_execution.drain_pending(session_id)
        self._drive_pending(session_id)
        if session.parent_session_id is not None:
            self._delegation.notify_child_state_changed()
        if expired:
            raise AskError(f"ask {request_id!r} expired without an answer; the run continued with the expiry failure")
        return result

    def _settle_expired_ask_candidate(self, candidate: WaitingAskPark) -> None:
        """Settle one sweep candidate's expired ask and continue its run, if still due.

        Stale candidates (answered, cancelled, or resumed since listing) return
        quietly; anything else fails loudly per session in the sweep caller.
        """
        session_id = candidate.session_id
        with self._session_lock(session_id):
            self.ensure_open()
            abort = Cancellation()
            run_abort_id = uuid4().hex
            with self._aborts_lock:
                self._session_aborts[session_id] = (run_abort_id, abort)
            resumed_notice: tuple[str, str] | None = None
            settled = False
            try:
                try:
                    execution, binding = self._ask_resume_prelude(session_id, candidate.ask_request_id, run_abort_id, abort)
                except AskError, KeyError:
                    return
                if not self._ask_binding_expired(binding):
                    return
                session = execution.session
                self._adopt_ask_terminal(execution, binding, status="expired", values={})
                result = self._drive(execution)
                settled = True
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
                        budget_exhaustion=result.budget_exhaustion,
                    )
                    if notify:
                        resumed_notice = (
                            session.parent_session_id,
                            child_completion_notice_id(session.parent_session_id, session_id, result.run_id),
                        )
            finally:
                with self._aborts_lock:
                    self._session_aborts.pop(session_id, None)
            if not settled:
                return
        if resumed_notice is not None:
            self._delegation._deliver_child_notice(*resumed_notice)
        self.drain_deferred_requests(session_id)
        self._background_execution.drain_pending(session_id)
        self._drive_pending(session_id)
        if session.parent_session_id is not None:
            self._delegation.notify_child_state_changed()

    def sweep_expired_asks(self) -> None:
        """Settle caller-declared ask expiries due now; one bad session never stops the sweep."""
        try:
            candidates = self.store.waiting_ask_parks()
        except Exception:
            logger.exception("ask expiry sweep failed to list parked asks")
            return
        for candidate in candidates:
            if self._closed:
                return
            try:
                self._settle_expired_ask_candidate(candidate)
            except Exception:
                logger.exception("ask expiry sweep failed for session %s", candidate.session_id)

    def _drive_pending(self, session_id: str, *, limit: int = 32) -> None:
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
            followup = self.store.claim_pending_followup(session_id)
            if followup is not None:
                cid, prompt, steps, raw_images, root_id, root_budget = followup
                try:
                    images = self._rehydrate_followup_images(raw_images, cid)
                except Exception:
                    try:
                        if self.store.continuation_status(cid) == "claimed":
                            self.store.complete_continuation(cid, status="failed")
                    except Exception:
                        pass
                    raise
                try:
                    run_res = self._execute_user_followup(session_id, cid, prompt, steps, root_id, root_budget, images=images)
                except Exception:
                    try:
                        if self.store.continuation_status(cid) == "claimed":
                            self.store.complete_continuation(cid, status="failed")
                    except Exception:
                        pass
                    raise
                if run_res.status not in ("completed", "idle"):
                    return
                continue
            try:
                results = self.drain_continuations(session_id, limit=1, _automatic=True)
            except StoreConflict:
                # No pending or claimed continuation remains: the chain is done.
                return
            if not results:
                return
            if results[-1].run_status not in ("completed", "idle"):
                return

    def drain_deferred_requests(
        self,
        session_id: str,
        *,
        background_action_id: str | None = None,
        action_abort: threading.Event | None = None,
    ) -> None:
        """Run committed deferred requests after their caller's callback lease has returned."""
        with self._session_lock(session_id):
            while True:
                self.ensure_open()
                session = self.session(session_id)
                if not self.admits_fresh_work(session):
                    return
                events = list(self.store.events(session_id))
                completed_sequences = {
                    event.payload.get("batch_sequence")
                    for event in events
                    if event.event_type in {EventType.TOOL_COMPLETED, EventType.TOOL_INTERRUPTED} and event.payload.get("deferred") is True
                }
                terminal_request_ids = {event.payload.get("request_id") for event in events if event.event_type == EventType.TOOL_DEFERRED_CANCELLED}
                request = next(
                    (
                        event
                        for event in events
                        if event.event_type == EventType.TOOL_DEFERRED
                        and event.sequence not in completed_sequences
                        and event.payload.get("request_id") not in terminal_request_ids
                        and (
                            event.payload.get("background_action_id") == background_action_id
                            if background_action_id is not None
                            else event.payload.get("background_action_id") is None
                        )
                    ),
                    None,
                )
                if request is None:
                    return
                raw_action_id = request.payload.get("background_action_id")
                request_action_id = raw_action_id if isinstance(raw_action_id, str) else None
                if action_abort is not None and action_abort.is_set():
                    if request_action_id is not None:
                        action_stop_cause = self._background.abort_cause(request_action_id)
                        status = "cancelled" if action_stop_cause == "owner_cancel" else "interrupted"
                        self.cancel_background_tool_requests(
                            session_id,
                            request_action_id,
                            status=status,
                            stop_cause=action_stop_cause,
                            reason="background action stopped before deferred dispatch",
                        )
                    return
                raw_root = request.payload.get("root_id")
                root_id = raw_root if isinstance(raw_root, str) and raw_root else ("" if request_action_id else uuid4().hex)
                raw_budget = request.payload.get("root_action_budget")
                root_budget = raw_budget if type(raw_budget) is int and raw_budget >= 0 else None
                raw_tools = request.payload.get("origin_visible_tools")
                origin_tools = (
                    tuple(raw_tools) if isinstance(raw_tools, (list, tuple)) and all(isinstance(name, str) and name for name in raw_tools) else None
                )
                raw_readonly = request.payload.get("origin_readonly")
                origin_readonly = raw_readonly if type(raw_readonly) is bool else None
                authority_missing = request_action_id is not None and (
                    not root_id or "root_action_budget" not in request.payload or origin_tools is None or origin_readonly is None
                )
                execution = _Execution(
                    session,
                    _Cursor(
                        root_id=root_id,
                        initial_root_budget=root_budget,
                        deferred_only=True,
                        deferred_action_id=request_action_id,
                        deferred_origin_tools=origin_tools,
                        deferred_origin_readonly=origin_readonly,
                        deferred_authority_missing=authority_missing,
                        logical_turn=uuid4().hex,
                    ),
                    events,
                    uuid4().hex,
                )
                if action_abort is None:
                    abort = Cancellation()
                else:
                    abort = Cancellation(
                        action_abort,
                        linked_reason="background action stopped",
                        linked_stop_cause_provider=lambda action_id=request_action_id or "": self._background.stop_cause(action_id),
                    )
                with self._aborts_lock:
                    self._session_aborts[session_id] = (execution.run_id, abort)
                try:
                    self._commit(execution, "running", "ready")
                    result = self._drive(execution)
                finally:
                    with self._aborts_lock:
                        self._session_aborts.pop(session_id, None)
                if result.status not in ("completed", "idle"):
                    return

    def command_catalog(self) -> CommandCatalog:
        """Command catalog captured together with the revision that admits it.

        Descriptor list and revision come from ONE immutable snapshot, so a client can
        bind a later action to exactly the generation it displayed.
        """
        self.ensure_open()
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

    def owner_cancel_claimed_work(
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
            entry = self._session_aborts.get(session_id)
            active = None if entry is None else entry[1]
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
        self.ensure_open()
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
        self.ensure_open()
        s = self.store.session(session_id)
        if s.workspace != str(self.workspace):
            raise ValueError("session belongs to another workspace")
        return s

    def sessions(self, *, limit: int = 100) -> tuple[SessionRecord, ...]:
        """Fetch sessions for this workspace, ordered by updated_at descending, up to limit."""
        self.ensure_open()
        return self.store.sessions(workspace=str(self.workspace), limit=limit)

    def show(self, session_id: str, *, events: bool = False) -> dict[str, object]:
        """Return session metadata, active checkpoint, and optional events as JSON-serializable dict."""
        self.ensure_open()
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
        self.ensure_open()
        self.session(session_id)
        return self.store.events_after(session_id, after_sequence=after_sequence, limit=limit)

    def transcript_page(self, session_id: str, *, after_sequence: int = 0, limit: int = 100) -> TranscriptPage:
        """One bounded transcript page; advance to next_sequence while has_more, even when empty."""
        self.ensure_open()
        self.session(session_id)
        if type(after_sequence) is not int or after_sequence < 0:
            raise ValueError("after_sequence must be a non-negative integer")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit must be an integer in 1..1000")
        return self.plugin_runtime.transcript_page(session_id, after_sequence, limit)

    def context_history(self, session_id: str, *, limit: int = 100) -> ContextHistory:
        """One bounded context prefix from the session's first record."""
        self.ensure_open()
        self.session(session_id)
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit must be an integer in 1..1000")
        return self.plugin_runtime.context_history(session_id, limit)

    def usage(self, session_id: str, *, since_sequence: int = 0, until_sequence: int | None = None) -> SessionUsageReport:
        """Calculate truthful token usage metrics strictly from immutable core facts."""
        self.ensure_open()
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
        resolved = self.session(session_id) if session is None else session
        return self._usage.report(resolved, since_sequence=since_sequence, until_sequence=until_sequence)

    def _get_owner_budget(self, session_id: str, owner: str) -> PluginBudget | None:
        return self.store.owner_budget(session_id, owner)

    @contextmanager
    def _command_abort(self, context: ToolContext) -> Generator[ToolContext]:
        # Commands may run alongside a turn and other commands: each owns its signal.
        abort = Cancellation()
        with self._aborts_lock:
            self.ensure_open()
            self._command_aborts.setdefault(context.session_id, {})[context.invocation_id] = abort
        try:
            yield replace(context, abort_signal=abort)
        finally:
            with self._aborts_lock:
                commands = self._command_aborts[context.session_id]
                commands.pop(context.invocation_id)
                if not commands:
                    self._command_aborts.pop(context.session_id)

    def _command_completion(self, name: str, owner: str, context: ToolContext, outcome: CommandInvocationOutcome) -> EventDraft:
        # Keep supervisor observations even when a command throws or is cancelled. They
        # describe observed changes, without claiming a complete account of its effects.
        changes: list[dict[str, object]] = []
        seen: set[tuple[str, str, str | None]] = set()
        for observed in outcome.staged_observations:
            path = canonical_path(self.workspace, observed.path)
            old_path = canonical_path(self.workspace, observed.old_path) if observed.old_path is not None else None
            key = (path, observed.operation, old_path)
            if key in seen:
                continue
            seen.add(key)
            changes.append({**observed.as_payload(), "path": path, **({"old_path": old_path} if old_path is not None else {})})
            if len(changes) >= MAX_MERGED_OBSERVATIONS:
                break
        return EventDraft(
            EventType.COMMAND_COMPLETED,
            f"plugin.{owner}",
            context.actor,
            {"name": name, "result": outcome.result.as_payload(), "observed_changes": changes, "cancelled": outcome.cancelled},
        )

    def command_input(
        self, session_id: str, name: str, *, expected_revision: str, arguments: Mapping[str, object] | None = None
    ) -> CommandInputSnapshot:
        """Render optional plugin-owned input; never execute the command to discover values."""
        self.ensure_open()
        session = self.session(session_id)
        with self.dispatch("command_input") as snapshot:
            if expected_revision != snapshot.revision_id:
                raise StaleRevision("displayed catalog revision is stale; refresh the catalog", expected_revision, snapshot.revision_id)
            context = ToolContext(
                workspace=self.workspace,
                session_id=session_id,
                run_id=session.run_id or "",
                invocation_id=uuid4().hex,
                revision_id=snapshot.revision_id,
                actor="user",
            )
            presentation = snapshot.plugin_runtime.prepare_command_input(name, context=context, arguments=arguments)
            return CommandInputSnapshot(revision_id=snapshot.revision_id, presentation=presentation)

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
        self.ensure_open()
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
                catalog = self.command_catalog()
                lines = ["Available commands:"]
                for group, entries in grouped_help_entries(catalog.commands):
                    lines.append("")
                    lines.append(f"{group}:")
                    for entry in entries:
                        lines.append(f"  {entry.usage} — {entry.description}")
                        lines.extend(f"    e.g. {example}" for example in entry.examples)
                return CommandResult(status="ok", output="\n".join(lines), data=catalog.as_payload())
            return CommandResult(status="ok", data=dict(self.command_catalog().as_payload()))

        cmd_desc = self._snapshot.command_of(name)
        if cmd_desc is None:
            raise ValueError(f"Unknown command: {name}")

        if cmd_desc.busy_gate and s.status not in {"idle", "completed", "failed"}:
            raise ValueError(f"Session {session_id} is busy and command '{name}' requires an idle session")

        # Core read-only policy check: external mutation only.
        self.check_session(s)
        if self.session_read_only(session_id) and denied_under_restriction(frozenset(cmd_desc.effects)):
            return CommandResult(status="error", error="read-only policy denies command write effects")

        if cmd_desc.argument_schema:
            try:
                jsonschema.Draft202012Validator(json_wire_object(cmd_desc.argument_schema)).validate(json_wire_object(arguments))
            except jsonschema.ValidationError as val_err:
                return CommandResult(status="error", error=f"Command arguments failed schema: {val_err.message}")

        run_id = s.run_id or uuid4().hex
        inv_id = uuid4().hex
        root_action_id, initial_root_budget = self._capture_root_authorization(s)
        context = ToolContext(
            workspace=self.workspace,
            session_id=session_id,
            run_id=run_id,
            invocation_id=inv_id,
            revision_id=self.revision_id,
            actor="client",
            logical_turn=uuid4().hex,
            root_action_id=root_action_id,
            initial_root_budget=initial_root_budget,
            origin_visible_tools=self.visible_tool_names_for_session(session_id, self._snapshot),
            origin_readonly=self.session_read_only(session_id),
        )
        with self.dispatch(f"command:{name}"), self._command_abort(context) as context:
            outcome = self.plugin_runtime.invoke_command(
                session_id,
                name,
                arguments,
                context=context,
                readonly=self.session_read_only(session_id),
            )

            cmd_result = outcome.result
            staged_state = outcome.staged_state
            staged_events = outcome.staged_events
            staged_continuations = outcome.staged_continuations
            staged_cancellations = outcome.staged_cancellations
            staged_observer_receipts = outcome.staged_observer_receipts
            staged_budget = outcome.staged_budget

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
                self.notify_commit()
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
                completion_event = self._command_completion(name, cmd_desc.owner, context, outcome)
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
                    observer_receipts=staged_observer_receipts,
                )
                self.notify_commit()
                if self.auto_drain and staged_continuations:
                    latest_sess = self.store.session(session_id)
                    if self.admits_fresh_work(latest_sess):
                        self.drain_continuations(session_id)
                if staged_cancellations:
                    self.owner_cancel_claimed_work(
                        session_id,
                        staged_cancellations,
                        f"owner {cmd_desc.owner} cancelled its durable work",
                    )

        if any(event.event_type == EventType.TOOL_DEFERRED for event in staged_events):
            self.drain_deferred_requests(session_id)
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
        self.ensure_open()
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

        self.check_session(s)
        # The confirmation is provenance, not privilege: the command's declared effects are
        # re-checked against the session's CURRENT stance. A pending write approved after the
        # session moved to read-only (e.g. /plan) must not mutate anything.
        if self.session_read_only(session_id) and denied_under_restriction(frozenset(cmd_desc.effects)):
            self.store.decide_confirmation(request_id, "stale")
            self._commit_confirmation_decision(session_id, request_id, rec.command_name, "stale")
            return CommandResult(status="error", error="read-only policy denies command write effects")
        self.store.decide_confirmation(request_id, "allowed")
        run_id = s.run_id or uuid4().hex
        inv_id = uuid4().hex
        root_action_id, initial_root_budget = self._capture_root_authorization(s)
        context = ToolContext(
            workspace=self.workspace,
            session_id=session_id,
            run_id=run_id,
            invocation_id=inv_id,
            revision_id=self.revision_id,
            actor="user",
            logical_turn=uuid4().hex,
            root_action_id=root_action_id,
            initial_root_budget=initial_root_budget,
            origin_visible_tools=self.visible_tool_names_for_session(session_id, self._snapshot),
            origin_readonly=self.session_read_only(session_id),
        )

        with self.dispatch(f"command:{rec.command_name}"), self._command_abort(context) as context:
            outcome = self.plugin_runtime.invoke_command(
                session_id,
                rec.command_name,
                rec.arguments,
                context=context,
                readonly=self.session_read_only(session_id),
            )

            cmd_result = outcome.result
            staged_state = outcome.staged_state
            staged_events = outcome.staged_events
            staged_continuations = outcome.staged_continuations
            staged_cancellations = outcome.staged_cancellations
            staged_observer_receipts = outcome.staged_observer_receipts
            staged_budget = outcome.staged_budget

            staged_budget_updates = []
            if staged_budget == "clear":
                staged_budget_updates.append(StagedBudgetUpdate(owner=cmd_desc.owner, action="clear"))
            elif isinstance(staged_budget, PluginBudget):
                staged_budget_updates.append(StagedBudgetUpdate(owner=staged_budget.owner, action="set", budget=staged_budget))

            if cmd_result.status in ("ok", "error"):
                latest_session = self.store.session(session_id)
                completion_event = self._command_completion(rec.command_name, cmd_desc.owner, context, outcome)
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
                    observer_receipts=staged_observer_receipts,
                )
                self.notify_commit()
                if self.auto_drain and staged_continuations:
                    latest_sess = self.store.session(session_id)
                    if self.admits_fresh_work(latest_sess):
                        self.drain_continuations(session_id)
                if staged_cancellations:
                    self.owner_cancel_claimed_work(
                        session_id,
                        staged_cancellations,
                        f"owner {cmd_desc.owner} cancelled its durable work",
                    )

        if any(event.event_type == EventType.TOOL_DEFERRED for event in staged_events):
            self.drain_deferred_requests(session_id)
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
                        "core",
                        "user",
                        {"request_id": request_id, "command_name": command_name, "decision": decision},
                    ),
                ),
                status=session.status,
                checkpoint_kind=None,
                checkpoint_payload=None,
            )
            self.notify_commit()
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
        self.ensure_open()
        session = self.session(session_id)
        with self.dispatch("views") as snapshot:
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
        max_steps: int | None = None,
        continuation_id: str | None = None,
        _automatic: bool = False,
    ) -> tuple[ContinuationResult, ...]:
        """Claim and execute pending continuations as governed runs with plugin provenance.

        `continuation_id` targets one exact durable message (used by startup recovery); a plain
        drain keeps the existing oldest-first behavior.
        Automatic delivery leaves completion notices pending on interrupted sessions;
        an explicit follow-up or drain is still new intent.
        """
        self._validate_steps(max_steps)
        results: list[ContinuationResult] = []
        parent_deliveries: list[tuple[str, str]] = []
        with self._session_lock(session_id):
            self.ensure_open()
            self._abort.set_cancelled(False)
            s = self.session(session_id)
            abort = Cancellation()
            with self._aborts_lock:
                self._session_aborts[session_id] = (None, abort)

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
                        pending = self.store.pending_continuations(session_id, limit=100)
                        if continuation_id is not None:
                            pending = tuple(record for record in pending if record.continuation_id == continuation_id)
                        if pending and (not isinstance(pending[0].root_id, str) or not pending[0].root_id):
                            missing = pending[0]
                            self.store.complete_continuation(missing.continuation_id, status="failed")
                            results.append(
                                ContinuationResult(
                                    continuation_id=missing.continuation_id,
                                    owner=missing.owner,
                                    session_id=session_id,
                                    status="failed",
                                    payload=missing.payload,
                                    run_status="failed",
                                    run_id=missing.run_id,
                                    error="queued continuation has no captured root action identity; refusing to create a new budget",
                                    stop_cause="error",
                                )
                            )
                            if continuation_id is not None:
                                break
                            continue
                        try:
                            cont_record, session_record = self.store.claim_and_start_continuation(
                                session_id,
                                run_id,
                                logical_turn,
                                revision_id=self.revision_id,
                                initial_root_budget=None,
                                max_steps=max_steps,
                                continuation_id=continuation_id,
                                caller_chain=self._delegation._chain_of(session_id),
                                automatic=_automatic,
                            )
                            self.notify_commit()
                        except StoreConflict:
                            break

                    if not isinstance(cont_record.root_id, str) or not cont_record.root_id:
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
                                error="continuation has no captured root action identity; refusing to create a new budget",
                                stop_cause="error",
                            )
                        )
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
                        if (
                            str(checkpoint.run_id) != str(cont_record.run_id)
                            or str(cp.get("continuation_id")) != cont_record.continuation_id
                            or cursor.root_id != cont_record.root_id
                        ):
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
                                    error="claimed continuation does not own its checkpoint or captured root action identity",
                                    stop_cause="error",
                                )
                            )
                            break
                        run_id = cont_record.run_id
                        session_record = self.store.session(session_id)
                    else:
                        cur_session = self.store.session(session_id)
                        root_id = cont_record.root_id
                        root_state = self.store.root_budget_state(root_id)
                        payload_max_steps = cont_record.payload.get("max_steps")
                        cursor = _Cursor(
                            continuation_id=cont_record.continuation_id,
                            root_id=root_id,
                            logical_turn=logical_turn,
                            initial_root_budget=None if root_state is None else root_state[0],
                            max_steps=payload_max_steps if type(payload_max_steps) is int else None,
                        )
                        session_record = cur_session
                    execution = _Execution(session_record, cursor, list(self.store.events(session_id)), run_id)
                    root_state = self.store.root_budget_state(cursor.root_id)
                    if max_steps is not None:
                        cursor.max_steps = max_steps if cursor.max_steps is None else min(cursor.max_steps, max_steps)
                    with self._aborts_lock:
                        self._session_aborts[session_id] = (run_id, abort)
                    if root_state is not None and root_state[1] >= root_state[0]:
                        exhaustion = BudgetExhaustion(
                            dimension="root_actions",
                            source="root_action_budget",
                            limit=root_state[0],
                            consumed=root_state[1],
                            remaining=max(0, root_state[0] - root_state[1]),
                            continue_with="extend_root_actions",
                        )
                        run_res = self._stop(
                            execution,
                            f"root action budget exhausted: consumed {root_state[1]} of {root_state[0]}, remaining 0",
                            stop_cause="limit_reached",
                            budget_exhaustion=exhaustion,
                        )
                    else:
                        run_res = self._drive(execution)

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
                            budget_exhaustion=run_res.budget_exhaustion,
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
                                budget_exhaustion=run_res.budget_exhaustion,
                            )
                            if notify:
                                parent_deliveries.append((parent_id, child_completion_notice_id(parent_id, session_id, run_res.run_id)))
                    if run_res.status in ("interrupted", "failed", "waiting"):
                        break
            finally:
                with self._aborts_lock:
                    self._session_aborts.pop(session_id, None)
                # A parked follow-up run commits no terminal fact; wake child joiners past the state change.
                if self.store.session(session_id).parent_session_id is not None:
                    self._delegation.notify_child_state_changed()

        for parent_id, notice_id in dict.fromkeys(parent_deliveries):
            self._delegation._deliver_child_notice(parent_id, notice_id)
        return tuple(results)

    def ensure_open(self) -> None:
        """Refuse work on a closed core; also the explicit host-protocol open check."""
        if self._closed:
            raise RuntimeError("engine is closed")

    def check_session(self, session: SessionRecord) -> None:
        if session.workspace != str(self.workspace) or session.active_revision_id != self.revision_id:
            raise ValueError("session workspace or revision mismatch")

    def _check_expected_revision(self, expected_revision: str) -> None:
        """Bind a client action to the catalog generation it displayed."""
        if not isinstance(expected_revision, str) or not expected_revision.strip():
            raise InvalidRevision("expected_revision must be the revision the client displayed", expected_revision)
        if expected_revision != self.revision_id:
            raise StaleRevision(f"displayed catalog revision {expected_revision} is stale; refresh the catalog", expected_revision, self.revision_id)

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
        """Run one capability activation for an already-safe boundary; refusals stay durable outcomes."""
        return self._activation.activate(
            session_id=session_id,
            source=source,
            request_id=request_id,
            target_revision_id=target_revision_id,
        )

    @property
    def state_dir(self) -> Path:
        return self._state_dir

    @property
    def config_filename(self) -> str:
        return self._config_filename

    @property
    def admits(self) -> Callable[..., Admission] | None:
        return self._admits

    @property
    def snapshot(self) -> _Snapshot | None:
        return self._snapshot

    @property
    def closed(self) -> bool:
        return self._closed

    def begin_activation(self) -> None:
        """Enter the dispatch fence, refusing while another activation or foreign dispatch is active."""
        thread_id = threading.get_ident()
        with self._calls_lock:
            if self._activating_thread is not None:
                raise ActivationFenced("another capability activation is already in progress")
            if self._active_calls:
                busy = ", ".join(sorted(self._active_calls.values()))
                raise ActivationFenced(f"capability activation is blocked by in-flight work: {busy}")
            self._activating_thread = thread_id

    def end_activation(self) -> None:
        self._clear_fence()

    def park_background_drivers(self, reason: str) -> None:
        self._background.park(reason)

    def flush_old_generation_facts(self, deferred_to_candidate: frozenset[str]) -> None:
        self._replay_pending_observers(require_complete=True, defer_to_candidate=deferred_to_candidate)

    def _build_runtime(self, sources: tuple[PluginSource, ...], *, revision_id: str, sandbox_backend: SandboxBackend | None) -> PluginRuntime:
        """Assemble one runtime generation with every host callback bound to this core."""
        return PluginRuntime(
            sources,
            workspace=self.workspace,
            revision_id=revision_id,
            store=self.store,
            usage_callback=self._get_session_usage,
            budget_callback=self._get_owner_budget,
            spawn_child_callback=self._spawn_child_callback,
            find_child_callback=self._delegation.find_keyed_child,
            join_child_callback=lambda parent, child, timeout: self.join_child(parent, child, timeout_seconds=timeout),
            wait_children_callback=lambda _owner, parent, children, guard: self._delegation.wait_children(parent, children, admission_guard=guard),
            acknowledge_child_completion_callback=lambda _owner, parent, child, run: self._delegation.acknowledge_child_completion(
                parent, child, run
            ),
            send_child_callback=lambda parent, child, prompt, label, notify, guard: self.send_child(
                parent,
                child,
                prompt,
                label=label,
                notify_on_completion=notify,
                admission_guard=guard,
            ),
            cancel_child_callback=self.cancel_child,
            list_descendants_callback=self.list_descendants,
            read_child_snapshot_callback=self._delegation.read_child_snapshot,
            cancel_subtree_callback=lambda bound, target, run, reason: self.cancel_descendant_subtree(
                bound, target, expected_run_id=run, reason=reason
            ),
            cancel_background_action_callback=self.cancel_background_action,
            model_query_callback=self.governed_model_query,
            model_query_structured_callback=self.governed_model_query_structured,
            progress_callback=self._report_progress,
            tool_definition_callback=self._visible_tool_definition,
            artifacts_dir=self._state_dir / "artifacts",
            sandbox_backend=sandbox_backend,
            process_spool_dir=self._process_spool_dir(),
        )

    def build_candidate_runtime(self, admission: Admission, target_revision_id: str) -> PluginRuntime:
        """Assemble one candidate generation with every host callback bound to this core."""
        return self._build_runtime(
            admission.plugins,
            revision_id=target_revision_id,
            sandbox_backend=self._activation.candidate_sandbox_backend(admission),
        )

    def publish_activation(self, snapshot: ActivationSnapshot, prepared: PreparedActivation) -> None:
        """Swap the generation pointer, release the fence, and retire the old runtime."""
        admission = prepared.admission
        with self._calls_lock:
            self._snapshot = _Snapshot.build(
                revision_id=admission.revision_id,
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
        self._background.notify()

    def activation_checkpoint(self, payload: Mapping[str, object] | None, /, *, source: str, request_id: str) -> Mapping[str, object]:
        """Carry the session's own cursor across the switch, plus a real resume intent where owed."""
        cursor = _Cursor()
        if payload is not None:
            cursor = _Cursor.model_validate(json_wire_object(payload))
        if source == "model":
            cursor.resume_intent = request_id
        return json_wire_object(cursor.model_dump())

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

    def record_activation_refusal(
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
                    "core",
                    "core",
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

    def _spawn_child_callback(self, request: ChildSpawnRequest) -> str:
        return self.spawn_child(
            request.parent_session_id,
            request.prompt,
            label=request.label,
            tool_allowlist=request.tool_allowlist,
            max_steps=request.max_steps,
            readonly=request.readonly,
            notify_on_completion=request.notify_on_completion,
            owner=request.owner,
            idempotency_key=request.idempotency_key,
            root_action_id=request.root_action_id,
            initial_root_budget=request.initial_root_budget,
            admission_guard=request.admission_guard,
            result_schema=request.result_schema,
        )

    def _capture_root_authorization(self, session: SessionRecord) -> tuple[str, int | None]:
        """Capture the current invocation's root, or inherit an immutable child birth."""
        if session.parent_session_id is not None:
            birth = self._delegation._birth_record(session.session_id)
            if birth is None or "root_action_budget" not in birth:
                raise StoreConflict(f"child session {session.session_id!r} has no captured root authorization")
            root_id = birth.get("root_id")
            root_budget = birth.get("root_action_budget")
        elif session.status in ("running", "waiting"):
            checkpoint = self.store.checkpoint(session.session_id)
            payload = checkpoint.payload if checkpoint is not None and isinstance(checkpoint.payload, Mapping) else {}
            if "initial_root_budget" not in payload:
                raise StoreConflict(f"active session {session.session_id!r} has no captured root budget")
            root_id = payload.get("root_id")
            root_budget = payload.get("initial_root_budget")
        else:
            return uuid4().hex, None
        if not isinstance(root_id, str) or not root_id or "\0" in root_id:
            raise StoreConflict(f"session {session.session_id!r} has no valid captured root action identity")
        if root_budget is not None and (type(root_budget) is not int or root_budget < 0):
            raise StoreConflict(f"session {session.session_id!r} has an invalid captured root budget")
        return root_id, root_budget

    def spawn_child(
        self,
        parent_session_id: str,
        prompt: str,
        *,
        label: str = "",
        tool_allowlist: tuple[str, ...] | None = None,
        max_steps: int | None = None,
        readonly: bool = False,
        notify_on_completion: bool = False,
        owner: str = "",
        idempotency_key: str | None = None,
        root_action_id: str | None = None,
        initial_root_budget: int | None = None,
        admission_guard: Callable[[], bool] | None = None,
        result_schema: Mapping[str, object] | None = None,
    ) -> str:
        if root_action_id is None:
            if initial_root_budget is not None:
                raise ValueError("a finite child budget requires its captured root action identity")
            if idempotency_key is not None:
                retry_id = self._delegation._keyed_child_id(parent_session_id, owner, idempotency_key)
                retry_birth = self._delegation._birth_record(retry_id)
                if retry_birth is not None:
                    if "root_action_budget" not in retry_birth:
                        raise StoreConflict(f"keyed child {retry_id!r} has no captured root budget")
                    candidate_root_id = retry_birth.get("root_id")
                    if not isinstance(candidate_root_id, str) or not candidate_root_id:
                        raise StoreConflict(f"keyed child {retry_id!r} has no captured root action identity")
                    root_action_id = candidate_root_id
                    candidate_budget = retry_birth.get("root_action_budget")
                    if candidate_budget is None:
                        initial_root_budget = None
                    elif type(candidate_budget) is int and candidate_budget >= 0:
                        initial_root_budget = candidate_budget
                    else:
                        raise StoreConflict(f"keyed child {retry_id!r} has an invalid captured root budget")
            if root_action_id is None:
                parent_sess = self.store.session(parent_session_id)
                if idempotency_key is not None and parent_sess.parent_session_id is None and parent_sess.status not in ("running", "waiting"):
                    retry_id = self._delegation._keyed_child_id(parent_session_id, owner, idempotency_key)
                    root_action_id = hashlib.sha256(f"root:{retry_id}".encode()).hexdigest()
                    initial_root_budget = None
                else:
                    root_action_id, initial_root_budget = self._capture_root_authorization(parent_sess)
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
            root_action_id=root_action_id,
            initial_root_budget=initial_root_budget,
            admission_guard=admission_guard,
            result_schema=result_schema,
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
        admission_guard: Callable[[], bool] | None = None,
    ) -> ChildSendReceipt:
        return self._delegation.send_child(
            parent_session_id,
            child_id,
            prompt,
            label=label,
            notify_on_completion=notify_on_completion,
            admission_guard=admission_guard,
        )

    def cancel_child(self, parent_session_id: str, child_id: str, reason: str = "owner cancellation") -> bool:
        return self._delegation.cancel_child(parent_session_id, child_id, reason=reason)

    def list_descendants(self, session_id: str) -> tuple[DescendantNode, ...]:
        return self._delegation.list_descendants(session_id)

    def cancel_descendant_subtree(
        self, bound_session_id: str, target_session_id: str, *, expected_run_id: str | None, reason: str = "owner cancellation"
    ) -> CancelSubtreeReceipt:
        return self._delegation.cancel_descendant_subtree(bound_session_id, target_session_id, expected_run_id=expected_run_id, reason=reason)

    def tick(self, now_ms: int | None = None) -> dict[str, object]:
        """Fire at most one due wakeup: claim it atomically with its run, then drive that run."""
        self.ensure_open()
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
        self.notify_commit()
        wakeup, session = claimed
        cursor = _Cursor(root_id=wakeup.root_id, logical_turn=logical_turn)
        execution = _Execution(session, cursor, list(self.store.events(session.session_id)), run_id)
        with self._session_lock(session.session_id):
            self._deliver_facts(execution, (self._event(execution, session.last_event_sequence, EventType.WAKEUP_FIRED),))
            result = self._drive(execution)
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

    def drive_background_action(self, claimed: ContinuationRecord, abort: threading.Event) -> None:
        """Delegate one claimed action to the background executor."""
        self._background_execution.drive(claimed, abort)

    def _commit_tool_wait_events(
        self,
        session_id: str,
        expected_session: SessionRecord,
        events: Sequence[EventDraft],
        *,
        status: str | None = None,
        checkpoint_kind: Literal["ready", "approval_wait", "terminal", "interrupted"] | None = None,
        checkpoint_payload: Mapping[str, object] | None = None,
    ) -> None:
        events = self._stamp_fact_owners(events)
        self.store.commit_staged(
            session_id,
            expected_sequence=expected_session.last_event_sequence,
            expected_run_id=expected_session.run_id,
            run_id=expected_session.run_id or uuid4().hex,
            revision_id=expected_session.active_revision_id,
            events=events,
            status=expected_session.status if status is None else status,
            checkpoint_kind=checkpoint_kind,
            checkpoint_payload=checkpoint_payload,
        )
        self.notify_commit()
        self._background.notify_tool_waiters()

    def cancel_background_tool_requests(
        self,
        session_id: str,
        action_id: str,
        *,
        status: Literal["cancelled", "interrupted", "core_lost", "budget_exhausted"],
        stop_cause: StopCause | None,
        reason: str,
    ) -> None:
        while True:
            expected_session = self.store.session(session_id)
            events = list(self.store.events(session_id))
            terminal_request_ids = self._background_execution.terminal_request_ids(events)
            requests = [
                event
                for event in events
                if event.event_type == EventType.TOOL_DEFERRED
                and event.payload.get("background_action_id") == action_id
                and event.payload.get("result_delivery") == "requester"
                and event.payload.get("request_id") not in terminal_request_ids
            ]
            if not requests:
                return
            terminal_events: list[EventDraft] = []
            request_intents: dict[int, bool] = {}
            for request in requests:
                request_id = request.payload.get("request_id")
                if not isinstance(request_id, str) or not request_id:
                    continue
                intent_exists = any(
                    event.event_type == EventType.TOOL_INTENT and event.payload.get("batch_sequence") == request.sequence for event in events
                )
                request_intents[request.sequence] = intent_exists
                if status == "cancelled" and intent_exists:
                    continue
                payload: dict[str, object] = {
                    "request_id": request_id,
                    "background_action_id": action_id,
                    "owner": request.payload.get("requester", ""),
                    "revision_id": request.payload.get("expected_revision", ""),
                    "tool_name": decode_tool_call(_wire_object(request.payload["call"], "deferred tool call")).tool_name,
                    "status": status,
                    "reason": reason,
                }
                if stop_cause is not None:
                    payload["stop_cause"] = stop_cause
                if intent_exists and status in {"interrupted", "core_lost"}:
                    payload["execution"] = {
                        "cancellation_signalled": stop_cause in {"user_stop", "owner_cancel", "shutdown"},
                        "execution_stopped": False,
                        "side_effect_state": "unknown",
                    }
                terminal_events.append(EventDraft(EventType.TOOL_DEFERRED_CANCELLED, "core", "plugin", payload))
            if not terminal_events:
                return
            checkpoint = self.store.checkpoint(session_id)
            checkpoint_payload: Mapping[str, object] | None = None
            checkpoint_kind: Literal["ready", "approval_wait", "terminal", "interrupted"] | None = None
            new_status: str | None = None
            if checkpoint is not None and checkpoint.kind != "terminal" and isinstance(checkpoint.payload, Mapping):
                cursor = _Cursor.model_validate(json_wire_object(checkpoint.payload))
                current_request = next((request for request in requests if request.sequence == cursor.deferred_sequence), None)
                if (
                    current_request is not None
                    and current_request.payload.get("background_action_id") == action_id
                    and (not request_intents.get(current_request.sequence, False) or status == "core_lost")
                ):
                    approval_id: str | None = None
                    if cursor.approval_sequence is not None:
                        approval = next(
                            (
                                event
                                for event in events
                                if event.sequence == cursor.approval_sequence and event.event_type == EventType.APPROVAL_REQUESTED
                            ),
                            None,
                        )
                        raw_approval_id = None if approval is None else approval.payload.get("request_id")
                        approval_id = raw_approval_id if isinstance(raw_approval_id, str) else None
                    if approval_id is not None:
                        terminal_events.append(
                            EventDraft(
                                EventType.APPROVAL_DECIDED,
                                "core",
                                "core",
                                {"request_id": approval_id, "decision": "deny", "reason": reason},
                            )
                        )
                    run_payload: dict[str, object] = {
                        "reason": reason,
                        "logical_turn": cursor.logical_turn,
                        "deferred_request_id": current_request.payload.get("request_id"),
                        "background_action_id": action_id,
                    }
                    if stop_cause is not None:
                        run_payload["stop_cause"] = stop_cause
                    terminal_events.append(EventDraft(EventType.RUN_INTERRUPTED, "core", "core", run_payload))
                    cursor.batch_sequence = None
                    cursor.next_call = 0
                    cursor.intent_sequence = None
                    cursor.approval_sequence = None
                    cursor.deferred_sequence = None
                    cursor.deferred_depth = 0
                    cursor.deferred_action_id = None
                    cursor.deferred_origin_tools = None
                    cursor.deferred_origin_readonly = None
                    cursor.deferred_authority_missing = False
                    checkpoint_payload = cursor.model_dump(mode="json")
                    checkpoint_kind = "terminal"
                    new_status = "interrupted"
            try:
                self._commit_tool_wait_events(
                    session_id,
                    expected_session,
                    terminal_events,
                    status=new_status,
                    checkpoint_kind=checkpoint_kind,
                    checkpoint_payload=checkpoint_payload,
                )
                return
            except SequenceConflict:
                continue

    def cancel_background_action(self, session_id: str, owner: str, action_id: str, reason: str = "owner cancellation") -> bool:
        """Cancel one owner action and only its own queued or in-flight tool requests."""
        return self._background.cancel_action(session_id, owner, action_id, reason)

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
        return self._background.wait_actions(session_id, action_id=action_id, timeout_seconds=timeout_seconds)

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
        turn, provider_name, model_name = self._model_query_turn(session_id, prompt, model, max_output_tokens, include_context)
        self._commit_model_query_event(session_id, turn, provider_name, model_name)
        return turn.output or ""

    def governed_model_query_structured(
        self,
        session_id: str,
        prompt: str,
        result_schema: Mapping[str, object],
        model: str | None = None,
        max_output_tokens: int | None = None,
        include_context: bool = False,
    ) -> StructuredModelResult:
        """A tool-free side turn parsed and validated against ``result_schema``.

        Same dispatch, abort, usage event, and tool-calls guard as
        ``governed_model_query``. ``result_schema`` must be a JSON Schema
        describing one JSON object. An invalid reply yields a callable
        ``parse_error``; cancellation, budget exhaustion, and provider errors
        propagate exactly as the bare query — never as data.
        """
        if not isinstance(result_schema, Mapping):
            raise ValueError("result_schema must be an object")
        try:
            schema_json = json_wire_object(own_json_object(result_schema))
        except ValueError as exc:
            raise ValueError(f"result_schema must be a JSON object: {exc}") from exc
        try:
            jsonschema.Draft202012Validator.check_schema(schema_json)
        except jsonschema.exceptions.SchemaError as err:
            raise ValueError(f"result_schema is not a valid JSON schema: {err.message}") from err
        turn, provider_name, model_name = self._model_query_turn(
            session_id,
            f"{prompt}\n\nReply with exactly one JSON object and nothing else.",
            model,
            max_output_tokens,
            include_context,
        )
        self._commit_model_query_event(session_id, turn, provider_name, model_name)
        raw_text = turn.output or ""
        stripped = raw_text.strip()
        if not stripped:
            return StructuredModelResult(
                raw_text=raw_text,
                parse_error=StructuredQueryParseError(kind="invalid_json", message="model reply is empty"),
                done_reason=turn.done_reason,
            )
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError as exc:
            return StructuredModelResult(
                raw_text=raw_text,
                parse_error=StructuredQueryParseError(kind="invalid_json", message=f"model reply is not JSON: {exc.msg}"),
                done_reason=turn.done_reason,
            )
        if not isinstance(parsed, dict):
            return StructuredModelResult(
                raw_text=raw_text,
                parse_error=StructuredQueryParseError(kind="schema_violation", message="top-level JSON value must be an object"),
                done_reason=turn.done_reason,
            )
        try:
            owned = own_json_object(parsed)
        except ValueError as exc:
            return StructuredModelResult(
                raw_text=raw_text,
                parse_error=StructuredQueryParseError(kind="invalid_json", message=f"model reply is not JSON: {exc}"),
                done_reason=turn.done_reason,
            )
        try:
            jsonschema.Draft202012Validator(schema_json).validate(json_wire_object(owned))
        except jsonschema.ValidationError as exc:
            return StructuredModelResult(
                raw_text=raw_text,
                parse_error=StructuredQueryParseError(kind="schema_violation", message=exc.message, path=exc.json_path),
                done_reason=turn.done_reason,
            )
        return StructuredModelResult(raw_text=raw_text, data=owned, done_reason=turn.done_reason)

    def _model_query_turn(
        self,
        session_id: str,
        prompt: str,
        model: str | None,
        max_output_tokens: int | None,
        include_context: bool,
    ) -> tuple[ProviderTurnResult, str, str]:
        """The shared governed side-turn: resolution, abort, dispatch, tool-calls guard."""
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode("utf-8")) > 64 * 1024:
            raise ValueError("model query prompt must be nonempty UTF-8 text within 64 KiB")
        self.ensure_open()
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
            context = self._messages(execution, completed_only=True, model=selected)
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
        with self.dispatch("model-query") as snapshot:
            turn = snapshot.plugin_runtime.propose_turn(request, provider=provider_name)
        if turn.tool_calls:
            raise RuntimeError("governed model query returned tool calls; none were offered")
        return turn, provider_name, model_name

    def _commit_model_query_event(self, session_id: str, turn: ProviderTurnResult, provider_name: str, model_name: str) -> None:
        """Record one governed query's usage; the question and answer stay out of history."""
        self._commit_control_event(
            session_id,
            EventDraft(
                EventType.MODEL_QUERY,
                "core",
                "plugin",
                {
                    "provider": provider_name,
                    "model": model_name,
                    "usage": None if turn.usage is None else turn.usage.metadata_payload(),
                    "cost_usd": None if turn.usage is None else turn.usage.cost_usd,
                },
            ),
        )

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
            self.notify_commit()
        except Exception:
            # A control fact is diagnostic; losing it never blocks the owner's real work.
            return

    def _report_progress(self, lease: LeaseContext, payload: Mapping[str, object]) -> None:
        if len(canonical_json(payload).encode("utf-8")) > 16 * 1024:
            raise ValueError("progress report exceeds the 16 KiB bound")
        self._commit_control_event(
            lease.identity.session_id,
            EventDraft(
                EventType.TOOL_PROGRESS,
                f"plugin.{lease.identity.owner}",
                lease.identity.actor,
                {
                    "invocation_id": lease.identity.invocation_id,
                    "owner": lease.identity.owner,
                    "run_id": lease.identity.run_id,
                    "revision_id": lease.identity.revision_id,
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
            source="core",
            actor="core",
            payload={
                "owner": ", ".join(missing),
                "error": f"event {event.sequence} was produced by revision {event.revision_id} but its registered owner is not loaded",
                "event_sequence": event.sequence,
            },
        )

    def _observer_root_authorization(self, execution: _Execution) -> tuple[str | None, int | None]:
        """Recorded root authorization for observer leases delivering this execution's facts.

        Reads the run's own durable root — the birth record for child sessions, the
        checkpoint cursor otherwise (the same sources `_capture_root_authorization`
        uses) — and never mints a fresh identity: a terminal fact's observer spends
        the run's own budget chain or fails visibly. Returns `(None, None)` when no
        valid root is recorded (legacy rows); the caller then leaves the lease
        rootless and the spawn/background/wakeup guards fail closed.
        """
        session = execution.session
        if session.parent_session_id is not None:
            birth = self._delegation._birth_record(session.session_id)
            if birth is None or "root_action_budget" not in birth:
                return None, None
            root_id = birth.get("root_id")
            budget = birth.get("root_action_budget")
        else:
            root_id = execution.cursor.root_id
            budget = execution.cursor.initial_root_budget
        if not isinstance(root_id, str) or not root_id or "\0" in root_id:
            return None, None
        if budget is not None and (type(budget) is not int or budget < 0):
            return None, None
        return root_id, budget

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
        with self.dispatch(f"observer:{fact_type}") as snapshot:
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
            root_id, root_budget = self._observer_root_authorization(execution)
            obs_res = snapshot.plugin_runtime.notify_fact(
                fact_type, fact_payload, owners=tuple(active), logical_turn=logical_turn, root_action_id=root_id, initial_root_budget=root_budget
            )
            receipts = tuple(ObserverReceiptRecord(session_id=ev.session_id, event_sequence=ev.sequence, owner=o) for o in active)
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
                    "core",
                    "core",
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
        root_budget_grant: tuple[str, int, str] | None = None,
        start_turn: bool = False,
        checkpoint_bindings: Sequence[tuple[str, str]] = (),
        pinned_run_id: str | None = None,
        enforce_pin: bool = False,
        ask_answer_guard: str | None = None,
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
        if enforce_pin and execution.run_id != pinned_run_id:
            raise StoreConflict(f"execution run {execution.run_id!r} does not match bound run {pinned_run_id!r}")
        for attempt in range(16):
            current = self.store.session(execution.session.session_id)
            if enforce_pin and current.run_id != pinned_run_id:
                raise StoreConflict(f"session run {current.run_id!r} does not match bound run {pinned_run_id!r}")
            self._reconcile_execution_events(
                execution,
                upto=current.last_event_sequence,
                session=current,
                parked=True,
            )
            before = len(execution.events)
            previous = current
            root_id = execution.cursor.root_id
            root_reservation: tuple[str, int | None] | None = None
            if reserve_root and root_id:
                root_reservation = (root_id, execution.cursor.initial_root_budget)
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
                    root_action_id=root_id or None,
                    root_action_budget=execution.cursor.initial_root_budget,
                    root_budget_grant=root_budget_grant,
                    staged_state=staged_state,
                    staged_continuations=stamped_continuations,
                    staged_cancellations=staged_cancellations,
                    staged_budgets=staged_budgets,
                    observer_receipts=observer_receipts,
                    completed_continuation_id=completed_continuation_id,
                    completed_continuation_status=completed_continuation_status,
                    start_turn=start_turn,
                    checkpoint_bindings=checkpoint_bindings,
                    ask_answer_guard=ask_answer_guard,
                )
                self.notify_commit()
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
        observer_receipts: Sequence[ObserverReceiptRecord] = (),
        completed_continuation_id: str | None = None,
        completed_continuation_status: Literal["completed", "failed", "cancelled"] = "completed",
        reserve_root: bool = False,
        root_budget_grant: tuple[str, int, str] | None = None,
        checkpoint_bindings: Sequence[tuple[str, str]] = (),
        pinned_run_id: str | None = None,
        enforce_pin: bool = False,
        ask_answer_guard: str | None = None,
    ) -> tuple[StoredEvent, ...]:
        known_before = len(execution.events)
        new_events = self._commit_staged_rebased(
            execution,
            status=status,
            kind=kind,
            events=events,
            reserve_root=reserve_root,
            root_budget_grant=root_budget_grant,
            checkpoint_bindings=checkpoint_bindings,
            staged_state=staged_state,
            staged_continuations=staged_continuations,
            staged_cancellations=staged_cancellations,
            staged_budgets=staged_budgets,
            observer_receipts=observer_receipts,
            completed_continuation_id=completed_continuation_id,
            completed_continuation_status=completed_continuation_status,
            start_turn=start_turn,
            pinned_run_id=pinned_run_id,
            enforce_pin=enforce_pin,
            ask_answer_guard=ask_answer_guard,
        )
        self._background.notify_tool_waiters()
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
        completed_sequences = {
            event.payload.get("batch_sequence")
            for event in execution.events
            if event.event_type in {EventType.TOOL_COMPLETED, EventType.TOOL_INTERRUPTED} and event.payload.get("deferred") is True
        }
        terminal_request_ids = {
            event.payload.get("request_id") for event in execution.events if event.event_type == EventType.TOOL_DEFERRED_CANCELLED
        }
        action_id = execution.cursor.deferred_action_id
        return next(
            (
                event
                for event in execution.events
                if event.event_type == EventType.TOOL_DEFERRED
                and event.sequence not in completed_sequences
                and event.payload.get("request_id") not in terminal_request_ids
                and event.payload.get("root_id", "") in {"", execution.cursor.root_id}
                and (
                    event.payload.get("background_action_id") == action_id
                    if action_id is not None
                    else event.payload.get("background_action_id") is None
                )
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
        text = result.output.text if isinstance(result.output, TextOutput) else (result.output.presentation or "")
        candidates = [hook for hook in snapshot.plugin_runtime.post_tool_hooks if call.tool_name in hook[1] or "*" in hook[1]]
        if not candidates:
            return result
        restricted = self.session_read_only(execution.session.session_id)
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
            steering_context=result.steering_context,
        )

    @staticmethod
    def _tool_schema_characters(tools: tuple[ToolDefinition, ...]) -> int:
        """Characters of the core-side neutral tool encoding this dispatch advertises."""
        return sum(len(canonical_json(encode_tool_definition(tool))) for tool in tools)

    @staticmethod
    def _request_window(
        messages: tuple[ProviderMessage, ...],
        tools: tuple[ToolDefinition, ...],
    ) -> RequestWindow:
        """Measure the request exactly as it is about to be sent (core truth, no estimates).

        Per-segment facts are character counts and digests of the literal strings, so a client
        panel derives its view from measurements; token counts stay provider-reported usage.
        Image inventory counts carried attachments and their base64-text bytes, never tokens.
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
                    image_count=len(message.images),
                    image_bytes=sum(len(image.data.encode("utf-8")) for image in message.images),
                )
            )
        tool_schema_characters = Core._tool_schema_characters(tools)
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

    def session_read_only(self, session_id: str) -> bool:
        """Effective read-only for one session: the frozen core policy OR owner overlays."""
        if self.config.policy.read_only:
            return True
        return bool(self.store.session_overlays(session_id))

    def _visible_tools(self, execution: _Execution, snapshot: _Snapshot) -> tuple[ToolDefinition, ...]:
        """The catalog this session may see: current scope intersected with captured origin authority."""
        tools = snapshot.tools
        scope = execution.session.tool_allowlist
        if scope is not None:
            tools = tuple(tool for tool in tools if tool.name in scope)
        if execution.cursor.deferred_origin_tools is not None:
            allowed = set(execution.cursor.deferred_origin_tools)
            tools = tuple(tool for tool in tools if tool.name in allowed)
        restrictive = self.session_read_only(execution.session.session_id) or execution.cursor.deferred_origin_readonly is True
        if restrictive:
            tools = tuple(tool for tool in tools if not denied_under_restriction(tool.effects))
        return tools

    def visible_tool_names_for_session(self, session_id: str, snapshot: _Snapshot) -> tuple[str, ...]:
        session = self.store.session(session_id)
        tools = snapshot.tools
        if session.tool_allowlist is not None:
            tools = tuple(tool for tool in tools if tool.name in session.tool_allowlist)
        if self.session_read_only(session_id):
            tools = tuple(tool for tool in tools if not denied_under_restriction(tool.effects))
        return tuple(tool.name for tool in tools)

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
        if self.session_read_only(session_id) and denied_under_restriction(definition.effects):
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
    def _validate_steps(max_steps: int | None) -> None:
        if max_steps is not None and (type(max_steps) is not int or max_steps < 1):
            raise ValueError("max_steps must be a positive integer or null")

    @staticmethod
    def _validate_interaction(interaction: Literal["interactive", "unattended"] | None) -> None:
        if interaction is not None and interaction not in ("interactive", "unattended"):
            raise ValueError('interaction must be "interactive", "unattended", or null')

    @staticmethod
    def _validate_root_action_budget(budget: int | None) -> None:
        if budget is not None and (type(budget) is not int or not 0 <= budget <= 9_223_372_036_854_775_807):
            raise ValueError("root_action_budget must fit a non-negative SQLite INTEGER")

    @staticmethod
    def _validate_root_action_extension(amount: int | None, grant_id: str | None) -> None:
        if amount is None:
            if grant_id is not None:
                raise ValueError("root_action_grant_id requires extend_root_actions")
            return
        if type(amount) is not int or amount < 1:
            raise ValueError("extend_root_actions must be a positive integer")
        if not isinstance(grant_id, str) or not grant_id or len(grant_id) > 128 or "\0" in grant_id:
            raise ValueError("root_action_grant_id must be nonempty text within 128 characters")

    def _drive_session_abort(self, execution: _Execution) -> Cancellation | None:
        """The session abort entry for this drive, if any; the process abort stays on self."""
        with self._aborts_lock:
            entry = self._session_aborts.get(execution.session.session_id)
            return None if entry is None else entry[1]

    def _drive(self, execution: _Execution) -> CoreResult:
        while True:
            session_abort = self._drive_session_abort(execution)
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
                    if cursor.deferred_authority_missing:
                        call = decode_tool_call(_wire_object(request.payload["call"], "deferred tool call"))
                        self._report(execution, call, ToolFailure(call.tool_name, "background tool request lacks captured origin authorization"))
                        continue
                    try:
                        self._commit(
                            execution,
                            "running",
                            "ready",
                            reserve_root=True,
                        )
                    except RootBudgetExhausted as exc:
                        exhaustion = BudgetExhaustion(
                            dimension="root_actions",
                            source="root_action_budget",
                            limit=exc.budget,
                            consumed=exc.reserved,
                            remaining=max(0, exc.budget - exc.reserved),
                            continue_with="extend_root_actions",
                        )
                        message = f"root action budget exhausted: consumed {exc.reserved} of {exc.budget}, remaining 0"
                        if request.payload.get("background_action_id") is not None and request.payload.get("result_delivery") == "requester":
                            return self._stop(execution, message, stop_cause="limit_reached", budget_exhaustion=exhaustion)
                        cursor.deferred_sequence = None
                        cursor.deferred_depth = 0
                        return self._stop(execution, message, stop_cause="limit_reached", budget_exhaustion=exhaustion)
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
                            "core",
                            "plugin",
                            {"output": None, "logical_turn": cursor.logical_turn, **self._measured_run_elapsed_fields(execution)},
                        ),
                    )
                    return self._result(execution)
                if cursor.max_steps is not None and cursor.step >= cursor.max_steps:
                    exhaustion = BudgetExhaustion(
                        dimension="provider_turns",
                        source="max_steps",
                        limit=cursor.max_steps,
                        consumed=cursor.step,
                        remaining=max(0, cursor.max_steps - cursor.step),
                        continue_with="resume_with_max_steps",
                    )
                    message = f"provider turn cap exhausted: consumed {cursor.step} of {cursor.max_steps}, remaining 0"
                    return self._stop(execution, message, stop_cause="limit_reached", budget_exhaustion=exhaustion)

                budget_stop = self._budget_admission(execution)
                if budget_stop is not None:
                    return budget_stop

                # Safe boundary: every member of the previous native batch is reported and
                # committed, so messages addressed to this running session may join THIS run
                # before the next provider dispatch. Never across an in-flight tool call, an
                # approval checkpoint, or a token stream.
                self._delegation._merge_child_messages(execution)

                with self.dispatch("provider") as snapshot:
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
                        visible_tools = self._visible_tools(execution, snapshot)
                        messages = self._messages(
                            execution,
                            model=model,
                            tool_schema_characters=self._tool_schema_characters(visible_tools),
                        )
                    except ValueError as exc:
                        return self._fail(execution, str(exc))
                    except (OSError, RuntimeError) as exc:
                        # Critical context/admission failure: the run finishes durably
                        # failed before any intent is committed or provider is billed.
                        return self._fail(execution, f"context assembly failed: {type(exc).__name__}: {exc}")

                    effective_abort = session_abort or self._abort
                    request = ProviderTurnRequest(
                        messages=messages,
                        available_tools=visible_tools,
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
                    allocation = execution.projection_allocation
                    allocation_payload: dict[str, object] | None = None
                    if allocation is not None:
                        allocation_payload = dict(allocation.as_payload())
                        allocation_payload["ineffective_tools"] = execution.allocation_ineffective_tools
                    try:
                        intent_events = self._commit(
                            execution,
                            "running",
                            "ready",
                            EventDraft(
                                EventType.PROVIDER_INTENT,
                                "core",
                                "core",
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
                                    # The sizing decision behind the tool projections above.
                                    "projection_allocation": allocation_payload,
                                },
                            ),
                            reserve_root=True,
                        )
                    except RootBudgetExhausted as exc:
                        exhaustion = BudgetExhaustion(
                            dimension="root_actions",
                            source="root_action_budget",
                            limit=exc.budget,
                            consumed=exc.reserved,
                            remaining=max(0, exc.budget - exc.reserved),
                            continue_with="extend_root_actions",
                        )
                        message = f"root action budget exhausted: consumed {exc.reserved} of {exc.budget}, remaining 0"
                        return self._stop(execution, message, stop_cause="limit_reached", budget_exhaustion=exhaustion)
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
                                "core",
                                "core",
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
                                "core",
                                "core",
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
                    if effective_abort.cancelled and turn.done_reason == "cancelled":
                        # A provider may acknowledge cancellation with a typed result
                        # instead of raising. Preserve its measured result/usage, then
                        # settle the actual user stop rather than inventing a failure.
                        self._commit(
                            execution,
                            "interrupted",
                            "interrupted",
                            EventDraft(EventType.MODEL_TURN, "provider", "model", model_turn_payload),
                        )
                        return self._stop(
                            execution,
                            effective_abort.reason or "run cancelled",
                            stop_cause=effective_abort.stop_cause or "user_stop",
                        )

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
                                "core",
                                "core",
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
                self._report(execution, call, ToolFailure(call.tool_name, "tool is outside this session's profile scope"))
                continue
            if cursor.deferred_origin_tools is not None and call.tool_name not in cursor.deferred_origin_tools:
                self._report(execution, call, ToolFailure(call.tool_name, "tool is outside the captured origin scope"))
                continue
            definition = self._snapshot.definition_of(call.tool_name)
            if definition is None:
                self._report(execution, call, ToolFailure(call.tool_name, "tool is not in the active capability revision"))
                continue
            effective_read_only = self.session_read_only(execution.session.session_id) or cursor.deferred_origin_readonly is True
            denial = self._denial(call, definition, read_only=effective_read_only)
            if denial is None:
                denial = self._gate_denial(execution, snapshot, call, readonly=effective_read_only)
            if denial:
                self._report(execution, call, ToolFailure(call.tool_name, denial))
                continue
            if cursor.intent_sequence is None and self._needs_approval(definition):
                if execution.interaction == "unattended":
                    # D3: an unattended run refuses the approval park at the
                    # park-commit site with a typed failure and continues; the
                    # model sees explicit unsupported, never a hang or a fabricated allow.
                    self._report(
                        execution,
                        call,
                        ToolFailure(call.tool_name, f"approval is unsupported while unattended; {call.tool_name} was not executed"),
                    )
                    continue
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
                    target_revision_id, _admission = self._activation.prepare_target()
                    payload["target_revision_id"] = target_revision_id
                    payload["activation_pending"] = True
                self._commit(
                    execution,
                    "waiting",
                    "approval_wait",
                    EventDraft(EventType.APPROVAL_REQUESTED, "core", "plugin" if cursor.deferred_sequence is not None else "model", payload),
                    checkpoint_bindings=(("approval_sequence", EventType.APPROVAL_REQUESTED),),
                )
                return self._result(execution, pending_approval=payload)
            stopped = self._invoke(execution, call, definition)
            if stopped is not None:
                return stopped

    def _invoke(self, execution: _Execution, call: ToolCall, definition: ToolDefinition) -> CoreResult | None:
        with self.dispatch(f"tool:{call.tool_name}") as snapshot:
            cursor = execution.cursor
            session_abort = None
            with self._aborts_lock:
                entry = self._session_aborts.get(execution.session.session_id)
                session_abort = None if entry is None else entry[1]
            effective_abort = session_abort or self._abort
            if effective_abort.cancelled:
                reason = effective_abort.reason or "cancelled"
                return self._stop(execution, reason, stop_cause=effective_abort.stop_cause or "user_stop")
            if cursor.intent_sequence is None:
                invocation_id = uuid4().hex
                self._commit(
                    execution,
                    "running",
                    "ready",
                    EventDraft(
                        EventType.TOOL_INTENT,
                        "core",
                        "plugin" if cursor.deferred_sequence is not None else "model",
                        {
                            "invocation_id": invocation_id,
                            "batch_sequence": cursor.deferred_sequence or cursor.batch_sequence,
                            "call": encode_tool_call(call),
                            "owner": snapshot.owner_of(call.tool_name),
                            "effects": sorted(effect.value for effect in definition.effects),
                            "semantics": [],
                            "replay_policy": definition.effective_replay_policy,
                            "icon": {"glyph": definition.icon.glyph} if definition.icon is not None else None,
                            "logical_turn": execution.cursor.logical_turn,
                            "depth": cursor.deferred_depth,
                        },
                    ),
                    checkpoint_bindings=(("intent_sequence", EventType.TOOL_INTENT),),
                )
            else:
                intent = self._event(execution, cursor.intent_sequence, EventType.TOOL_INTENT)
                invocation_id = str(intent.payload["invocation_id"])

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
                            "core",
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
            current_readonly = self.session_read_only(execution.session.session_id)
            origin_readonly = current_readonly or cursor.deferred_origin_readonly is True
            origin_visible_tools = tuple(tool.name for tool in self._visible_tools(execution, snapshot))
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
                root_action_id=cursor.root_id,
                initial_root_budget=cursor.initial_root_budget,
                origin_visible_tools=origin_visible_tools,
                origin_readonly=origin_readonly,
                background_action_id=cursor.deferred_action_id,
            )
            try:
                outcome = snapshot.plugin_runtime.invoke(
                    call,
                    context=context,
                    return_outcome=True,
                    readonly=origin_readonly,
                )
                if not isinstance(outcome, ToolInvocationOutcome):
                    raise TypeError("plugin runtime returned a bare tool result for an outcome request")
                result = outcome.result
                staged_state = outcome.staged_state
                staged_events = outcome.staged_events
                staged_conts = outcome.staged_continuations
                staged_cancels = outcome.staged_cancellations
                staged_receipts = outcome.staged_observer_receipts
                staged_budget = outcome.staged_budget
                staged_observations = outcome.staged_observations
            except ToolSuspension as suspension:
                # Suspend-by-unwind surfaced with frozen staged work: the park-commit
                # site below performs the atomic commit (park event + checkpoint
                # binding + waiting). Caught nowhere else; BaseExceptions bypass the
                # generic handler by construction.
                return self._park_suspended_tool(execution, call, snapshot, suspension, effective_abort)
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
                staged_receipts = ()
                staged_observations = ()

            if result.tool_name != call.tool_name:
                return self._stop(execution, "tool result identity differs from the dispatched native call", stop_cause="error")

            if effective_abort.cancelled:
                # A cancelled call's self-report is unverifiable. A deliberate stop
                # settles the batch with unknown effects before its terminal checkpoint.
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
                staged_receipts=staged_receipts,
                staged_observations=staged_observations,
            )
            claim_id = execution.cursor.continuation_id
            claim_owner = self.store.continuation_owner(claim_id) if claim_id is not None else None
            if any(self._cancels_claimed(cancellation, claim_id, claim_owner) for cancellation in staged_cancels):
                # The tool cancelled its own claimed work: the effect and audit above are
                # durable, and this chain stops here with the owner cause it stated.
                return self._stop(execution, "tool cancelled its own durable work", stop_cause="owner_cancel")
            return None

    def _park_suspended_tool(
        self,
        execution: _Execution,
        call: ToolCall,
        snapshot: _Snapshot,
        suspension: ToolSuspension,
        effective_abort: Cancellation,
    ) -> CoreResult | None:
        """Commit one suspended tool's park (event + binding + waiting), or refuse it visibly.

        The single park-commit site: the invoke boundary froze staged work and
        revoked the lease; this commit is atomic like the approval park. Refusals
        (unattended, second suspend, unimplemented composition) adopt a typed
        ToolFailure with the frozen staged work and continue the run.
        """
        cursor = execution.cursor
        staged = suspension.staged
        if effective_abort.cancelled:
            # Best-effort pre-join read at the boundary; re-checked here before committing.
            return self._stop(execution, "tool invocation was cancelled", stop_cause=effective_abort.stop_cause or "user_stop")
        park = suspension.park
        if park.park_kind != "ask":
            self._report(
                execution,
                call,
                ToolFailure(
                    call.tool_name,
                    "foreground composition is not implemented: the composition slice (driver, rows, terminals) has not landed",
                ),
                staged_state=staged.staged_state,
                staged_events=staged.staged_events,
                staged_conts=staged.staged_continuations,
                staged_cancels=staged.staged_cancellations,
                staged_budget=staged.staged_budget,
                staged_receipts=staged.staged_observer_receipts,
                staged_observations=staged.staged_observations,
            )
            return None
        if cursor.ask_sequence is not None or cursor.ask_request_id is not None:
            live = cursor.ask_request_id if cursor.ask_request_id is not None else "unknown"
            self._report(
                execution,
                call,
                ToolFailure(
                    call.tool_name,
                    f"AskAlreadyParked: session {execution.session.session_id!r} already holds live ask {live!r}; refusing a second suspend",
                ),
                staged_state=staged.staged_state,
                staged_events=staged.staged_events,
                staged_conts=staged.staged_continuations,
                staged_cancels=staged.staged_cancellations,
                staged_budget=staged.staged_budget,
                staged_receipts=staged.staged_observer_receipts,
                staged_observations=staged.staged_observations,
            )
            return None
        if execution.interaction == "unattended":
            self._report(
                execution,
                call,
                ToolFailure(call.tool_name, f"ask is unsupported while unattended; {call.tool_name} was not parked"),
                staged_state=staged.staged_state,
                staged_events=staged.staged_events,
                staged_conts=staged.staged_continuations,
                staged_cancels=staged.staged_cancellations,
                staged_budget=staged.staged_budget,
                staged_receipts=staged.staged_observer_receipts,
                staged_observations=staged.staged_observations,
            )
            return None
        draft = AskRequestDraft.from_payload(_wire_object(park.intent.get("draft"), "suspend ask draft"))
        raw_key = park.intent.get("idempotency_key")
        idempotency_key = raw_key if isinstance(raw_key, str) and raw_key else None
        request_id = uuid4().hex
        request = AskRequest(
            request_id=request_id,
            session_id=execution.session.session_id,
            revision_id=self.revision_id,
            owner=snapshot.owner_of(call.tool_name),
            title=draft.title,
            questions=draft.questions,
            context=draft.context,
            media=draft.media,
            timeout_ms=draft.timeout_ms,
        )
        envelope: dict[str, object] = {"request": request.as_payload(), "parked_at_ms": int(time.time() * 1000)}
        if idempotency_key is not None:
            envelope["idempotency_key"] = idempotency_key
        staged_budget_updates: list[StagedBudgetUpdate] = []
        if staged.staged_budget == "clear":
            staged_budget_updates.append(StagedBudgetUpdate(owner=snapshot.owner_of(call.tool_name), action="clear"))
        elif isinstance(staged.staged_budget, PluginBudget):
            staged_budget_updates.append(StagedBudgetUpdate(owner=staged.staged_budget.owner, action="set", budget=staged.staged_budget))
        cursor.ask_request_id = request_id
        self._commit(
            execution,
            "waiting",
            "ready",
            EventDraft(EventType.ASK_REQUESTED, "core", "plugin" if cursor.deferred_sequence is not None else "model", envelope),
            *staged.staged_events,
            staged_state=staged.staged_state,
            staged_continuations=staged.staged_continuations,
            staged_cancellations=staged.staged_cancellations,
            staged_budgets=tuple(staged_budget_updates),
            observer_receipts=staged.staged_observer_receipts,
            checkpoint_bindings=(("ask_sequence", EventType.ASK_REQUESTED),),
        )
        return self._result(execution, pending_ask=envelope)

    def artifact_page(self, digest: str, offset: int, limit: int) -> ArtifactPage:
        return self._projection.page(digest, offset, limit)

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
        staged_receipts: tuple[ObserverReceiptRecord, ...] = (),
        budget_exhaustion: BudgetExhaustion | None = None,
        staged_observations: tuple[Change, ...] = (),
        pinned_run_id: str | None = None,
        enforce_pin: bool = False,
        prefix_events: tuple[EventDraft, ...] = (),
        ask_answer_guard: str | None = None,
    ) -> None:
        # Transparent backend merge: supervisor-observed changes join the tool's self-report
        # (success AND failure — a failed invocation may have committed a prefix). Deduped by
        # (path, operation, old_path); the tool's own entries win ties. Capped: at most
        # MAX_MERGED_OBSERVATIONS supervisor entries join one result; the helper already caps
        # its own report at 256, so this only bounds multi-child accumulation per invocation.
        if staged_observations:
            seen = {(item.path, item.operation, item.old_path) for item in result.changes}
            extra: list[Change] = []
            for observed in staged_observations:
                key = (observed.path, observed.operation, observed.old_path)
                if key in seen:
                    continue
                seen.add(key)
                extra.append(Change(path=observed.path, operation=observed.operation, old_path=observed.old_path))
                if len(extra) >= MAX_MERGED_OBSERVATIONS:
                    break
            if extra:
                result = replace(result, changes=(*result.changes, *extra))
        if result.steering_context:
            execution.ephemeral_steering.append(result.steering_context)
        cursor = execution.cursor
        deferred = cursor.deferred_sequence is not None
        batch_sequence = cursor.deferred_sequence or cursor.batch_sequence
        request = self._event(execution, batch_sequence or 0, EventType.TOOL_DEFERRED) if deferred else None
        requester_only = request is not None and request.payload.get("result_delivery") == "requester"
        invocation_id: str | None = None
        intent: StoredEvent | None = None
        if cursor.intent_sequence is not None:
            intent = self._event(execution, cursor.intent_sequence, EventType.TOOL_INTENT)
            raw_invocation = intent.payload.get("invocation_id")
            invocation_id = raw_invocation if isinstance(raw_invocation, str) else None
        cursor.intent_sequence = None
        cursor.approval_sequence = None
        cursor.ask_sequence = None
        cursor.ask_request_id = None
        definition = self._snapshot.definition_of(call.tool_name)
        declaration = intent.payload if intent is not None else {}
        diagnostic = result.diagnostics if isinstance(result, ToolFailure) else None
        committed_result, result_bytes, projection_content_clipped = self._projection.prepare_report(result)
        model_event = next(
            (event for event in reversed(execution.events) if event.sequence == batch_sequence and event.event_type == EventType.MODEL_TURN), None
        )
        metadata: dict[str, object] = {
            "semantics": declaration.get("semantics", []) if intent is not None else [],
            "model": model_event.payload.get("model") if model_event is not None else None,
            "argument_bytes": len(canonical_json(dict(call.arguments)).encode("utf-8")),
            "result_bytes": result_bytes,
            "truncated": result.output.bounds.truncated or (projection_content_clipped and not requester_only),
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
                    "clipped": item.clipped or (projection_content_clipped and not requester_only),
                }
                for item in result.coverage
            ]
        metadata["changes"] = [
            {
                **item.as_payload(),
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
            "result": encode_tool_result(committed_result),
            "fact_metadata": metadata,
            "icon": declaration.get("icon") if intent is not None else ({"glyph": definition.icon.glyph} if definition and definition.icon else None),
            "logical_turn": execution.cursor.logical_turn,
        }
        if deferred:
            assert request is not None
            payload["deferred"] = True
            payload["request_id"] = request.payload["request_id"]
            payload["depth"] = request.payload["depth"]
            if isinstance(request.payload.get("background_action_id"), str):
                payload["background_action_id"] = request.payload["background_action_id"]
            if requester_only:
                payload["result_delivery"] = "requester"
            if interrupted and requester_only:
                payload["stop_cause"] = execution.cursor.stop_cause or "error"
                if budget_exhaustion is not None:
                    payload["budget_exhaustion"] = asdict(budget_exhaustion)
        if invocation_id is not None:
            payload["invocation_id"] = invocation_id
        report_event = EventDraft(
            EventType.TOOL_INTERRUPTED if interrupted else EventType.TOOL_COMPLETED,
            "core" if interrupted else "tool",
            "plugin" if deferred else "model",
            payload,
        )
        all_events = prefix_events + (report_event,) + staged_events

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
            observer_receipts=staged_receipts,
            pinned_run_id=pinned_run_id,
            enforce_pin=enforce_pin,
            ask_answer_guard=ask_answer_guard,
        )

    def _interrupt_batch(
        self,
        execution: _Execution,
        *,
        budget_exhaustion: BudgetExhaustion | None = None,
        pinned_run_id: str | None = None,
        enforce_pin: bool = False,
    ) -> None:
        first = execution.cursor.intent_sequence is not None
        bridge_request = execution.cursor.deferred_action_id is not None and execution.cursor.deferred_sequence is not None
        execution.cursor.abandon_batch = True
        while execution.cursor.batch_sequence is not None or execution.cursor.deferred_sequence is not None:
            _, call = self._pending(execution)
            message = (
                "execution was interrupted; external effects are unknown; not replayed"
                if first
                else "not executed: prior batch member was interrupted"
            )
            result = (
                ToolFailure(
                    call.tool_name,
                    message,
                    execution=UnconfirmedStop(cancellation_signalled=execution.cursor.stop_cause in ("user_stop", "owner_cancel", "shutdown")),
                )
                if bridge_request and first
                else ToolFailure(call.tool_name, message)
            )
            self._report(
                execution,
                call,
                result,
                interrupted=True,
                budget_exhaustion=budget_exhaustion if bridge_request and first else None,
                pinned_run_id=pinned_run_id,
                enforce_pin=enforce_pin,
            )
            first = False

    def _messages(
        self,
        execution: _Execution,
        *,
        allow_pending: bool = False,
        completed_only: bool = False,
        model: str | None = None,
        tool_schema_characters: int = 0,
    ) -> tuple[ProviderMessage, ...]:
        messages: list[ProviderMessage] = []
        pending_calls: tuple[ToolCall, ...] = ()
        pending_index = 0
        batch_start = 0
        batch_sequence: int | None = None
        shrinkable: list[_ShrinkableToolProjection] = []
        execution.projection_allocation = None
        execution.allocation_ineffective_tools = 0
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
                raw_images = event.payload.get("images")
                imgs = tuple(ImageAttachment.from_payload(item) for item in raw_images) if isinstance(raw_images, (list, tuple)) else ()
                messages.append(ProviderMessage("user", str(event.payload["content"]), images=imgs))
            elif event.event_type == EventType.CONTINUATION_STARTED:
                if pending_calls:
                    raise ValueError("continuation crossed an unfinished native batch")
                p_payload = event.payload.get("payload")
                p_map = p_payload if isinstance(p_payload, Mapping) else {}
                payload_str = json.dumps(json_wire_object(p_map), ensure_ascii=False)
                if event.payload.get("role") == "user":
                    # The owner declared a user-role message: its text (or payload) is verbatim.
                    text = p_map.get("text")
                    if text is None and "prompt" in p_map:
                        text = p_map.get("prompt")
                    raw_images = p_map.get("images")
                    imgs = tuple(ImageAttachment.from_payload(item) for item in raw_images) if isinstance(raw_images, (list, tuple)) else ()
                    messages.append(ProviderMessage("user", text if isinstance(text, str) else payload_str, images=imgs))
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
                    if request.payload.get("result_delivery") == "requester":
                        continue
                    result = decode_tool_result(_wire_object(event.payload["result"], "tool result"))
                    text, images, _clipped = project_tool_result(result, max_bytes=TOOL_OUTPUT_BYTES, artifact_reader=self.artifact_page)
                    label = (
                        f"[Untrusted deferred tool result: {result.tool_name}; "
                        f"invocation {event.payload.get('invocation_id', 'not-dispatched')}; "
                        f"request {request.payload['request_id']}; caller {request.payload['caller_invocation_id']}]\n"
                    )
                    messages.append(ProviderMessage("user", label + (text or ""), images=images))
                    shrinkable.append(_ShrinkableToolProjection(len(messages) - 1, result, label))
                    continue
                if not pending_calls or event.payload["batch_sequence"] != batch_sequence:
                    raise ValueError("reported tool result crossed an unfinished native batch")
                call = pending_calls[pending_index]
                if _wire_object(event.payload["call"], "tool call") != json_wire_object(encode_tool_call(call)):
                    raise ValueError("reported tool result does not match the active batch call")
                result = decode_tool_result(_wire_object(event.payload["result"], "tool result"))
                text, images, _clipped = project_tool_result(result, max_bytes=TOOL_OUTPUT_BYTES, artifact_reader=self.artifact_page)
                messages.append(
                    ProviderMessage(
                        "tool",
                        text,
                        tool_call_id=call.tool_call_id,
                        tool_name=call.tool_name,
                        is_error=isinstance(result, ToolFailure),
                        images=images,
                    )
                )
                shrinkable.append(_ShrinkableToolProjection(len(messages) - 1, result, None))
                pending_index += 1
                if pending_index == len(pending_calls):
                    pending_calls = ()
        if pending_calls and completed_only:
            del messages[batch_start:]
            shrinkable = [entry for entry in shrinkable if entry.index < len(messages)]
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
            with self.dispatch("context") as snapshot:
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
            if execution.ephemeral_steering:
                messages.append(ProviderMessage("system", "[Tool Guidance]\n" + "\n".join(execution.ephemeral_steering)))
                execution.ephemeral_steering.clear()

        if model is not None and not allow_pending:
            self._apply_projection_allocation(execution, messages, shrinkable, model=model, tool_schema_characters=tool_schema_characters)

        return tuple(messages)

    def _apply_projection_allocation(
        self,
        execution: _Execution,
        messages: list[ProviderMessage],
        shrinkable: list[_ShrinkableToolProjection],
        *,
        model: str,
        tool_schema_characters: int,
    ) -> None:
        """Shrink ceiling-rendered tool projections to the active model's remaining headroom.

        Phase one above rendered every tool result at the fixed ``TOOL_OUTPUT_BYTES``
        host ceiling; this phase measures that window, pairs the session's own
        committed token rate for the active model, and re-projects smaller only
        when every input is known. Any unknown keeps the ceiling bytes exactly,
        and a per-tool re-projection that cannot fit its identities keeps its own
        ceiling rendering: adaptive sizing never refuses a dispatch.
        """
        provider_name, separator, model_name = model.partition("/")
        limits = None
        rate: float | None = None
        if separator and model_name:
            with self.dispatch("context") as snapshot:
                limits = snapshot.plugin_runtime.provider_model_limits(provider_name, model_name)
            rate = measured_tokens_per_character(execution.events, provider_name=provider_name, model_name=model_name)
        shrinkable_characters = 0
        for entry in shrinkable:
            shrinkable_characters += len(messages[entry.index].content or "") - len(entry.label or "")
        fixed_characters = sum(len(message.content or "") + len(message.reasoning or "") for message in messages) - shrinkable_characters
        allocation = allocate_tool_text_budget(
            tool_count=len(shrinkable),
            fixed_characters=fixed_characters,
            tool_schema_characters=tool_schema_characters,
            images_present=any(message.images for message in messages),
            limits=limits,
            rate_tokens_per_char=rate,
            ceiling_bytes=TOOL_OUTPUT_BYTES,
        )
        execution.projection_allocation = allocation
        budget = allocation.per_tool_max_bytes
        if allocation.policy != "adaptive" or budget is None:
            return
        ineffective = 0
        for entry in shrinkable:
            try:
                text, images, _clipped = project_tool_result(entry.result, max_bytes=budget, artifact_reader=self.artifact_page)
            except ValueError, OSError, RuntimeError:
                # The ceiling rendering in hand is authoritative; a budget that
                # cannot fit this tool's identities keeps it instead of failing.
                ineffective += 1
                continue
            current = messages[entry.index]
            messages[entry.index] = replace(current, content=(entry.label or "") + text, images=images)
        execution.allocation_ineffective_tools = ineffective

    def _drive_pending_activations(self) -> None:
        """Finish committed command-tool work and staged continuations left across restart."""
        try:
            for session in self.store.sessions(workspace=str(self.workspace)):
                if self._closed:
                    return
                self._background_execution.reconcile_lost(session.session_id)
                if self.admits_fresh_work(session):
                    self.drain_deferred_requests(session.session_id)
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
            if not self.admits_fresh_work(session):
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
                        _automatic=True,
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
            self.ensure_open()
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
            abort = Cancellation()
            with self._aborts_lock:
                self._session_aborts[session_id] = (execution.run_id, abort)
            try:
                self._drive(execution)
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

    def _stop(
        self,
        execution: _Execution,
        message: str,
        *,
        stop_cause: StopCause = "user_stop",
        budget_exhaustion: BudgetExhaustion | None = None,
    ) -> CoreResult:
        """Stop a run without replaying an abandoned native batch."""
        cursor = execution.cursor
        cursor.stop_cause = stop_cause
        deliberate = stop_cause in ("user_stop", "owner_cancel")
        bridge_request: StoredEvent | None = None
        bridge_action_id: str | None = None
        bridge_request_id: str | None = None
        if cursor.deferred_sequence is not None and cursor.deferred_action_id is not None:
            bridge_request = self._event(execution, cursor.deferred_sequence, EventType.TOOL_DEFERRED)
            if bridge_request.payload.get("background_action_id") != cursor.deferred_action_id:
                raise StoreConflict("deferred request action identity changed before stop")
            bridge_action_id = cursor.deferred_action_id
            bridge_request_id = str(bridge_request.payload["request_id"])
        bridge_not_dispatched = bridge_request is not None and cursor.intent_sequence is None
        if (
            (deliberate or bridge_request is not None)
            and not bridge_not_dispatched
            and (cursor.batch_sequence is not None or cursor.deferred_sequence is not None)
        ):
            self._interrupt_batch(execution, budget_exhaustion=budget_exhaustion)
        checkpoint_kind = "terminal" if deliberate else "interrupted"
        self.store.cancel_pending_continuations(execution.session.session_id, "core")
        stop_payload: dict[str, object] = {
            "reason": message,
            "stop_cause": stop_cause,
            "logical_turn": cursor.logical_turn,
            **({"budget_exhaustion": asdict(budget_exhaustion)} if budget_exhaustion is not None else {}),
            **self._measured_run_elapsed_fields(execution),
        }
        if bridge_request_id is not None and bridge_action_id is not None:
            stop_payload["deferred_request_id"] = bridge_request_id
            stop_payload["background_action_id"] = bridge_action_id
        stop_event = EventDraft(EventType.RUN_INTERRUPTED, "core", "core", stop_payload)
        terminal_events: list[EventDraft] = []
        if bridge_not_dispatched:
            if budget_exhaustion is not None and stop_cause == "limit_reached":
                terminal_status: Literal["cancelled", "interrupted", "budget_exhausted"] = "budget_exhausted"
            elif stop_cause in ("user_stop", "owner_cancel"):
                terminal_status = "cancelled"
            else:
                terminal_status = "interrupted"
            terminal_payload: dict[str, object] = {
                "request_id": bridge_request_id,
                "background_action_id": bridge_action_id,
                "owner": bridge_request.payload.get("requester", ""),
                "revision_id": bridge_request.payload.get("expected_revision", ""),
                "tool_name": decode_tool_call(_wire_object(bridge_request.payload["call"], "deferred tool call")).tool_name,
                "status": terminal_status,
                "reason": message,
                "stop_cause": stop_cause,
            }
            if budget_exhaustion is not None:
                terminal_payload["budget_exhaustion"] = asdict(budget_exhaustion)
            terminal_events.append(EventDraft(EventType.TOOL_DEFERRED_CANCELLED, "core", "plugin", terminal_payload))
            cursor.deferred_sequence = None
            cursor.deferred_depth = 0
            cursor.deferred_action_id = None
            cursor.deferred_origin_tools = None
            cursor.deferred_origin_readonly = None
            cursor.deferred_authority_missing = False
            cursor.batch_sequence = None
            cursor.next_call = 0
            cursor.intent_sequence = None
            cursor.approval_sequence = None
        terminal_events.append(stop_event)
        if deliberate and cursor.continuation_id is not None:
            self._commit(
                execution,
                "interrupted",
                checkpoint_kind,
                *terminal_events,
                completed_continuation_id=cursor.continuation_id,
                completed_continuation_status="cancelled",
            )
        else:
            self._commit(execution, "interrupted", checkpoint_kind, *terminal_events)
        return self._result(
            execution,
            error=message,
            stop_cause=stop_cause,
            budget_exhaustion=budget_exhaustion,
        )

    def _fail(self, execution: _Execution, message: str) -> CoreResult:
        execution.cursor.stop_cause = "error"
        self._commit(
            execution,
            "failed",
            "terminal",
            EventDraft(
                EventType.RUN_FAILED,
                "core",
                "core",
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
        pending_ask: Mapping[str, object] | None = None,
        stop_cause: StopCause | None = None,
        budget_exhaustion: BudgetExhaustion | None = None,
    ) -> CoreResult:
        return CoreResult(
            execution.session.session_id,
            execution.run_id,
            self.revision_id,
            execution.session.status,
            output=output,
            error=error,
            pending_approval=pending_approval,
            pending_ask=pending_ask,
            stop_cause=stop_cause,
            budget_exhaustion=budget_exhaustion,
        )
