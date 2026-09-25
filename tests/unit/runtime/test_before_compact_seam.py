"""before_compact seam: cancel skips compaction, custom_summary reaches projector input only."""

from __future__ import annotations

from collections.abc import Mapping

from voidcode.runtime.config import RuntimeCompactionConfig
from voidcode.runtime.context.window import (
    BeforeCompactInput,
    ContextWindowPolicy,
    prepare_provider_context,
)
from voidcode.tools.contracts import ToolResult


def _result(content: str, tool_name: str = "read") -> ToolResult:
    return ToolResult(tool_name=tool_name, status="ok", content=content)


def _over_budget_results() -> tuple[ToolResult, ToolResult]:
    # Sized so the reclaim crosses the production savings floor (20_000 tokens).
    return (_result("x" * 60_000), _result("y" * 60_000))


def test_cancel_skips_compaction() -> None:
    window = prepare_provider_context(
        prompt="Summarize the workspace changes.",
        tool_results=_over_budget_results(),
        session_metadata={},
        policy=ContextWindowPolicy(default_tool_result_chars=None, compaction=RuntimeCompactionConfig(keep_recent_tool_tokens=0)),
        context_window=100,
        payload_bytes=0,
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
        policy=ContextWindowPolicy(
            default_tool_result_chars=None, summary_strategy="model_assisted", compaction=RuntimeCompactionConfig(keep_recent_tool_tokens=0)
        ),
        summary_projector=_projector,
        context_window=100,
        payload_bytes=0,
        before_compact=BeforeCompactInput(custom_summary="keep the deploy notes"),
    )
    assert window.compacted is True
    assert seen.get("custom_summary") == "keep the deploy notes"
    # Deterministic engine output is untouched: prompt passes through as-is.
    assert window.prompt == "Summarize the workspace changes."


def test_compaction_never_splits_tool_result_boundary() -> None:
    """Pruning replaces content; it never removes a result or its pairing."""
    results = _over_budget_results()
    window = prepare_provider_context(
        prompt="Summarize the workspace changes.",
        tool_results=results,
        session_metadata={},
        policy=ContextWindowPolicy(default_tool_result_chars=None, compaction=RuntimeCompactionConfig(keep_recent_tool_tokens=0)),
        context_window=100,
        payload_bytes=0,
    )
    assert window.compacted is True
    assert window.dropped_tool_result_count == len(results)
    assert len(window.tool_results) == len(results)
    for rendered, original in zip(window.tool_results, results, strict=True):
        assert rendered.result == original
        assert (rendered.content or "").startswith("[Runtime context pruning:")
        assert f"omitted_bytes={len(original.content or '')}" in (rendered.content or "")
