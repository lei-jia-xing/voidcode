"""The cost the usage record already carries reaches the effectiveness projection."""

from __future__ import annotations

from voidcode.runtime.effectiveness import ToolEffectivenessEvent, project_tool_effectiveness
from voidcode.runtime.events import EventEnvelope


def _metadata(cost: object) -> dict[str, object]:
    return {
        "provider_usage": {
            "latest": {"input_tokens": 100, "output_tokens": 10, "cost_usd": cost},
            "cumulative": {
                "input_tokens": 300,
                "output_tokens": 30,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "uncached_input_tokens": 300,
                "cost_usd": cost,
            },
        }
    }


def test_the_projection_reports_the_cumulative_cost() -> None:
    report = project_tool_effectiveness(
        workspace_id="w",
        session_ids=("s1", "s2"),
        session_metadata={"s1": _metadata(0.25), "s2": _metadata(0.5)},
        events=(),
    )

    assert report.cost_usd == 0.75
    payload = report.to_payload()
    assert payload["provider_usage"]["cost_usd"] == 0.75  # type: ignore[index]


def test_an_unpriced_usage_reports_no_cost_rather_than_zero() -> None:
    """A model with no shipped rates leaves ``cost_usd`` absent in the record, so the
    aggregate must stay ``None`` instead of claiming the sessions were free."""
    report = project_tool_effectiveness(
        workspace_id="w",
        session_ids=("s1",),
        session_metadata={"s1": {"provider_usage": {"cumulative": {"input_tokens": 300, "output_tokens": 30}}}},
        events=(),
    )

    assert report.cost_usd is None


def test_unknown_session_cost_is_not_summed_as_zero() -> None:
    report = project_tool_effectiveness(
        workspace_id="w",
        session_ids=("priced", "unknown"),
        session_metadata={"priced": _metadata(0.25), "unknown": _metadata(None)},
        events=(),
    )

    assert report.cost_usd is None
    assert report.to_payload()["provider_usage"]["cost_usd"] is None  # type: ignore[index]


def test_unknown_usage_count_is_not_reported_as_a_partial_total() -> None:
    report = project_tool_effectiveness(
        workspace_id="w",
        session_ids=("known", "unknown"),
        session_metadata={
            "known": _metadata(0.25),
            "unknown": {
                "provider_usage": {
                    "cumulative": {
                        "input_tokens": None,
                        "output_tokens": 10,
                        "cache_read_tokens": None,
                        "cache_write_tokens": 0,
                        "uncached_input_tokens": None,
                        "cost_usd": 0.25,
                    }
                }
            },
        },
        events=(),
    )

    assert report.input_tokens is None
    assert report.output_tokens == 40
    assert report.cache_hit_rate is None


def test_provider_turn_without_usage_metadata_makes_totals_unknown() -> None:
    event = ToolEffectivenessEvent(
        session_id="unreported",
        event=EventEnvelope(
            session_id="unreported",
            sequence=1,
            event_type="core.model_turn",
            source="core",
            payload={"mode": "provider"},
        ),
    )
    report = project_tool_effectiveness(
        workspace_id="w",
        session_ids=("priced", "unreported"),
        session_metadata={"priced": _metadata(0.25)},
        events=(event,),
    )

    assert report.input_tokens is None
    assert report.output_tokens is None
    assert report.cost_usd is None
