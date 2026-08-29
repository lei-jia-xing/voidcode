"""Single authoritative derivation of a child session's terminal outcome."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from .contracts import RuntimeResponse
from .events import GRAPH_RESPONSE_READY, RUNTIME_TOOL_COMPLETED, EventEnvelope


@dataclass(frozen=True, slots=True)
class ChildCompletionEvidence:
    """Transcript evidence shared by child finalization and result projection."""

    handoff: dict[str, object] | None = None
    response_ready: bool = False

    @property
    def completed(self) -> bool:
        return self.handoff is not None and self.response_ready


class ChildCompletionProtocol(Protocol):
    """Minimal contract for deriving child evidence and terminal decisions."""

    def inspect(self, events: Sequence[EventEnvelope]) -> ChildCompletionEvidence: ...

    def terminal_decision(
        self,
        *,
        session_status: str,
        evidence: ChildCompletionEvidence,
    ) -> Literal["completed", "failed"] | None: ...


class _TranscriptChildCompletionProtocol:
    def inspect(self, events: Sequence[EventEnvelope]) -> ChildCompletionEvidence:
        handoff: dict[str, object] | None = None
        response_ready = False
        for event in events:
            if event.event_type == RUNTIME_TOOL_COMPLETED and event.payload.get("tool") == "submit_result" and event.payload.get("status") == "ok":
                raw_handoff = event.payload.get("handoff")
                if isinstance(raw_handoff, dict):
                    summary = raw_handoff.get("summary")
                    if isinstance(summary, str) and summary.strip():
                        handoff = dict(raw_handoff)
                        continue
            if handoff is not None and event.event_type == GRAPH_RESPONSE_READY:
                response_ready = True
        return ChildCompletionEvidence(handoff=handoff, response_ready=response_ready)

    def terminal_decision(
        self,
        *,
        session_status: str,
        evidence: ChildCompletionEvidence,
    ) -> Literal["completed", "failed"] | None:
        if session_status == "completed":
            return "completed"
        if session_status == "failed":
            return "failed"
        if session_status == "interrupted" and evidence.completed:
            return "completed"
        # ``running`` (permission-denied tail) maps to ``failed`` exactly like
        # the legacy derivation.
        if session_status == "running":
            return "failed"
        return None


child_completion_protocol: ChildCompletionProtocol = _TranscriptChildCompletionProtocol()


def child_completion_evidence(events: Sequence[EventEnvelope]) -> ChildCompletionEvidence:
    return child_completion_protocol.inspect(events)


def child_transcript_proves_completed(events: Sequence[EventEnvelope]) -> bool:
    """Return whether a transcript proves a completed run."""
    return child_completion_evidence(events).completed


def child_terminal_outcome(session_response: RuntimeResponse) -> Literal["completed", "failed"] | None:
    """Derive the child's terminal outcome from its session row + transcript."""
    return child_completion_protocol.terminal_decision(
        session_status=session_response.session.status,
        evidence=child_completion_evidence(session_response.events),
    )


__all__ = [
    "ChildCompletionEvidence",
    "ChildCompletionProtocol",
    "child_completion_evidence",
    "child_completion_protocol",
    "child_terminal_outcome",
    "child_transcript_proves_completed",
]
