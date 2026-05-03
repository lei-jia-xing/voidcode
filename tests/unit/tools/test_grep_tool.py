from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from voidcode.runtime.service import ToolRegistry
from voidcode.tools import GrepTool, ToolCall
from voidcode.tools._repair import ToolDiagnosticError
from voidcode.tools.grep import MAX_MATCHES


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
    matches = cast(list[dict[str, object]], result.data["matches"])
    assert [match["file"] for match in matches] == [
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
    matches = cast(list[dict[str, object]], result.data["matches"])
    assert [match["file"] for match in matches] == ["src/keep.py"]
    assert ".git/ignored.py" not in (result.content or "")
    assert "node_modules/ignored.py" not in (result.content or "")


def test_grep_tool_truncated_results_include_agent_guidance(tmp_path: Path) -> None:
    sample_file = tmp_path / "sample.txt"
    sample_file.write_text("".join("needle\n" for _ in range(MAX_MATCHES + 3)), encoding="utf-8")
    tool = GrepTool()

    result = tool.invoke(
        ToolCall(tool_name="grep", arguments={"pattern": "needle", "path": "sample.txt"}),
        workspace=tmp_path,
    )

    assert result.truncated is True
    assert "[TRUNCATED]" in (result.content or "")
    assert result.data["match_count"] == MAX_MATCHES
    diagnostics = cast(list[dict[str, object]], result.data["diagnostics"])
    assert diagnostics[-1]["reason"] == "results_truncated"
    retry_guidance = cast(str, diagnostics[-1]["retry_guidance"])
    assert "Refine path" in retry_guidance


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
        "diagnostics": [
            {
                "source": "grep",
                "severity": "info",
                "reason": "no_matches",
                "message": (
                    "No matches found. Broaden the path/include filter, verify the search "
                    "text with read_file, or retry with regex=false for literal text."
                ),
            }
        ],
    }


def test_grep_tool_rejects_invalid_arguments_and_non_utf8_files(tmp_path: Path) -> None:
    binary_file = tmp_path / "sample.bin"
    _ = binary_file.write_bytes(b"\xff\xfe\x00x")
    tool = GrepTool()
    pattern_type_error = (
        r"grep Validation error: pattern: "
        r"Input should be a valid string \(received int\)"
        r"\. Please retry with corrected arguments that satisfy the tool schema\."
    )

    with pytest.raises(ValueError, match=pattern_type_error):
        tool.invoke(
            ToolCall(tool_name="grep", arguments={"pattern": 123, "path": "sample.txt"}),
            workspace=tmp_path,
        )

    path_type_error = (
        r"grep Validation error: path: "
        r"Input should be a valid string \(received int\)"
        r"\. Please retry with corrected arguments that satisfy the tool schema\."
    )
    with pytest.raises(ValueError, match=path_type_error):
        tool.invoke(
            ToolCall(tool_name="grep", arguments={"pattern": "alpha", "path": 123}),
            workspace=tmp_path,
        )

    empty_pattern_error = (
        r"grep Validation error: pattern: Value error, "
        r"pattern must not be empty \(received str\)"
        r"\. Please retry with corrected arguments that satisfy the tool schema\."
    )
    with pytest.raises(ValueError, match=empty_pattern_error):
        tool.invoke(
            ToolCall(tool_name="grep", arguments={"pattern": "", "path": "sample.txt"}),
            workspace=tmp_path,
        )

    outside = tmp_path.parent / "outside-grep.txt"
    outside.write_text("alpha\n", encoding="utf-8")
    external = tool.invoke(
        ToolCall(tool_name="grep", arguments={"pattern": "alpha", "path": str(outside)}),
        workspace=tmp_path,
    )
    assert external.status == "ok"
    assert external.data["path"] == str(outside.resolve())

    result = tool.invoke(
        ToolCall(tool_name="grep", arguments={"pattern": "x", "path": "sample.bin"}),
        workspace=tmp_path,
    )
    assert result.status == "ok"
    assert result.data["match_count"] == 0


def test_grep_tool_reports_missing_required_args_and_invalid_regex(tmp_path: Path) -> None:
    tool = GrepTool()
    missing_pattern_error = (
        r"grep Validation error: pattern: "
        r"Input should be a valid string \(received NoneType\)"
        r"\. Please retry with corrected arguments that satisfy the tool schema\."
    )
    missing_path_error = (
        r"grep Validation error: path: "
        r"Input should be a valid string \(received NoneType\)"
        r"\. Please retry with corrected arguments that satisfy the tool schema\."
    )

    with pytest.raises(ValueError, match=missing_pattern_error):
        tool.invoke(
            ToolCall(tool_name="grep", arguments={"path": "sample.txt"}),
            workspace=tmp_path,
        )

    with pytest.raises(ValueError, match=missing_path_error):
        tool.invoke(ToolCall(tool_name="grep", arguments={"pattern": "alpha"}), workspace=tmp_path)

    with pytest.raises(
        ValueError,
        match=r"grep Validation error: pattern: invalid regex pattern .* \(received str\)",
    ) as exc_info:
        tool.invoke(
            ToolCall(
                tool_name="grep",
                arguments={"pattern": "[unclosed", "path": ".", "regex": True},
            ),
            workspace=tmp_path,
        )

    assert isinstance(exc_info.value, ToolDiagnosticError)
    assert exc_info.value.error_kind == "tool_input_validation"
    assert exc_info.value.error_details["reason"] == "invalid_regex"
    assert exc_info.value.retry_guidance is not None
    assert "regex=false" in exc_info.value.retry_guidance


def test_tools_package_and_default_registry_export_grep_tool() -> None:
    registry = ToolRegistry.with_defaults()

    assert "GrepTool" in __import__("voidcode.tools", fromlist=["__all__"]).__all__
    assert registry.resolve("grep").definition.name == "grep"
    assert registry.resolve("grep").definition.read_only is True
