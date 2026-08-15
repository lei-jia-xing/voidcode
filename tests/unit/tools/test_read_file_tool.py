from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from voidcode.runtime.service import ToolRegistry
from voidcode.tools import ReadFileTool, ToolCall
from voidcode.tools.read_file import MAX_ATTACHMENT_BYTES, MAX_LINE_LENGTH
from voidcode.tools.runtime_context import RuntimeToolInvocationContext, bind_runtime_tool_context


def test_read_file_tool_reads_text_file_with_offset_and_limit(tmp_path: Path) -> None:
    sample = tmp_path / "sample.txt"
    _ = sample.write_text("alpha\nbeta\ngamma\ndelta\n", encoding="utf-8")
    tool = ReadFileTool()

    result = tool.invoke(
        ToolCall(tool_name="read_file", arguments={"path": "sample.txt", "offset": 2, "limit": 2}),
        workspace=tmp_path,
    )

    assert result.tool_name == "read_file"
    assert result.status == "ok"
    assert result.content == "Read 2 line(s) from sample.txt; output is truncated."
    assert result.data["raw_content"] == "beta\ngamma"
    assert result.data["path"] == "sample.txt"
    assert result.data["offset"] == 2
    assert result.data["limit"] == 2
    assert result.data["next_offset"] == 4
    assert "copy_guidance" not in result.data


def test_read_file_tool_rejects_directories_with_suggestions(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "b.txt").write_text("b", encoding="utf-8")
    subdir = tmp_path / "subdir"
    subdir.mkdir()

    tool = ReadFileTool()

    with pytest.raises(ValueError, match="does not support directories") as exc_info:
        tool.invoke(ToolCall(tool_name="read_file", arguments={"path": "."}), workspace=tmp_path)

    assert "Did you mean:" in str(exc_info.value)


def test_read_file_tool_returns_attachment_for_images(tmp_path: Path) -> None:
    image = tmp_path / "image.png"
    _ = image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"fake")
    tool = ReadFileTool()

    result = tool.invoke(
        ToolCall(tool_name="read_file", arguments={"path": "image.png"}),
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
        ToolCall(tool_name="read_file", arguments={"path": "../outside-read.txt"}),
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
        ToolCall(tool_name="read_file", arguments={"path": "link.txt"}),
        workspace=tmp_path,
    )
    assert result.status == "ok"
    assert result.data["path"] == str(outside.resolve())


def test_read_file_tool_sniffs_text_with_bounded_stream_read(tmp_path: Path) -> None:
    sample = tmp_path / "sample.txt"
    _ = sample.write_text("alpha\nbeta\n", encoding="utf-8")
    tool = ReadFileTool()

    with patch.object(Path, "read_bytes", side_effect=AssertionError("read_bytes should not be used")):
        result = tool.invoke(
            ToolCall(tool_name="read_file", arguments={"path": "sample.txt"}),
            workspace=tmp_path,
        )

    assert result.status == "ok"
    assert result.content == "Read 2 line(s) from sample.txt."
    assert result.data["raw_content"] == "alpha\nbeta"


def test_read_file_tool_rejects_non_regular_target(tmp_path: Path) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("mkfifo is not available on this platform")

    fifo_path = tmp_path / "sample.fifo"
    os.mkfifo(fifo_path)
    tool = ReadFileTool()

    with pytest.raises(ValueError, match="only supports regular files"):
        tool.invoke(
            ToolCall(tool_name="read_file", arguments={"path": "sample.fifo"}),
            workspace=tmp_path,
        )


def test_read_file_tool_reports_field_specific_validation_errors(tmp_path: Path) -> None:
    tool = ReadFileTool()

    file_path_error = (
        r"read_file Validation error: path: "
        r"Input should be a valid string \(received int\)"
        r"\. Please retry with corrected arguments that satisfy the tool schema\."
    )
    with pytest.raises(ValueError, match=file_path_error):
        tool.invoke(
            ToolCall(tool_name="read_file", arguments={"path": 123}),
            workspace=tmp_path,
        )

    offset_error = (
        r"read_file Validation error: offset: Value error, "
        r"offset must be greater than or equal to 1 \(received int\)"
        r"\. Please retry with corrected arguments that satisfy the tool schema\."
    )
    with pytest.raises(ValueError, match=offset_error):
        tool.invoke(
            ToolCall(tool_name="read_file", arguments={"path": "sample.txt", "offset": 0}),
            workspace=tmp_path,
        )

    limit_error = (
        r"read_file Validation error: limit: Value error, "
        r"limit must be greater than or equal to 1 \(received int\)"
        r"\. Please retry with corrected arguments that satisfy the tool schema\."
    )
    with pytest.raises(ValueError, match=limit_error):
        tool.invoke(
            ToolCall(tool_name="read_file", arguments={"path": "sample.txt", "limit": 0}),
            workspace=tmp_path,
        )


def test_read_file_tool_reports_missing_file_path(tmp_path: Path) -> None:
    tool = ReadFileTool()
    missing_file_path_error = (
        r"read_file Validation error: path: "
        r"Input should be a valid string \(received NoneType\)"
        r"\. Please retry with corrected arguments that satisfy the tool schema\."
    )

    with pytest.raises(ValueError, match=missing_file_path_error):
        tool.invoke(ToolCall(tool_name="read_file", arguments={}), workspace=tmp_path)


def test_read_file_tool_rejects_oversized_attachment_before_read_bytes(tmp_path: Path) -> None:
    image = tmp_path / "image.png"
    _ = image.write_bytes(b"\x89PNG\r\n\x1a\n" + (b"x" * MAX_ATTACHMENT_BYTES))
    tool = ReadFileTool()

    with patch.object(Path, "read_bytes", side_effect=AssertionError("read_bytes should not be used")):
        with pytest.raises(ValueError, match="attachment exceeds the maximum supported size"):
            tool.invoke(
                ToolCall(tool_name="read_file", arguments={"path": "image.png"}),
                workspace=tmp_path,
            )


def test_read_file_tool_does_not_emit_offset_guidance_for_clipped_line_only(tmp_path: Path) -> None:
    sample = tmp_path / "sample.txt"
    _ = sample.write_text("x" * (MAX_LINE_LENGTH + 5), encoding="utf-8")
    tool = ReadFileTool()

    result = tool.invoke(
        ToolCall(tool_name="read_file", arguments={"path": "sample.txt"}),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert "truncated" in (result.content or "")
    assert result.data["raw_content"].startswith("x" * MAX_LINE_LENGTH)
    assert result.data["next_offset"] is None
    assert result.data["truncated"] is True
    assert result.data["partial"] is True


def test_tools_package_and_default_registry_export_read_file_tool() -> None:
    registry = ToolRegistry.with_defaults()

    assert "ReadFileTool" in __import__("voidcode.tools", fromlist=["__all__"]).__all__
    assert registry.resolve("read_file").definition.name == "read_file"


class _FakeArtifactFacade:
    """Minimal RuntimeArtifactReadFacade stand-in mirroring bounded read semantics."""

    def __init__(self, artifact_id: str, content: str) -> None:
        self._artifact_id = artifact_id
        self._content = content
        self.requests: list[tuple[str, int | None, int | None]] = []

    def read_artifact(
        self,
        *,
        artifact_id: str,
        offset: int | None = None,
        limit: int | None = None,
    ) -> dict[str, object] | None:
        self.requests.append((artifact_id, offset, limit))
        if artifact_id != self._artifact_id:
            return None
        lines = self._content.splitlines(keepends=True)
        start = max(0, offset or 0)
        bounded = max(0, limit or 2000)
        selected = lines[start : start + bounded]
        next_offset = start + len(selected) if start + len(selected) < len(lines) else None
        return {
            "artifact_id": artifact_id,
            "status": "available",
            "artifact_missing": False,
            "offset": start,
            "limit": bounded,
            "line_count": len(lines),
            "next_offset": next_offset,
            "content": "".join(selected),
        }


_ARTIFACT_ID = "artifact_0123456789abcdef01234567"


def test_read_file_tool_resolves_artifact_uri_with_bounded_content(tmp_path: Path) -> None:
    content = "".join(f"line-{index}\n" for index in range(3000))
    facade = _FakeArtifactFacade(_ARTIFACT_ID, content)
    tool = ReadFileTool()

    with bind_runtime_tool_context(RuntimeToolInvocationContext(session_id="session-1", artifact=facade)):
        result = tool.invoke(
            ToolCall(
                tool_name="read_file",
                arguments={"path": f"voidcode://artifact/{_ARTIFACT_ID}", "limit": 100},
            ),
            workspace=tmp_path,
        )

    assert result.status == "ok"
    assert result.data["type"] == "artifact"
    assert result.data["artifact_id"] == _ARTIFACT_ID
    assert result.data["raw_content"] == "".join(f"line-{index}\n" for index in range(100))
    assert result.data["next_offset"] == 100
    assert result.data["line_count"] == 3000
    assert result.data["truncated"] is True
    assert result.data["partial"] is True
    assert result.data["offset"] == 1
    assert result.data["limit"] == 100
    assert "output is truncated" in (result.content or "")
    assert facade.requests == [(_ARTIFACT_ID, 0, 100)]


def test_read_file_tool_artifact_uri_pages_with_one_based_offset(tmp_path: Path) -> None:
    content = "".join(f"line-{index}\n" for index in range(3000))
    facade = _FakeArtifactFacade(_ARTIFACT_ID, content)
    tool = ReadFileTool()

    with bind_runtime_tool_context(RuntimeToolInvocationContext(session_id="session-1", artifact=facade)):
        result = tool.invoke(
            ToolCall(
                tool_name="read_file",
                arguments={"path": f"voidcode://artifact/{_ARTIFACT_ID}", "offset": 101, "limit": 100},
            ),
            workspace=tmp_path,
        )

    assert result.data["raw_content"] == "".join(f"line-{index}\n" for index in range(100, 200))
    assert result.data["next_offset"] == 200
    assert result.data["offset"] == 101
    assert facade.requests == [(_ARTIFACT_ID, 100, 100)]


def test_read_file_tool_rejects_unknown_artifact_id(tmp_path: Path) -> None:
    facade = _FakeArtifactFacade(_ARTIFACT_ID, "content")
    tool = ReadFileTool()

    with bind_runtime_tool_context(RuntimeToolInvocationContext(session_id="session-1", artifact=facade)):
        with pytest.raises(ValueError, match="artifact not found in current session"):
            tool.invoke(
                ToolCall(
                    tool_name="read_file",
                    arguments={"path": "voidcode://artifact/artifact_ffffffffffffffffffffffff"},
                ),
                workspace=tmp_path,
            )


def test_read_file_tool_rejects_malformed_artifact_id(tmp_path: Path) -> None:
    tool = ReadFileTool()

    with bind_runtime_tool_context(RuntimeToolInvocationContext(session_id="session-1")):
        with pytest.raises(ValueError, match="invalid artifact id"):
            tool.invoke(
                ToolCall(tool_name="read_file", arguments={"path": "voidcode://artifact/not-an-id"}),
                workspace=tmp_path,
            )
        with pytest.raises(ValueError, match="requires an artifact id"):
            tool.invoke(
                ToolCall(tool_name="read_file", arguments={"path": "voidcode://artifact/"}),
                workspace=tmp_path,
            )


def test_read_file_tool_artifact_uri_requires_runtime_artifact_reader(tmp_path: Path) -> None:
    tool = ReadFileTool()

    with bind_runtime_tool_context(RuntimeToolInvocationContext(session_id="session-1")):
        with pytest.raises(ValueError, match="without a runtime artifact reader"):
            tool.invoke(
                ToolCall(
                    tool_name="read_file",
                    arguments={"path": f"voidcode://artifact/{_ARTIFACT_ID}"},
                ),
                workspace=tmp_path,
            )
