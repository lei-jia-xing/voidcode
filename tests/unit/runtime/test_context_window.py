from __future__ import annotations

from typing import Literal

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
    assert context.max_tool_result_count == 3
    assert context.continuity_state is None


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
    assert context.max_tool_result_count == 2
    assert context.continuity_state is not None
    assert context.continuity_state.dropped_tool_result_count == 2
    assert context.continuity_state.retained_tool_result_count == 2
    assert context.continuity_state.source == "tool_result_window"
    assert context.continuity_state.summary_text is not None
    assert "Compacted 2 earlier tool results:" in context.continuity_state.summary_text
    assert 'content_preview="content-1"' in context.continuity_state.summary_text
    assert 'content_preview="content-2"' in context.continuity_state.summary_text
    assert context.metadata_payload()["continuity_state"] == {
        "summary_text": context.continuity_state.summary_text,
        "dropped_tool_result_count": 2,
        "retained_tool_result_count": 2,
        "source": "tool_result_window",
        "version": 1,
    }


def test_prepare_single_agent_context_uses_explicit_continuity_preview_policy() -> None:
    context = prepare_single_agent_context(
        prompt="read sample.txt",
        tool_results=(_tool_result(1), _tool_result(2), _tool_result(3), _tool_result(4)),
        session_metadata={},
        policy=ContextWindowPolicy(
            max_tool_results=1,
            continuity_preview_items=1,
            continuity_preview_chars=5,
        ),
    )

    assert context.continuity_state is not None
    assert context.continuity_state.summary_text == (
        "Compacted 3 earlier tool results:\n"
        '1. read_file ok content_preview="conte..."\n'
        "... and 2 more"
    )


def _continuity_tool_result(
    status: Literal["ok", "error"], content: str | None = None
) -> ToolResult:
    return ToolResult(
        tool_name="fake_tool",
        status=status,
        content=content,
        data={},
        error=None,
    )


def test_prepare_single_agent_context_continuity_metadata_includes_version() -> None:
    context = prepare_single_agent_context(
        prompt="read sample.txt",
        tool_results=(
            _continuity_tool_result("ok", content="dropped"),
            _continuity_tool_result("ok", content="retained"),
        ),
        session_metadata={},
        policy=ContextWindowPolicy(max_tool_results=1),
    )

    assert context.continuity_state is not None
    payload = context.continuity_state.metadata_payload()
    assert payload.get("version") == 1
    assert payload.get("source") == "tool_result_window"
    assert payload.get("dropped_tool_result_count") == 1
    assert payload.get("retained_tool_result_count") == 1
