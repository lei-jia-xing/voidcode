"""Typed context contribution and fact observer contracts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from .provider import ProviderTokenUsage

ObservedFactKind = Literal["usage", "invocation_finished", "run_finished"]

#: Canonical run status reported to observers.
RunStatus = Literal["completed", "failed", "interrupted"]
#: Canonical neutral stop cause shared by core and SDK.
StopCause = Literal["completed", "error", "user_stop", "owner_cancel", "limit_reached", "shutdown"]


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
    effects: tuple[str, ...] = ()


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


type PluginFact = UsageFact | InvocationFinishedFact | RunFinishedFact


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


@dataclass(frozen=True, slots=True)
class WindowSegment:
    """One measured segment of a dispatched provider request.

    `characters` is the exact length of the string that was sent — never an estimate —
    and the digest pins the content the dispatch actually carried.
    """

    role: str
    characters: int
    sha256: str
    tool_calls: int = 0
    tool_name: str | None = None
    reasoning_characters: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.role, str) or not self.role:
            raise ValueError("window segment role must be a non-empty string")
        if type(self.characters) is not int or self.characters < 0:
            raise ValueError("window segment characters must be a non-negative integer")

    def as_payload(self) -> dict[str, object]:
        return {
            "role": self.role,
            "characters": self.characters,
            "sha256": self.sha256,
            "tool_calls": self.tool_calls,
            "tool_name": self.tool_name,
            "reasoning_characters": self.reasoning_characters,
        }

    @staticmethod
    def from_payload(payload: Mapping[str, object]) -> WindowSegment:
        return WindowSegment(
            role=str(payload["role"]),
            characters=_measured_int(payload.get("characters"), "characters"),
            sha256=str(payload["sha256"]),
            tool_calls=_measured_int(payload.get("tool_calls", 0), "tool_calls"),
            tool_name=None if payload.get("tool_name") is None else str(payload["tool_name"]),
            reasoning_characters=_measured_int(payload.get("reasoning_characters", 0), "reasoning_characters"),
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

    Lifecycle hooks are notifications with evidence: the core records what each one reported
    (or how it failed) as a durable event. They cannot inject context — context travels through
    the context contribution — and their return value is a note, never a decision.
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
