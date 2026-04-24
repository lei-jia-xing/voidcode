from __future__ import annotations

from pathlib import Path

import pytest

from voidcode.runtime.service import ToolRegistry
from voidcode.tools import ReadFileTool, ToolCall


def test_read_file_tool_reads_text_file_with_offset_and_limit(tmp_path: Path) -> None:
    sample = tmp_path / "sample.txt"
    _ = sample.write_text("alpha\nbeta\ngamma\ndelta\n", encoding="utf-8")
    tool = ReadFileTool()

    result = tool.invoke(
        ToolCall(
            tool_name="read_file", arguments={"filePath": "sample.txt", "offset": 2, "limit": 2}
        ),
        workspace=tmp_path,
    )

    assert result.tool_name == "read_file"
    assert result.status == "ok"
    assert "2: beta" in (result.content or "")
    assert "3: gamma" in (result.content or "")
    assert result.data["path"] == "sample.txt"
    assert result.data["offset"] == 2
    assert result.data["limit"] == 2


def test_read_file_tool_rejects_directories_with_suggestions(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "b.txt").write_text("b", encoding="utf-8")
    subdir = tmp_path / "subdir"
    subdir.mkdir()

    tool = ReadFileTool()

    with pytest.raises(ValueError, match="does not support directories") as exc_info:
        tool.invoke(
            ToolCall(tool_name="read_file", arguments={"filePath": "."}), workspace=tmp_path
        )

    assert "Did you mean:" in str(exc_info.value)


def test_read_file_tool_returns_attachment_for_images(tmp_path: Path) -> None:
    image = tmp_path / "image.png"
    _ = image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"fake")
    tool = ReadFileTool()

    result = tool.invoke(
        ToolCall(tool_name="read_file", arguments={"filePath": "image.png"}),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert result.data["type"] == "attachment"
    assert isinstance(result.data["attachment"], dict)


def test_read_file_tool_rejects_workspace_escape(tmp_path: Path) -> None:
    tool = ReadFileTool()

    with pytest.raises(ValueError, match="inside the workspace"):
        tool.invoke(
            ToolCall(tool_name="read_file", arguments={"filePath": "../escape.txt"}),
            workspace=tmp_path,
        )


def test_tools_package_and_default_registry_export_read_file_tool() -> None:
    registry = ToolRegistry.with_defaults()

    assert "ReadFileTool" in __import__("voidcode.tools", fromlist=["__all__"]).__all__
    assert registry.resolve("read_file").definition.name == "read_file"
