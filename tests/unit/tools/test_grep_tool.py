from __future__ import annotations

from pathlib import Path

import pytest

from voidcode.core.tool_context import ToolContext
from voidcode.tools.contracts import TextOutput, ToolCall
from voidcode.tools.grep import GrepResultBody, GrepTool


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
    assert isinstance(result.body, GrepResultBody)
    body = result.body
    assert result.tool_name == "grep"
    assert result.status == "ok"
    assert body.path == "sample.txt"
    assert body.pattern == "alpha"
    assert body.regex is False
    assert body.ignore_case is False
    assert body.context == 0
    assert body.match_count == 2
    assert body.truncated is False
    assert [(match.file, match.line, match.text, match.columns, match.before, match.after) for match in body.matches] == [
        ("sample.txt", 1, "alpha beta", (1,), (), ()),
        ("sample.txt", 3, "alpha", (1,), (), ()),
    ]


def test_grep_renders_requested_matches_context_columns_and_limit_state(tmp_path: Path) -> None:
    sample = tmp_path / "sample.txt"
    sample.write_text("before\né hit hit\nmiddle\nlast hit\n" + "".join(f"hit {line}\n" for line in range(11)), encoding="utf-8")

    result = GrepTool().invoke(
        ToolCall(tool_name="grep", arguments={"pattern": "hit", "path": "sample.txt", "context": 1, "limit": 2}),
        context=ToolContext(workspace=tmp_path),
    )

    assert isinstance(result.body, GrepResultBody)
    assert isinstance(result.output, TextOutput)
    assert result.body.truncated is True
    assert [match.columns for match in result.body.matches] == [(3, 7), (6,)]
    assert "sample.txt:2:3,7: é hit hit" in result.output.text
    assert "sample.txt:4:6: last hit" in result.output.text
    assert "sample.txt:3:-: middle" in result.output.text
    assert result.output.text.count("sample.txt:3:-: middle") == 1


def test_grep_adjacent_selected_matches_keep_their_columns(tmp_path: Path) -> None:
    (tmp_path / "adjacent.txt").write_text("hit\nhit\n", encoding="utf-8")
    result = GrepTool().invoke(
        ToolCall(tool_name="grep", arguments={"pattern": "hit", "path": "adjacent.txt", "context": 1}),
        context=ToolContext(workspace=tmp_path),
    )

    assert isinstance(result.output, TextOutput)
    assert "adjacent.txt:1:1: hit" in result.output.text
    assert "adjacent.txt:2:1: hit" in result.output.text


def test_grep_exact_limit_and_more_than_limit(tmp_path: Path) -> None:
    (tmp_path / "sample.txt").write_text("hit\nmiss\nhit\n", encoding="utf-8")
    tool = GrepTool()
    exact = tool.invoke(
        ToolCall(tool_name="grep", arguments={"pattern": "hit", "path": "sample.txt", "limit": 2}), context=ToolContext(workspace=tmp_path)
    )
    more = tool.invoke(
        ToolCall(tool_name="grep", arguments={"pattern": "hit", "path": "sample.txt", "limit": 1}), context=ToolContext(workspace=tmp_path)
    )
    assert isinstance(exact.body, GrepResultBody) and exact.body.truncated is False
    assert isinstance(more.body, GrepResultBody) and more.body.truncated is True


def test_grep_renders_more_than_ten_selected_results(tmp_path: Path) -> None:
    (tmp_path / "many.txt").write_text("".join(f"hit {line}\n" for line in range(12)), encoding="utf-8")
    result = GrepTool().invoke(
        ToolCall(tool_name="grep", arguments={"pattern": "hit", "path": "many.txt", "limit": 20}),
        context=ToolContext(workspace=tmp_path),
    )
    assert isinstance(result.output, TextOutput)
    assert "many.txt:12:1: hit 11" in result.output.text


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

    assert isinstance(result.body, GrepResultBody)
    body = result.body
    assert body.regex is True
    assert body.context == 1
    assert body.match_count == 2
    assert [(match.file, match.line, match.text, match.columns, match.before, match.after) for match in body.matches] == [
        ("src/sample.py", 1, "alpha", (1,), (), ({"line": 2, "text": "beta"},)),
        ("src/sample.py", 3, "alpha", (1,), ({"line": 2, "text": "beta"},), ()),
    ]
    assert isinstance(result.output, TextOutput)
    assert "ignored.txt" not in result.output.text


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

    assert isinstance(result.body, GrepResultBody)
    assert [match.file for match in result.body.matches] == ["src/keep.py"]
    assert isinstance(result.output, TextOutput)
    assert ".git/ignored.py" not in result.output.text
    assert "node_modules/ignored.py" not in result.output.text


def test_grep_tool_returns_zero_matches_summary(tmp_path: Path) -> None:
    sample_file = tmp_path / "sample.txt"
    _ = sample_file.write_text("alpha beta\n", encoding="utf-8")
    tool = GrepTool()

    result = tool.invoke(
        ToolCall(tool_name="grep", arguments={"pattern": "missing", "path": "sample.txt"}),
        context=ToolContext(workspace=tmp_path),
    )

    assert isinstance(result.body, GrepResultBody)
    assert result.body.path == "sample.txt"
    assert result.body.pattern == "missing"
    assert result.body.match_count == 0
    assert result.body.matches == ()
    assert len(result.body.diagnostics) == 1
    assert {key: result.body.diagnostics[0][key] for key in ("source", "severity", "reason")} == {
        "source": "grep",
        "severity": "info",
        "reason": "no_matches",
    }


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
    assert isinstance(external.body, GrepResultBody)
    assert external.body.path == str(outside.resolve())

    result = tool.invoke(
        ToolCall(tool_name="grep", arguments={"pattern": "x", "path": "sample.bin"}),
        context=ToolContext(workspace=tmp_path),
    )
    assert isinstance(result.body, GrepResultBody)
    assert result.body.match_count == 0


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

    assert isinstance(result.body, GrepResultBody)
    assert [match.file for match in result.body.matches] == ["sample.py"]

    case_sensitive = tool.invoke(
        ToolCall(tool_name="grep", arguments={"pattern": "alpha", "path": "sample.py"}),
        context=ToolContext(workspace=tmp_path),
    )
    assert isinstance(case_sensitive.body, GrepResultBody)
    assert case_sensitive.body.match_count == 0
