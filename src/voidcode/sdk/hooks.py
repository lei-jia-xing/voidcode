"""Typed context contribution and fact observer contracts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, TypeGuard

from .json_values import own_json_object
from .provider import ProviderTokenUsage
from .services import BudgetExhaustion
from .tools import Change, ReadObservation, ToolEffect

ObservedFactKind = Literal[
    "usage", "invocation_finished", "run_finished", "run_requested", "approval_requested", "child_spawned", "context_projected", "wakeup_fired"
]

#: Canonical run status reported to observers.
RunStatus = Literal["completed", "failed", "interrupted"]
#: Canonical neutral stop cause shared by core and SDK.
StopCause = Literal["completed", "error", "user_stop", "owner_cancel", "limit_reached", "shutdown"]


def is_stop_cause(value: object) -> TypeGuard[StopCause]:
    """Check whether a value is a valid canonical StopCause."""
    return value in ("completed", "error", "user_stop", "owner_cancel", "limit_reached", "shutdown")


ContributionRole = Literal["system", "user"]


@dataclass(frozen=True, slots=True)
class ContextContribution:
    """Governed prompt context contribution produced by a plugin.

    The role is declarative: a `user` contribution is rendered verbatim as a user message
    (e.g. a reminder block), never re-wrapped into core system prose.
    """

    content: str
    role: ContributionRole = "system"

    def __post_init__(self) -> None:
        if not isinstance(self.content, str) or not self.content.strip():
            raise ValueError("context contribution content must be a non-empty string")
        if self.role not in ("system", "user"):
            raise ValueError("context contribution role must be 'system' or 'user'")


@dataclass(frozen=True, slots=True)
class UsageFact:
    """Truthful provider dispatch and token usage fact."""

    session_id: str
    run_id: str
    revision_id: str
    logical_turn: str
    event_sequence: int
    dispatch_sequence: int
    provider_name: str
    model_name: str
    usage: ProviderTokenUsage | None
    elapsed_seconds: float | None = None
    cost_usd: float | None = None
    #: The core's effective input limit for this dispatch, read from the durable
    #: ``PROVIDER_INTENT.projection_allocation.limit_tokens`` the request was assembled
    #: with. ``None`` means the allocation is absent or the limit is unknown; observers
    #: must not substitute a live catalog probe or a guessed window.
    input_limit_tokens: int | None = None
    context_window: int | None = None
    #: What produced this provider dispatch. A governed side query
    #: (``core.model_query``) has no separate dispatch intent event: its own
    #: committed event is the dispatch record, so ``dispatch_sequence`` equals
    #: ``event_sequence`` there. A normal turn points at its ``PROVIDER_INTENT``.
    source: Literal["turn", "model_query"] = "turn"
    #: The invoking plugin owner for a model query; ``None`` for a turn.
    owner: str | None = None


@dataclass(frozen=True, slots=True)
class InvocationFinishedFact:
    """Outcome of a completed tool or action invocation."""

    session_id: str
    run_id: str
    revision_id: str
    logical_turn: str
    event_sequence: int
    tool_name: str
    tool_call_id: str | None
    status: Literal["ok", "error", "cancelled"]
    error: str | None = None
    #: Owner plugin of the invoked tool, as declared at admission.
    owner: str = ""
    #: Declared effects the core governed this invocation under.
    effects: tuple[ToolEffect, ...] = ()
    #: Attribution and semantic observations frozen at completion. Consumers
    #: must not consult the current catalog to reinterpret these facts.
    model: str | None = None
    semantics: tuple[str, ...] = ()
    argument_bytes: int = 0
    result_bytes: int = 0
    truncated: bool = False
    partial: bool = False
    error_kind: str | None = None
    has_retry_guidance: bool = False
    reads: tuple[ReadObservation, ...] = ()
    changes: tuple[Change, ...] = ()
    resource_paths: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RunFinishedFact:
    """Terminal or paused state of a governed agent run."""

    session_id: str
    run_id: str
    revision_id: str
    logical_turn: str
    event_sequence: int
    status: RunStatus
    stop_cause: StopCause | None = None
    elapsed_seconds: float | None = None
    #: The typed finite cap that stopped this run, when the core recorded one.
    #: Absent means either no cap stopped it or the stop was untyped; this fact
    #: never invents a dimension.
    budget_exhaustion: BudgetExhaustion | None = None


@dataclass(frozen=True, slots=True)
class RunRequestedFact:
    session_id: str
    run_id: str
    revision_id: str
    logical_turn: str
    event_sequence: int
    resumed: bool


@dataclass(frozen=True, slots=True)
class ApprovalRequestedFact:
    session_id: str
    run_id: str
    revision_id: str
    logical_turn: str
    event_sequence: int
    request_id: str


@dataclass(frozen=True, slots=True)
class ChildSpawnedFact:
    session_id: str
    run_id: str
    revision_id: str
    logical_turn: str
    event_sequence: int
    child_session_id: str


@dataclass(frozen=True, slots=True)
class ContextProjectedFact:
    session_id: str
    run_id: str
    revision_id: str
    logical_turn: str
    event_sequence: int
    owner: str
    covered_bytes: int
    dropped_bytes: int


@dataclass(frozen=True, slots=True)
class WakeupFiredFact:
    """A durable wakeup claim started its recorded run, not a run completion."""

    session_id: str
    run_id: str
    revision_id: str
    logical_turn: str
    event_sequence: int
    owner: str
    wakeup_id: str
    role: Literal["system", "user"]
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        if not isinstance(self.owner, str) or not self.owner or not isinstance(self.wakeup_id, str) or not self.wakeup_id:
            raise ValueError("wakeup fact requires owner and wakeup identity")
        if self.role not in ("system", "user"):
            raise ValueError("wakeup fact role must be system or user")
        object.__setattr__(self, "payload", own_json_object(self.payload))


type PluginFact = (
    UsageFact
    | InvocationFinishedFact
    | RunFinishedFact
    | RunRequestedFact
    | ApprovalRequestedFact
    | ChildSpawnedFact
    | ContextProjectedFact
    | WakeupFiredFact
)


@dataclass(frozen=True, slots=True)
class FactPage:
    """One bounded source-event page; advance even when it contains no public facts.

    One source event yields at most one fact. Its resources are nested observations,
    so event_sequence is the stable identity and cannot skip sibling facts.
    """

    facts: tuple[PluginFact, ...]
    next_sequence: int
    has_more: bool


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    """Result of one governed tool invocation, as seen by a post-tool hook.

    Hooks are observers of an already-decided outcome: they can append one note to the
    result the model reads, never rewrite the tool's own verdict or its recorded effects.
    """

    tool_name: str
    tool_call_id: str | None
    status: Literal["ok", "error", "cancelled"]
    text: str
    arguments: Mapping[str, object]

    def __post_init__(self) -> None:
        if not isinstance(self.tool_name, str) or not self.tool_name:
            raise ValueError("hook outcome tool_name must be a non-empty string")
        if self.status not in ("ok", "error", "cancelled"):
            raise ValueError("hook outcome status must be 'ok', 'error' or 'cancelled'")
        if not isinstance(self.text, str):
            raise ValueError("hook outcome text must be a string")


def _measured_int(value: object, field: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"measured window field {field} must be a non-negative integer")
    return value


def _measured_optional_int(value: object, field: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise ValueError(f"measured window field {field} must be a non-negative integer or None")
    return value


@dataclass(frozen=True, slots=True)
class WindowSegment:
    """One measured segment of a dispatched provider request.

    `characters` is the exact length of the string that was sent — never an estimate —
    and the digest pins the content the dispatch actually carried. Image inventory is
    measured the same way: `image_bytes` is the UTF-8 bytes of the base64 text carried,
    never decoded pixels and never tokens. ``None`` means the dispatch predates image
    inventory and its image occupancy is unknown — never zero.
    """

    role: str
    characters: int
    sha256: str
    tool_calls: int = 0
    tool_name: str | None = None
    reasoning_characters: int = 0
    image_count: int | None = None
    image_bytes: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.role, str) or not self.role:
            raise ValueError("window segment role must be a non-empty string")
        if type(self.characters) is not int or self.characters < 0:
            raise ValueError("window segment characters must be a non-negative integer")
        for name in ("image_count", "image_bytes"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"window segment {name} must be a non-negative integer or None")

    def as_payload(self) -> dict[str, object]:
        return {
            "role": self.role,
            "characters": self.characters,
            "sha256": self.sha256,
            "tool_calls": self.tool_calls,
            "tool_name": self.tool_name,
            "reasoning_characters": self.reasoning_characters,
            "image_count": self.image_count,
            "image_bytes": self.image_bytes,
        }

    @staticmethod
    def from_payload(payload: Mapping[str, object]) -> WindowSegment:
        return WindowSegment(
            role=str(payload["role"]),
            characters=_measured_int(payload.get("characters"), "characters"),
            sha256=str(payload["sha256"]),
            tool_calls=_measured_int(payload.get("tool_calls", 0), "tool_calls"),
            tool_name=None if payload.get("tool_name") is None else str(payload.get("tool_name")),
            reasoning_characters=_measured_int(payload.get("reasoning_characters", 0), "reasoning_characters"),
            image_count=_measured_optional_int(payload.get("image_count"), "image_count"),
            image_bytes=_measured_optional_int(payload.get("image_bytes"), "image_bytes"),
        )


@dataclass(frozen=True, slots=True)
class RequestWindow:
    """Measured inventory of one dispatched provider request (core truth, no estimates).

    Token counts are deliberately absent: only the provider's own reported usage is a token
    fact, and it lives on the dispatch's usage record. Message segments are exact character
    counts and digests of the literal strings the core handed to the provider, so a client
    panel derives its view from measurements instead of a characters-per-token heuristic.
    `tool_schema_characters` measures the core-side neutral encoding of the tools this
    dispatch advertised (the provider adapter owns the wire encoding, which the core does
    not claim to measure).
    """

    message_characters: int
    tool_schema_characters: int
    segments: tuple[WindowSegment, ...] = ()
    tool_count: int = 0

    def __post_init__(self) -> None:
        for name, value in (
            ("message_characters", self.message_characters),
            ("tool_schema_characters", self.tool_schema_characters),
            ("tool_count", self.tool_count),
        ):
            if type(value) is not int or value < 0:
                raise ValueError(f"request window {name} must be a non-negative integer")

    @property
    def total_characters(self) -> int:
        return self.message_characters + self.tool_schema_characters

    @property
    def total_image_count(self) -> int | None:
        """Measured images carried, or ``None`` when any segment predates inventory."""
        total = 0
        for segment in self.segments:
            if segment.image_count is None:
                return None
            total += segment.image_count
        return total

    def as_payload(self) -> dict[str, object]:
        return {
            "message_characters": self.message_characters,
            "tool_schema_characters": self.tool_schema_characters,
            "tool_count": self.tool_count,
            "segments": [segment.as_payload() for segment in self.segments],
        }

    @staticmethod
    def from_payload(payload: Mapping[str, object]) -> RequestWindow:
        raw_segments = payload.get("segments")
        # Core-side payloads keep arrays as tuples (own_json_object), while a payload that
        # crossed the wire arrives as a list; both are the same decoded array.
        segments = tuple(
            WindowSegment.from_payload(segment)
            for segment in (raw_segments if isinstance(raw_segments, (tuple, list)) else ())
            if isinstance(segment, Mapping)
        )
        return RequestWindow(
            message_characters=_measured_int(payload.get("message_characters"), "message_characters"),
            tool_schema_characters=_measured_int(payload.get("tool_schema_characters"), "tool_schema_characters"),
            segments=segments,
            tool_count=_measured_int(payload.get("tool_count", 0), "tool_count"),
        )


@dataclass(frozen=True, slots=True)
class ToolIntent:
    """One tool call the core is about to execute, offered to a pre-tool gate.

    A gate answers "may this run?". It observes the call only: it cannot rewrite the
    arguments, the tool, or the verdict the core records.
    """

    tool_name: str
    tool_call_id: str | None
    arguments: Mapping[str, object]

    def __post_init__(self) -> None:
        if not isinstance(self.tool_name, str) or not self.tool_name:
            raise ValueError("tool intent tool_name must be a non-empty string")


@dataclass(frozen=True, slots=True)
class RunLifecycle:
    """One lifecycle point of a governed run, offered to a session_start/session_end hook.

    Lifecycle hooks are notifications with evidence: each invocation appends one durable
    ``core.hook_invoked`` event carrying the returned note, a bounded failure, or an
    explicit ``silent`` outcome, and one owner/phase runs at most once per run id. They
    cannot inject context — context travels through the context contribution — and their
    return value is a note, never a decision. A failure is recorded and never changes what
    the run decided.

    ``session_start`` fires once before the run's first provider dispatch. ``session_end``
    fires with the run's terminal commit for ``completed``, ``failed``, and
    ``interrupted`` (user stop, owner cancel, limit reached, shutdown); ``error`` carries
    the failure or stop reason when one exists. The event's ``status`` mirrors this field.
    """

    phase: Literal["start", "end"]
    session_id: str
    run_id: str
    logical_turn: str = ""
    status: Literal["completed", "failed", "interrupted", ""] = ""
    error: str | None = None

    def __post_init__(self) -> None:
        if self.phase not in ("start", "end"):
            raise ValueError("lifecycle phase must be 'start' or 'end'")
        if not self.session_id or not self.run_id:
            raise ValueError("lifecycle point needs a session and a run identity")
