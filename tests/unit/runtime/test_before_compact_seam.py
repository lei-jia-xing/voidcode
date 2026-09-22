"""before_compact seam: cancel skips compaction, custom_summary reaches projector input only."""

from __future__ import annotations

from collections.abc import Mapping

from voidcode.runtime.context.window import (
    BeforeCompactInput,
    ContextWindowPolicy,
    prepare_provider_context,
)
from voidcode.tools.contracts import ToolResult


def _result(content: str, tool_name: str = "read") -> ToolResult:
    return ToolResult(tool_name=tool_name, status="ok", content=content)


def _over_budget_results() -> tuple[ToolResult, ToolResult]:
    return (_result("x" * 4000), _result("y" * 4000))


def test_cancel_skips_compaction() -> None:
    window = prepare_provider_context(
        prompt="Summarize the workspace changes.",
        tool_results=_over_budget_results(),
        session_metadata={},
        policy=ContextWindowPolicy(),
        context_window=100,
        before_compact=BeforeCompactInput(cancel=True, reason="operator_hold"),
    )
    assert window.compacted is False
    assert window.compaction_reason == "operator_hold"
    assert window.continuity_state is None


def test_custom_summary_visible_in_summary_input_only() -> None:
    seen: dict[str, object] = {}

    def _projector(facts: Mapping[str, object]) -> str:
        seen.update(facts)
        return "model summary"

    window = prepare_provider_context(
        prompt="Summarize the workspace changes.",
        tool_results=_over_budget_results(),
        session_metadata={},
        policy=ContextWindowPolicy(summary_strategy="model_assisted"),
        summary_projector=_projector,
        context_window=100,
        before_compact=BeforeCompactInput(custom_summary="keep the deploy notes"),
    )
    assert window.compacted is True
    assert seen.get("custom_summary") == "keep the deploy notes"
    # Deterministic engine output is untouched: prompt passes through as-is.
    assert window.prompt == "Summarize the workspace changes."


def test_compaction_never_splits_tool_result_boundary() -> None:
    results = _over_budget_results()
    window = prepare_provider_context(
        prompt="Summarize the workspace changes.",
        tool_results=results,
        session_metadata={},
        policy=ContextWindowPolicy(),
        context_window=100,
    )
    assert window.compacted is True
    original_contents = [r.content or "" for r in results]
    assert len(window.tool_results) == len(results)
    for retained, original in zip(window.tool_results, original_contents, strict=True):
        assert (retained.content or "") == original
