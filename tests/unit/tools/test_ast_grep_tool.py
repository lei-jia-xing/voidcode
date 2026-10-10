from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest

from voidcode.core.tool_context import ToolContext
from voidcode.core.transcript import output_text
from voidcode.tools.ast_grep import AstGrepTool
from voidcode.tools.contracts import ToolCall

_TOOL_NAME = "ast_grep"


def _search_call(**overrides: object) -> ToolCall:
    args: dict[str, object] = {"mode": "search", "pattern": "print($X)", "path": "sample.py"}
    args.update(overrides)
    return ToolCall(tool_name=_TOOL_NAME, arguments=args)


def _preview_call(**overrides: object) -> ToolCall:
    args: dict[str, object] = {
        "mode": "preview",
        "pattern": "print($X)",
        "rewrite": "logger.info($X)",
        "path": "sample.py",
        "lang": "python",
    }
    args.update(overrides)
    return ToolCall(tool_name=_TOOL_NAME, arguments=args)


def _replace_call(**overrides: object) -> ToolCall:
    args: dict[str, object] = {
        "mode": "replace",
        "pattern": "print($X)",
        "rewrite": "logger.info($X)",
        "path": "sample.py",
        "apply": True,
    }
    args.update(overrides)
    return ToolCall(tool_name=_TOOL_NAME, arguments=args)


def test_ast_grep_search_parses_json_stream_results(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    _ = sample.write_text("print('hello')\n", encoding="utf-8")
    tool = AstGrepTool()
    completed = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=('{"text":"print(\'hello\')","file":"sample.py","range":{"start":{"line":0,"column":0},"end":{"line":0,"column":14}}}\n'),
        stderr="",
    )

    with patch("subprocess.run", return_value=completed) as run_mock:
        result = tool.invoke(
            _search_call(pattern="print($X)", lang="python"),
            context=ToolContext(workspace=tmp_path),
        )

    assert result.status == "ok"
    assert result.body is not None
    data = result.body.as_payload()
    assert data["match_count"] == 1
    assert data["path"] == "sample.py"
    first_match = cast(list[dict[str, object]], data["matches"])[0]
    assert first_match["file"] == "sample.py"
    assert "--json=stream" in run_mock.call_args.args[0]
    assert "--lang" in run_mock.call_args.args[0]


def test_ast_grep_search_renders_locations_and_source_text(tmp_path: Path) -> None:
    (tmp_path / "sample.py").write_text("print('hello')\n", encoding="utf-8")
    completed = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout='{"text":"print(hello)","file":"sample.py","range":{"start":{"line":0,"column":2}}}\n',
        stderr="",
    )
    with patch("subprocess.run", return_value=completed):
        result = AstGrepTool().invoke(_search_call(), context=ToolContext(workspace=tmp_path))

    assert "sample.py:1:3: print(hello)" in (output_text(result.output) or "")


def test_ast_grep_search_accepts_external_paths_after_argument_validation(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text("print('hello')\n", encoding="utf-8")
    external = tmp_path.parent / f"{tmp_path.name}-external.py"
    external.write_text("print('external')\n", encoding="utf-8")
    tool = AstGrepTool()

    with pytest.raises(ValueError, match="Validation error"):
        tool.invoke(_search_call(pattern=123), context=ToolContext(workspace=tmp_path))

    with pytest.raises(ValueError, match="Validation error"):
        tool.invoke(_search_call(pattern=""), context=ToolContext(workspace=tmp_path))

    with pytest.raises(ValueError, match="Validation error"):
        tool.invoke(_search_call(path=123), context=ToolContext(workspace=tmp_path))

    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    with patch("voidcode.tools.ast_grep.subprocess.run", return_value=completed) as run_mock:
        tool.invoke(_search_call(path=str(external)), context=ToolContext(workspace=tmp_path))
    assert run_mock.call_args.args[0][-1] == str(external)


def test_ast_grep_preview_defaults_to_read_only_preview_mode(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    _ = sample.write_text("print('hello')\n", encoding="utf-8")
    tool = AstGrepTool()
    completed = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=('{"text":"print(\'hello\')","file":"sample.py","replacement":"logger.info(\'hello\')"}\n'),
        stderr="",
    )

    with patch("subprocess.run", return_value=completed) as run_mock:
        result = tool.invoke(
            _preview_call(pattern="print($X)", rewrite="logger.info($X)", lang="python"),
            context=ToolContext(workspace=tmp_path),
        )

    assert result.status == "ok"
    assert result.body is not None
    data = result.body.as_payload()
    assert data["replacement_count"] == 1
    assert data["applied"] is False
    assert "sample.py: print('hello')" in (output_text(result.output) or "")
    assert "-> logger.info('hello')" in (output_text(result.output) or "")
    assert "-U" not in run_mock.call_args.args[0]
    assert "-r" in run_mock.call_args.args[0]
    assert "--json=stream" in run_mock.call_args.args[0]


def test_ast_grep_replace_can_apply_changes(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    _ = sample.write_text("print('hello')\n", encoding="utf-8")
    tool = AstGrepTool()
    preview_completed = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=('{"text":"print(\'hello\')","file":"sample.py","replacement":"logger.info(\'hello\')"}\n'),
        stderr="",
    )
    apply_completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="Applied 1 changes\n")

    subprocess_calls = 0

    def run_ast_grep(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal subprocess_calls
        subprocess_calls += 1
        if subprocess_calls == 2:
            sample.write_text("logger.info('hello')\n", encoding="utf-8")
            return apply_completed
        return preview_completed

    with patch("subprocess.run", side_effect=run_ast_grep) as run_mock:
        result = tool.invoke(
            _replace_call(pattern="print($X)", rewrite="logger.info($X)", apply=True),
            context=ToolContext(workspace=tmp_path),
        )

    assert result.status == "ok"
    assert result.body is not None
    data = result.body.as_payload()
    assert data["applied"] is True
    assert data["replacement_count"] == 1
    first_match = cast(list[dict[str, object]], data["matches"])[0]
    assert first_match["file"] == "sample.py"
    final_bytes = b"logger.info('hello')\n"
    assert sample.read_bytes() == final_bytes
    assert data["final_hash"] == hashlib.sha256(final_bytes).hexdigest()
    assert data["diff"] == ("--- a/sample.py\n+++ b/sample.py\n@@ -1 +1 @@\n-print('hello')\n+logger.info('hello')\n")
    assert "--json=stream" in run_mock.call_args_list[0].args[0]
    assert "-U" not in run_mock.call_args_list[0].args[0]
    assert "-U" in run_mock.call_args_list[1].args[0]
    assert "--json=stream" not in run_mock.call_args_list[1].args[0]


def test_ast_grep_replace_requires_apply_true(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    _ = sample.write_text("print('hello')\n", encoding="utf-8")
    tool = AstGrepTool()

    with pytest.raises(ValueError, match="requires apply=True"):
        tool.invoke(
            _replace_call(apply=False),
            context=ToolContext(workspace=tmp_path),
        )
