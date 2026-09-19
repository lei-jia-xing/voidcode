from pathlib import Path

import pytest

from voidcode.hook.typed import validate_tool_input_schema
from voidcode.tools.contracts import ToolCall
from voidcode.tools.runtime_context import RuntimeToolInvocationContext, bind_runtime_tool_context
from voidcode.tools.yield_tool import YieldTool


@pytest.mark.parametrize(
    ("arguments", "valid"),
    (
        # Accepted terminal/error/summary/progress forms.
        pytest.param({"summary": "Done."}, True, id="summary"),
        pytest.param({"summary": "Done.", "type": "result", "data": {}}, True, id="summary-result"),
        pytest.param({"summary": "Done.", "type": ["result"], "data": {}}, True, id="summary-result-list"),
        pytest.param({"error": "child failed"}, True, id="error"),
        pytest.param({"error": "child failed", "type": "error", "data": {}}, True, id="error-type"),
        pytest.param({"error": "child failed", "type": ["error"], "data": {}}, True, id="error-type-list"),
        pytest.param({"type": "progress", "result": "Still working."}, True, id="progress"),
        pytest.param({"type": ["progress", "checkpoint"], "data": {"step": 1}}, True, id="progress-checkpoint"),
        pytest.param({"type": ["progress", "progress"], "result": "Still working."}, True, id="progress-progress"),
        # Rejected mixed or incomplete forms.
        pytest.param({"type": "error"}, False, id="type-error-without-payload"),
        pytest.param({"type": "result"}, False, id="type-result-without-payload"),
        pytest.param({"type": "progress"}, False, id="progress-without-payload"),
        pytest.param({"type": "progress", "data": {}}, False, id="progress-withdata-only"),
        pytest.param({"type": "", "result": "still working"}, False, id="blank-type"),
        pytest.param({"type": " ", "result": "still working"}, False, id="whitespace-type"),
        pytest.param({"type": "x" * 65, "result": "still working"}, False, id="oversized-type"),
        pytest.param({"type": [], "result": "still working"}, False, id="empty-type-list"),
        pytest.param({"type": ["progress", ""], "result": "still working"}, False, id="blank-type-in-list"),
        pytest.param({"type": ["progress", " "], "result": "still working"}, False, id="whitespace-type-in-list"),
        pytest.param({"type": ["x" * 65], "result": "still working"}, False, id="oversized-type-in-list"),
        pytest.param({"type": ["progress"] * 101, "result": "still working"}, False, id="too-many-types"),
        pytest.param({"type": ["progress", "result"], "result": "mixed"}, False, id="mixed-result-progress"),
        pytest.param({"type": ["progress", "error"], "result": "mixed"}, False, id="mixed-error-progress"),
        pytest.param({"summary": "Done.", "type": "progress", "result": "mixed"}, False, id="summary-with-progress"),
        pytest.param({"summary": "Done.", "error": "child failed"}, False, id="summary-and-error"),
        pytest.param({"error": "child failed", "type": "progress"}, False, id="error-with-progress-type"),
        pytest.param({"error": "child failed", "type": "progress", "result": "mixed"}, False, id="error-mixed-progress"),
        pytest.param({"error": "child failed", "type": "result"}, False, id="error-with-result-type"),
        pytest.param({"error": "child failed", "result": "mixed"}, False, id="error-with-result"),
    ),
)
def test_yield_input_schema_matrix(arguments: dict[str, object], valid: bool) -> None:
    if valid:
        validate_tool_input_schema(YieldTool.definition, arguments)
        return

    with pytest.raises(ValueError, match="input schema validation failed"):
        validate_tool_input_schema(YieldTool.definition, arguments)

    with bind_runtime_tool_context(RuntimeToolInvocationContext(session_id="child", parent_session_id="parent")):
        with pytest.raises(ValueError, match="yield Validation error"):
            YieldTool().invoke(ToolCall(tool_name="yield", arguments=arguments), workspace=Path("."))


def test_yield_invokes_error_only_terminal_payload() -> None:
    arguments = {"error": "child failed"}
    validate_tool_input_schema(YieldTool.definition, arguments)

    with bind_runtime_tool_context(RuntimeToolInvocationContext(session_id="child", parent_session_id="parent")):
        result = YieldTool().invoke(ToolCall(tool_name="yield", arguments=arguments), workspace=Path("."))

    assert result.status == "error"
    assert result.error == "child failed"
    assert result.data["yield_kind"] == "terminal_error"


def test_yield_returns_summary_and_arbitrary_data_handoff() -> None:
    tool = YieldTool()
    with bind_runtime_tool_context(RuntimeToolInvocationContext(session_id="child", parent_session_id="parent")):
        result = tool.invoke(
            ToolCall(
                tool_name="yield",
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


def test_yield_is_rejected_for_top_level_session() -> None:
    with bind_runtime_tool_context(RuntimeToolInvocationContext(session_id="leader")):
        with pytest.raises(ValueError, match="delegated child"):
            YieldTool().invoke(ToolCall(tool_name="yield", arguments={"summary": "nope"}), workspace=Path("."))


def test_yield_progress_is_nonterminal_and_supports_bounded_type_list() -> None:
    with bind_runtime_tool_context(RuntimeToolInvocationContext(session_id="child", parent_session_id="parent")):
        result = YieldTool().invoke(
            ToolCall(
                tool_name="yield",
                arguments={"type": ["progress", "checkpoint"], "result": "Read the storage boundary."},
            ),
            workspace=Path("."),
        )

    assert result.status == "ok"
    assert result.data["yield_kind"] == "progress"
    assert result.data["progress"] == {
        "type": ["progress", "checkpoint"],
        "result": "Read the storage boundary.",
    }
    assert "handoff" not in result.data
