from __future__ import annotations

from pathlib import Path

import pytest

from voidcode.runtime.service import ToolRegistry
from voidcode.tools import ListTool, ToolCall


def test_list_tool_shows_files_and_directories(tmp_path: Path) -> None:
    (tmp_path / "file1.txt").write_text("content1", encoding="utf-8")
    (tmp_path / "file2.txt").write_text("content2", encoding="utf-8")
    subdir = tmp_path / "subdir"
    subdir.mkdir()
    (subdir / "nested.txt").write_text("nested", encoding="utf-8")

    tool = ListTool()

    result = tool.invoke(
        ToolCall(tool_name="list", arguments={}),
        workspace=tmp_path,
    )
    assert result.content is not None

    assert result.tool_name == "list"
    assert result.status == "ok"
    assert "file1.txt" in result.content
    assert "file2.txt" in result.content
    assert "subdir/" in result.content
    assert "nested.txt" in result.content


def test_list_tool_defaults_to_root(tmp_path: Path) -> None:
    (tmp_path / "root.txt").write_text("root", encoding="utf-8")
    subdir = tmp_path / "subdir"
    subdir.mkdir()
    (subdir / "nested.txt").write_text("nested", encoding="utf-8")

    tool = ListTool()

    result = tool.invoke(
        ToolCall(tool_name="list", arguments={}),
        workspace=tmp_path,
    )
    assert result.content is not None

    assert "root.txt" in result.content
    assert "subdir/" in result.content
    assert "nested.txt" in result.content


def test_list_tool_respects_path_argument(tmp_path: Path) -> None:
    (tmp_path / "root.txt").write_text("root", encoding="utf-8")
    subdir = tmp_path / "subdir"
    subdir.mkdir()
    (subdir / "nested.txt").write_text("nested", encoding="utf-8")

    tool = ListTool()

    result = tool.invoke(
        ToolCall(tool_name="list", arguments={"path": "subdir"}),
        workspace=tmp_path,
    )
    assert result.content is not None

    assert "nested.txt" in result.content
    assert "root.txt" not in result.content


def test_list_tool_allows_path_outside_workspace(tmp_path: Path) -> None:
    tool = ListTool()
    outside = tmp_path.parent / "outside-list"
    outside.mkdir(exist_ok=True)
    (outside / "x.txt").write_text("x", encoding="utf-8")

    result = tool.invoke(
        ToolCall(tool_name="list", arguments={"path": str(outside)}),
        workspace=tmp_path,
    )
    assert result.status == "ok"
    assert result.data["path"] == str(outside.resolve())
    assert "x.txt" in (result.content or "")


def test_list_tool_rejects_nonexistent_path(tmp_path: Path) -> None:
    tool = ListTool()

    with pytest.raises(ValueError, match="does not exist"):
        tool.invoke(
            ToolCall(tool_name="list", arguments={"path": "missing"}),
            workspace=tmp_path,
        )


def test_list_tool_rejects_non_directory_path(tmp_path: Path) -> None:
    file_path = tmp_path / "file.txt"
    file_path.write_text("content", encoding="utf-8")

    tool = ListTool()

    with pytest.raises(ValueError, match="not a directory"):
        tool.invoke(
            ToolCall(tool_name="list", arguments={"path": "file.txt"}),
            workspace=tmp_path,
        )


def test_list_tool_ignores_common_directories(tmp_path: Path) -> None:
    (tmp_path / "code.py").write_text("print('code')", encoding="utf-8")
    node_modules = tmp_path / "node_modules"
    node_modules.mkdir()
    (node_modules / "dep.js").write_text("// dependency", encoding="utf-8")

    tool = ListTool()

    result = tool.invoke(
        ToolCall(tool_name="list", arguments={}),
        workspace=tmp_path,
    )
    assert result.content is not None

    assert "code.py" in result.content
    assert "node_modules" not in result.content
    assert "dep.js" not in result.content


def test_tools_package_and_default_registry_export_list_tool() -> None:
    registry = ToolRegistry.with_defaults()

    assert "ListTool" in __import__("voidcode.tools", fromlist=["__all__"]).__all__
    assert registry.resolve("list").definition.name == "list"
    assert registry.resolve("list").definition.read_only is True


def test_list_tool_glob_ignore_does_not_match_prefix_siblings(tmp_path: Path) -> None:
    build_dir = tmp_path / "build"
    build_dir.mkdir()
    (build_dir / "in_build.txt").write_text("x", encoding="utf-8")

    build2_dir = tmp_path / "build2"
    build2_dir.mkdir()
    (build2_dir / "in_build2.txt").write_text("x", encoding="utf-8")

    tool = ListTool()
    result = tool.invoke(
        ToolCall(tool_name="list", arguments={"ignore": ["build/**"]}),
        workspace=tmp_path,
    )
    assert result.content is not None

    assert "build/" not in result.content
    assert "in_build.txt" not in result.content
    assert "build2/" in result.content
    assert "in_build2.txt" in result.content
