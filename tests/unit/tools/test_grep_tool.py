from __future__ import annotations

from pathlib import Path

import pytest

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
    assert result.content == (
        "Found 2 match(es) for 'alpha' in sample.txt\nsample.txt:1: alpha beta\nsample.txt:3: alpha"
    )
    assert result.data == {
        "path": "sample.txt",
        "pattern": "alpha",
        "regex": False,
        "context": 0,
        "match_count": 2,
        "truncated": False,
        "partial": False,
        "matches": [
            {
                "file": "sample.txt",
                "line": 1,
                "text": "alpha beta",
                "columns": [1],
                "before": [],
                "after": [],
            },
            {
                "file": "sample.txt",
                "line": 3,
                "text": "alpha",
                "columns": [1],
                "before": [],
                "after": [],
            },
        ],
    }


def test_grep_tool_supports_regex_context_and_include_exclude(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    sample = src / "sample.py"
    _ = sample.write_text("alpha\nbeta\nalpha\n", encoding="utf-8")
    ignored = src / "ignored.txt"
    _ = ignored.write_text("alpha\n", encoding="utf-8")
    tool = GrepTool()

    result = tool.invoke(
        ToolCall(
            tool_name="grep",
            arguments={
                "pattern": "^alpha$",
                "path": "src",
                "regex": True,
                "context": 1,
                "include": ["**/*.py"],
                "exclude": ["**/ignored.*"],
            },
        ),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert result.data["regex"] is True
    assert result.data["context"] == 1
    assert result.data["match_count"] == 2
    assert result.data["matches"] == [
        {
            "file": "src/sample.py",
            "line": 1,
            "text": "alpha",
            "columns": [1],
            "before": [],
            "after": [{"line": 2, "text": "beta"}],
        },
        {
            "file": "src/sample.py",
            "line": 3,
            "text": "alpha",
            "columns": [1],
            "before": [{"line": 2, "text": "beta"}],
            "after": [],
        },
    ]
    assert "ignored.txt" not in (result.content or "")


def test_grep_tool_searches_top_level_files_in_directory_targets_by_default(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    _ = (src / "sample.py").write_text("alpha\n", encoding="utf-8")
    tool = GrepTool()

    result = tool.invoke(
        ToolCall(tool_name="grep", arguments={"pattern": "alpha", "path": "src"}),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert result.data["match_count"] == 1
    assert result.data["matches"] == [
        {
            "file": "src/sample.py",
            "line": 1,
            "text": "alpha",
            "columns": [1],
            "before": [],
            "after": [],
        }
    ]


def test_grep_tool_sorts_directory_targets_deterministically(tmp_path: Path) -> None:
    src = tmp_path / "src"
    nested = src / "nested"
    nested.mkdir(parents=True)
    _ = (nested / "zeta.py").write_text("alpha\n", encoding="utf-8")
    _ = (src / "alpha.py").write_text("alpha\n", encoding="utf-8")
    _ = (nested / "beta.py").write_text("alpha\n", encoding="utf-8")
    tool = GrepTool()

    result = tool.invoke(
        ToolCall(tool_name="grep", arguments={"pattern": "alpha", "path": "src"}),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert [match["file"] for match in result.data["matches"]] == [
        "src/alpha.py",
        "src/nested/beta.py",
        "src/nested/zeta.py",
    ]
    assert result.content == (
        "Found 3 match(es) for 'alpha' in src\n"
        "src/alpha.py:1: alpha\n"
        "src/nested/beta.py:1: alpha\n"
        "src/nested/zeta.py:1: alpha"
    )


def test_grep_tool_ignores_common_directories_by_default(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    _ = (src / "keep.py").write_text("alpha\n", encoding="utf-8")

    ignored_dirs = [".git", "node_modules", "__pycache__", "dist", "build"]
    for dirname in ignored_dirs:
        ignored_dir = tmp_path / dirname
        ignored_dir.mkdir()
        _ = (ignored_dir / "ignored.py").write_text("alpha\n", encoding="utf-8")

    tool = GrepTool()
    result = tool.invoke(
        ToolCall(tool_name="grep", arguments={"pattern": "alpha", "path": "."}),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert [match["file"] for match in result.data["matches"]] == ["src/keep.py"]
    assert ".git/ignored.py" not in (result.content or "")
    assert "node_modules/ignored.py" not in (result.content or "")


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
        "regex": False,
        "context": 0,
        "match_count": 0,
        "truncated": False,
        "partial": False,
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

    result = tool.invoke(
        ToolCall(tool_name="grep", arguments={"pattern": "x", "path": "sample.bin"}),
        workspace=tmp_path,
    )
    assert result.status == "ok"
    assert result.data["match_count"] == 0


def test_tools_package_and_default_registry_export_grep_tool() -> None:
    registry = ToolRegistry.with_defaults()

    assert "GrepTool" in __import__("voidcode.tools", fromlist=["__all__"]).__all__
    assert registry.resolve("grep").definition.name == "grep"
    assert registry.resolve("grep").definition.read_only is True
