from pathlib import Path

import pytest

from voidcode.hook.typed import validate_tool_input_schema
from voidcode.tools.contracts import ToolCall
from voidcode.tools.runtime_context import RuntimeToolInvocationContext, bind_runtime_tool_context
from voidcode.tools.yield_tool import YieldTool


@pytest.mark.parametrize(
    "arguments",
    [
        {"summary": "Done."},
        {"summary": "Done.", "type": "result", "data": {}},
        {"summary": "Done.", "type": ["result"], "data": {}},
        {"error": "child failed"},
        {"error": "child failed", "type": "error", "data": {}},
        {"error": "child failed", "type": ["error"], "data": {}},
        {"type": "progress", "result": "Still working."},
        {"type": ["progress", "checkpoint"], "data": {"step": 1}},
        {"type": ["progress", "progress"], "result": "Still working."},
    ],
)
def test_yield_schema_accepts_terminal_error_summary_and_progress_forms(arguments: dict[str, object]) -> None:
    validate_tool_input_schema(YieldTool.definition, arguments)


def test_yield_invokes_error_only_terminal_payload() -> None:
    arguments = {"error": "child failed"}
    validate_tool_input_schema(YieldTool.definition, arguments)

    with bind_runtime_tool_context(RuntimeToolInvocationContext(session_id="child", parent_session_id="parent")):
        result = YieldTool().invoke(ToolCall(tool_name="yield", arguments=arguments), workspace=Path("."))

    assert result.status == "error"
    assert result.error == "child failed"
    assert result.data["yield_kind"] == "terminal_error"


@pytest.mark.parametrize(
    "arguments",
    [
        {"type": "error"},
        {"type": "result"},
        {"type": "progress"},
        {"type": "progress", "data": {}},
        {"type": "", "result": "still working"},
        {"type": " ", "result": "still working"},
        {"type": "x" * 65, "result": "still working"},
        {"type": [], "result": "still working"},
        {"type": ["progress", ""], "result": "still working"},
        {"type": ["progress", " "], "result": "still working"},
        {"type": ["x" * 65], "result": "still working"},
        {"type": ["progress"] * 101, "result": "still working"},
        {"type": ["progress", "result"], "result": "mixed"},
        {"type": ["progress", "error"], "result": "mixed"},
        {"summary": "Done.", "type": "progress", "result": "mixed"},
        {"summary": "Done.", "error": "child failed"},
        {"error": "child failed", "type": "progress"},
        {"error": "child failed", "type": "progress", "result": "mixed"},
        {"error": "child failed", "type": "result"},
        {"error": "child failed", "result": "mixed"},
    ],
)
def test_yield_schema_and_invocation_reject_mixed_or_incomplete_forms(arguments: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="input schema validation failed"):
        validate_tool_input_schema(YieldTool.definition, arguments)

    with bind_runtime_tool_context(RuntimeToolInvocationContext(session_id="child", parent_session_id="parent")):
        with pytest.raises(ValueError, match="yield Validation error"):
            YieldTool().invoke(ToolCall(tool_name="yield", arguments=arguments), workspace=Path("."))


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


def test_yield_defaults_data_to_empty_object() -> None:
    tool = YieldTool()
    with bind_runtime_tool_context(RuntimeToolInvocationContext(session_id="child", parent_session_id="parent")):
        result = tool.invoke(
            ToolCall(
                tool_name="yield",
                arguments={"summary": "Done."},
            ),
            workspace=Path("."),
        )

    assert result.status == "ok"
    assert result.data["handoff"] == {"summary": "Done.", "data": {}}


def test_yield_rejects_legacy_fixed_fields() -> None:
    """The terminal yield payload is closed to summary/data only."""
    tool = YieldTool()
    with bind_runtime_tool_context(RuntimeToolInvocationContext(session_id="child", parent_session_id="parent")):
        with pytest.raises(ValueError, match="yield Validation error"):
            tool.invoke(
                ToolCall(
                    tool_name="yield",
                    arguments={"summary": "nope", "completed_work": ["Read service.py"]},
                ),
                workspace=Path("."),
            )


def test_yield_is_rejected_for_top_level_session() -> None:
    with bind_runtime_tool_context(RuntimeToolInvocationContext(session_id="leader")):
        with pytest.raises(ValueError, match="delegated child"):
            YieldTool().invoke(ToolCall(tool_name="yield", arguments={"summary": "nope"}), workspace=Path("."))


def test_yield_rejects_whitespace_only_summary() -> None:
    with bind_runtime_tool_context(RuntimeToolInvocationContext(session_id="child", parent_session_id="parent")):
        with pytest.raises(ValueError, match="summary"):
            YieldTool().invoke(ToolCall(tool_name="yield", arguments={"summary": " \t\n"}), workspace=Path("."))


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


def test_yield_rejects_empty_progress_and_handles_terminal_errors_deterministically() -> None:
    with bind_runtime_tool_context(RuntimeToolInvocationContext(session_id="child", parent_session_id="parent")):
        with pytest.raises(ValueError, match="incremental yield requires"):
            YieldTool().invoke(ToolCall(tool_name="yield", arguments={"type": "progress"}), workspace=Path("."))
        error_result = YieldTool().invoke(
            ToolCall(tool_name="yield", arguments={"type": "error", "error": "child failed"}),
            workspace=Path("."),
        )
    assert error_result.status == "error"
    assert error_result.data["yield_kind"] == "terminal_error"
    assert error_result.error == "child failed"


def test_yield_rejects_unbounded_progress_text() -> None:
    with bind_runtime_tool_context(RuntimeToolInvocationContext(session_id="child", parent_session_id="parent")):
        with pytest.raises(ValueError, match="at most 4096"):
            YieldTool().invoke(
                ToolCall(tool_name="yield", arguments={"type": "progress", "result": "x" * 4097}),
                workspace=Path("."),
            )
