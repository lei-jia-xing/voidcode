from __future__ import annotations

from pathlib import Path

import pytest

from voidcode.runtime.service import ToolRegistry
from voidcode.tools import EditTool, ToolCall


def test_edit_tool_replaces_exact_text(tmp_path: Path) -> None:
    file_path = tmp_path / "test.txt"
    file_path.write_text("hello world", encoding="utf-8")

    tool = EditTool()

    result = tool.invoke(
        ToolCall(
            tool_name="edit",
            arguments={
                "path": "test.txt",
                "oldString": "world",
                "newString": "voidcode",
            },
        ),
        workspace=tmp_path,
    )

    assert result.tool_name == "edit"
    assert result.status == "ok"
    assert result.content == "Edit applied successfully."
    assert file_path.read_text(encoding="utf-8") == "hello voidcode"
    assert result.data["additions"] == 1
    assert result.data["deletions"] == 1


def test_edit_tool_replaces_all_occurrences(tmp_path: Path) -> None:
    file_path = tmp_path / "test.txt"
    file_path.write_text("foo bar foo baz foo", encoding="utf-8")

    tool = EditTool()

    result = tool.invoke(
        ToolCall(
            tool_name="edit",
            arguments={
                "path": "test.txt",
                "oldString": "foo",
                "newString": "qux",
                "replaceAll": True,
            },
        ),
        workspace=tmp_path,
    )

    assert file_path.read_text(encoding="utf-8") == "qux bar qux baz qux"
    assert "3 occurrences replaced" in result.content


def test_edit_tool_rejects_non_string_arguments(tmp_path: Path) -> None:
    tool = EditTool()

    with pytest.raises(ValueError, match="string path"):
        tool.invoke(
            ToolCall(tool_name="edit", arguments={"path": 123, "oldString": "a", "newString": "b"}),
            workspace=tmp_path,
        )

    with pytest.raises(ValueError, match="string oldString"):
        tool.invoke(
            ToolCall(
                tool_name="edit", arguments={"path": "f.txt", "oldString": 123, "newString": "b"}
            ),
            workspace=tmp_path,
        )

    with pytest.raises(ValueError, match="string newString"):
        tool.invoke(
            ToolCall(
                tool_name="edit", arguments={"path": "f.txt", "oldString": "a", "newString": 123}
            ),
            workspace=tmp_path,
        )


def test_edit_tool_rejects_path_outside_workspace(tmp_path: Path) -> None:
    tool = EditTool()

    with pytest.raises(ValueError, match="inside the workspace"):
        tool.invoke(
            ToolCall(
                tool_name="edit",
                arguments={"path": "../escape.txt", "oldString": "a", "newString": "b"},
            ),
            workspace=tmp_path,
        )


def test_edit_tool_rejects_nonexistent_file(tmp_path: Path) -> None:
    tool = EditTool()

    with pytest.raises(ValueError, match="does not exist"):
        tool.invoke(
            ToolCall(
                tool_name="edit",
                arguments={"path": "missing.txt", "oldString": "a", "newString": "b"},
            ),
            workspace=tmp_path,
        )


def test_edit_tool_rejects_identical_old_and_new(tmp_path: Path) -> None:
    file_path = tmp_path / "test.txt"
    file_path.write_text("hello", encoding="utf-8")

    tool = EditTool()

    with pytest.raises(ValueError, match="identical"):
        tool.invoke(
            ToolCall(
                tool_name="edit",
                arguments={"path": "test.txt", "oldString": "hello", "newString": "hello"},
            ),
            workspace=tmp_path,
        )


def test_edit_tool_rejects_when_old_string_not_found(tmp_path: Path) -> None:
    file_path = tmp_path / "test.txt"
    file_path.write_text("hello", encoding="utf-8")

    tool = EditTool()

    with pytest.raises(ValueError, match="Could not find oldString"):
        tool.invoke(
            ToolCall(
                tool_name="edit",
                arguments={"path": "test.txt", "oldString": "missing", "newString": "b"},
            ),
            workspace=tmp_path,
        )


def test_edit_tool_preserves_line_endings(tmp_path: Path) -> None:
    file_path = tmp_path / "test.txt"
    file_path.write_bytes(b"line1\r\nline2\r\nline3")

    tool = EditTool()

    tool.invoke(
        ToolCall(
            tool_name="edit",
            arguments={"path": "test.txt", "oldString": "line2", "newString": "modified"},
        ),
        workspace=tmp_path,
    )

    content = file_path.read_bytes()
    assert content == b"line1\r\nmodified\r\nline3"


def test_tools_package_and_default_registry_export_edit_tool() -> None:
    registry = ToolRegistry.with_defaults()

    assert "EditTool" in __import__("voidcode.tools", fromlist=["__all__"]).__all__
    assert registry.resolve("edit").definition.name == "edit"
    assert registry.resolve("edit").definition.read_only is False
