"""Single authoritative derivation of a child session's terminal outcome.

Both the background-task supervisor (``background_tasks.py``) and the run loop
(the source of the transcript evidence this module reads back) need a shared
answer to "what counts as a completed child run". The session ROW can lag the
transcript: the run loop persists every event before the generator-driven
terminal seal, so an ``interrupted`` row whose transcript ends in a successful
``submit_result`` handoff plus ``graph.response_ready`` is actually a completed
run whose seal was skipped or downgraded. This module is the only place that
maps (row status + transcript evidence) to a terminal outcome; consumers never
re-derive it.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from .contracts import RuntimeResponse
from .events import GRAPH_RESPONSE_READY, RUNTIME_TOOL_COMPLETED, EventEnvelope


def child_transcript_proves_completed(events: Sequence[EventEnvelope]) -> bool:
    """Return whether a transcript proves a completed run.

    Evidence: a successful ``submit_result`` tool result carrying a non-empty
    ``handoff.summary`` (mirrors the ``_child_handoff`` evidence) followed by
    the ``graph.response_ready`` event the run loop emits immediately after,
    so a mid-flight or genuinely interrupted child is never misclassified as
    completed. Trusts only transcript evidence, never the bare row status.
    """
    saw_handoff = False
    for event in events:
        if event.event_type == RUNTIME_TOOL_COMPLETED and event.payload.get("tool") == "submit_result" and event.payload.get("status") == "ok":
            handoff = event.payload.get("handoff")
            if isinstance(handoff, dict) and isinstance(handoff.get("summary"), str) and handoff["summary"].strip():
                saw_handoff = True
                continue
        if saw_handoff and event.event_type == GRAPH_RESPONSE_READY:
            return True
    return False


def child_terminal_outcome(session_response: RuntimeResponse) -> Literal["completed", "failed"] | None:
    """Derive the child's terminal outcome from its session row + transcript.

    ``completed``/``failed`` rows map directly; an ``interrupted`` row whose
    transcript proves a ``submit_result`` handoff is a completed run whose seal
    was skipped or downgraded; ``running`` (permission-denied tail) maps to
    ``failed`` exactly like the legacy derivation. Genuinely resumable
    interrupted children (no handoff) yield ``None`` and are never sealed or
    terminalized.
    """
    status = session_response.session.status
    if status == "completed":
        return "completed"
    if status == "failed":
        return "failed"
    if status == "interrupted" and child_transcript_proves_completed(session_response.events):
        return "completed"
    # ``running`` (permission-denied tail) maps to ``failed`` exactly like
    # the legacy derivation.
    if status == "running":
        return "failed"
    return None


__all__ = [
    "child_terminal_outcome",
    "child_transcript_proves_completed",
]
