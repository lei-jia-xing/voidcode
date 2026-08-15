from __future__ import annotations

import pytest

from voidcode.runtime.edit_schema_policy import (
    AMBIGUOUS_MATCH_STRICT_THRESHOLD,
    EditSchema,
    select_edit_schema,
)
from voidcode.runtime.effectiveness import ToolEffectivenessEvent, ToolEffectivenessReport, project_tool_effectiveness
from voidcode.runtime.events import EventEnvelope


def _completed(
    *,
    session_id: str,
    sequence: int,
    tool: str,
    status: str,
    **payload: object,
) -> ToolEffectivenessEvent:
    return ToolEffectivenessEvent(
        session_id=session_id,
        event=EventEnvelope(
            session_id=session_id,
            sequence=sequence,
            event_type="runtime.tool_completed",
            source="tool",
            payload={"tool": tool, "status": status, **payload},
        ),
    )


def _report_with_edit_outcomes(
    *,
    model: str,
    ambiguous_matches: int,
    successful_edits: int,
    other_edit_errors: int = 0,
) -> ToolEffectivenessReport:
    events: list[ToolEffectivenessEvent] = []
    sequence = 1
    for _ in range(ambiguous_matches):
        events.append(
            _completed(
                session_id="s1",
                sequence=sequence,
                tool="edit",
                status="error",
                model=model,
                error="ambiguous",
                error_kind="ambiguous_match",
            )
        )
        sequence += 1
    for _ in range(other_edit_errors):
        events.append(
            _completed(
                session_id="s1",
                sequence=sequence,
                tool="edit",
                status="error",
                model=model,
                error="stale",
                error_kind="stale_edit",
            )
        )
        sequence += 1
    for _ in range(successful_edits):
        events.append(
            _completed(
                session_id="s1",
                sequence=sequence,
                tool="edit",
                status="ok",
                model=model,
                content="updated",
            )
        )
        sequence += 1
    return project_tool_effectiveness(
        workspace_id="/workspace",
        session_ids=("s1",),
        events=tuple(events),
    )


def test_select_edit_schema_returns_strict_for_high_ambiguous_match_model() -> None:
    report = _report_with_edit_outcomes(model="model-a", ambiguous_matches=3, successful_edits=1)

    assert select_edit_schema("model-a", report) is EditSchema.STRICT


def test_select_edit_schema_returns_flexible_for_low_ambiguous_match_model() -> None:
    report = _report_with_edit_outcomes(model="model-b", ambiguous_matches=1, successful_edits=4)

    assert select_edit_schema("model-b", report) is EditSchema.FLEXIBLE


def test_select_edit_schema_threshold_is_inclusive() -> None:
    report = _report_with_edit_outcomes(model="model-c", ambiguous_matches=2, successful_edits=2)

    assert AMBIGUOUS_MATCH_STRICT_THRESHOLD == 0.5
    assert report.edit_stats_for_model("model-c") is not None
    assert report.edit_stats_for_model("model-c").edit_ambiguous_match_rate == 0.5
    assert select_edit_schema("model-c", report) is EditSchema.STRICT


def test_select_edit_schema_returns_flexible_for_unknown_model() -> None:
    report = _report_with_edit_outcomes(model="model-a", ambiguous_matches=3, successful_edits=1)

    assert select_edit_schema("model-never-seen", report) is EditSchema.FLEXIBLE


def test_select_edit_schema_returns_flexible_without_effectiveness_data() -> None:
    assert select_edit_schema("model-a", None) is EditSchema.FLEXIBLE
    assert select_edit_schema(None, None) is EditSchema.FLEXIBLE
    assert select_edit_schema(None, _report_with_edit_outcomes(model="model-a", ambiguous_matches=3, successful_edits=1)) is EditSchema.FLEXIBLE


def test_edit_schema_profiles_are_string_valued() -> None:
    assert EditSchema.FLEXIBLE.value == "flexible"
    assert EditSchema.STRICT.value == "strict"
    assert EditSchema.FLEXIBLE == "flexible"
    assert EditSchema.STRICT == "strict"


@pytest.mark.parametrize(
    ("ambiguous_matches", "successful_edits", "expected"),
    [
        (5, 0, EditSchema.STRICT),
        (2, 1, EditSchema.STRICT),
        (1, 2, EditSchema.FLEXIBLE),
        (0, 10, EditSchema.FLEXIBLE),
        (0, 0, EditSchema.FLEXIBLE),
    ],
)
def test_select_edit_schema_rate_boundaries(
    ambiguous_matches: int,
    successful_edits: int,
    expected: EditSchema,
) -> None:
    report = _report_with_edit_outcomes(
        model="model-x",
        ambiguous_matches=ambiguous_matches,
        successful_edits=successful_edits,
    )

    assert select_edit_schema("model-x", report) is expected
