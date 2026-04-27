from __future__ import annotations

import subprocess
import sys
import urllib.error
from pathlib import Path
from typing import cast
from unittest.mock import patch

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from voidcode.tools import (
    ApplyPatchTool,
    EditTool,
    GlobTool,
    ListTool,
    MultiEditTool,
    TodoWriteTool,
    ToolCall,
    WebFetchTool,
)
from voidcode.tools.code_search import CodeSearchTool
from voidcode.tools.web_search import WebSearchTool


def _run_git(args: list[str], *, cwd: Path) -> None:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stdout)


def test_apply_patch_tool_applies_diff_in_real_git_workspace(tmp_path: Path) -> None:
    _run_git(["init"], cwd=tmp_path)

    target = tmp_path / "hello.txt"
    target.write_text("hello old\n", encoding="utf-8")

    patch = "\n".join(
        [
            "diff --git a/hello.txt b/hello.txt",
            "index 0000000..1111111 100644",
            "--- a/hello.txt",
            "+++ b/hello.txt",
            "@@ -1 +1 @@",
            "-hello old",
            "+hello new",
            "",
        ]
    )

    tool = ApplyPatchTool()
    result = tool.invoke(
        ToolCall(tool_name="apply_patch", arguments={"patch": patch}),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert target.read_text(encoding="utf-8") == "hello new\n"
    assert result.data.get("count") == 1
    assert result.data.get("changes") == [{"path": "hello.txt", "status": "M"}]
    assert result.content == "M hello.txt"
    assert not (tmp_path / ".voidcode_apply_patch.patch").exists()


def test_apply_patch_tool_cleans_temp_patch_file_on_failure(tmp_path: Path) -> None:
    _run_git(["init"], cwd=tmp_path)
    (tmp_path / "file.txt").write_text("content\n", encoding="utf-8")

    tool = ApplyPatchTool()
    with pytest.raises(ValueError):
        tool.invoke(
            ToolCall(tool_name="apply_patch", arguments={"patch": "not a patch"}),
            workspace=tmp_path,
        )

    assert not (tmp_path / ".voidcode_apply_patch.patch").exists()


def test_edit_tool_multiple_match_guard_and_replace_all(tmp_path: Path) -> None:
    target = tmp_path / "edit.txt"
    target.write_text("foo x foo\n", encoding="utf-8")

    tool = EditTool()
    with pytest.raises(ValueError, match="Multiple matches found"):
        tool.invoke(
            ToolCall(
                tool_name="edit",
                arguments={"path": "edit.txt", "oldString": "foo", "newString": "bar"},
            ),
            workspace=tmp_path,
        )

    assert target.read_text(encoding="utf-8") == "foo x foo\n"

    result = tool.invoke(
        ToolCall(
            tool_name="edit",
            arguments={
                "path": "edit.txt",
                "oldString": "foo",
                "newString": "bar",
                "replaceAll": True,
            },
        ),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert target.read_text(encoding="utf-8") == "bar x bar\n"
    assert result.data.get("match_count") == 2


def test_web_fetch_tool_blocks_localhost_integration(tmp_path: Path) -> None:
    _ = tmp_path
    tool = WebFetchTool()

    with pytest.raises(ValueError, match="blocked"):
        tool.invoke(
            ToolCall(
                tool_name="web_fetch",
                arguments={"url": "http://127.0.0.1:8123", "format": "text"},
            ),
            workspace=Path("."),
        )


def test_multi_edit_tool_applies_ordered_edits_integration(tmp_path: Path) -> None:
    target = tmp_path / "multi.txt"
    target.write_text("a1\na2\na1\n", encoding="utf-8")

    tool = MultiEditTool()
    result = tool.invoke(
        ToolCall(
            tool_name="multi_edit",
            arguments={
                "path": "multi.txt",
                "edits": [
                    {"oldString": "a1", "newString": "A1", "replaceAll": True},
                    {"oldString": "a2", "newString": "A2"},
                ],
            },
        ),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert target.read_text(encoding="utf-8") == "A1\nA2\nA1\n"
    assert result.data.get("applied") == 2


def test_todo_write_tool_persists_summary_integration(tmp_path: Path) -> None:
    tool = TodoWriteTool()
    result = tool.invoke(
        ToolCall(
            tool_name="todo_write",
            arguments={
                "todos": [
                    {"content": "task1", "status": "pending", "priority": "high"},
                    {"content": "task2", "status": "in_progress", "priority": "medium"},
                    {"content": "task3", "status": "completed", "priority": "low"},
                ]
            },
        ),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    summary_raw = result.data.get("summary")
    assert isinstance(summary_raw, dict)
    summary = cast(dict[str, object], summary_raw)
    assert summary.get("total") == 3
    assert summary.get("pending") == 1
    assert summary.get("in_progress") == 1
    assert summary.get("completed") == 1
    assert not (tmp_path / ".voidcode" / "todos.json").exists()
    todos_raw = result.data.get("todos")
    assert isinstance(todos_raw, list)


def test_glob_and_list_tools_handle_paths_and_ignores_integration(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('x')\n", encoding="utf-8")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "generated.py").write_text("print('g')\n", encoding="utf-8")

    glob_tool = GlobTool()
    glob_result = glob_tool.invoke(
        ToolCall(tool_name="glob", arguments={"pattern": "**/*.py"}),
        workspace=tmp_path,
    )
    assert glob_result.content is not None

    assert glob_result.status == "ok"
    assert "src/main.py" in glob_result.content
    assert "build/generated.py" not in glob_result.content

    list_tool = ListTool()
    list_result = list_tool.invoke(
        ToolCall(tool_name="list", arguments={"ignore": ["build/**"]}),
        workspace=tmp_path,
    )
    assert list_result.content is not None

    assert list_result.status == "ok"
    assert "src/" in list_result.content
    assert "build/" not in list_result.content


def test_web_search_tool_uses_fallback_when_no_exa_key_integration(tmp_path: Path) -> None:
    _ = tmp_path
    tool = WebSearchTool()

    html = (
        '<a class="result__a" href="https://example.com/a">Result A</a>'
        '<a class="result__snippet">Snippet A</a>'
    )

    response = httpx.Response(
        200,
        text=html,
        request=httpx.Request("GET", "https://html.duckduckgo.com/html/?q=voidcode+tools&kl=wt-wt"),
    )

    with patch("httpx.Client.get", return_value=response):
        result = tool.invoke(
            ToolCall(tool_name="web_search", arguments={"query": "voidcode tools"}),
            workspace=Path("."),
        )

    assert result.status == "ok"
    assert result.data.get("source") in {"duckduckgo", "exa"}
    assert isinstance(result.content, str)


def test_code_search_tool_fallback_on_network_error_integration(tmp_path: Path) -> None:
    _ = tmp_path
    tool = CodeSearchTool()

    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("offline")):
        result = tool.invoke(
            ToolCall(tool_name="code_search", arguments={"query": "python dataclass"}),
            workspace=Path("."),
        )

    assert result.status == "ok"
    assert result.data.get("source") == "duckduckgo_fallback"
