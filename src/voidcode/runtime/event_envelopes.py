from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from ..acp import AcpDelegatedExecution
from ..graph.contracts import GraphEvent
from .events import (
    EMITTED_EVENT_TYPES,
    RUNTIME_REASONING_PART,
    EventEnvelope,
    runtime_reasoning_part_from_provider_stream,
)

# Each surface (LSP/ACP/MCP) accepts exactly the ``EMITTED_EVENT_TYPES`` members
# of its own family. The sets are DERIVED by prefix from events.py's
# authoritative tuple (not hand-picked copies) so a newly added runtime event
# type of the same family flows through its envelope converter automatically
# instead of being silently dropped. The converters only ever see events
# emitted by their own surface manager, so prefix membership cannot admit a
# foreign event type in practice.
_LSP_RUNTIME_EVENT_TYPES = frozenset(event_type for event_type in EMITTED_EVENT_TYPES if event_type.startswith("runtime.lsp_server_"))
_ACP_RUNTIME_EVENT_TYPES = frozenset(event_type for event_type in EMITTED_EVENT_TYPES if event_type.startswith("runtime.acp_"))
_MCP_RUNTIME_EVENT_TYPES = frozenset(event_type for event_type in EMITTED_EVENT_TYPES if event_type.startswith("runtime.mcp_server_"))


def resequence_event(event: EventEnvelope, *, sequence: int) -> EventEnvelope:
    return EventEnvelope(
        session_id=event.session_id,
        sequence=sequence,
        event_type=event.event_type,
        source=event.source,
        payload=event.payload,
    )


def envelopes_for_lsp_events(
    *,
    session_id: str,
    start_sequence: int,
    lsp_events: tuple[object, ...],
) -> tuple[EventEnvelope, ...]:
    known_event_types = _LSP_RUNTIME_EVENT_TYPES
    envelopes: list[EventEnvelope] = []
    sequence = start_sequence
    for raw_event in lsp_events:
        if isinstance(raw_event, dict):
            raw_event_dict = cast(dict[str, object], raw_event)
            event_type = raw_event_dict.get("event_type")
            payload = raw_event_dict.get("payload")
        else:
            event_type = getattr(raw_event, "event_type", None)
            payload = getattr(raw_event, "payload", None)
        if event_type not in known_event_types or not isinstance(payload, dict):
            continue
        envelopes.append(
            EventEnvelope(
                session_id=session_id,
                sequence=sequence,
                event_type=cast(str, event_type),
                source="runtime",
                payload=cast(dict[str, object], payload),
            )
        )
        sequence += 1
    return tuple(envelopes)


def envelopes_for_acp_events(
    *,
    session_id: str,
    start_sequence: int,
    acp_events: tuple[object, ...],
) -> tuple[EventEnvelope, ...]:
    known_event_types = _ACP_RUNTIME_EVENT_TYPES
    envelopes: list[EventEnvelope] = []
    sequence = start_sequence
    for raw_event in acp_events:
        acp_session_id: str | None = None
        acp_parent_session_id: str | None = None
        acp_delegation: AcpDelegatedExecution | None = None
        if isinstance(raw_event, dict):
            raw_event_dict = cast(dict[str, object], raw_event)
            event_type = raw_event_dict.get("event_type")
            payload = raw_event_dict.get("payload")
        else:
            event_type = getattr(raw_event, "event_type", None)
            payload = getattr(raw_event, "payload", None)
            acp_session_id = cast(str | None, getattr(raw_event, "session_id", None))
            acp_parent_session_id = cast(str | None, getattr(raw_event, "parent_session_id", None))
            acp_delegation = cast(
                AcpDelegatedExecution | None,
                getattr(raw_event, "delegation", None),
            )
        if event_type not in known_event_types or not isinstance(payload, dict):
            continue
        envelopes.append(
            EventEnvelope(
                session_id=session_id,
                sequence=sequence,
                event_type=cast(str, event_type),
                source="runtime",
                payload={
                    **cast(dict[str, object], payload),
                    **(
                        {
                            "session_id": acp_session_id,
                            "parent_session_id": acp_parent_session_id,
                            "delegation": acp_delegation.as_payload(),
                        }
                        if acp_delegation is not None
                        else {
                            **({"session_id": acp_session_id} if acp_session_id is not None else {}),
                            **({"parent_session_id": acp_parent_session_id} if acp_parent_session_id is not None else {}),
                        }
                    ),
                },
            )
        )
        sequence += 1
    return tuple(envelopes)


def envelopes_for_mcp_events(
    *,
    session_id: str,
    start_sequence: int,
    mcp_events: tuple[object, ...],
) -> tuple[EventEnvelope, ...]:
    known_event_types = _MCP_RUNTIME_EVENT_TYPES
    envelopes: list[EventEnvelope] = []
    sequence = start_sequence
    for raw_event in mcp_events:
        if isinstance(raw_event, dict):
            raw_event_dict = cast(dict[str, object], raw_event)
            event_type = raw_event_dict.get("event_type")
            payload = raw_event_dict.get("payload")
        else:
            event_type = getattr(raw_event, "event_type", None)
            payload = getattr(raw_event, "payload", None)
        if event_type not in known_event_types or not isinstance(payload, dict):
            continue
        envelopes.append(
            EventEnvelope(
                session_id=session_id,
                sequence=sequence,
                event_type=cast(str, event_type),
                source="runtime",
                payload=cast(dict[str, object], payload),
            )
        )
        sequence += 1
    return tuple(envelopes)


@dataclass(slots=True)
class ReasoningCaptureState:
    part_count: int = 0
    text_char_count: int = 0
    stream_observed: bool = False
    reasoning_observed: bool = False
    output_diagnostic_emitted: bool = False


def renumber_events(
    events: tuple[GraphEvent, ...],
    *,
    session_id: str,
    start_sequence: int,
    reasoning_capture_state: ReasoningCaptureState | None = None,
) -> tuple[EventEnvelope, ...]:
    envelopes: list[EventEnvelope] = []
    capture_state = reasoning_capture_state or ReasoningCaptureState()
    for event in events:
        event_type = event.event_type
        source = event.source
        payload = event.payload
        reasoning_payload = None
        if event.event_type == "graph.provider_stream":
            capture_state.stream_observed = True
            reasoning_payload = runtime_reasoning_part_from_provider_stream(event.payload)
        if reasoning_payload is not None:
            capture_state.reasoning_observed = True
            text_char_count = reasoning_payload.get("text_char_count")
            event_type = RUNTIME_REASONING_PART
            source = "runtime"
            payload = reasoning_payload
            capture_state.part_count += 1
            if isinstance(text_char_count, int):
                capture_state.text_char_count += text_char_count
        envelopes.append(
            EventEnvelope(
                session_id=session_id,
                sequence=start_sequence + len(envelopes),
                event_type=event_type,
                source=source,
                payload=payload,
            )
        )
    return tuple(envelopes)


__all__ = [
    "ReasoningCaptureState",
    "envelopes_for_acp_events",
    "envelopes_for_lsp_events",
    "envelopes_for_mcp_events",
    "renumber_events",
    "resequence_event",
]
