from __future__ import annotations

import hashlib
from pathlib import Path
from typing import cast

import pytest

from voidcode.tools import EditTool, ToolCall
from voidcode.tools._repair import ToolDiagnosticError
from voidcode.tools.runtime_context import RuntimeToolInvocationContext, bind_runtime_tool_context


def _content_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
                "expectedHash": _content_hash(file_path),
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
                "expectedHash": _content_hash(file_path),
            },
        ),
        workspace=tmp_path,
    )

    assert file_path.read_text(encoding="utf-8") == "qux bar qux baz qux"
    assert result.content is not None
    assert "3 occurrences replaced" in result.content


def test_edit_tool_rejects_multiple_exact_matches_without_replace_all(tmp_path: Path) -> None:
    file_path = tmp_path / "test.txt"
    file_path.write_text("foo bar foo", encoding="utf-8")

    tool = EditTool()

    with pytest.raises(ToolDiagnosticError, match="Multiple matches found") as exc_info:
        tool.invoke(
            ToolCall(
                tool_name="edit",
                arguments={
                    "path": "test.txt",
                    "oldString": "foo",
                    "newString": "qux",
                    "expectedHash": _content_hash(file_path),
                },
            ),
            workspace=tmp_path,
        )

    diagnostic = exc_info.value
    assert diagnostic.error_kind == "ambiguous_match"
    assert diagnostic.error_details["reason"] == "ambiguous_match"
    assert diagnostic.error_details["match_count"] == 2
    matches = cast(list[dict[str, object]], diagnostic.error_details["matches"])
    assert matches[0]["line_numbers"] == [1, 1]
    assert "foo" in str(matches[0]["preview"])
    assert isinstance(diagnostic.retry_guidance, str)
    assert diagnostic.retry_guidance
    assert "replaceAll" in diagnostic.retry_guidance


def test_edit_tool_rejects_when_old_string_not_found(tmp_path: Path) -> None:
    file_path = tmp_path / "test.txt"
    file_path.write_text("hello", encoding="utf-8")

    tool = EditTool()

    with pytest.raises(ToolDiagnosticError, match="Could not find oldString") as exc_info:
        tool.invoke(
            ToolCall(
                tool_name="edit",
                arguments={
                    "path": "test.txt",
                    "oldString": "missing",
                    "newString": "b",
                    "expectedHash": _content_hash(file_path),
                },
            ),
            workspace=tmp_path,
        )

    diagnostic = exc_info.value
    assert diagnostic.error_kind == "tool_input_mismatch"
    assert diagnostic.error_details["reason"] == "old_string_not_found"
    assert "Replacers attempted:" in str(diagnostic)
    message = str(diagnostic)
    assert "SimpleReplacer" in message
    assert "ContextAwareReplacer" in message
    assert "No nearby text match found" in message
    assert "attempted_replacers" in diagnostic.error_details
    assert diagnostic.error_details["line_number_prefix_suspected"] is False
    assert isinstance(diagnostic.retry_guidance, str)
    assert diagnostic.retry_guidance


def _read_lines_context(path: Path, lines: set[int]) -> RuntimeToolInvocationContext:
    resolved = path.resolve().as_posix()
    return RuntimeToolInvocationContext(
        session_id="test",
        read_paths=frozenset({resolved}),
        read_lines={resolved: frozenset(lines)},
    )


def test_edit_tool_rejects_edit_of_line_outside_read_window(tmp_path: Path) -> None:
    file_path = tmp_path / "sample.txt"
    file_path.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    tool = EditTool()

    with bind_runtime_tool_context(_read_lines_context(file_path, {1})):
        with pytest.raises(ToolDiagnosticError, match="never revealed by read") as exc_info:
            tool.invoke(
                ToolCall(
                    tool_name="edit",
                    arguments={"path": "sample.txt", "oldString": "gamma", "newString": "GAMMA", "expectedHash": _content_hash(file_path)},
                ),
                workspace=tmp_path,
            )

    diagnostic = exc_info.value
    assert diagnostic.error_kind == "tool_input_mismatch"
    assert diagnostic.error_details["reason"] == "unseen_range"
    assert diagnostic.error_details["path"] == "sample.txt"
    assert diagnostic.error_details["unseen_line_ranges"] == [{"start": 3, "end": 3}]
    assert "read" in (diagnostic.retry_guidance or "")
    assert file_path.read_text(encoding="utf-8") == "alpha\nbeta\ngamma\n"
