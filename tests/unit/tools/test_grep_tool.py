from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from voidcode.core.tool_context import ToolContext
from voidcode.tools.contracts import ToolCall
from voidcode.tools.grep import GrepTool


def test_grep_tool_searches_utf8_file_inside_workspace(tmp_path: Path) -> None:
    sample_file = tmp_path / "sample.txt"
    _ = sample_file.write_text("alpha beta\nbeta\nalpha\n", encoding="utf-8")
    tool = GrepTool()

    result = tool.invoke(
        ToolCall(tool_name="grep", arguments={"pattern": "alpha", "path": "sample.txt"}),
        context=ToolContext(workspace=tmp_path),
    )

    assert result.tool_name == "grep"
    assert result.status == "ok"
    assert result.data == {
        "path": "sample.txt",
        "pattern": "alpha",
        "regex": False,
        "ignore_case": False,
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
        context=ToolContext(workspace=tmp_path),
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
        context=ToolContext(workspace=tmp_path),
    )

    assert result.status == "ok"
    matches = cast(list[dict[str, object]], result.data["matches"])
    assert [match["file"] for match in matches] == ["src/keep.py"]
    assert ".git/ignored.py" not in (result.content or "")
    assert "node_modules/ignored.py" not in (result.content or "")


def test_grep_tool_returns_zero_matches_summary(tmp_path: Path) -> None:
    sample_file = tmp_path / "sample.txt"
    _ = sample_file.write_text("alpha beta\n", encoding="utf-8")
    tool = GrepTool()

    result = tool.invoke(
        ToolCall(tool_name="grep", arguments={"pattern": "missing", "path": "sample.txt"}),
        context=ToolContext(workspace=tmp_path),
    )

    data = dict(result.data)
    diagnostics = cast(list[dict[str, object]], data.pop("diagnostics"))
    assert data == {
        "path": "sample.txt",
        "pattern": "missing",
        "regex": False,
        "ignore_case": False,
        "context": 0,
        "match_count": 0,
        "truncated": False,
        "partial": False,
        "matches": [],
    }
    assert len(diagnostics) == 1
    assert {key: diagnostics[0][key] for key in ("source", "severity", "reason")} == {"source": "grep", "severity": "info", "reason": "no_matches"}


def test_grep_tool_rejects_invalid_arguments_and_non_utf8_files(tmp_path: Path) -> None:
    binary_file = tmp_path / "sample.bin"
    _ = binary_file.write_bytes(b"\xff\xfe\x00x")
    tool = GrepTool()

    with pytest.raises(ValueError):
        tool.invoke(
            ToolCall(tool_name="grep", arguments={"pattern": 123, "path": "sample.txt"}),
            context=ToolContext(workspace=tmp_path),
        )

    with pytest.raises(ValueError):
        tool.invoke(
            ToolCall(tool_name="grep", arguments={"pattern": "alpha", "path": 123}),
            context=ToolContext(workspace=tmp_path),
        )

    with pytest.raises(ValueError):
        tool.invoke(
            ToolCall(tool_name="grep", arguments={"pattern": "", "path": "sample.txt"}),
            context=ToolContext(workspace=tmp_path),
        )

    outside = tmp_path.parent / "outside-grep.txt"
    outside.write_text("alpha\n", encoding="utf-8")
    external = tool.invoke(
        ToolCall(tool_name="grep", arguments={"pattern": "alpha", "path": str(outside)}),
        context=ToolContext(workspace=tmp_path),
    )
    assert external.status == "ok"
    assert external.data["path"] == str(outside.resolve())

    result = tool.invoke(
        ToolCall(tool_name="grep", arguments={"pattern": "x", "path": "sample.bin"}),
        context=ToolContext(workspace=tmp_path),
    )
    assert result.status == "ok"
    assert result.data["match_count"] == 0


def test_grep_tool_supports_case_insensitive_and_gitignore_skip(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("skipped.py\n", encoding="utf-8")
    (tmp_path / "sample.py").write_text("ALPHA\n", encoding="utf-8")
    (tmp_path / "skipped.py").write_text("ALPHA\n", encoding="utf-8")
    tool = GrepTool()

    result = tool.invoke(
        ToolCall(
            tool_name="grep",
            arguments={"pattern": "alpha", "path": ".", "ignore_case": True, "respect_gitignore": True},
        ),
        context=ToolContext(workspace=tmp_path),
    )

    assert result.status == "ok"
    assert [cast(dict[str, object], match)["file"] for match in cast(list[object], result.data["matches"])] == ["sample.py"]

    case_sensitive = tool.invoke(
        ToolCall(tool_name="grep", arguments={"pattern": "alpha", "path": "sample.py"}),
        context=ToolContext(workspace=tmp_path),
    )
    assert case_sensitive.data["match_count"] == 0
