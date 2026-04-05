from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from voidcode.runtime.service import ToolRegistry
from voidcode.tools import GrepTool, ToolCall


def test_grep_tool_searches_utf8_file_inside_workspace(tmp_path: Path) -> None:
    sample_file = tmp_path / "sample.txt"
    _ = sample_file.write_text("alpha beta\nbeta\nalpha\n", encoding="utf-8")
    tool = GrepTool()

    result = tool.invoke(
        ToolCall(tool_name="grep", arguments={"pattern": "alpha", "path": "sample.txt"}),
        workspace=tmp_path,
    )

    assert result.tool_name == "grep"
    assert result.status == "ok"
    assert result.content == "Found 2 match(es) for 'alpha' in sample.txt\n1: alpha beta\n3: alpha"
    assert result.data == {
        "path": "sample.txt",
        "pattern": "alpha",
        "match_count": 2,
        "matches": [
            {"line": 1, "text": "alpha beta", "columns": [1]},
            {"line": 3, "text": "alpha", "columns": [1]},
        ],
    }


def test_grep_tool_returns_zero_matches_summary(tmp_path: Path) -> None:
    sample_file = tmp_path / "sample.txt"
    _ = sample_file.write_text("alpha beta\n", encoding="utf-8")
    tool = GrepTool()

    result = tool.invoke(
        ToolCall(tool_name="grep", arguments={"pattern": "missing", "path": "sample.txt"}),
        workspace=tmp_path,
    )

    assert result.content == "Found 0 match(es) for 'missing' in sample.txt"
    assert result.data == {
        "path": "sample.txt",
        "pattern": "missing",
        "match_count": 0,
        "matches": [],
    }


def test_grep_tool_rejects_invalid_arguments_and_non_utf8_files(tmp_path: Path) -> None:
    binary_file = tmp_path / "sample.bin"
    _ = binary_file.write_bytes(b"\xff\xfe\x00x")
    tool = GrepTool()

    with pytest.raises(ValueError, match="string pattern"):
        tool.invoke(
            ToolCall(tool_name="grep", arguments={"pattern": 123, "path": "sample.txt"}),
            workspace=tmp_path,
        )

    with pytest.raises(ValueError, match="string path"):
        tool.invoke(
            ToolCall(tool_name="grep", arguments={"pattern": "alpha", "path": 123}),
            workspace=tmp_path,
        )

    with pytest.raises(ValueError, match="must not be empty"):
        tool.invoke(
            ToolCall(tool_name="grep", arguments={"pattern": "", "path": "sample.txt"}),
            workspace=tmp_path,
        )

    with pytest.raises(ValueError, match="inside the workspace"):
        tool.invoke(
            ToolCall(tool_name="grep", arguments={"pattern": "alpha", "path": "../escape.txt"}),
            workspace=tmp_path,
        )

    with pytest.raises(ValueError, match="UTF-8 text files"):
        tool.invoke(
            ToolCall(tool_name="grep", arguments={"pattern": "x", "path": "sample.bin"}),
            workspace=tmp_path,
        )


def test_tools_package_and_default_registry_export_grep_tool() -> None:
    registry = ToolRegistry.with_defaults()

    assert "GrepTool" in __import__("voidcode.tools", fromlist=["__all__"]).__all__
    assert registry.resolve("grep").definition.name == "grep"
    assert registry.resolve("grep").definition.read_only is True
