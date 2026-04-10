from __future__ import annotations

from voidcode.runtime.events import (
    EMITTED_EVENT_TYPES,
    GRAPH_LOOP_STEP,
    GRAPH_MODEL_TURN,
    PROTOTYPE_ADDITIVE_EVENT_TYPES,
    RUNTIME_MEMORY_REFRESHED,
    RUNTIME_SKILLS_APPLIED,
)


def test_emitted_event_types_include_stable_graph_protocol_events() -> None:
    assert GRAPH_LOOP_STEP in EMITTED_EVENT_TYPES
    assert GRAPH_MODEL_TURN in EMITTED_EVENT_TYPES
    assert RUNTIME_SKILLS_APPLIED in EMITTED_EVENT_TYPES


def test_future_additive_event_types_keep_memory_refresh_only() -> None:
    assert PROTOTYPE_ADDITIVE_EVENT_TYPES == (RUNTIME_MEMORY_REFRESHED,)
