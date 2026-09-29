from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from voidcode.tools import GlobTool, ToolCall


def test_glob_tool_finds_matching_files(tmp_path: Path) -> None:
    (tmp_path / "test.py").write_text("print('test')", encoding="utf-8")
    (tmp_path / "main.py").write_text("print('main')", encoding="utf-8")
    (tmp_path / "README.md").write_text("# Readme", encoding="utf-8")
    (tmp_path / "data.txt").write_text("data", encoding="utf-8")

    tool = GlobTool()

    result = tool.invoke(
        ToolCall(tool_name="glob", arguments={"pattern": "*.py"}),
        workspace=tmp_path,
    )
    content = cast(list[str], result.data["matches"])

    assert result.tool_name == "glob"
    assert result.status == "ok"
    assert "test.py" in content
    assert "main.py" in content
    assert "README.md" not in content
    assert "data.txt" not in content
    assert result.data["pattern"] == "*.py"
    assert result.data["count"] == 2


def test_glob_tool_returns_no_files_when_none_match(tmp_path: Path) -> None:
    (tmp_path / "test.py").write_text("print('test')", encoding="utf-8")

    tool = GlobTool()

    result = tool.invoke(
        ToolCall(tool_name="glob", arguments={"pattern": "*.md"}),
        workspace=tmp_path,
    )

    assert result.content == "Found 0 file(s)."
    assert result.data["count"] == 0


def test_glob_tool_rejects_empty_pattern(tmp_path: Path) -> None:
    tool = GlobTool()

    with pytest.raises(ValueError, match="must not be empty"):
        tool.invoke(
            ToolCall(tool_name="glob", arguments={"pattern": ""}),
            workspace=tmp_path,
        )


def test_glob_tool_respects_path_argument(tmp_path: Path) -> None:
    subdir = tmp_path / "subdir"
    subdir.mkdir()
    (tmp_path / "root.txt").write_text("root", encoding="utf-8")
    (subdir / "nested.txt").write_text("nested", encoding="utf-8")

    tool = GlobTool()

    result = tool.invoke(
        ToolCall(tool_name="glob", arguments={"pattern": "*.txt", "path": "subdir"}),
        workspace=tmp_path,
    )
    content = cast(list[str], result.data["matches"])

    assert "subdir/nested.txt" in content
    assert "root.txt" not in content


def test_glob_tool_ignores_common_directories(tmp_path: Path) -> None:
    (tmp_path / "code.py").write_text("print('code')", encoding="utf-8")
    node_modules = tmp_path / "node_modules"
    node_modules.mkdir()
    (node_modules / "dep.js").write_text("// dependency", encoding="utf-8")

    tool = GlobTool()

    result = tool.invoke(
        ToolCall(tool_name="glob", arguments={"pattern": "**/*.js"}),
        workspace=tmp_path,
    )
    content = cast(str, result.content)

    assert "dep.js" not in content


def test_glob_tool_applies_hidden_gitignore_and_limit_filters(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("generated.py\n", encoding="utf-8")
    (tmp_path / "keep.py").write_text("keep", encoding="utf-8")
    (tmp_path / ".hidden.py").write_text("hidden", encoding="utf-8")
    (tmp_path / "generated.py").write_text("generated", encoding="utf-8")
    (tmp_path / "other.py").write_text("other", encoding="utf-8")

    tool = GlobTool()

    filtered = tool.invoke(
        ToolCall(
            tool_name="glob",
            arguments={"pattern": "*.py", "include_hidden": False, "respect_gitignore": True},
        ),
        workspace=tmp_path,
    )
    visible = cast(list[str], filtered.data["matches"])
    assert ".hidden.py" not in visible
    assert "generated.py" not in visible
    assert "keep.py" in visible

    bounded = tool.invoke(
        ToolCall(tool_name="glob", arguments={"pattern": "*.py", "include_hidden": False, "limit": 1}),
        workspace=tmp_path,
    )
    assert bounded.data["count"] == 1
    assert bounded.data["truncated"] is True
