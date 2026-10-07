from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Final, Literal, Protocol, TypedDict, TypeIs, cast, runtime_checkable

from ..provider.reasoning_effort import normalize_reasoning_effort
from ..tools.contracts import TerminalYield
from . import mode as runtime_mode
from .background.execution import (
    SubagentExecutionContract,
)
from .background.models import (
    BackgroundTaskObservability,
    BackgroundTaskStatus,
    SchemaValidation,
)
from .background.routing import (
    ResolvedSubagentRoute,
    SubagentRoutingIdentity,
    parse_subagent_routing_identity,
    resolve_subagent_route,
)
from .composition import CompositionRef
from .events import (
    DelegatedExecutionPayload,
    DelegatedLifecycleEventPayload,
    DelegatedLifecycleMessage,
    DelegatedRoutingPayload,
    EventEnvelope,
)
from .session import SessionState, SessionStatus


class RuntimeRequestError(ValueError):
    """Raised when a client-supplied runtime request is invalid."""


class UnknownSessionError(ValueError):
    """Raised when a referenced session does not exist in storage."""


class UnknownBackgroundTaskError(ValueError):
    """Raised when a referenced background task does not exist in storage."""


class RuntimeSessionForkBoundaryError(ValueError):
    """Raised when a fork boundary would split an interaction from its result.

    A tool call without its ``runtime.tool_completed``, or an approval/question
    request without its resolution, cannot be copied into a fork: the child
    would replay a call the log never closes. ``code`` is the stable
    machine-readable reason the transport carries on its error envelope.
    """

    code = "fork_boundary_splits_interaction"


class RuntimeSessionCheckoutBoundaryError(ValueError):
    """Raised when a checkout target would split an interaction from its result.

    Checking out to a sequence whose root→target path leaves a tool call
    without its ``runtime.tool_completed``, or an approval/question request
    without its resolution, would resume the session mid-interaction: the next
    run would see a call the log never closes. ``code`` is the stable
    machine-readable reason the transport carries on its error envelope.
    """

    code = "checkout_boundary_splits_interaction"


class SessionTreePathError(ValueError):
    """Raised when stored event ancestors cannot be walked back to a root.

    Each event's ``parent_sequence`` points at the event it follows, so an
    event's ancestors are always a finite chain back to the session's first
    entry. A ``parent_sequence`` naming a row that does not exist, or a cycle,
    means the persistence layer lost the path; the walk refuses instead of
    truncating silently or looping forever. ``code`` is the stable
    machine-readable reason a transport carries on its error envelope.
    """

    code = "session_tree_path_broken"


class SessionLineageCycleError(ValueError):
    """Raised when persisted fork provenance contains a cycle.

    Every session has at most one ``forked_from_session_id``, so provenance is
    a forest — unless the stored rows are corrupted (A forked from B forked
    from A). A cycle has no root, so it cannot be laid out as a tree; the
    forest projection refuses it instead of walking it forever. ``code`` is the
    stable machine-readable reason a transport carries on its error envelope.
    """

    code = "session_lineage_cycle"


class NoPendingQuestionError(ValueError):
    """Raised when a session has no pending question to answer.

    ``code`` is the stable machine-readable reason the HTTP transport carries on
    its error envelope, so a client can tell "nothing is waiting for an answer"
    apart from every other rejection without matching on message text.
    """

    code = "no_pending_question"


class NoPendingApprovalError(ValueError):
    """Raised when a session has no pending approval to resolve.

    ``code`` is the stable machine-readable reason the HTTP transport carries on
    its error envelope, so a client can tell "nothing is waiting for approval"
    apart from every other rejection without matching on message text.
    """

    code = "no_pending_approval"


class RuntimeCommandMetadata(TypedDict, total=False):
    name: str
    source: str
    arguments: list[str]
    raw_arguments: str
    original_prompt: str
    mode: runtime_mode.RuntimeMode


class RuntimeRequestMetadata(TypedDict, total=False):
    abort_requested: bool
    agent: dict[str, object]
    command: RuntimeCommandMetadata
    context_transform_refs: list[str]
    delegation: RuntimeSubagentRoutingMetadata
    mode: runtime_mode.RuntimeMode
    read_only: bool
    provider_stream: bool
    reasoning_effort: str
    show_thinking: bool
    skills: list[str]
    force_load_skills: list[str]
    keep_alive: bool


class InternalRuntimeRequestMetadata(RuntimeRequestMetadata, total=False):
    background_run: bool
    background_rate_limit_retry: bool
    background_task_id: str
    composition_ref: dict[str, object]
    keep_alive_turn: bool


type RuntimeRequestMetadataPayload = RuntimeRequestMetadata | InternalRuntimeRequestMetadata

type RoutingSchemaMode = Literal["permissive", "strict"]

type RoutingMode = Literal["sync", "background"]


def is_schema_mode(value: object) -> TypeIs[RoutingSchemaMode]:
    """Whether an untrusted ``schema_mode`` token names one of the delegation schema modes."""
    return value in ("permissive", "strict")


def is_routing_mode(value: object) -> TypeIs[RoutingMode]:
    """Whether an untrusted ``mode`` token names one of the delegation modes."""
    return value in ("sync", "background")


class RuntimeSubagentRoutingMetadata(TypedDict, total=False):
    mode: RoutingMode
    subagent_type: str
    description: str
    command: str
    depth: int
    remaining_spawn_budget: int
    selected_preset: str
    selected_execution_engine: str
    parallel_group_id: str
    parallel_group_size: int
    output_schema: dict[str, object]
    schema_mode: RoutingSchemaMode


class AcpStateMetadata(TypedDict, total=False):
    mode: str
    configured_enabled: bool
    status: str
    available: bool
    last_error: str | None
    last_request_type: str | None
    last_request_id: str | None
    last_event_type: str | None
    last_delegation: dict[str, object] | None  # AcpDelegationPayload（acp.py as_payload）


class TodosStateMetadata(TypedDict):
    version: Literal[2]
    revision: int
    phases: list[dict[str, object]]
    summary: dict[str, object]


class RuntimeStateMetadata(TypedDict, total=False):
    run_id: str
    # Nested sections are validated to be objects only
    # (``parse_runtime_state_metadata``); each section's owner parses its own
    # shape on read (context/window.py, todos.py, acp.py, ...).
    acp: dict[str, object]
    context_projection: dict[str, object]
    context_projection_summary: dict[str, object]
    todos: dict[str, object]
    pending_tool_intent: dict[str, object]
    turn_batch: dict[str, object]
    context_compacted: dict[str, object]
    context_transform_applied: dict[str, object]
    # Per-call reminder cycle counters (``reminders.py``); never reminder text.
    reminders: dict[str, object]


RUNTIME_STATE_METADATA_KEYS = frozenset(RuntimeStateMetadata.__annotations__)


class PlanStateMetadata(TypedDict, total=False):
    status: str
    approval_request_id: str
    blocked_tool: str
    last_error: str


PLAN_STATE_METADATA_KEYS = frozenset(PlanStateMetadata.__annotations__)


DELEGATION_METADATA_KEYS = frozenset(RuntimeSubagentRoutingMetadata.__annotations__)


class SkillSnapshotMetadata(TypedDict, total=False):
    snapshot_version: int
    source: str
    selected_skill_names: list[str]
    applied_skill_payloads: list[dict[str, str]]
    skill_prompt_context: str
    binding_snapshot: dict[str, object]
    snapshot_hash: str


SKILL_SNAPSHOT_METADATA_KEYS = frozenset(SkillSnapshotMetadata.__annotations__)


_STABLE_RUNTIME_REQUEST_METADATA_KEYS = frozenset(RuntimeRequestMetadata.__annotations__)
_INTERNAL_RUNTIME_REQUEST_METADATA_KEYS = frozenset(InternalRuntimeRequestMetadata.__annotations__) - _STABLE_RUNTIME_REQUEST_METADATA_KEYS


def _empty_runtime_request_metadata() -> RuntimeRequestMetadata:
    return {}


def _validate_optional_runtime_metadata_string(
    value: object,
    *,
    field_name: str,
) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeRequestError(f"request metadata '{field_name}' must be a non-empty string")
    return value


_PARALLEL_GROUP_SIZE_ERROR = "request metadata 'delegation.parallel_group_size' must be a positive integer"


def _validate_parallel_group_size(value: object) -> int:
    if isinstance(value, bool):
        raise RuntimeRequestError(_PARALLEL_GROUP_SIZE_ERROR)
    if isinstance(value, int):
        group_size = value
    elif isinstance(value, str):
        try:
            group_size = int(value)
        except ValueError:
            raise RuntimeRequestError(_PARALLEL_GROUP_SIZE_ERROR) from None
    else:
        raise RuntimeRequestError(_PARALLEL_GROUP_SIZE_ERROR)
    if group_size < 1:
        raise RuntimeRequestError(_PARALLEL_GROUP_SIZE_ERROR)
    return group_size


def _parse_runtime_mode(value: object) -> runtime_mode.RuntimeMode:
    try:
        return runtime_mode.parse_runtime_mode(value)
    except ValueError as exc:
        raise RuntimeRequestError("request metadata 'mode' must be 'normal' or 'plan'") from exc


def _parse_string_list(raw: list[object], *, field: str) -> list[str]:
    """Parse a JSON list of non-empty strings, labelling errors by ``field``."""
    parsed: list[str] = []
    for index, raw_name in enumerate(raw):
        if not isinstance(raw_name, str) or not raw_name:
            raise RuntimeRequestError(f"{field}[{index}] must be a non-empty string")
        parsed.append(raw_name)
    return parsed


def validate_runtime_command_metadata(metadata: object) -> RuntimeCommandMetadata:
    if not isinstance(metadata, dict):
        raise RuntimeRequestError("request metadata 'command' must be an object when provided")
    payload: Mapping[str, object] = metadata
    allowed_keys = frozenset(RuntimeCommandMetadata.__annotations__)
    non_string_keys = sorted(repr(key) for key in payload if not isinstance(key, str))
    if non_string_keys:
        joined = ", ".join(non_string_keys)
        raise RuntimeRequestError(f"request metadata 'command' keys must be strings; received invalid key(s): {joined}")
    unknown_keys = sorted(key for key in payload if key not in allowed_keys)
    if unknown_keys:
        joined = ", ".join(unknown_keys)
        raise RuntimeRequestError(f"unsupported request metadata 'command' field(s): {joined}")
    required_keys = {"name", "source", "arguments", "raw_arguments", "original_prompt"}
    missing_keys = sorted(required_keys - payload.keys())
    if missing_keys:
        joined = ", ".join(missing_keys)
        raise RuntimeRequestError(f"request metadata 'command' is missing required field(s): {joined}")

    name = _validate_optional_runtime_metadata_string(
        payload.get("name"),
        field_name="command.name",
    )
    source = _validate_optional_runtime_metadata_string(
        payload.get("source"),
        field_name="command.source",
    )
    raw_arguments = payload["raw_arguments"]
    if not isinstance(raw_arguments, str):
        raise RuntimeRequestError("request metadata 'command.raw_arguments' must be a string")
    original_prompt = _validate_optional_runtime_metadata_string(
        payload.get("original_prompt"),
        field_name="command.original_prompt",
    )
    raw_arguments_list = payload["arguments"]
    if not isinstance(raw_arguments_list, list):
        raise RuntimeRequestError("request metadata 'command.arguments' must be a list")
    arguments: list[str] = []
    for index, argument in enumerate(cast(list[object], raw_arguments_list)):
        if not isinstance(argument, str):
            raise RuntimeRequestError(f"request metadata 'command.arguments[{index}]' must be a string")
        arguments.append(argument)
    normalized: RuntimeCommandMetadata = {
        "name": name,
        "source": source,
        "arguments": arguments,
        "raw_arguments": raw_arguments,
        "original_prompt": original_prompt,
    }
    if "mode" in payload:
        normalized["mode"] = _parse_runtime_mode(payload["mode"])
    return normalized


def validate_runtime_subagent_routing_metadata(
    metadata: object,
) -> RuntimeSubagentRoutingMetadata:
    if not isinstance(metadata, dict):
        raise RuntimeRequestError("request metadata 'delegation' must be an object when provided")

    metadata_items: Mapping[str, object] = metadata
    non_string_keys = sorted(repr(key) for key in metadata_items if not isinstance(key, str))
    if non_string_keys:
        joined = ", ".join(non_string_keys)
        raise RuntimeRequestError(f"request metadata 'delegation' keys must be strings; received invalid key(s): {joined}")

    routing_metadata = dict(metadata_items)

    allowed_keys = DELEGATION_METADATA_KEYS
    unknown_keys = sorted(key for key in routing_metadata if key not in allowed_keys)
    if unknown_keys:
        joined = ", ".join(unknown_keys)
        raise RuntimeRequestError(f"unsupported request metadata 'delegation' field(s): {joined}")

    try:
        identity = parse_subagent_routing_identity(routing_metadata)
    except ValueError as exc:
        raise RuntimeRequestError(str(exc)) from exc

    normalized: RuntimeSubagentRoutingMetadata = {"mode": identity.mode}
    if identity.subagent_type is not None:
        normalized["subagent_type"] = identity.subagent_type
    if identity.description is not None:
        normalized["description"] = identity.description
    if identity.command is not None:
        normalized["command"] = identity.command
    if "depth" in routing_metadata:
        raw_depth = routing_metadata["depth"]
        if not isinstance(raw_depth, int) or isinstance(raw_depth, bool) or raw_depth < 1:
            raise RuntimeRequestError("request metadata 'delegation.depth' must be a positive integer")
        normalized["depth"] = raw_depth
    if "remaining_spawn_budget" in routing_metadata:
        raw_remaining_budget = routing_metadata["remaining_spawn_budget"]
        if not isinstance(raw_remaining_budget, int) or isinstance(raw_remaining_budget, bool) or raw_remaining_budget < 0:
            raise RuntimeRequestError("request metadata 'delegation.remaining_spawn_budget' must be a non-negative integer")
        normalized["remaining_spawn_budget"] = raw_remaining_budget
    if "selected_preset" in routing_metadata:
        normalized["selected_preset"] = _validate_optional_runtime_metadata_string(
            routing_metadata["selected_preset"],
            field_name="delegation.selected_preset",
        )
    if "selected_execution_engine" in routing_metadata:
        selected_execution_engine = _validate_optional_runtime_metadata_string(
            routing_metadata["selected_execution_engine"],
            field_name="delegation.selected_execution_engine",
        )
        if selected_execution_engine != "provider":
            raise RuntimeRequestError("request metadata 'delegation.selected_execution_engine' must be 'provider'")
        normalized["selected_execution_engine"] = selected_execution_engine
    if "parallel_group_id" in routing_metadata:
        normalized["parallel_group_id"] = _validate_optional_runtime_metadata_string(
            routing_metadata["parallel_group_id"],
            field_name="delegation.parallel_group_id",
        )
    if "parallel_group_size" in routing_metadata:
        normalized["parallel_group_size"] = _validate_parallel_group_size(
            routing_metadata["parallel_group_size"],
        )
    if "output_schema" in routing_metadata:
        raw_output_schema = routing_metadata["output_schema"]
        if not isinstance(raw_output_schema, dict):
            raise RuntimeRequestError("request metadata 'delegation.output_schema' must be an object")
        normalized["output_schema"] = dict(raw_output_schema)
    if "schema_mode" in routing_metadata:
        raw_schema_mode = routing_metadata["schema_mode"]
        if not is_schema_mode(raw_schema_mode):
            raise RuntimeRequestError("request metadata 'delegation.schema_mode' must be 'permissive' or 'strict'")
        normalized["schema_mode"] = raw_schema_mode
    return normalized


def runtime_subagent_routing_from_metadata(
    metadata: RuntimeRequestMetadataPayload | dict[str, object] | None,
) -> SubagentRoutingIdentity | None:
    if metadata is None:
        return None
    raw_routing = metadata.get("delegation")
    if raw_routing is None:
        return None
    normalized = validate_runtime_subagent_routing_metadata(raw_routing)
    mode = normalized.get("mode")
    if mode is None:
        raise RuntimeRequestError("request metadata 'delegation.mode' is required")
    subagent_type = normalized.get("subagent_type")
    if subagent_type is None:
        raise RuntimeRequestError("request metadata 'delegation.subagent_type' is required")
    return SubagentRoutingIdentity(
        mode=mode,
        subagent_type=subagent_type,
        description=normalized.get("description"),
        command=normalized.get("command"),
    )


def runtime_subagent_route_from_metadata(
    metadata: RuntimeRequestMetadataPayload | dict[str, object] | None,
    *,
    callable_subagent_presets: frozenset[str] | None = None,
) -> ResolvedSubagentRoute | None:
    routing = runtime_subagent_routing_from_metadata(metadata)
    if routing is None:
        return None
    try:
        resolved = resolve_subagent_route(
            routing,
            callable_subagent_presets=callable_subagent_presets,
        )
    except ValueError as exc:
        raise RuntimeRequestError(str(exc)) from exc
    if metadata is None:
        return resolved
    raw_routing = metadata.get("delegation")
    if not isinstance(raw_routing, dict):
        return resolved
    routing_metadata = cast(dict[str, object], raw_routing)
    persisted_selected_preset = routing_metadata.get("selected_preset")
    if persisted_selected_preset is None:
        return resolved
    if not isinstance(persisted_selected_preset, str):
        raise RuntimeRequestError("request metadata 'delegation.selected_preset' must be a non-empty string")
    if persisted_selected_preset != resolved.selected_preset:
        raise RuntimeRequestError("request metadata 'delegation.selected_preset' does not match the resolved child preset")
    persisted_execution_engine = routing_metadata.get("selected_execution_engine")
    if persisted_execution_engine is None:
        return resolved
    if not isinstance(persisted_execution_engine, str):
        raise RuntimeRequestError("request metadata 'delegation.selected_execution_engine' must be 'provider'")
    if persisted_execution_engine != resolved.execution_engine:
        raise RuntimeRequestError("request metadata 'delegation.selected_execution_engine' does not match the resolved child execution engine")
    return resolved


def validate_runtime_request_metadata(
    metadata: dict[str, object],
    *,
    allow_internal_fields: bool = False,
) -> RuntimeRequestMetadataPayload:
    metadata_items: Mapping[str, object] = metadata
    non_string_keys = sorted(repr(key) for key in metadata_items if not isinstance(key, str))
    if non_string_keys:
        joined = ", ".join(non_string_keys)
        raise RuntimeRequestError(f"request metadata keys must be strings; received invalid key(s): {joined}")

    allowed_keys = set(_STABLE_RUNTIME_REQUEST_METADATA_KEYS)
    if allow_internal_fields:
        allowed_keys.update(_INTERNAL_RUNTIME_REQUEST_METADATA_KEYS)
    unknown_keys = sorted(key for key in metadata if key not in allowed_keys)
    if unknown_keys:
        joined = ", ".join(unknown_keys)
        raise RuntimeRequestError(f"unsupported request metadata field(s): {joined}")

    normalized: InternalRuntimeRequestMetadata = {}

    if "abort_requested" in metadata:
        abort_requested = metadata["abort_requested"]
        if not isinstance(abort_requested, bool):
            raise RuntimeRequestError("request metadata 'abort_requested' must be a boolean")
        normalized["abort_requested"] = abort_requested

    if "agent" in metadata:
        agent = metadata["agent"]
        if not isinstance(agent, dict):
            raise RuntimeRequestError("request metadata 'agent' must be an object when provided")
        normalized["agent"] = dict(cast(dict[str, object], agent))

    if "command" in metadata:
        normalized["command"] = validate_runtime_command_metadata(metadata["command"])

    if "delegation" in metadata:
        normalized["delegation"] = validate_runtime_subagent_routing_metadata(metadata["delegation"])

    if "mode" in metadata:
        normalized["mode"] = _parse_runtime_mode(metadata["mode"])

    if "read_only" in metadata:
        read_only = metadata["read_only"]
        if not isinstance(read_only, bool):
            raise RuntimeRequestError("request metadata 'read_only' must be a boolean")
        normalized["read_only"] = read_only

    if "provider_stream" in metadata:
        provider_stream = metadata["provider_stream"]
        if not isinstance(provider_stream, bool):
            raise RuntimeRequestError("request metadata 'provider_stream' must be a boolean")
        normalized["provider_stream"] = provider_stream

    if "reasoning_effort" in metadata:
        normalized["reasoning_effort"] = normalize_reasoning_effort(metadata["reasoning_effort"])

    if "show_thinking" in metadata:
        show_thinking = metadata["show_thinking"]
        if not isinstance(show_thinking, bool):
            raise RuntimeRequestError("request metadata 'show_thinking' must be a boolean")
        normalized["show_thinking"] = show_thinking

    if "skills" in metadata:
        raw_skills = metadata["skills"]
        if not isinstance(raw_skills, list):
            raise RuntimeRequestError("request metadata 'skills' must be a list of skill names")
        normalized["skills"] = _parse_string_list(cast(list[object], raw_skills), field="request metadata 'skills'")

    if "context_transform_refs" in metadata:
        raw_transform_refs = metadata["context_transform_refs"]
        if not isinstance(raw_transform_refs, list):
            raise RuntimeRequestError("request metadata 'context_transform_refs' must be a list of transform provider names")
        normalized["context_transform_refs"] = _parse_string_list(
            cast(list[object], raw_transform_refs),
            field="request metadata 'context_transform_refs'",
        )

    if "force_load_skills" in metadata:
        raw_force_load = metadata["force_load_skills"]
        if not isinstance(raw_force_load, list):
            raise RuntimeRequestError("request metadata 'force_load_skills' must be a list of skill names")
        normalized["force_load_skills"] = _parse_string_list(
            cast(list[object], raw_force_load),
            field="request metadata 'force_load_skills'",
        )

    if "keep_alive" in metadata:
        keep_alive = metadata["keep_alive"]
        if not isinstance(keep_alive, bool):
            raise RuntimeRequestError("request metadata 'keep_alive' must be a boolean")
        normalized["keep_alive"] = keep_alive

    if allow_internal_fields and "background_run" in metadata:
        background_run = metadata["background_run"]
        if not isinstance(background_run, bool):
            raise RuntimeRequestError("request metadata 'background_run' must be a boolean")
        normalized["background_run"] = background_run

    if allow_internal_fields and "background_rate_limit_retry" in metadata:
        background_rate_limit_retry = metadata["background_rate_limit_retry"]
        if not isinstance(background_rate_limit_retry, bool):
            raise RuntimeRequestError("request metadata 'background_rate_limit_retry' must be a boolean")
        normalized["background_rate_limit_retry"] = background_rate_limit_retry

    if allow_internal_fields and "background_task_id" in metadata:
        background_task_id = metadata["background_task_id"]
        if not isinstance(background_task_id, str) or not background_task_id:
            raise RuntimeRequestError("request metadata 'background_task_id' must be a non-empty string")
        normalized["background_task_id"] = background_task_id

    if allow_internal_fields and "composition_ref" in metadata:
        try:
            composition_ref = CompositionRef.model_validate(metadata["composition_ref"])
        except Exception as error:
            raise RuntimeRequestError("request metadata 'composition_ref' must be a canonical CompositionRef") from error
        normalized["composition_ref"] = composition_ref.model_dump(mode="json")

    if allow_internal_fields and "keep_alive_turn" in metadata:
        keep_alive_turn = metadata["keep_alive_turn"]
        if not isinstance(keep_alive_turn, bool):
            raise RuntimeRequestError("request metadata 'keep_alive_turn' must be a boolean")
        normalized["keep_alive_turn"] = keep_alive_turn

    return normalized


@dataclass(frozen=True, slots=True)
class RuntimeRequest:
    prompt: str
    session_id: str | None = None
    parent_session_id: str | None = None
    # A validated payload, or a loose mapping forwarded from persisted session
    # metadata (readers only ever ``.get`` individual keys).
    metadata: RuntimeRequestMetadataPayload | dict[str, object] = field(default_factory=_empty_runtime_request_metadata)
    allocate_session_id: bool = False

    @property
    def subagent_routing(self) -> SubagentRoutingIdentity | None:
        return runtime_subagent_routing_from_metadata(self.metadata)

    @property
    def subagent_execution(self) -> SubagentExecutionContract:
        metadata = self.metadata
        delegated_task_id = None
        raw_background_task_id = metadata.get("background_task_id")
        if isinstance(raw_background_task_id, str):
            delegated_task_id = raw_background_task_id
        return SubagentExecutionContract.from_snapshot(
            parent_session_id=self.parent_session_id,
            requested_child_session_id=self.session_id,
            child_session_id=None,
            delegated_task_id=delegated_task_id,
            metadata=metadata,
        )


def validate_id(value: str, *, field_name: str = "session_id") -> str:
    if not value:
        raise RuntimeRequestError(f"{field_name} must be a non-empty string when provided")
    if "/" in value:
        raise RuntimeRequestError(f"{field_name} must not contain '/'")
    return value


#: The user-settable session title bound. Titles are a short label slot, not a
#: note field: every client renders this inline (sidebar row, TUI picker line),
#: so the runtime rejects anything longer instead of silently truncating.
SESSION_TITLE_MAX_LENGTH: Final[int] = 120


def validate_session_title(value: str) -> str:
    """Normalize and bound a user-settable session title.

    The single enforcement point for the title slot: whitespace runs (including
    newlines) collapse to one space so a title always renders on one line, the
    result must be non-empty, and it is capped at
    ``SESSION_TITLE_MAX_LENGTH`` characters. Absence of a title is ``NULL`` in
    the row, never an empty string, so the list/CLI/TUI prompt fallback stays
    the only way to render a title-less session.
    """
    title = " ".join(value.split())
    if not title:
        raise RuntimeRequestError("title must be a non-empty string")
    if len(title) > SESSION_TITLE_MAX_LENGTH:
        raise RuntimeRequestError(f"title must be at most {SESSION_TITLE_MAX_LENGTH} characters")
    return title


@dataclass(frozen=True, slots=True)
class RuntimeResponse:
    session: SessionState
    events: tuple[EventEnvelope, ...] = ()
    output: str | None = None


@dataclass(frozen=True, slots=True)
class SessionEventBatch:
    """Bounded incremental slice of one session transcript for follow clients.

    Produced by ``VoidCodeRuntime.session_events_after`` for transports that
    poll a session they already replayed: ``events`` holds only the persisted
    events after the client cursor (ascending ``sequence``, same runtime policy
    projection as a full replay) and ``status`` is the persisted row status at
    read time, so a follow loop can detect a terminal session without
    re-loading the whole log.
    """

    status: SessionStatus
    events: tuple[EventEnvelope, ...] = ()


type GitStatusState = Literal["git_ready", "not_git_repo", "git_error"]
type CapabilityState = Literal["running", "stopped", "failed", "unconfigured"]
type ReviewTreeNodeKind = Literal["file", "directory"]
type ReviewFileDiffState = Literal["changed", "clean", "not_git_repo"]


@dataclass(frozen=True, slots=True)
class WorkspaceSummary:
    path: str
    label: str
    available: bool
    current: bool = False
    last_opened_at: int | None = None


@dataclass(frozen=True, slots=True)
class WorkspaceRegistrySnapshot:
    current: WorkspaceSummary | None
    recent: tuple[WorkspaceSummary, ...] = ()
    candidates: tuple[WorkspaceSummary, ...] = ()


@dataclass(frozen=True, slots=True)
class ProviderSummary:
    name: str
    label: str
    configured: bool
    current: bool = False


@dataclass(frozen=True, slots=True)
class ProviderModelMetadata:
    context_window: int | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    supports_tools: bool | None = None
    supports_vision: bool | None = None
    supports_streaming: bool | None = None
    supports_reasoning: bool | None = None
    supports_json_mode: bool | None = None
    cost_per_input_token: float | None = None
    cost_per_output_token: float | None = None
    cost_per_cache_read_token: float | None = None
    cost_per_cache_write_token: float | None = None
    supports_reasoning_effort: bool | None = None
    default_reasoning_effort: str | None = None
    supported_effort_levels: tuple[str, ...] | None = None
    supports_reasoning_summary: bool | None = None
    supports_thinking_budget: bool | None = None
    supports_interleaved_reasoning: bool | None = None
    reasoning_visibility: str | None = None
    modalities_input: tuple[str, ...] | None = None
    modalities_output: tuple[str, ...] | None = None
    model_status: str | None = None
    tool_feedback_mode: Literal["standard", "synthetic_user_message"] | None = None
    api: str | None = None
    display_name: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderModelsResult:
    provider: str
    configured: bool
    models: tuple[str, ...] = ()
    model_metadata: dict[str, ProviderModelMetadata] = field(default_factory=dict)
    source: str | None = None
    last_refresh_status: str | None = None
    last_error: str | None = None
    discovery_mode: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderReadinessResult:
    provider: str | None
    model: str | None
    configured: bool
    ok: bool
    status: str
    guidance: str
    auth_present: bool | None = None
    streaming_configured: bool | None = None
    streaming_supported: bool | None = None
    context_window: int | None = None
    max_output_tokens: int | None = None
    fallback_chain: tuple[str, ...] = ()
    reasoning_controls: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProviderInspectResult:
    summary: ProviderSummary
    models: ProviderModelsResult
    validation: ProviderValidationResult
    current_model: str | None = None
    current_model_metadata: ProviderModelMetadata | None = None
    readiness: ProviderReadinessResult | None = None


@dataclass(frozen=True, slots=True)
class ProviderValidationResult:
    provider: str
    configured: bool
    ok: bool
    status: str
    message: str
    source: str | None = None
    last_error: str | None = None
    discovery_mode: str | None = None
    failure_kind: str | None = None
    guidance: str | None = None


@dataclass(frozen=True, slots=True)
class AgentSummary:
    id: str
    label: str
    description: str | None = None
    mode: str | None = None
    selectable: bool = True
    configured: bool = False
    execution_engine: str | None = None
    model: str | None = None
    model_label: str | None = None
    model_source: str | None = None
    provider: str | None = None
    fallback_chain: tuple[str, ...] = ()
    source_scope: str | None = None
    source_path: str | None = None


@dataclass(frozen=True, slots=True)
class SkillSummary:
    name: str
    description: str
    origin: str
    source_path: str | None = None


@dataclass(frozen=True, slots=True)
class CommandSummary:
    name: str
    description: str
    source: str
    enabled: bool = True
    hidden: bool = False
    agent: str | None = None
    model: str | None = None
    subtask: bool = False
    path: str | None = None


@dataclass(frozen=True, slots=True)
class GitStatusSnapshot:
    state: GitStatusState
    root: str | None = None
    branch: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class CapabilityStatusSnapshot:
    state: CapabilityState
    error: str | None = None
    details: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RuntimeBackgroundTaskStatusSnapshot:
    active_worker_slots: int
    queued_count: int
    running_count: int
    terminal_count: int
    default_concurrency: int
    provider_concurrency: dict[str, int] = field(default_factory=dict)
    model_concurrency: dict[str, int] = field(default_factory=dict)
    status_counts: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RuntimeStatusSnapshot:
    git: GitStatusSnapshot
    lsp: CapabilityStatusSnapshot
    mcp: CapabilityStatusSnapshot
    acp: CapabilityStatusSnapshot
    background_tasks: RuntimeBackgroundTaskStatusSnapshot


@dataclass(frozen=True, slots=True)
class ReviewChangedFile:
    path: str
    change_type: str
    old_path: str | None = None


@dataclass(frozen=True, slots=True)
class ReviewTreeNode:
    path: str
    name: str
    kind: ReviewTreeNodeKind
    changed: bool
    children: tuple[ReviewTreeNode, ...] = ()


@dataclass(frozen=True, slots=True)
class ReviewFileDiff:
    root: str
    path: str
    state: ReviewFileDiffState
    diff: str | None = None


@dataclass(frozen=True, slots=True)
class WorkspaceReviewSnapshot:
    root: str
    git: GitStatusSnapshot
    changed_files: tuple[ReviewChangedFile, ...] = ()
    tree: tuple[ReviewTreeNode, ...] = ()


@dataclass(frozen=True, slots=True)
class RuntimeSessionDebugEvent:
    sequence: int
    event_type: str
    source: str
    payload: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RuntimeSessionDebugPendingApproval:
    request_id: str
    tool_name: str
    target_summary: str
    reason: str
    policy_mode: str
    arguments: dict[str, object] = field(default_factory=dict)
    owner_session_id: str | None = None
    owner_parent_session_id: str | None = None
    delegated_task_id: str | None = None
    path_scope: str | None = None
    operation_class: str | None = None
    canonical_path: str | None = None
    matched_rule: str | None = None
    policy_surface: str | None = None


@dataclass(frozen=True, slots=True)
class RuntimeSessionDebugPendingQuestion:
    request_id: str
    tool_name: str
    question_count: int
    headers: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RuntimeSessionDebugToolSummary:
    tool_name: str
    status: str
    summary: str
    arguments: dict[str, object] = field(default_factory=dict)
    artifact: dict[str, object] = field(default_factory=dict)
    sequence: int | None = None


@dataclass(frozen=True, slots=True)
class RuntimeSessionDebugFailure:
    classification: str
    message: str


type RuntimeProviderContextDiagnosticPolicyMode = Literal["off", "warn", "block"]
type RuntimeProviderContextDiagnosticPolicyAction = Literal["none", "ignored", "warn", "block"]


@dataclass(frozen=True, slots=True)
class RuntimeProviderContextDiagnostic:
    severity: Literal["info", "warning", "error"]
    code: str
    message: str
    source: str | None = None
    segment_indices: tuple[int, ...] = ()
    suggested_fix: str | None = None
    details: dict[str, object] = field(default_factory=dict)
    policy_action: RuntimeProviderContextDiagnosticPolicyAction = "none"
    policy_blocking: bool = False


@dataclass(frozen=True, slots=True)
class RuntimeProviderContextPolicyDecision:
    mode: RuntimeProviderContextDiagnosticPolicyMode
    action: RuntimeProviderContextDiagnosticPolicyAction
    blocked: bool
    diagnostic_count: int = 0
    diagnostic_codes: tuple[str, ...] = ()
    blocking_diagnostic_codes: tuple[str, ...] = ()
    message: str = "Provider-context diagnostic policy did not find actionable diagnostics."


@dataclass(frozen=True, slots=True)
class RuntimeProviderContextSegmentSnapshot:
    index: int
    role: str
    source: str
    content: str | None = None
    content_truncated: bool = False
    tool_call_id: str | None = None
    tool_name: str | None = None
    tool_arguments: dict[str, object] = field(default_factory=dict)
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RuntimeProviderMessageSnapshot:
    index: int
    role: str
    source: str
    content: str | None = None
    content_truncated: bool = False
    tool_call_id: str | None = None
    tool_calls: tuple[dict[str, object], ...] = ()


@dataclass(frozen=True, slots=True)
class RuntimeProviderContextSnapshot:
    provider: str
    model: str
    execution_engine: str
    segment_count: int
    message_count: int
    context_window: dict[str, object] = field(default_factory=dict)
    segments: tuple[RuntimeProviderContextSegmentSnapshot, ...] = ()
    provider_messages: tuple[RuntimeProviderMessageSnapshot, ...] = ()
    diagnostics: tuple[RuntimeProviderContextDiagnostic, ...] = ()
    policy_decision: RuntimeProviderContextPolicyDecision | None = None


@dataclass(frozen=True, slots=True)
class RuntimeHookPresetSnapshot:
    refs: tuple[str, ...]
    kinds: tuple[str, ...]
    source: str
    count: int


@dataclass(frozen=True, slots=True)
class RuntimeSessionDebugSnapshot:
    session: SessionState
    prompt: str
    persisted_status: str
    current_status: str
    active: bool
    resumable: bool
    replayable: bool
    terminal: bool
    resume_checkpoint_kind: str | None = None
    pending_approval: RuntimeSessionDebugPendingApproval | None = None
    pending_question: RuntimeSessionDebugPendingQuestion | None = None
    last_event_sequence: int = 0
    last_relevant_event: RuntimeSessionDebugEvent | None = None
    last_failure_event: RuntimeSessionDebugEvent | None = None
    failure: RuntimeSessionDebugFailure | None = None
    last_tool: RuntimeSessionDebugToolSummary | None = None
    provider_context: RuntimeProviderContextSnapshot | None = None
    hook_presets: RuntimeHookPresetSnapshot | None = None
    suggested_operator_action: str = "inspect_session"
    operator_guidance: str = "Inspect the persisted session state."


@dataclass(frozen=True, slots=True)
class RuntimeSessionResult:
    session: SessionState
    prompt: str
    status: str
    summary: str
    output: str | None = None
    error: str | None = None
    transcript: tuple[EventEnvelope, ...] = ()
    last_event_sequence: int = 0
    #: User-settable display label; ``None`` means "derive the label from ``prompt``".
    title: str | None = None

    @property
    def delegated_events(self) -> tuple[DelegatedLifecycleEventPayload, ...]:
        return tuple(delegated for event in self.transcript if (delegated := event.delegated_lifecycle) is not None)


@dataclass(frozen=True, slots=True)
class BackgroundTaskResult:
    task_id: str
    parent_session_id: str | None
    child_session_id: str | None
    status: BackgroundTaskStatus
    requested_child_session_id: str | None = None
    delegated_prompt: str | None = None
    routing: SubagentRoutingIdentity | None = None
    approval_request_id: str | None = None
    question_request_id: str | None = None
    approval_blocked: bool = False
    handoff: TerminalYield | None = None
    summary_output: str | None = None
    error: str | None = None
    result_available: bool = False
    cancellation_cause: str | None = None
    duration_seconds: float | None = None
    tool_call_count: int = 0
    observability: BackgroundTaskObservability | None = None
    hook_reminder: dict[str, object] | None = None
    structured_output: dict[str, object] | None = None
    schema_validation: SchemaValidation | None = None
    # Bounded child progress projection; never a transcript replacement.
    progress: tuple[dict[str, object], ...] = ()

    @property
    def subagent_execution(self) -> SubagentExecutionContract:
        return SubagentExecutionContract.from_snapshot(
            parent_session_id=self.parent_session_id,
            requested_child_session_id=self.requested_child_session_id,
            child_session_id=self.child_session_id,
            delegated_task_id=self.task_id,
            approval_request_id=self.approval_request_id,
            question_request_id=self.question_request_id,
            metadata=(
                {
                    "delegation": {
                        "mode": self.routing.mode,
                        **({"subagent_type": self.routing.subagent_type} if self.routing.subagent_type is not None else {}),
                        **({"description": self.routing.description} if self.routing.description is not None else {}),
                        **({"command": self.routing.command} if self.routing.command is not None else {}),
                    }
                }
                if self.routing is not None
                else None
            ),
        )

    @property
    def delegated_routing(self) -> DelegatedRoutingPayload | None:
        if self.routing is None:
            return None
        return DelegatedRoutingPayload(
            mode=self.routing.mode,
            subagent_type=self.routing.subagent_type,
            description=self.routing.description,
            command=self.routing.command,
        )

    @property
    def delegated_execution(self) -> DelegatedExecutionPayload:
        metadata = self.subagent_execution
        selected_preset = None
        selected_execution_engine = None
        if self.routing is not None:
            route = runtime_subagent_route_from_metadata(
                {
                    "delegation": {
                        "mode": self.routing.mode,
                        **({"subagent_type": self.routing.subagent_type} if self.routing.subagent_type is not None else {}),
                        **({"description": self.routing.description} if self.routing.description is not None else {}),
                        **({"command": self.routing.command} if self.routing.command is not None else {}),
                    }
                }
            )
            if route is not None:
                selected_preset = route.selected_preset
                selected_execution_engine = route.execution_engine
        lifecycle_status: Literal[
            "queued",
            "running",
            "idle",
            "waiting_approval",
            "completed",
            "failed",
            "cancelled",
            "interrupted",
        ] = "waiting_approval" if self.approval_blocked else self.status
        return DelegatedExecutionPayload(
            parent_session_id=metadata.correlation.parent_session_id,
            requested_child_session_id=metadata.correlation.requested_child_session_id,
            child_session_id=metadata.correlation.child_session_id,
            delegated_task_id=metadata.correlation.delegated_task_id,
            approval_request_id=metadata.correlation.approval_request_id,
            question_request_id=metadata.correlation.question_request_id,
            routing=self.delegated_routing,
            selected_preset=selected_preset,
            selected_execution_engine=selected_execution_engine,
            lifecycle_status=lifecycle_status,
            approval_blocked=self.approval_blocked,
            result_available=self.result_available,
            cancellation_cause=self.cancellation_cause,
        )

    @property
    def delegated_message(self) -> DelegatedLifecycleMessage:
        return DelegatedLifecycleMessage(
            status=self.delegated_execution.lifecycle_status,
            summary_output=self.summary_output,
            error=self.error,
            approval_blocked=self.approval_blocked,
            result_available=self.result_available,
        )

    @property
    def delegated_event(self) -> DelegatedLifecycleEventPayload:
        delegated_execution = self.delegated_execution
        return DelegatedLifecycleEventPayload(
            session_id=self.child_session_id,
            parent_session_id=self.parent_session_id,
            delegation=delegated_execution,
            message=self.delegated_message,
        )


@dataclass(frozen=True, slots=True)
class BackgroundTaskGroupResult:
    """Bounded aggregate view of runtime-owned background task results."""

    parallel_group_id: str | None
    expected_task_count: int | None
    results: tuple[BackgroundTaskResult, ...]
    timed_out: bool = False

    @property
    def task_ids(self) -> tuple[str, ...]:
        return tuple(result.task_id for result in self.results)

    @property
    def terminal_task_count(self) -> int:
        return sum(result.status in {"completed", "failed", "cancelled", "interrupted"} for result in self.results)

    @property
    def complete(self) -> bool:
        return bool(self.results) and self.terminal_task_count == len(self.results)

    @property
    def counts(self) -> dict[str, int]:
        statuses = ("queued", "running", "idle", "completed", "failed", "cancelled", "interrupted")
        return {status: sum(result.status == status for result in self.results) for status in statuses}


type RuntimeStreamChunkKind = Literal["event", "output"]


@dataclass(frozen=True, slots=True)
class RuntimeStreamChunk:
    kind: RuntimeStreamChunkKind
    session: SessionState
    event: EventEnvelope | None = None
    output: str | None = None

    def __post_init__(self) -> None:
        if self.kind == "event" and self.event is None:
            raise ValueError("event chunks require an event")
        if self.kind == "output" and self.output is None:
            raise ValueError("output chunks require output content")

    def require_event(self) -> EventEnvelope:
        """The chunk's event, which ``__post_init__`` guarantees on an event chunk.

        Reading ``.event`` directly leaves every caller to assert or cast the
        invariant away; this is the one place that states it, and it fails the
        same way the invariant itself does.
        """
        event = self.event
        if event is None:
            raise ValueError("event chunks require an event")
        return event


@runtime_checkable
class RuntimeEntrypoint(Protocol):
    def run(self, request: RuntimeRequest) -> RuntimeResponse: ...


@runtime_checkable
class StreamingRuntimeEntrypoint(Protocol):
    def run_stream(self, request: RuntimeRequest) -> Iterator[RuntimeStreamChunk]: ...
