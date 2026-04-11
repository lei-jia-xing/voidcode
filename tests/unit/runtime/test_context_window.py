from __future__ import annotations

from voidcode.runtime.context_window import ContextWindowPolicy, prepare_single_agent_context
from voidcode.tools.contracts import ToolResult


def _tool_result(index: int) -> ToolResult:
    return ToolResult(
        tool_name="read_file",
        content=f"content-{index}",
        status="ok",
        data={"index": index},
    )


def test_prepare_single_agent_context_keeps_results_within_limit() -> None:
    context = prepare_single_agent_context(
        prompt="read sample.txt",
        tool_results=(_tool_result(1), _tool_result(2)),
        session_metadata={},
        policy=ContextWindowPolicy(max_tool_results=3),
    )

    assert context.prompt == "read sample.txt"
    assert tuple(result.data["index"] for result in context.tool_results) == (1, 2)
    assert context.compacted is False
    assert context.compaction_reason is None
    assert context.original_tool_result_count == 2
    assert context.retained_tool_result_count == 2


def test_prepare_single_agent_context_compacts_old_results_and_reports_metadata() -> None:
    context = prepare_single_agent_context(
        prompt="read sample.txt",
        tool_results=(_tool_result(1), _tool_result(2), _tool_result(3), _tool_result(4)),
        session_metadata={},
        policy=ContextWindowPolicy(max_tool_results=2),
    )

    assert tuple(result.data["index"] for result in context.tool_results) == (3, 4)
    assert context.compacted is True
    assert context.compaction_reason == "tool_result_window"
    assert context.original_tool_result_count == 4
    assert context.retained_tool_result_count == 2
