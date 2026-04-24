from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal, cast

type BackgroundTaskStatus = Literal[
    "queued",
    "running",
    "completed",
    "failed",
    "cancelled",
]
type SubagentExecutionMode = Literal["sync", "background"]
type SubagentResultOwner = Literal["child_session"]
type SubagentSummaryOwner = Literal["background_task"]
type SubagentApprovalOwner = Literal["child_session"]
type SubagentCancellationOwner = Literal["delegated_task"]
type SubagentResumeOwner = Literal["child_session"]
type SubagentRestartReconciliationOwner = Literal["runtime"]
type SubagentExecutablePreset = Literal[
    "worker",
    "advisor",
    "explore",
    "researcher",
    "product",
]


def _normalized_optional_string(
    value: object,
    *,
    field_name: str,
) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


@dataclass(frozen=True, slots=True)
class SubagentRoutingIdentity:
    mode: SubagentExecutionMode
    category: str | None = None
    subagent_type: str | None = None
    description: str | None = None
    command: str | None = None

    def __post_init__(self) -> None:
        if bool(self.category) == bool(self.subagent_type):
            raise ValueError(
                "subagent routing must provide exactly one of category or subagent_type"
            )


@dataclass(frozen=True, slots=True)
class ResolvedSubagentRoute:
    requested: SubagentRoutingIdentity
    selected_preset: SubagentExecutablePreset
    execution_engine: Literal["provider"] = "provider"

    @property
    def selected_identity(self) -> dict[str, object]:
        return {
            "preset": self.selected_preset,
            "mode": "subagent",
            "requested_mode": self.requested.mode,
            **(
                {"requested_category": self.requested.category}
                if self.requested.category is not None
                else {}
            ),
            **(
                {"requested_subagent_type": self.requested.subagent_type}
                if self.requested.subagent_type is not None
                else {}
            ),
            **(
                {"description": self.requested.description}
                if self.requested.description is not None
                else {}
            ),
            **({"command": self.requested.command} if self.requested.command is not None else {}),
        }


_CATEGORY_TO_SUBAGENT_PRESET: dict[str, SubagentExecutablePreset] = {
    "quick": "worker",
    "deep": "worker",
    "ultrabrain": "advisor",
    "writing": "product",
    "visual-engineering": "product",
    "unspecified-high": "worker",
}

_CALLABLE_SUBAGENT_PRESETS = frozenset({"advisor", "explore", "product", "researcher", "worker"})
_BACKGROUND_TASK_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
_BACKGROUND_TASK_ALLOWED_TRANSITIONS: dict[
    BackgroundTaskStatus, frozenset[BackgroundTaskStatus]
] = {
    "queued": frozenset({"running", "completed", "failed", "cancelled"}),
    "running": frozenset({"completed", "failed", "cancelled"}),
    "completed": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}


def resolve_subagent_route(requested: SubagentRoutingIdentity) -> ResolvedSubagentRoute:
    if requested.subagent_type is not None:
        if requested.subagent_type == "leader":
            raise ValueError("subagent_type 'leader' is not a callable child preset")
        if requested.subagent_type not in _CALLABLE_SUBAGENT_PRESETS:
            raise ValueError(
                "unknown subagent_type "
                f"'{requested.subagent_type}'; valid child presets are: "
                "advisor, explore, product, researcher, worker"
            )
        return ResolvedSubagentRoute(
            requested=requested,
            selected_preset=cast(SubagentExecutablePreset, requested.subagent_type),
        )

    assert requested.category is not None
    selected_preset = _CATEGORY_TO_SUBAGENT_PRESET.get(requested.category)
    if selected_preset is None:
        valid_categories = ", ".join(sorted(_CATEGORY_TO_SUBAGENT_PRESET))
        raise ValueError(
            f"unsupported task category '{requested.category}'; valid categories are: "
            f"{valid_categories}"
        )
    return ResolvedSubagentRoute(requested=requested, selected_preset=selected_preset)


def is_background_task_terminal(status: BackgroundTaskStatus) -> bool:
    return status in _BACKGROUND_TASK_TERMINAL_STATUSES


def is_background_task_transition_allowed(
    *,
    current_status: BackgroundTaskStatus,
    next_status: BackgroundTaskStatus,
) -> bool:
    if current_status == next_status:
        return True
    return next_status in _BACKGROUND_TASK_ALLOWED_TRANSITIONS[current_status]


def subagent_routing_identity_from_metadata(
    metadata: Mapping[str, object] | None,
) -> SubagentRoutingIdentity | None:
    if metadata is None:
        return None
    raw_routing = metadata.get("delegation")
    if raw_routing is None:
        return None
    if not isinstance(raw_routing, Mapping):
        raise ValueError("delegation metadata must be an object")

    routing_items = cast(dict[object, object], raw_routing)
    non_string_keys = sorted(repr(key) for key in routing_items if not isinstance(key, str))
    if non_string_keys:
        joined = ", ".join(non_string_keys)
        raise ValueError(
            f"delegation metadata keys must be strings; received invalid key(s): {joined}"
        )

    routing_metadata: dict[str, object] = {
        key: value for key, value in routing_items.items() if isinstance(key, str)
    }

    mode = routing_metadata.get("mode")
    if mode not in ("sync", "background"):
        raise ValueError("delegation metadata mode must be 'sync' or 'background'")

    category = routing_metadata.get("category")
    subagent_type = routing_metadata.get("subagent_type")
    normalized_category = (
        _normalized_optional_string(category, field_name="delegation.category")
        if category is not None
        else None
    )
    normalized_subagent_type = (
        _normalized_optional_string(subagent_type, field_name="delegation.subagent_type")
        if subagent_type is not None
        else None
    )
    description = routing_metadata.get("description")
    command = routing_metadata.get("command")
    return SubagentRoutingIdentity(
        mode=mode,
        category=normalized_category,
        subagent_type=normalized_subagent_type,
        description=(
            _normalized_optional_string(description, field_name="delegation.description")
            if description is not None
            else None
        ),
        command=(
            _normalized_optional_string(command, field_name="delegation.command")
            if command is not None
            else None
        ),
    )


@dataclass(frozen=True, slots=True)
class SubagentExecutionCorrelation:
    parent_session_id: str | None = None
    requested_child_session_id: str | None = None
    child_session_id: str | None = None
    delegated_task_id: str | None = None
    approval_request_id: str | None = None
    question_request_id: str | None = None


@dataclass(frozen=True, slots=True)
class SubagentExecutionOwnership:
    result_owner: SubagentResultOwner = "child_session"
    summary_owner: SubagentSummaryOwner = "background_task"
    approval_owner: SubagentApprovalOwner = "child_session"
    cancellation_owner: SubagentCancellationOwner = "delegated_task"
    resume_owner: SubagentResumeOwner = "child_session"


@dataclass(frozen=True, slots=True)
class SubagentExecutionLifecycle:
    cancellation_semantics: str = (
        "queued tasks cancel immediately; running tasks record cancellation "
        "on the delegated task handle"
    )
    resume_semantics: str = (
        "resume targets the child session id; parent sessions receive "
        "backfilled delegated lifecycle events only"
    )
    restart_reconciliation_semantics: str = (
        "runtime reconciles delegated tasks from persisted child-session truth "
        "and backfills one waiting or terminal parent event"
    )
    restart_reconciliation_owner: SubagentRestartReconciliationOwner = "runtime"


@dataclass(frozen=True, slots=True)
class SubagentExecutionContract:
    correlation: SubagentExecutionCorrelation
    routing: SubagentRoutingIdentity | None = None
    ownership: SubagentExecutionOwnership = field(default_factory=SubagentExecutionOwnership)
    lifecycle: SubagentExecutionLifecycle = field(default_factory=SubagentExecutionLifecycle)

    @classmethod
    def from_snapshot(
        cls,
        *,
        parent_session_id: str | None,
        requested_child_session_id: str | None,
        child_session_id: str | None,
        delegated_task_id: str | None,
        metadata: Mapping[str, object] | None = None,
        approval_request_id: str | None = None,
        question_request_id: str | None = None,
    ) -> SubagentExecutionContract:
        return cls(
            correlation=SubagentExecutionCorrelation(
                parent_session_id=parent_session_id,
                requested_child_session_id=requested_child_session_id,
                child_session_id=child_session_id,
                delegated_task_id=delegated_task_id,
                approval_request_id=approval_request_id,
                question_request_id=question_request_id,
            ),
            routing=subagent_routing_identity_from_metadata(metadata),
        )


@dataclass(frozen=True, slots=True)
class BackgroundTaskRef:
    id: str


@dataclass(frozen=True, slots=True)
class BackgroundTaskRequestSnapshot:
    prompt: str
    session_id: str | None = None
    parent_session_id: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)
    allocate_session_id: bool = False

    @property
    def routing_identity(self) -> SubagentRoutingIdentity | None:
        return subagent_routing_identity_from_metadata(self.metadata)

    @property
    def subagent_execution(self) -> SubagentExecutionContract:
        return SubagentExecutionContract.from_snapshot(
            parent_session_id=self.parent_session_id,
            requested_child_session_id=self.session_id,
            child_session_id=None,
            delegated_task_id=None,
            metadata=self.metadata,
        )


@dataclass(frozen=True, slots=True)
class BackgroundTaskState:
    task: BackgroundTaskRef
    status: BackgroundTaskStatus = "queued"
    request: BackgroundTaskRequestSnapshot = field(
        default_factory=lambda: BackgroundTaskRequestSnapshot(prompt="")
    )
    session_id: str | None = None
    approval_request_id: str | None = None
    question_request_id: str | None = None
    cancellation_cause: str | None = None
    result_available: bool = False
    error: str | None = None
    created_at: int = 0
    updated_at: int = 0
    started_at: int | None = None
    finished_at: int | None = None
    cancel_requested_at: int | None = None

    @property
    def parent_session_id(self) -> str | None:
        return self.request.parent_session_id

    @property
    def child_session_id(self) -> str | None:
        return self.session_id

    @property
    def routing_identity(self) -> SubagentRoutingIdentity | None:
        return self.request.routing_identity

    @property
    def subagent_execution(self) -> SubagentExecutionContract:
        return SubagentExecutionContract.from_snapshot(
            parent_session_id=self.parent_session_id,
            requested_child_session_id=self.request.session_id,
            child_session_id=self.session_id,
            delegated_task_id=self.task.id,
            metadata=self.request.metadata,
            approval_request_id=self.approval_request_id,
            question_request_id=self.question_request_id,
        )


@dataclass(frozen=True, slots=True)
class StoredBackgroundTaskSummary:
    task: BackgroundTaskRef
    status: BackgroundTaskStatus
    prompt: str
    session_id: str | None
    error: str | None
    created_at: int
    updated_at: int


def validate_background_task_id(task_id: str) -> str:
    if not task_id:
        raise ValueError("task_id must be a non-empty string")
    if "/" in task_id:
        raise ValueError("task_id must not contain '/'")
    return task_id
