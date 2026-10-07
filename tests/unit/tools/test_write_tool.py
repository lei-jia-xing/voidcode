from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from voidcode.core.tool_context import ToolContext
from voidcode.tools._repair import ToolDiagnosticError
from voidcode.tools.contracts import TextOutput, ToolCall
from voidcode.tools.write import WriteResultBody, WriteTool


def _content_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_write_tool_writes_utf8_content_inside_workspace(tmp_path: Path) -> None:
    tool = WriteTool()

    result = tool.invoke(
        ToolCall(
            tool_name="write",
            arguments={"path": "nested/output.txt", "content": "hello utf8 π"},
        ),
        context=ToolContext(workspace=tmp_path),
    )

    assert (tmp_path / "nested" / "output.txt").read_text(encoding="utf-8") == "hello utf8 π"
    assert result.tool_name == "write"
    assert result.status == "ok"
    assert isinstance(result.output, TextOutput)
    assert result.output.text == "Wrote file successfully: nested/output.txt"
    assert isinstance(result.body, WriteResultBody)
    assert result.body.path == "nested/output.txt"
    assert result.body.byte_count == len("hello utf8 π".encode())
    assert result.body.diff == "--- a/nested/output.txt\n+++ b/nested/output.txt\n@@ -0,0 +1 @@\n+hello utf8 π"


def test_write_tool_returns_diff_for_rewrite(tmp_path: Path) -> None:
    note_path = tmp_path / "note.txt"
    note_path.write_text("old\n", encoding="utf-8")
    tool = WriteTool()

    result = tool.invoke(
        ToolCall(
            tool_name="write",
            arguments={
                "path": "note.txt",
                "content": "new\n",
                "expectedHash": _content_hash(note_path),
            },
        ),
        context=ToolContext(workspace=tmp_path),
    )

    assert isinstance(result.body, WriteResultBody)
    assert result.body.diff == ("--- a/note.txt\n+++ b/note.txt\n@@ -1 +1 @@\n-old\n+new\n")


def test_write_tool_rejects_non_string_arguments(tmp_path: Path) -> None:
    tool = WriteTool()

    with pytest.raises(ValueError):
        tool.invoke(
            ToolCall(tool_name="write", arguments={"path": 123, "content": "x"}),
            context=ToolContext(workspace=tmp_path),
        )

    with pytest.raises(ValueError):
        tool.invoke(
            ToolCall(tool_name="write", arguments={"path": "out.txt", "content": 123}),
            context=ToolContext(workspace=tmp_path),
        )


def test_write_tool_rejects_overwrite_without_expected_hash(tmp_path: Path) -> None:
    target = tmp_path / "note.txt"
    target.write_text("old\n", encoding="utf-8")
    tool = WriteTool()

    with pytest.raises(ToolDiagnosticError, match="expectedHash") as exc_info:
        tool.invoke(
            ToolCall(
                tool_name="write",
                arguments={"path": "note.txt", "content": "new\n"},
            ),
            context=ToolContext(workspace=tmp_path),
        )

    diagnostic = exc_info.value
    assert diagnostic.error_kind == "tool_input_mismatch"
    assert diagnostic.error_details["reason"] == "missing_expected_hash"
    assert diagnostic.error_details["path"] == "note.txt"
    assert "read" in (diagnostic.retry_guidance or "")
    assert target.read_text(encoding="utf-8") == "old\n"


def test_write_tool_allows_full_overwrite_after_full_file_read(tmp_path: Path) -> None:
    target = tmp_path / "note.txt"
    target.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    resolved = target.resolve().as_posix()
    read_hash = _content_hash(target)
    tool = WriteTool()
    result = tool.invoke(
        ToolCall(
            tool_name="write",
            arguments={"path": "note.txt", "content": "replacement", "expectedHash": read_hash},
        ),
        context=ToolContext(
            workspace=tmp_path,
            session_id="test",
            read_paths=frozenset({resolved}),
            read_lines={(resolved, read_hash): frozenset({1, 2, 3})},
            read_whole_files=frozenset({(resolved, read_hash)}),
            read_hash=read_hash,
        ),
    )

    assert result.status == "ok"
    assert target.read_text(encoding="utf-8") == "replacement"
