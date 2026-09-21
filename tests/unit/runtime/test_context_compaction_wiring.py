"""Compaction wiring in prepare_provider_context: over-budget compacts, under-budget passes through."""

from __future__ import annotations

from collections.abc import Mapping

from voidcode.runtime.context.window import (
    ContextWindowPolicy,
    prepare_provider_context,
)
from voidcode.tools.contracts import ToolResult


def _result(content: str, tool_name: str = "read") -> ToolResult:
    return ToolResult(tool_name=tool_name, status="ok", content=content)


def test_over_budget_returns_compacted_projection_and_anchor() -> None:
    prompt = "Summarize the workspace changes."
    results = (_result("x" * 4000), _result("y" * 4000))
    window = prepare_provider_context(
        prompt=prompt,
        tool_results=results,
        session_metadata={},
        policy=ContextWindowPolicy(),
        context_window=100,
    )
    assert window.compacted is True
    assert window.compaction_reason is not None
    assert window.continuity_state is not None
    assert window.summary_anchor is not None
    assert window.summary_source is not None


def test_under_budget_passthrough_shape_preserved() -> None:
    prompt = "Summarize the workspace changes."
    results = (_result("small"),)
    window = prepare_provider_context(
        prompt=prompt,
        tool_results=results,
        session_metadata={},
        policy=ContextWindowPolicy(),
        context_window=100000,
    )
    assert window.compacted is False
    assert window.compaction_reason is None
    assert window.continuity_state is None
    assert window.summary_anchor is None
    assert window.summary_source is None
    assert window.prompt == prompt
    assert window.original_tool_result_count == 1
    assert window.retained_tool_result_count == 1
    assert window.summary_strategy == "deterministic"


def test_model_assisted_projector_failure_falls_back() -> None:
    prompt = "Summarize the workspace changes."
    results = (_result("x" * 4000),)

    def _boom(_facts: Mapping[str, object]) -> str:
        raise RuntimeError("projector down")

    window = prepare_provider_context(
        prompt=prompt,
        tool_results=results,
        session_metadata={},
        policy=ContextWindowPolicy(summary_strategy="model_assisted"),
        summary_projector=_boom,
        context_window=100,
    )
    assert window.compacted is True
    assert window.summary_strategy == "fallback"
    assert window.summary_fallback_reason is not None
    assert window.continuity_state is not None
    assert window.continuity_state.summary_text
