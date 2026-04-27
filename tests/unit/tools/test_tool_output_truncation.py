from __future__ import annotations

from pathlib import Path

from voidcode.tools import (
    ToolResult,
    cap_tool_result_output,
    sanitize_tool_arguments,
    sanitize_tool_result_data,
)


def test_cap_tool_result_output_noops_under_limits(tmp_path: Path) -> None:
    result = ToolResult(tool_name="sample", status="ok", content="small output")

    capped = cap_tool_result_output(result, workspace=tmp_path)

    assert capped is result
    assert not (tmp_path / ".voidcode" / "tool-output").exists()


def test_cap_tool_result_output_caps_by_line_count_and_saves_full_output(tmp_path: Path) -> None:
    content = "".join(f"line-{index}\n" for index in range(6))
    result = ToolResult(tool_name="sample", status="ok", content=content)

    capped = cap_tool_result_output(result, workspace=tmp_path, max_lines=3, max_bytes=10_000)

    assert capped.content is not None
    assert "line-0" in capped.content
    assert "line-3" not in capped.content
    assert "Tool output truncated" in capped.content
    assert capped.truncated is True
    assert capped.partial is True
    assert isinstance(capped.reference, str)
    assert (tmp_path / capped.reference).read_text(encoding="utf-8") == content
    assert capped.data["output_path"] == capped.reference
    assert capped.data["original_line_count"] == 6


def test_cap_tool_result_output_caps_by_utf8_byte_count_safely(tmp_path: Path) -> None:
    content = "π" * 100
    result = ToolResult(tool_name="unicode", status="ok", content=content)

    capped = cap_tool_result_output(result, workspace=tmp_path, max_lines=2000, max_bytes=51)

    assert capped.content is not None
    assert "�" not in capped.content
    assert "Tool output truncated" in capped.content
    assert isinstance(capped.reference, str)
    assert (tmp_path / capped.reference).read_text(encoding="utf-8") == content


def test_cap_tool_result_output_skips_errors_and_empty_content(tmp_path: Path) -> None:
    empty = ToolResult(tool_name="sample", status="ok", content="")

    assert cap_tool_result_output(empty, workspace=tmp_path) is empty


def test_cap_tool_result_output_caps_large_errors(tmp_path: Path) -> None:
    error_text = "".join(f"error-{index}\n" for index in range(10))
    result = ToolResult(tool_name="sample", status="error", error=error_text)

    capped = cap_tool_result_output(result, workspace=tmp_path, max_lines=3, max_bytes=10_000)

    assert capped.error is not None
    assert "error-0" in capped.error
    assert "error-4" not in capped.error
    assert "Tool error truncated" in capped.error
    assert capped.truncated is True
    assert isinstance(capped.reference, str)
    assert (tmp_path / capped.reference).read_text(encoding="utf-8") == error_text


def test_sanitize_tool_arguments_omits_sensitive_text_fields() -> None:
    sanitized = sanitize_tool_arguments(
        {
            "path": "sample.txt",
            "content": "secret contents",
            "edits": [{"oldString": "old", "newString": "new"}],
        }
    )

    assert sanitized["path"] == "sample.txt"
    assert sanitized["content"] == {"omitted": True, "byte_count": 15, "line_count": 1}
    edits = sanitized["edits"]
    assert isinstance(edits, list)
    assert edits[0] == {
        "oldString": {"omitted": True, "byte_count": 3, "line_count": 1},
        "newString": {"omitted": True, "byte_count": 3, "line_count": 1},
    }


def test_sanitize_tool_result_data_strips_inline_blobs_and_nested_arguments() -> None:
    data_uri = "data:image/png;base64," + "A" * 100
    sanitized = sanitize_tool_result_data(
        {
            "arguments": {"content": "raw file body", "path": "out.txt"},
            "attachment": {"mime": "image/png", "data_uri": data_uri},
            "todos": [{"content": "raw todo text", "status": "pending"}],
        }
    )

    assert sanitized["arguments"] == {
        "content": {"omitted": True, "byte_count": 13, "line_count": 1},
        "path": "out.txt",
    }
    attachment = sanitized["attachment"]
    assert isinstance(attachment, dict)
    assert attachment["mime"] == "image/png"
    assert attachment["data_uri"] == {
        "omitted": True,
        "byte_count": len(data_uri.encode("utf-8")),
        "line_count": 1,
    }
    todos = sanitized["todos"]
    assert isinstance(todos, list)
    assert todos[0] == {
        "content": {"omitted": True, "byte_count": 13, "line_count": 1},
        "status": "pending",
    }
