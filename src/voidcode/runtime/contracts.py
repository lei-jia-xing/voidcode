from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Literal, Protocol, TypedDict, cast, runtime_checkable

from .events import EventEnvelope
from .question import QuestionResponse
from .session import SessionRef, SessionState
from .task import BackgroundTaskState, BackgroundTaskStatus, StoredBackgroundTaskSummary


class RuntimeRequestError(ValueError):
    """Raised when a client-supplied runtime request is invalid."""


class UnknownSessionError(ValueError):
    """Raised when a referenced session does not exist in storage."""


class NoPendingQuestionError(ValueError):
    """Raised when a session has no pending question to answer."""


class RuntimeRequestMetadata(TypedDict, total=False):
    abort_requested: bool
    agent: dict[str, object]
    max_steps: int
    provider_stream: bool
    skills: list[str]


class InternalRuntimeRequestMetadata(RuntimeRequestMetadata, total=False):
    background_run: bool
    background_task_id: str


type RuntimeRequestMetadataPayload = RuntimeRequestMetadata | InternalRuntimeRequestMetadata


_STABLE_RUNTIME_REQUEST_METADATA_KEYS = frozenset(
    {"abort_requested", "agent", "max_steps", "provider_stream", "skills"}
)
_INTERNAL_RUNTIME_REQUEST_METADATA_KEYS = frozenset({"background_run", "background_task_id"})


def _empty_runtime_request_metadata() -> RuntimeRequestMetadata:
    return {}


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


@dataclass(frozen=True, slots=True)
class BackgroundTaskResult:
    task_id: str
    parent_session_id: str | None
    child_session_id: str | None
    status: BackgroundTaskStatus
    approval_blocked: bool = False
    summary_output: str | None = None
    error: str | None = None
    result_available: bool = False


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
