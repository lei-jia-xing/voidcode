from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import cast

import pytest

from voidcode.formatter import RuntimeFormatterPresetConfig
from voidcode.hook.config import RuntimeHooksConfig
from voidcode.tools import ApplyPatchTool, EditTool, ToolCall
from voidcode.tools._syntax_validation import (
    language_for_path,
    post_edit_syntax_diagnostics,
    syntax_diagnostics_for_file,
)

_TREE_SITTER_LANGUAGE_PACK = "tree_sitter_language_pack"


def _content_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_syntax_validation_returns_empty_when_tree_sitter_is_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broken = tmp_path / "broken.py"
    broken.write_text("def f(:\n    pass\n", encoding="utf-8")

    # None in sys.modules makes `import tree_sitter_language_pack` raise ImportError,
    # simulating the optional dependency being absent without touching the venv.
    monkeypatch.setitem(sys.modules, _TREE_SITTER_LANGUAGE_PACK, None)

    assert syntax_diagnostics_for_file(path=broken, workspace=tmp_path) == []
    assert post_edit_syntax_diagnostics(workspace=tmp_path, paths=["broken.py"]) == []


def test_syntax_validation_reports_broken_snippet_in_installed_grammar(
    tmp_path: Path,
) -> None:
    pytest.importorskip(_TREE_SITTER_LANGUAGE_PACK)
    broken = tmp_path / "broken.py"
    broken.write_text("def f(:\n    pass\n", encoding="utf-8")

    diagnostics = syntax_diagnostics_for_file(path=broken, workspace=tmp_path)

    assert diagnostics == [
        {
            "path": "broken.py",
            "source": "tree-sitter",
            "severity": "error",
            "message": "Missing )",
            "line": 1,
            "character": 7,
        }
    ]


def test_syntax_validation_returns_empty_for_valid_file_in_installed_grammar(
    tmp_path: Path,
) -> None:
    pytest.importorskip(_TREE_SITTER_LANGUAGE_PACK)
    valid = tmp_path / "ok.py"
    valid.write_text("def f():\n    return 'ok'\n", encoding="utf-8")

    assert syntax_diagnostics_for_file(path=valid, workspace=tmp_path) == []


def test_syntax_validation_returns_empty_for_unknown_language(tmp_path: Path) -> None:
    pytest.importorskip(_TREE_SITTER_LANGUAGE_PACK)
    notes = tmp_path / "notes.txt"
    notes.write_text("arbitrary text\n", encoding="utf-8")

    assert syntax_diagnostics_for_file(path=notes, workspace=tmp_path) == []


def test_syntax_validation_never_raises_on_arbitrary_content(tmp_path: Path) -> None:
    pytest.importorskip(_TREE_SITTER_LANGUAGE_PACK)
    weird = tmp_path / "weird.py"
    weird.write_bytes(b"\x00\xff\xfe garbage \x80\x81 not utf-8")

    assert syntax_diagnostics_for_file(path=weird, workspace=tmp_path) == []


def test_language_detection_by_extension() -> None:
    assert language_for_path(Path("main.py")) == "python"
    assert language_for_path(Path("src/component.tsx")) == "tsx"
    assert language_for_path(Path("component.ts")) == "typescript"
    assert language_for_path(Path("config.json")) == "json"
    assert language_for_path(Path("config.YAML")) == "yaml"
    assert language_for_path(Path("README.md")) == "markdown"
    assert language_for_path(Path("Makefile")) is None
    assert language_for_path(Path("notes.txt")) is None


def test_syntax_validation_env_gate_disables_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip(_TREE_SITTER_LANGUAGE_PACK)
    broken = tmp_path / "broken.py"
    broken.write_text("def f(:\n    pass\n", encoding="utf-8")

    monkeypatch.setenv("VOIDCODE_SYNTAX_VALIDATION", "0")

    assert syntax_diagnostics_for_file(path=broken, workspace=tmp_path) == []
    assert post_edit_syntax_diagnostics(workspace=tmp_path, paths=["broken.py"]) == []


def test_edit_tool_surfaces_tree_sitter_diagnostics(tmp_path: Path) -> None:
    pytest.importorskip(_TREE_SITTER_LANGUAGE_PACK)
    file_path = tmp_path / "main.py"
    file_path.write_text("def f():\n    pass\n", encoding="utf-8")
    tool = EditTool()

    result = tool.invoke(
        ToolCall(
            tool_name="edit",
            arguments={
                "path": "main.py",
                "oldString": "def f():",
                "newString": "def f(:",
                "expectedHash": _content_hash(file_path),
            },
        ),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert file_path.read_text(encoding="utf-8") == "def f(:\n    pass\n"
    diagnostics = cast(list[dict[str, object]], result.data["diagnostics"])
    syntax_diagnostics = [d for d in diagnostics if d.get("source") == "tree-sitter"]
    assert syntax_diagnostics
    assert syntax_diagnostics[0]["severity"] == "error"
    assert "Missing )" in str(syntax_diagnostics[0]["message"])
    assert syntax_diagnostics[0]["path"] == "main.py"


def test_edit_tool_omits_tree_sitter_diagnostics_when_gated_off(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip(_TREE_SITTER_LANGUAGE_PACK)
    file_path = tmp_path / "main.py"
    file_path.write_text("def f():\n    pass\n", encoding="utf-8")
    monkeypatch.setenv("VOIDCODE_SYNTAX_VALIDATION", "0")
    tool = EditTool()

    result = tool.invoke(
        ToolCall(
            tool_name="edit",
            arguments={
                "path": "main.py",
                "oldString": "def f():",
                "newString": "def f(:",
                "expectedHash": _content_hash(file_path),
            },
        ),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert file_path.read_text(encoding="utf-8") == "def f(:\n    pass\n"
    raw_diagnostics = result.data.get("diagnostics")
    diagnostics = raw_diagnostics if isinstance(raw_diagnostics, list) else []
    assert not any(d.get("source") == "tree-sitter" for d in diagnostics)


def test_apply_patch_surfaces_tree_sitter_diagnostics(tmp_path: Path) -> None:
    pytest.importorskip(_TREE_SITTER_LANGUAGE_PACK)
    patch_text = "\n".join(
        [
            "*** Begin Patch",
            "*** Add File: main.py",
            "+def f(:",
            "+    pass",
            "*** End Patch",
        ]
    )
    tool = ApplyPatchTool(
        hooks_config=RuntimeHooksConfig(
            formatter_presets={
                "python": RuntimeFormatterPresetConfig(
                    command=("missing-formatter-binary",),
                    extensions=(".py",),
                )
            }
        )
    )

    result = tool.invoke(
        ToolCall(tool_name="apply_patch", arguments={"patch": patch_text}),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert (tmp_path / "main.py").read_text(encoding="utf-8").startswith("def f(:")
    diagnostics = cast(list[dict[str, object]], result.data["diagnostics"])
    syntax_diagnostics = [d for d in diagnostics if d.get("source") == "tree-sitter"]
    assert syntax_diagnostics
    assert syntax_diagnostics[0]["severity"] == "error"
    assert syntax_diagnostics[0]["path"] == "main.py"
