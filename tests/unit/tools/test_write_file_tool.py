from __future__ import annotations

from pathlib import Path

import pytest

from voidcode.runtime.service import ToolRegistry
from voidcode.tools import ToolCall, WriteFileTool


def test_write_file_tool_writes_utf8_content_inside_workspace(tmp_path: Path) -> None:
    tool = WriteFileTool()

    result = tool.invoke(
        ToolCall(
            tool_name="write_file",
            arguments={"path": "nested/output.txt", "content": "hello utf8 π"},
        ),
        workspace=tmp_path,
    )

    assert (tmp_path / "nested" / "output.txt").read_text(encoding="utf-8") == "hello utf8 π"
    assert result.tool_name == "write_file"
    assert result.status == "ok"
    assert result.content == "Wrote file successfully: nested/output.txt"
    assert result.data == {
        "path": "nested/output.txt",
        "byte_count": len("hello utf8 π".encode()),
    }


def test_write_file_tool_rejects_non_string_arguments(tmp_path: Path) -> None:
    tool = WriteFileTool()

    with pytest.raises(ValueError, match="string path"):
        tool.invoke(
            ToolCall(tool_name="write_file", arguments={"path": 123, "content": "x"}),
            workspace=tmp_path,
        )

    with pytest.raises(ValueError, match="string content"):
        tool.invoke(
            ToolCall(tool_name="write_file", arguments={"path": "out.txt", "content": 123}),
            workspace=tmp_path,
        )


def test_write_file_tool_rejects_paths_outside_workspace(tmp_path: Path) -> None:
    tool = WriteFileTool()

    with pytest.raises(ValueError, match="inside the workspace"):
        tool.invoke(
            ToolCall(
                tool_name="write_file",
                arguments={"path": "../escape.txt", "content": "nope"},
            ),
            workspace=tmp_path,
        )


def test_tools_package_and_default_registry_export_write_file_tool() -> None:
    registry = ToolRegistry.with_defaults()

    assert "WriteFileTool" in __import__("voidcode.tools", fromlist=["__all__"]).__all__
    assert registry.resolve("write_file").definition.name == "write_file"
    assert registry.resolve("write_file").definition.read_only is False
