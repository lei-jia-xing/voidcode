from __future__ import annotations

from pathlib import Path

import pytest

from voidcode.tools import ReadTool, ToolCall
from voidcode.tools.runtime_context import RuntimeToolInvocationContext, bind_runtime_tool_context


def test_read_tool_reads_text_file_with_offset_and_limit(tmp_path: Path) -> None:
    sample = tmp_path / "sample.txt"
    _ = sample.write_text("alpha\nbeta\ngamma\ndelta\n", encoding="utf-8")
    tool = ReadTool()

    result = tool.invoke(
        ToolCall(tool_name="read", arguments={"path": "sample.txt", "offset": 2, "limit": 2}),
        workspace=tmp_path,
    )

    assert result.tool_name == "read"
    assert result.status == "ok"
    assert result.content == "Read 2 line(s) from sample.txt; output is truncated."
    assert result.data["raw_content"] == "beta\ngamma"
    assert result.data["path"] == "sample.txt"
    assert result.data["offset"] == 2
    assert result.data["limit"] == 2
    assert result.data["next_offset"] == 4
    assert "copy_guidance" not in result.data


def test_read_tool_rejects_directories_with_suggestions(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "b.txt").write_text("b", encoding="utf-8")
    subdir = tmp_path / "subdir"
    subdir.mkdir()

    tool = ReadTool()

    with pytest.raises(ValueError, match="does not support directories") as exc_info:
        tool.invoke(ToolCall(tool_name="read", arguments={"path": "."}), workspace=tmp_path)

    assert "Did you mean:" in str(exc_info.value)


def test_read_tool_allows_workspace_escape_path_with_absolute_display(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-read.txt"
    outside.write_text("outside", encoding="utf-8")
    tool = ReadTool()

    result = tool.invoke(
        ToolCall(tool_name="read", arguments={"path": "../outside-read.txt"}),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert result.data["path"] == str(outside.resolve())


def test_read_tool_reports_missing_file_path(tmp_path: Path) -> None:
    tool = ReadTool()
    missing_file_path_error = (
        r"read Validation error: path: "
        r"Input should be a valid string \(received NoneType\)"
        r"\. Please retry with corrected arguments that satisfy the tool schema\."
    )

    with pytest.raises(ValueError, match=missing_file_path_error):
        tool.invoke(ToolCall(tool_name="read", arguments={}), workspace=tmp_path)


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


def test_read_tool_rejects_unknown_artifact_id(tmp_path: Path) -> None:
    facade = _FakeArtifactFacade(_ARTIFACT_ID, "content")
    tool = ReadTool()

    with bind_runtime_tool_context(RuntimeToolInvocationContext(session_id="session-1", artifact=facade)):
        with pytest.raises(ValueError, match="artifact not found in current session"):
            tool.invoke(
                ToolCall(
                    tool_name="read",
                    arguments={"path": "voidcode://artifact/artifact_ffffffffffffffffffffffff"},
                ),
                workspace=tmp_path,
            )
