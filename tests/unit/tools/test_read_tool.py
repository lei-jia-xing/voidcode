from __future__ import annotations

import zipfile
from pathlib import Path
from typing import cast

import pytest

from voidcode.core.tool_context import ToolContext
from voidcode.core.transcript import tool_result_output
from voidcode.tools.contracts import ToolCall
from voidcode.tools.read import ReadTool


def test_read_tool_reads_text_file_with_offset_and_limit(tmp_path: Path) -> None:
    sample = tmp_path / "sample.txt"
    _ = sample.write_text("alpha\nbeta\ngamma\ndelta\n", encoding="utf-8")
    tool = ReadTool()

    result = tool.invoke(
        ToolCall(tool_name="read", arguments={"path": "sample.txt", "offset": 2, "limit": 2}),
        context=ToolContext(workspace=tmp_path),
    )

    assert result.tool_name == "read"
    assert result.status == "ok"
    assert result.data["raw_content"] == "beta\ngamma"
    assert result.data["path"] == "sample.txt"
    assert result.data["offset"] == 2
    assert result.data["limit"] == 2
    assert result.data["next_offset"] == 4
    assert "copy_guidance" not in result.data


def test_read_tool_lists_directory_tree_and_marks_empty_directory(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    subdir = tmp_path / "subdir"
    subdir.mkdir()
    (subdir / "nested.txt").write_text("nested", encoding="utf-8")
    (tmp_path / "empty").mkdir()

    tool = ReadTool()

    result = tool.invoke(ToolCall(tool_name="read", arguments={"path": "."}), context=ToolContext(workspace=tmp_path))

    assert result.status == "ok"
    assert result.data["type"] == "directory"
    rendered = cast(str, result.data["raw_content"])
    assert "a.txt (1 B," in rendered
    assert "subdir/" in rendered
    assert "nested.txt (6 B," in rendered
    assert "empty/" in rendered
    assert "(empty directory)" in rendered


def test_read_tool_rejects_archive_path_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "bundle.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        _ = handle.writestr("safe.txt", "safe\n")

    tool = ReadTool()

    with pytest.raises(ValueError, match=r"must not contain '\.\.'"):
        tool.invoke(ToolCall(tool_name="read", arguments={"path": "bundle.zip:../outside.txt"}), context=ToolContext(workspace=tmp_path))


def test_read_tool_lists_and_decodes_archive_members(tmp_path: Path) -> None:
    archive = tmp_path / "bundle.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        _ = handle.writestr("src/main.py", "line one\nline two\n")
        _ = handle.writestr("notes.txt", "note\n")

    tool = ReadTool()

    listing = tool.invoke(ToolCall(tool_name="read", arguments={"path": "bundle.zip"}), context=ToolContext(workspace=tmp_path))
    rendered = cast(str, listing.data["raw_content"])
    assert "main.py" not in rendered
    assert "src/" in rendered
    assert "notes.txt (5 B)" in rendered

    member = tool.invoke(ToolCall(tool_name="read", arguments={"path": "bundle.zip:src/main.py"}), context=ToolContext(workspace=tmp_path))
    assert member.data["raw_content"] == "line one\nline two"
    assert member.data["type"] == "archive"


def test_read_tool_reports_non_utf8_archive_member_without_raising(tmp_path: Path) -> None:
    archive = tmp_path / "bundle.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        _ = handle.writestr("blob.bin", b"\xff\xfe\x00\x01")

    tool = ReadTool()

    result = tool.invoke(ToolCall(tool_name="read", arguments={"path": "bundle.zip:blob.bin"}), context=ToolContext(workspace=tmp_path))

    assert result.status == "ok"
    assert result.data["type"] == "archive_binary"
    output = tool_result_output(result)
    assert output is not None
    assert "not UTF-8 text" in output
    assert "4 bytes" in output


def test_read_tool_allows_workspace_escape_path_with_absolute_display(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-read.txt"
    outside.write_text("outside", encoding="utf-8")
    tool = ReadTool()

    result = tool.invoke(
        ToolCall(tool_name="read", arguments={"path": "../outside-read.txt"}),
        context=ToolContext(workspace=tmp_path),
    )

    assert result.status == "ok"
    assert result.data["path"] == str(outside.resolve())


def test_read_tool_reports_missing_file_path(tmp_path: Path) -> None:
    tool = ReadTool()

    with pytest.raises(ValueError):
        tool.invoke(ToolCall(tool_name="read", arguments={}), context=ToolContext(workspace=tmp_path))


class _FakeArtifactFacade:
    """Minimal RuntimeArtifactReadFacade stand-in mirroring bounded read semantics."""

    def __init__(self, artifact_id: str, content: str) -> None:
        self._artifact_id = artifact_id
        self._content = content

    def read_artifact(
        self,
        *,
        caller_session_id: str,
        artifact_id: str,
        offset: int | None = None,
        limit: int | None = None,
    ) -> dict[str, object] | None:
        _ = caller_session_id
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


def test_read_tool_rejects_unknown_artifact_id(tmp_path: Path) -> None:
    facade = _FakeArtifactFacade(_ARTIFACT_ID, "content")
    tool = ReadTool()

    context = ToolContext(workspace=tmp_path, session_id="session-1", artifact=facade)
    with pytest.raises(ValueError):
        tool.invoke(
            ToolCall(
                tool_name="read",
                arguments={"path": "voidcode://artifact/artifact_ffffffffffffffffffffffff"},
            ),
            context=context,
        )
