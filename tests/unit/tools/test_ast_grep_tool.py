from __future__ import annotations

import subprocess
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest

from voidcode.tools import AstGrepPreviewTool, AstGrepReplaceTool, AstGrepSearchTool, ToolCall


def test_ast_grep_search_parses_json_stream_results(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    _ = sample.write_text("print('hello')\n", encoding="utf-8")
    tool = AstGrepSearchTool()
    completed = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=(
            '{"text":"print(\'hello\')","file":"sample.py","range":{"start":{"line":0,"column":0},"end":{"line":0,"column":14}}}\n'
        ),
        stderr="",
    )

    with patch("subprocess.run", return_value=completed) as run_mock:
        result = tool.invoke(
            ToolCall(
                tool_name="ast_grep_search",
                arguments={"pattern": "print($X)", "path": "sample.py", "lang": "python"},
            ),
            workspace=tmp_path,
        )

    assert result.status == "ok"
    assert result.data["match_count"] == 1
    assert result.data["path"] == "sample.py"
    first_match = cast(list[dict[str, object]], result.data["matches"])[0]
    assert first_match["file"] == "sample.py"
    assert "Found 1 AST match(es)" in (result.content or "")
    assert "--json=stream" in run_mock.call_args.args[0]
    assert "--lang" in run_mock.call_args.args[0]


def test_ast_grep_search_rejects_invalid_arguments_and_workspace_escape(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    _ = sample.write_text("print('hello')\n", encoding="utf-8")
    tool = AstGrepSearchTool()

    with pytest.raises(ValueError, match="string pattern"):
        tool.invoke(
            ToolCall(tool_name="ast_grep_search", arguments={"pattern": 123, "path": "sample.py"}),
            workspace=tmp_path,
        )

    with pytest.raises(ValueError, match="must not be empty"):
        tool.invoke(
            ToolCall(tool_name="ast_grep_search", arguments={"pattern": "", "path": "sample.py"}),
            workspace=tmp_path,
        )

    with pytest.raises(ValueError, match="string path"):
        tool.invoke(
            ToolCall(tool_name="ast_grep_search", arguments={"pattern": "print($X)", "path": 123}),
            workspace=tmp_path,
        )

    with pytest.raises(ValueError, match="inside the workspace"):
        tool.invoke(
            ToolCall(
                tool_name="ast_grep_search",
                arguments={"pattern": "print($X)", "path": "../escape.py"},
            ),
            workspace=tmp_path,
        )


def test_ast_grep_search_returns_error_when_cli_is_missing(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    _ = sample.write_text("print('hello')\n", encoding="utf-8")
    tool = AstGrepSearchTool()

    with patch("subprocess.run", side_effect=OSError("not found")):
        result = tool.invoke(
            ToolCall(
                tool_name="ast_grep_search",
                arguments={"pattern": "print($X)", "path": "sample.py"},
            ),
            workspace=tmp_path,
        )

    assert result.status == "error"
    assert "ast-grep" in (result.error or "")


def test_ast_grep_search_raises_on_cli_failure(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    _ = sample.write_text("print('hello')\n", encoding="utf-8")
    tool = AstGrepSearchTool()
    completed = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="bad pattern")

    with patch("subprocess.run", return_value=completed):
        with pytest.raises(ValueError, match="bad pattern"):
            tool.invoke(
                ToolCall(
                    tool_name="ast_grep_search",
                    arguments={"pattern": "print(", "path": "sample.py", "lang": "python"},
                ),
                workspace=tmp_path,
            )


def test_ast_grep_search_returns_zero_matches_for_empty_cli_result(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    _ = sample.write_text("print('hello')\n", encoding="utf-8")
    tool = AstGrepSearchTool()
    completed = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="")

    with patch("subprocess.run", return_value=completed):
        result = tool.invoke(
            ToolCall(
                tool_name="ast_grep_search",
                arguments={"pattern": "missing($X)", "path": "sample.py", "lang": "python"},
            ),
            workspace=tmp_path,
        )

    assert result.status == "ok"
    assert result.content == "Found 0 AST match(es) in sample.py"
    assert result.data == {
        "path": "sample.py",
        "pattern": "missing($X)",
        "lang": "python",
        "match_count": 0,
        "matches": [],
    }


def test_ast_grep_search_raises_on_invalid_json_stream_output(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    _ = sample.write_text("print('hello')\n", encoding="utf-8")
    tool = AstGrepSearchTool()
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="not-json\n", stderr="")

    with patch("subprocess.run", return_value=completed):
        with pytest.raises(ValueError, match="invalid JSON stream output"):
            tool.invoke(
                ToolCall(
                    tool_name="ast_grep_search",
                    arguments={"pattern": "print($X)", "path": "sample.py", "lang": "python"},
                ),
                workspace=tmp_path,
            )


def test_ast_grep_preview_defaults_to_read_only_preview_mode(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    _ = sample.write_text("print('hello')\n", encoding="utf-8")
    tool = AstGrepPreviewTool()
    completed = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=(
            '{"text":"print(\'hello\')","file":"sample.py","replacement":"logger.info(\'hello\')"}\n'
        ),
        stderr="",
    )

    with patch("subprocess.run", return_value=completed) as run_mock:
        result = tool.invoke(
            ToolCall(
                tool_name="ast_grep_preview",
                arguments={
                    "pattern": "print($X)",
                    "rewrite": "logger.info($X)",
                    "path": "sample.py",
                    "lang": "python",
                },
            ),
            workspace=tmp_path,
        )

    assert result.status == "ok"
    assert result.data["replacement_count"] == 1
    assert result.data["applied"] is False
    assert "Previewed 1 AST replacement(s)" in (result.content or "")
    assert "-U" not in run_mock.call_args.args[0]
    assert "-r" in run_mock.call_args.args[0]
    assert "--json=stream" in run_mock.call_args.args[0]


def test_ast_grep_preview_allows_empty_rewrite_strings(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    _ = sample.write_text("print('hello')\n", encoding="utf-8")
    tool = AstGrepPreviewTool()
    completed = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout='{"text":"print(\'hello\')","file":"sample.py","replacement":""}\n',
        stderr="",
    )

    with patch("subprocess.run", return_value=completed):
        result = tool.invoke(
            ToolCall(
                tool_name="ast_grep_preview",
                arguments={
                    "pattern": "print($X)",
                    "rewrite": "",
                    "path": "sample.py",
                    "lang": "python",
                },
            ),
            workspace=tmp_path,
        )

    assert result.status == "ok"
    assert result.data["rewrite"] == ""
    assert result.data["replacement_count"] == 1


def test_ast_grep_replace_can_apply_changes(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    _ = sample.write_text("print('hello')\n", encoding="utf-8")
    tool = AstGrepReplaceTool()
    preview_completed = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=(
            '{"text":"print(\'hello\')","file":"sample.py","replacement":"logger.info(\'hello\')"}\n'
        ),
        stderr="",
    )
    apply_completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="", stderr="Applied 1 changes\n"
    )

    with patch("subprocess.run", side_effect=[preview_completed, apply_completed]) as run_mock:
        result = tool.invoke(
            ToolCall(
                tool_name="ast_grep_replace",
                arguments={
                    "pattern": "print($X)",
                    "rewrite": "logger.info($X)",
                    "path": "sample.py",
                    "apply": True,
                },
            ),
            workspace=tmp_path,
        )

    assert result.status == "ok"
    assert result.data["applied"] is True
    assert result.data["replacement_count"] == 1
    first_match = cast(list[dict[str, object]], result.data["matches"])[0]
    assert first_match["file"] == "sample.py"
    assert result.content == "Applied 1 AST replacement(s) in sample.py"
    assert "--json=stream" in run_mock.call_args_list[0].args[0]
    assert "-U" not in run_mock.call_args_list[0].args[0]
    assert "-U" in run_mock.call_args_list[1].args[0]
    assert "--json=stream" not in run_mock.call_args_list[1].args[0]


def test_ast_grep_replace_requires_apply_true(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    _ = sample.write_text("print('hello')\n", encoding="utf-8")
    tool = AstGrepReplaceTool()

    with pytest.raises(ValueError, match="requires apply=True"):
        tool.invoke(
            ToolCall(
                tool_name="ast_grep_replace",
                arguments={
                    "pattern": "print($X)",
                    "rewrite": "logger.info($X)",
                    "path": "sample.py",
                },
            ),
            workspace=tmp_path,
        )


def test_ast_grep_replace_allows_empty_rewrite_strings(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    _ = sample.write_text("print('hello')\n", encoding="utf-8")
    tool = AstGrepReplaceTool()
    preview_completed = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout='{"text":"print(\'hello\')","file":"sample.py","replacement":""}\n',
        stderr="",
    )
    apply_completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    with patch("subprocess.run", side_effect=[preview_completed, apply_completed]):
        result = tool.invoke(
            ToolCall(
                tool_name="ast_grep_replace",
                arguments={
                    "pattern": "print($X)",
                    "rewrite": "",
                    "path": "sample.py",
                    "apply": True,
                },
            ),
            workspace=tmp_path,
        )

    assert result.status == "ok"
    assert result.data["rewrite"] == ""
    assert result.data["replacement_count"] == 1


def test_ast_grep_replace_raises_on_cli_failure(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    _ = sample.write_text("print('hello')\n", encoding="utf-8")
    tool = AstGrepReplaceTool()
    preview_completed = subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr="bad rewrite"
    )

    with patch("subprocess.run", return_value=preview_completed):
        with pytest.raises(ValueError, match="bad rewrite"):
            tool.invoke(
                ToolCall(
                    tool_name="ast_grep_replace",
                    arguments={
                        "pattern": "print($X)",
                        "rewrite": "logger.info($X)",
                        "path": "sample.py",
                        "lang": "python",
                        "apply": True,
                    },
                ),
                workspace=tmp_path,
            )


def test_ast_grep_preview_raises_on_invalid_json_stream_output(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    _ = sample.write_text("print('hello')\n", encoding="utf-8")
    tool = AstGrepPreviewTool()
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="not-json\n", stderr="")

    with patch("subprocess.run", return_value=completed):
        with pytest.raises(ValueError, match="invalid JSON stream output"):
            tool.invoke(
                ToolCall(
                    tool_name="ast_grep_preview",
                    arguments={
                        "pattern": "print($X)",
                        "rewrite": "logger.info($X)",
                        "path": "sample.py",
                        "lang": "python",
                    },
                ),
                workspace=tmp_path,
            )


def test_ast_grep_preview_returns_zero_matches_for_empty_cli_result(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    _ = sample.write_text("print('hello')\n", encoding="utf-8")
    tool = AstGrepPreviewTool()
    completed = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="")

    with patch("subprocess.run", return_value=completed):
        result = tool.invoke(
            ToolCall(
                tool_name="ast_grep_preview",
                arguments={
                    "pattern": "missing($X)",
                    "rewrite": "logger.info($X)",
                    "path": "sample.py",
                    "lang": "python",
                },
            ),
            workspace=tmp_path,
        )

    assert result.status == "ok"
    assert result.content == "Previewed 0 AST replacement(s) in sample.py"
    assert result.data == {
        "path": "sample.py",
        "pattern": "missing($X)",
        "rewrite": "logger.info($X)",
        "lang": "python",
        "replacement_count": 0,
        "matches": [],
        "applied": False,
    }


def test_ast_grep_replace_apply_returns_zero_changes_for_empty_cli_result(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    _ = sample.write_text("print('hello')\n", encoding="utf-8")
    tool = AstGrepReplaceTool()
    preview_completed = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="")
    apply_completed = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="")

    with patch("subprocess.run", side_effect=[preview_completed, apply_completed]):
        result = tool.invoke(
            ToolCall(
                tool_name="ast_grep_replace",
                arguments={
                    "pattern": "missing($X)",
                    "rewrite": "logger.info($X)",
                    "path": "sample.py",
                    "apply": True,
                },
            ),
            workspace=tmp_path,
        )

    assert result.status == "ok"
    assert result.content == "Applied 0 AST replacement(s) in sample.py"
    assert result.data == {
        "path": "sample.py",
        "pattern": "missing($X)",
        "rewrite": "logger.info($X)",
        "lang": None,
        "replacement_count": 0,
        "matches": [],
        "applied": True,
    }


def test_ast_grep_replace_apply_raises_when_apply_step_returns_stderr(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    _ = sample.write_text("print('hello')\n", encoding="utf-8")
    tool = AstGrepReplaceTool()
    preview_completed = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=(
            '{"text":"print(\'hello\')","file":"sample.py","replacement":"logger.info(\'hello\')"}\n'
        ),
        stderr="",
    )
    apply_completed = subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr="apply failed"
    )

    with patch("subprocess.run", side_effect=[preview_completed, apply_completed]):
        with pytest.raises(ValueError, match="apply failed"):
            tool.invoke(
                ToolCall(
                    tool_name="ast_grep_replace",
                    arguments={
                        "pattern": "print($X)",
                        "rewrite": "logger.info($X)",
                        "path": "sample.py",
                        "lang": "python",
                        "apply": True,
                    },
                ),
                workspace=tmp_path,
            )
