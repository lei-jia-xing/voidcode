from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from voidcode.runtime.service import ToolRegistry
from voidcode.tools import ReadFileTool, ToolCall
from voidcode.tools.read_file import MAX_ATTACHMENT_BYTES, MAX_LINE_LENGTH


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
    assert "Use offset=4 to continue." in (result.content or "")
    assert result.data["path"] == "sample.txt"
    assert result.data["offset"] == 2
    assert result.data["limit"] == 2
    assert result.data["next_offset"] == 4
    assert "omit those prefixes" in str(result.data["copy_guidance"])


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


def test_read_file_tool_allows_workspace_escape_path_with_absolute_display(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-read.txt"
    outside.write_text("outside", encoding="utf-8")
    tool = ReadFileTool()

    result = tool.invoke(
        ToolCall(tool_name="read_file", arguments={"filePath": "../outside-read.txt"}),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert result.data["path"] == str(outside.resolve())


def test_read_file_tool_allows_symlink_escape_when_runtime_permission_allows(
    tmp_path: Path,
) -> None:
    outside = tmp_path.parent / "outside_read_escape.txt"
    outside.write_text("secret", encoding="utf-8")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink is not available on this platform")

    tool = ReadFileTool()
    result = tool.invoke(
        ToolCall(tool_name="read_file", arguments={"filePath": "link.txt"}),
        workspace=tmp_path,
    )
    assert result.status == "ok"
    assert result.data["path"] == str(outside.resolve())


def test_read_file_tool_sniffs_text_with_bounded_stream_read(tmp_path: Path) -> None:
    sample = tmp_path / "sample.txt"
    _ = sample.write_text("alpha\nbeta\n", encoding="utf-8")
    tool = ReadFileTool()

    with patch.object(
        Path, "read_bytes", side_effect=AssertionError("read_bytes should not be used")
    ):
        result = tool.invoke(
            ToolCall(tool_name="read_file", arguments={"filePath": "sample.txt"}),
            workspace=tmp_path,
        )

    assert result.status == "ok"
    assert "1: alpha" in (result.content or "")


def test_read_file_tool_rejects_non_regular_target(tmp_path: Path) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("mkfifo is not available on this platform")

    fifo_path = tmp_path / "sample.fifo"
    os.mkfifo(fifo_path)
    tool = ReadFileTool()

    with pytest.raises(ValueError, match="only supports regular files"):
        tool.invoke(
            ToolCall(tool_name="read_file", arguments={"filePath": "sample.fifo"}),
            workspace=tmp_path,
        )


def test_read_file_tool_reports_field_specific_validation_errors(tmp_path: Path) -> None:
    tool = ReadFileTool()

    file_path_error = (
        r"read_file Validation error: filePath: "
        r"Input should be a valid string \(received int\)"
        r"\. Please retry with corrected arguments that satisfy the tool schema\."
    )
    with pytest.raises(ValueError, match=file_path_error):
        tool.invoke(
            ToolCall(tool_name="read_file", arguments={"filePath": 123}),
            workspace=tmp_path,
        )

    offset_error = (
        r"read_file Validation error: offset: Value error, "
        r"offset must be greater than or equal to 1 \(received int\)"
        r"\. Please retry with corrected arguments that satisfy the tool schema\."
    )
    with pytest.raises(ValueError, match=offset_error):
        tool.invoke(
            ToolCall(tool_name="read_file", arguments={"filePath": "sample.txt", "offset": 0}),
            workspace=tmp_path,
        )

    limit_error = (
        r"read_file Validation error: limit: Value error, "
        r"limit must be greater than or equal to 1 \(received int\)"
        r"\. Please retry with corrected arguments that satisfy the tool schema\."
    )
    with pytest.raises(ValueError, match=limit_error):
        tool.invoke(
            ToolCall(tool_name="read_file", arguments={"filePath": "sample.txt", "limit": 0}),
            workspace=tmp_path,
        )


def test_read_file_tool_reports_missing_file_path(tmp_path: Path) -> None:
    tool = ReadFileTool()
    missing_file_path_error = (
        r"read_file Validation error: filePath: "
        r"Input should be a valid string \(received NoneType\)"
        r"\. Please retry with corrected arguments that satisfy the tool schema\."
    )

    with pytest.raises(ValueError, match=missing_file_path_error):
        tool.invoke(ToolCall(tool_name="read_file", arguments={}), workspace=tmp_path)


def test_read_file_tool_rejects_oversized_attachment_before_read_bytes(tmp_path: Path) -> None:
    image = tmp_path / "image.png"
    _ = image.write_bytes(b"\x89PNG\r\n\x1a\n" + (b"x" * MAX_ATTACHMENT_BYTES))
    tool = ReadFileTool()

    with patch.object(
        Path, "read_bytes", side_effect=AssertionError("read_bytes should not be used")
    ):
        with pytest.raises(ValueError, match="attachment exceeds the maximum supported size"):
            tool.invoke(
                ToolCall(tool_name="read_file", arguments={"filePath": "image.png"}),
                workspace=tmp_path,
            )


def test_read_file_tool_does_not_emit_offset_guidance_for_clipped_line_only(tmp_path: Path) -> None:
    sample = tmp_path / "sample.txt"
    _ = sample.write_text("x" * (MAX_LINE_LENGTH + 5), encoding="utf-8")
    tool = ReadFileTool()

    result = tool.invoke(
        ToolCall(tool_name="read_file", arguments={"filePath": "sample.txt"}),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert "line truncated to 2000 chars" in (result.content or "")
    assert "Use offset=" not in (result.content or "")
    assert "(End of file - total 1 lines)" in (result.content or "")
    assert result.data["next_offset"] is None
    assert result.data["truncated"] is True
    assert result.data["partial"] is True


def test_tools_package_and_default_registry_export_read_file_tool() -> None:
    registry = ToolRegistry.with_defaults()

    assert "ReadFileTool" in __import__("voidcode.tools", fromlist=["__all__"]).__all__
    assert registry.resolve("read_file").definition.name == "read_file"
