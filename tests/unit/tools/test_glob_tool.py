from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from voidcode.runtime.service import ToolRegistry
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
    content = cast(str, result.content)

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

    assert result.content == "No files found"
    assert result.data["count"] == 0


def test_glob_tool_rejects_empty_pattern(tmp_path: Path) -> None:
    tool = GlobTool()

    with pytest.raises(ValueError, match="must not be empty"):
        tool.invoke(
            ToolCall(tool_name="glob", arguments={"pattern": ""}),
            workspace=tmp_path,
        )


def test_glob_tool_rejects_non_string_pattern(tmp_path: Path) -> None:
    tool = GlobTool()

    with pytest.raises(ValueError, match="string pattern"):
        tool.invoke(
            ToolCall(tool_name="glob", arguments={"pattern": 123}),
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
    content = cast(str, result.content)

    assert "nested.txt" in content
    assert "root.txt" not in content


def test_glob_tool_allows_path_outside_workspace(tmp_path: Path) -> None:
    tool = GlobTool()
    outside = tmp_path.parent / "outside-glob"
    outside.mkdir(exist_ok=True)
    (outside / "a.txt").write_text("a", encoding="utf-8")

    result = tool.invoke(
        ToolCall(tool_name="glob", arguments={"pattern": "*.txt", "path": str(outside)}),
        workspace=tmp_path,
    )
    assert result.status == "ok"
    assert result.data["path"] == str(outside.resolve())
    assert str((outside / "a.txt").resolve()) in cast(str, result.content)


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


def test_tools_package_and_default_registry_export_glob_tool() -> None:
    registry = ToolRegistry.with_defaults()

    assert "GlobTool" in __import__("voidcode.tools", fromlist=["__all__"]).__all__
    assert registry.resolve("glob").definition.name == "glob"
    assert registry.resolve("glob").definition.read_only is True
