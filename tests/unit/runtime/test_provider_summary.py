from __future__ import annotations

from voidcode.runtime.provider_inspection import ProviderSummaryProjector


def test_provider_summary_projector_sorts_and_projects_runtime_facts() -> None:
    projector = ProviderSummaryProjector()

    summaries = projector.project_all(
        ("zeta", "alpha"),
        current_provider="zeta",
        label_for=lambda name: name.upper(),
        is_configured=lambda name: name == "alpha",
    )

    assert tuple(summary.name for summary in summaries) == ("alpha", "zeta")
    assert summaries[0].label == "ALPHA"
    assert summaries[0].configured is True
    assert summaries[0].current is False
    assert summaries[1].configured is False
    assert summaries[1].current is True


def test_provider_summary_projector_builds_unknown_provider_summary() -> None:
    summary = ProviderSummaryProjector.project_one(
        "custom",
        current_provider=None,
        label_for=lambda name: f"Provider {name}",
        is_configured=lambda _name: False,
    )

    assert summary.name == "custom"
    assert summary.label == "Provider custom"
    assert summary.configured is False
    assert summary.current is False
