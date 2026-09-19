from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from voidcode.tools import ToolCall, WriteTool
from voidcode.tools._repair import ToolDiagnosticError
from voidcode.tools.runtime_context import RuntimeToolInvocationContext, bind_runtime_tool_context


def _content_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_write_tool_writes_utf8_content_inside_workspace(tmp_path: Path) -> None:
    tool = WriteTool()

    result = tool.invoke(
        ToolCall(
            tool_name="write",
            arguments={"path": "nested/output.txt", "content": "hello utf8 π"},
        ),
        workspace=tmp_path,
    )

    assert (tmp_path / "nested" / "output.txt").read_text(encoding="utf-8") == "hello utf8 π"
    assert result.tool_name == "write"
    assert result.status == "ok"
    assert result.content == "Wrote file successfully: nested/output.txt"
    assert result.data == {
        "path": "nested/output.txt",
        "byte_count": len("hello utf8 π".encode()),
        "diff": "--- a/nested/output.txt\n+++ b/nested/output.txt\n@@ -0,0 +1 @@\n+hello utf8 π",
    }


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
        workspace=tmp_path,
    )

    assert result.data["diff"] == ("--- a/note.txt\n+++ b/note.txt\n@@ -1 +1 @@\n-old\n+new\n")


def test_write_tool_rejects_non_string_arguments(tmp_path: Path) -> None:
    tool = WriteTool()

    with pytest.raises(
        ValueError,
        match=(
            r"write Validation error: path: Input should be a valid string \(received int\)\. "
            r"Please retry with corrected arguments that satisfy the tool schema\."
        ),
    ):
        tool.invoke(
            ToolCall(tool_name="write", arguments={"path": 123, "content": "x"}),
            workspace=tmp_path,
        )

    with pytest.raises(
        ValueError,
        match=(
            r"write Validation error: content: Input should be a valid string "
            r"\(received int\)\. "
            r"Please retry with corrected arguments that satisfy the tool schema\."
        ),
    ):
        tool.invoke(
            ToolCall(tool_name="write", arguments={"path": "out.txt", "content": 123}),
            workspace=tmp_path,
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
            workspace=tmp_path,
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
    tool = WriteTool()
    resolved = target.resolve().as_posix()

    with bind_runtime_tool_context(
        RuntimeToolInvocationContext(
            session_id="test",
            read_paths=frozenset({resolved}),
            read_lines={resolved: frozenset({1, 2, 3})},
        )
    ):
        result = tool.invoke(
            ToolCall(
                tool_name="write",
                arguments={"path": "note.txt", "content": "replacement", "expectedHash": _content_hash(target)},
            ),
            workspace=tmp_path,
        )

    assert result.status == "ok"
    assert target.read_text(encoding="utf-8") == "replacement"
