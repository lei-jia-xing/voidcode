from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, cast

from ..runtime.events import EventSource
from ..runtime.session import SessionStatus

type TuiStreamChunkKind = Literal["event", "output"]


@dataclass(frozen=True, slots=True)
class TuiSessionSummary:
    session_id: str
    status: SessionStatus
    turn: int
    prompt: str
    updated_at: int


@dataclass(frozen=True, slots=True)
class TuiSessionState:
    session_id: str
    status: SessionStatus
    turn: int
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TuiTimelineEvent:
    session_id: str
    sequence: int
    event_type: str
    source: EventSource
    payload: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TuiApprovalRequest:
    session_id: str
    sequence: int
    request_id: str
    tool: str
    arguments: dict[str, object] = field(default_factory=dict)
    target_summary: str = ""
    reason: str = ""
    decision: str = "ask"
    policy_mode: str = "ask"


@dataclass(frozen=True, slots=True)
class TuiSessionSnapshot:
    session: TuiSessionState
    timeline: tuple[TuiTimelineEvent, ...] = ()
    output: str | None = None

    @property
    def pending_approval(self) -> TuiApprovalRequest | None:
        if self.session.status != "waiting" or not self.timeline:
            return None
        return approval_request_from_event(self.timeline[-1])


@dataclass(frozen=True, slots=True)
class TuiStreamChunk:
    kind: TuiStreamChunkKind
    session: TuiSessionState
    event: TuiTimelineEvent | None = None
    output: str | None = None

    def __post_init__(self) -> None:
        if self.kind == "event" and self.event is None:
            raise ValueError("event chunks require an event")
        if self.kind == "output" and self.output is None:
            raise ValueError("output chunks require output content")

    @property
    def approval_request(self) -> TuiApprovalRequest | None:
        if self.event is None:
            return None
        return approval_request_from_event(self.event)


def approval_request_from_event(event: TuiTimelineEvent) -> TuiApprovalRequest | None:
    if event.event_type != "runtime.approval_requested":
        return None

    raw_arguments = event.payload.get("arguments", {})
    arguments = (
        dict(cast(dict[str, object], raw_arguments)) if isinstance(raw_arguments, dict) else {}
    )

    raw_policy = event.payload.get("policy", {})
    policy_mode = "ask"
    if isinstance(raw_policy, dict):
        raw_mode = cast(dict[str, object], raw_policy).get("mode")
        if isinstance(raw_mode, str) and raw_mode:
            policy_mode = raw_mode

    raw_decision = event.payload.get("decision", "ask")
    decision = raw_decision if isinstance(raw_decision, str) and raw_decision else "ask"

    return TuiApprovalRequest(
        session_id=event.session_id,
        sequence=event.sequence,
        request_id=str(event.payload.get("request_id", "")),
        tool=str(event.payload.get("tool", "")),
        arguments=arguments,
        target_summary=str(event.payload.get("target_summary", "")),
        reason=str(event.payload.get("reason", "")),
        decision=decision,
        policy_mode=policy_mode,
    )
