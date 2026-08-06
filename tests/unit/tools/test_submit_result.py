from pathlib import Path

import pytest

from voidcode.tools.contracts import ToolCall
from voidcode.tools.runtime_context import RuntimeToolInvocationContext, bind_runtime_tool_context
from voidcode.tools.submit_result import SubmitResultTool


def test_submit_result_returns_structured_child_handoff() -> None:
    tool = SubmitResultTool()
    with bind_runtime_tool_context(RuntimeToolInvocationContext(session_id="child", parent_session_id="parent")):
        result = tool.invoke(
            ToolCall(
                tool_name="submit_result",
                arguments={
                    "summary": "Inspected the runtime.",
                    "completed_work": ["Read service.py"],
                    "verification": ["pytest passed"],
                },
            ),
            workspace=Path("."),
        )

    assert result.status == "ok"
    assert result.data["handoff"] == {
        "summary": "Inspected the runtime.",
        "completed_work": ["Read service.py"],
        "files_touched": [],
        "verification": ["pytest passed"],
        "open_questions": [],
        "blockers": [],
    }


def test_submit_result_is_rejected_for_top_level_session() -> None:
    with bind_runtime_tool_context(RuntimeToolInvocationContext(session_id="leader")):
        with pytest.raises(ValueError, match="delegated child"):
            SubmitResultTool().invoke(ToolCall(tool_name="submit_result", arguments={"summary": "nope"}), workspace=Path("."))
