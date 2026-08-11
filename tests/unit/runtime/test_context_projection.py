from __future__ import annotations

from voidcode.runtime.context_projection import project_summary


def test_deterministic_projection_does_not_call_projector() -> None:
    called = False

    def projector(_: object) -> str:
        nonlocal called
        called = True
        return "model summary"

    assert project_summary(
        strategy="deterministic",
        facts={},
        deterministic_summary="facts",
        projector=projector,
    ) == ("facts", "deterministic", None)
    assert called is False


def test_model_assisted_projection_falls_back_without_projector() -> None:
    assert project_summary(
        strategy="model_assisted",
        facts={"objective": "fix"},
        deterministic_summary="facts",
    ) == ("facts", "fallback", "model_assisted_projector_unavailable")


def test_model_assisted_projection_falls_back_on_failure() -> None:
    def projector(_: object) -> str:
        raise RuntimeError("provider unavailable")

    summary, strategy, reason = project_summary(
        strategy="model_assisted",
        facts={},
        deterministic_summary="facts",
        projector=projector,
    )
    assert summary == "facts"
    assert strategy == "fallback"
    assert reason == "model_assisted_projector_failed:RuntimeError"
