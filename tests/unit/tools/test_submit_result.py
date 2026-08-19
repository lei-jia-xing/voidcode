from pathlib import Path

import pytest

from voidcode.tools.contracts import ToolCall
from voidcode.tools.runtime_context import RuntimeToolInvocationContext, bind_runtime_tool_context
from voidcode.tools.submit_result import SubmitResultTool


def test_submit_result_returns_summary_and_arbitrary_data_handoff() -> None:
    tool = SubmitResultTool()
    with bind_runtime_tool_context(RuntimeToolInvocationContext(session_id="child", parent_session_id="parent")):
        result = tool.invoke(
            ToolCall(
                tool_name="submit_result",
                arguments={
                    "summary": "Inspected the runtime.",
                    "data": {"completed_work": ["Read service.py"], "verification": ["pytest passed"]},
                },
            ),
            workspace=Path("."),
        )

    assert result.status == "ok"
    assert result.data["handoff"] == {
        "summary": "Inspected the runtime.",
        "data": {"completed_work": ["Read service.py"], "verification": ["pytest passed"]},
    }


def test_submit_result_defaults_data_to_empty_object() -> None:
    tool = SubmitResultTool()
    with bind_runtime_tool_context(RuntimeToolInvocationContext(session_id="child", parent_session_id="parent")):
        result = tool.invoke(
            ToolCall(
                tool_name="submit_result",
                arguments={"summary": "Done."},
            ),
            workspace=Path("."),
        )

    assert result.status == "ok"
    assert result.data["handoff"] == {"summary": "Done.", "data": {}}


def test_submit_result_rejects_legacy_fixed_fields() -> None:
    """The five fixed fields are deleted, not deprecated: sending them is a
    schema validation error (intended breaking change, design §4.5)."""
    tool = SubmitResultTool()
    with bind_runtime_tool_context(RuntimeToolInvocationContext(session_id="child", parent_session_id="parent")):
        with pytest.raises(ValueError, match="submit_result Validation error"):
            tool.invoke(
                ToolCall(
                    tool_name="submit_result",
                    arguments={"summary": "nope", "completed_work": ["Read service.py"]},
                ),
                workspace=Path("."),
            )


def test_submit_result_is_rejected_for_top_level_session() -> None:
    with bind_runtime_tool_context(RuntimeToolInvocationContext(session_id="leader")):
        with pytest.raises(ValueError, match="delegated child"):
            SubmitResultTool().invoke(ToolCall(tool_name="submit_result", arguments={"summary": "nope"}), workspace=Path("."))
