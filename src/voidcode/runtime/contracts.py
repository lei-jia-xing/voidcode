from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Literal, Protocol, TypedDict, cast, runtime_checkable

from .events import (
    DelegatedExecutionPayload,
    DelegatedLifecycleEventPayload,
    DelegatedLifecycleMessage,
    DelegatedRoutingPayload,
    EventEnvelope,
)
from .question import QuestionResponse
from .session import SessionRef, SessionState
from .task import (
    BackgroundTaskState,
    BackgroundTaskStatus,
    ResolvedSubagentRoute,
    StoredBackgroundTaskSummary,
    SubagentExecutionContract,
    SubagentRoutingIdentity,
    resolve_subagent_route,
)


class RuntimeRequestError(ValueError):
    """Raised when a client-supplied runtime request is invalid."""


class UnknownSessionError(ValueError):
    """Raised when a referenced session does not exist in storage."""


class NoPendingQuestionError(ValueError):
    """Raised when a session has no pending question to answer."""


class RuntimeCommandMetadata(TypedDict):
    name: str
    source: str
    arguments: list[str]
    raw_arguments: str
    original_prompt: str


class RuntimeRequestMetadata(TypedDict, total=False):
    abort_requested: bool
    agent: dict[str, object]
    command: RuntimeCommandMetadata
    delegation: RuntimeSubagentRoutingMetadata
    max_steps: int
    provider_stream: bool
    skills: list[str]


class InternalRuntimeRequestMetadata(RuntimeRequestMetadata, total=False):
    background_run: bool
    background_task_id: str


type RuntimeRequestMetadataPayload = RuntimeRequestMetadata | InternalRuntimeRequestMetadata

type RuntimeSubagentMode = Literal["sync", "background"]


class RuntimeSubagentRoutingMetadata(TypedDict, total=False):
    mode: RuntimeSubagentMode
    category: str
    subagent_type: str
    description: str
    command: str
    depth: int
    remaining_spawn_budget: int
    selected_preset: str
    selected_execution_engine: str


_STABLE_RUNTIME_REQUEST_METADATA_KEYS = frozenset(
    {"abort_requested", "agent", "command", "delegation", "max_steps", "provider_stream", "skills"}
)
_INTERNAL_RUNTIME_REQUEST_METADATA_KEYS = frozenset({"background_run", "background_task_id"})


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


def validate_runtime_command_metadata(metadata: object) -> RuntimeCommandMetadata:
    if not isinstance(metadata, dict):
        raise RuntimeRequestError("request metadata 'command' must be an object when provided")
    payload = cast(dict[object, object], metadata)
    allowed_keys = {"name", "source", "arguments", "raw_arguments", "original_prompt"}
    non_string_keys = sorted(repr(key) for key in payload if not isinstance(key, str))
    if non_string_keys:
        joined = ", ".join(non_string_keys)
        raise RuntimeRequestError(
            f"request metadata 'command' keys must be strings; received invalid key(s): {joined}"
        )
    command_payload = cast(dict[str, object], payload)
    unknown_keys = sorted(key for key in command_payload if key not in allowed_keys)
    if unknown_keys:
        joined = ", ".join(unknown_keys)
        raise RuntimeRequestError(f"unsupported request metadata 'command' field(s): {joined}")

    name = _validate_optional_runtime_metadata_string(
        command_payload.get("name"),
        field_name="command.name",
    )
    source = _validate_optional_runtime_metadata_string(
        command_payload.get("source"),
        field_name="command.source",
    )
    raw_arguments = command_payload.get("raw_arguments", "")
    if not isinstance(raw_arguments, str):
        raise RuntimeRequestError("request metadata 'command.raw_arguments' must be a string")
    original_prompt = _validate_optional_runtime_metadata_string(
        command_payload.get("original_prompt"),
        field_name="command.original_prompt",
    )
    raw_arguments_list = command_payload.get("arguments", [])
    if not isinstance(raw_arguments_list, list):
        raise RuntimeRequestError("request metadata 'command.arguments' must be a list")
    arguments: list[str] = []
    for index, argument in enumerate(cast(list[object], raw_arguments_list)):
        if not isinstance(argument, str):
            raise RuntimeRequestError(
                f"request metadata 'command.arguments[{index}]' must be a string"
            )
        arguments.append(argument)
    return {
        "name": name,
        "source": source,
        "arguments": arguments,
        "raw_arguments": raw_arguments,
        "original_prompt": original_prompt,
    }


def validate_runtime_subagent_routing_metadata(
    metadata: object,
) -> RuntimeSubagentRoutingMetadata:
    if not isinstance(metadata, dict):
        raise RuntimeRequestError("request metadata 'delegation' must be an object when provided")

    metadata_items = cast(dict[object, object], metadata)
    non_string_keys = sorted(repr(key) for key in metadata_items if not isinstance(key, str))
    if non_string_keys:
        joined = ", ".join(non_string_keys)
        raise RuntimeRequestError(
            f"request metadata 'delegation' keys must be strings; received invalid key(s): {joined}"
        )

    routing_metadata = {cast(str, key): value for key, value in metadata_items.items()}

    allowed_keys = {
        "mode",
        "category",
        "subagent_type",
        "description",
        "command",
        "depth",
        "remaining_spawn_budget",
        "selected_preset",
        "selected_execution_engine",
    }
    unknown_keys = sorted(key for key in routing_metadata if key not in allowed_keys)
    if unknown_keys:
        joined = ", ".join(unknown_keys)
        raise RuntimeRequestError(f"unsupported request metadata 'delegation' field(s): {joined}")

    raw_mode = routing_metadata.get("mode")
    if raw_mode not in ("sync", "background"):
        raise RuntimeRequestError(
            "request metadata 'delegation.mode' must be 'sync' or 'background'"
        )

    category = routing_metadata.get("category")
    subagent_type = routing_metadata.get("subagent_type")
    if bool(category) == bool(subagent_type):
        raise RuntimeRequestError(
            "request metadata 'delegation' must provide exactly one of "
            "'category' or 'subagent_type'"
        )

    normalized: RuntimeSubagentRoutingMetadata = {"mode": raw_mode}
    if category is not None:
        normalized["category"] = _validate_optional_runtime_metadata_string(
            category,
            field_name="delegation.category",
        )
    if subagent_type is not None:
        normalized["subagent_type"] = _validate_optional_runtime_metadata_string(
            subagent_type,
            field_name="delegation.subagent_type",
        )
    if "description" in routing_metadata:
        normalized["description"] = _validate_optional_runtime_metadata_string(
            routing_metadata["description"],
            field_name="delegation.description",
        )
    if "command" in routing_metadata:
        normalized["command"] = _validate_optional_runtime_metadata_string(
            routing_metadata["command"],
            field_name="delegation.command",
        )
    if "depth" in routing_metadata:
        raw_depth = routing_metadata["depth"]
        if not isinstance(raw_depth, int) or isinstance(raw_depth, bool) or raw_depth < 1:
            raise RuntimeRequestError(
                "request metadata 'delegation.depth' must be a positive integer"
            )
        normalized["depth"] = raw_depth
    if "remaining_spawn_budget" in routing_metadata:
        raw_remaining_budget = routing_metadata["remaining_spawn_budget"]
        if (
            not isinstance(raw_remaining_budget, int)
            or isinstance(raw_remaining_budget, bool)
            or raw_remaining_budget < 0
        ):
            raise RuntimeRequestError(
                "request metadata 'delegation.remaining_spawn_budget' must be a "
                "non-negative integer"
            )
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
            raise RuntimeRequestError(
                "request metadata 'delegation.selected_execution_engine' must be 'provider'"
            )
        normalized["selected_execution_engine"] = selected_execution_engine
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
    return SubagentRoutingIdentity(
        mode=mode,
        category=normalized.get("category"),
        subagent_type=normalized.get("subagent_type"),
        description=normalized.get("description"),
        command=normalized.get("command"),
    )


def runtime_subagent_route_from_metadata(
    metadata: RuntimeRequestMetadataPayload | dict[str, object] | None,
) -> ResolvedSubagentRoute | None:
    routing = runtime_subagent_routing_from_metadata(metadata)
    if routing is None:
        return None
    try:
        resolved = resolve_subagent_route(routing)
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
        raise RuntimeRequestError(
            "request metadata 'delegation.selected_preset' must be a non-empty string"
        )
    if persisted_selected_preset != resolved.selected_preset:
        raise RuntimeRequestError(
            "request metadata 'delegation.selected_preset' does not match the resolved child preset"
        )
    persisted_execution_engine = routing_metadata.get("selected_execution_engine")
    if persisted_execution_engine is None:
        return resolved
    if not isinstance(persisted_execution_engine, str):
        raise RuntimeRequestError(
            "request metadata 'delegation.selected_execution_engine' must be 'provider'"
        )
    if persisted_execution_engine != resolved.execution_engine:
        raise RuntimeRequestError(
            "request metadata 'delegation.selected_execution_engine' does not match the "
            "resolved child execution engine"
        )
    return resolved


def validate_runtime_request_metadata(
    metadata: dict[str, object],
    *,
    allow_internal_fields: bool = False,
) -> RuntimeRequestMetadataPayload:
    metadata_items = cast(dict[object, object], metadata)
    non_string_keys = sorted(repr(key) for key in metadata_items if not isinstance(key, str))
    if non_string_keys:
        joined = ", ".join(non_string_keys)
        raise RuntimeRequestError(
            f"request metadata keys must be strings; received invalid key(s): {joined}"
        )

    allowed_keys = set(_STABLE_RUNTIME_REQUEST_METADATA_KEYS)
    if allow_internal_fields:
        allowed_keys.update(_INTERNAL_RUNTIME_REQUEST_METADATA_KEYS)
    unknown_keys = sorted(key for key in metadata if key not in allowed_keys)
    if unknown_keys:
        joined = ", ".join(unknown_keys)
        raise RuntimeRequestError(f"unsupported request metadata field(s): {joined}")

    normalized: dict[str, object] = {}

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
        normalized["delegation"] = validate_runtime_subagent_routing_metadata(
            metadata["delegation"]
        )

    if "max_steps" in metadata:
        max_steps = metadata["max_steps"]
        if not isinstance(max_steps, int) or isinstance(max_steps, bool):
            raise RuntimeRequestError(
                "request metadata 'max_steps' must be an integer greater than or equal to 1"
            )
        if max_steps < 1:
            raise RuntimeRequestError("request metadata 'max_steps' must be at least 1")
        normalized["max_steps"] = max_steps

    if "provider_stream" in metadata:
        provider_stream = metadata["provider_stream"]
        if not isinstance(provider_stream, bool):
            raise RuntimeRequestError("request metadata 'provider_stream' must be a boolean")
        normalized["provider_stream"] = provider_stream

    if "skills" in metadata:
        raw_skills = metadata["skills"]
        if not isinstance(raw_skills, list):
            raise RuntimeRequestError("request metadata 'skills' must be a list of skill names")
        parsed_skills: list[str] = []
        for index, raw_name in enumerate(cast(list[object], raw_skills)):
            if not isinstance(raw_name, str) or not raw_name:
                raise RuntimeRequestError(
                    f"request metadata 'skills[{index}]' must be a non-empty string"
                )
            parsed_skills.append(raw_name)
        normalized["skills"] = parsed_skills

    if allow_internal_fields and "background_run" in metadata:
        background_run = metadata["background_run"]
        if not isinstance(background_run, bool):
            raise RuntimeRequestError("request metadata 'background_run' must be a boolean")
        normalized["background_run"] = background_run

    if allow_internal_fields and "background_task_id" in metadata:
        background_task_id = metadata["background_task_id"]
        if not isinstance(background_task_id, str) or not background_task_id:
            raise RuntimeRequestError(
                "request metadata 'background_task_id' must be a non-empty string"
            )
        normalized["background_task_id"] = background_task_id

    if allow_internal_fields:
        return cast(InternalRuntimeRequestMetadata, normalized)
    return cast(RuntimeRequestMetadata, normalized)


@dataclass(frozen=True, slots=True)
class RuntimeRequest:
    prompt: str
    session_id: str | None = None
    parent_session_id: str | None = None
    metadata: RuntimeRequestMetadataPayload = field(default_factory=_empty_runtime_request_metadata)
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


def validate_session_reference_id(value: str, *, field_name: str = "session_id") -> str:
    if not value:
        raise RuntimeRequestError(f"{field_name} must be a non-empty string when provided")
    if "/" in value:
        raise RuntimeRequestError(f"{field_name} must not contain '/'")
    return value


def validate_session_id(session_id: str) -> str:
    return validate_session_reference_id(session_id, field_name="session_id")


@dataclass(frozen=True, slots=True)
class RuntimeResponse:
    session: SessionState
    events: tuple[EventEnvelope, ...] = ()
    output: str | None = None


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
class ProviderModelsResult:
    provider: str
    configured: bool
    models: tuple[str, ...] = ()
    source: str | None = None
    last_refresh_status: str | None = None
    last_error: str | None = None
    discovery_mode: str | None = None


@dataclass(frozen=True, slots=True)
class AgentSummary:
    id: str
    label: str
    description: str | None = None


@dataclass(frozen=True, slots=True)
class GitStatusSnapshot:
    state: GitStatusState
    root: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class CapabilityStatusSnapshot:
    state: CapabilityState
    error: str | None = None
    details: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RuntimeStatusSnapshot:
    git: GitStatusSnapshot
    lsp: CapabilityStatusSnapshot
    mcp: CapabilityStatusSnapshot


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


type RuntimeNotificationKind = Literal[
    "completion",
    "failure",
    "cancellation",
    "approval_blocked",
    "question_blocked",
]
type RuntimeNotificationStatus = Literal["unread", "acknowledged"]


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

    @property
    def delegated_events(self) -> tuple[DelegatedLifecycleEventPayload, ...]:
        return tuple(
            delegated
            for event in self.transcript
            if (delegated := event.delegated_lifecycle) is not None
        )


@dataclass(frozen=True, slots=True)
class BackgroundTaskResult:
    task_id: str
    parent_session_id: str | None
    child_session_id: str | None
    status: BackgroundTaskStatus
    requested_child_session_id: str | None = None
    routing: SubagentRoutingIdentity | None = None
    approval_request_id: str | None = None
    question_request_id: str | None = None
    approval_blocked: bool = False
    summary_output: str | None = None
    error: str | None = None
    result_available: bool = False
    cancellation_cause: str | None = None

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
                        **(
                            {"category": self.routing.category}
                            if self.routing.category is not None
                            else {}
                        ),
                        **(
                            {"subagent_type": self.routing.subagent_type}
                            if self.routing.subagent_type is not None
                            else {}
                        ),
                        **(
                            {"description": self.routing.description}
                            if self.routing.description is not None
                            else {}
                        ),
                        **(
                            {"command": self.routing.command}
                            if self.routing.command is not None
                            else {}
                        ),
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
            category=self.routing.category,
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
                        **(
                            {"category": self.routing.category}
                            if self.routing.category is not None
                            else {}
                        ),
                        **(
                            {"subagent_type": self.routing.subagent_type}
                            if self.routing.subagent_type is not None
                            else {}
                        ),
                        **(
                            {"description": self.routing.description}
                            if self.routing.description is not None
                            else {}
                        ),
                        **(
                            {"command": self.routing.command}
                            if self.routing.command is not None
                            else {}
                        ),
                    }
                }
            )
            if route is not None:
                selected_preset = route.selected_preset
                selected_execution_engine = route.execution_engine
        lifecycle_status: Literal[
            "queued",
            "running",
            "waiting_approval",
            "completed",
            "failed",
            "cancelled",
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
class RuntimeNotification:
    id: str
    session: SessionRef
    kind: RuntimeNotificationKind
    status: RuntimeNotificationStatus
    summary: str
    event_sequence: int
    created_at: int
    acknowledged_at: int | None = None
    payload: dict[str, object] = field(default_factory=dict)


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


@runtime_checkable
class RuntimeEntrypoint(Protocol):
    def run(self, request: RuntimeRequest) -> RuntimeResponse: ...


@runtime_checkable
class StreamingRuntimeEntrypoint(Protocol):
    def run_stream(self, request: RuntimeRequest) -> Iterator[RuntimeStreamChunk]: ...


@runtime_checkable
class QuestionRuntimeEntrypoint(Protocol):
    def answer_question(
        self,
        session_id: str,
        *,
        question_request_id: str,
        responses: tuple[QuestionResponse, ...],
    ) -> RuntimeResponse: ...

    def answer_question_stream(
        self,
        session_id: str,
        *,
        question_request_id: str,
        responses: tuple[QuestionResponse, ...],
    ) -> Iterator[RuntimeStreamChunk]: ...


@runtime_checkable
class BackgroundTaskRuntimeEntrypoint(Protocol):
    def start_background_task(self, request: RuntimeRequest) -> BackgroundTaskState: ...

    def load_background_task(self, task_id: str) -> BackgroundTaskState: ...

    def load_background_task_result(self, task_id: str) -> BackgroundTaskResult: ...

    def list_background_tasks(self) -> tuple[StoredBackgroundTaskSummary, ...]: ...

    def list_background_tasks_by_parent_session(
        self, *, parent_session_id: str
    ) -> tuple[StoredBackgroundTaskSummary, ...]: ...

    def cancel_background_task(self, task_id: str) -> BackgroundTaskState: ...
