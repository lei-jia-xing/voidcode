from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from voidcode.tools.apply_workspace_edit import ApplyWorkspaceEditTool
from voidcode.tools.contracts import ToolCall


def _call(edits: list[dict[str, object]]) -> ToolCall:
    return ToolCall(tool_name="apply_workspace_edit", arguments={"edits": edits})


def test_applies_same_file_edits_against_original_ranges(tmp_path: Path) -> None:
    path = tmp_path / "sample.py"
    path.write_text("alpha beta gamma\n", encoding="utf-8")
    result = ApplyWorkspaceEditTool().invoke(
        _call(
            [
                {"path": "sample.py", "startLine": 1, "startCharacter": 1, "endLine": 1, "endCharacter": 6, "newText": "a"},
                {"path": "sample.py", "startLine": 1, "startCharacter": 12, "endLine": 1, "endCharacter": 17, "newText": "g"},
            ]
        ),
        workspace=tmp_path,
    )
    assert result.status == "ok"
    assert path.read_text(encoding="utf-8") == "a beta g\n"


def test_rejects_stale_edit_without_writing_any_file(tmp_path: Path) -> None:
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("first", encoding="utf-8")
    second.write_text("second", encoding="utf-8")
    valid_hash = hashlib.sha256(b"first").hexdigest()
    with pytest.raises(ValueError, match="stale edit"):
        ApplyWorkspaceEditTool().invoke(
            _call(
                [
                    {
                        "path": "first.txt",
                        "startLine": 1,
                        "startCharacter": 1,
                        "endLine": 1,
                        "endCharacter": 6,
                        "newText": "changed",
                        "expectedHash": valid_hash,
                    },
                    {
                        "path": "second.txt",
                        "startLine": 1,
                        "startCharacter": 1,
                        "endLine": 1,
                        "endCharacter": 7,
                        "newText": "changed",
                        "expectedHash": "stale",
                    },
                ]
            ),
            workspace=tmp_path,
        )
    assert first.read_text(encoding="utf-8") == "first"
    assert second.read_text(encoding="utf-8") == "second"


def test_rejects_overlapping_edits(tmp_path: Path) -> None:
    path = tmp_path / "sample.txt"
    path.write_text("abcdef", encoding="utf-8")
    with pytest.raises(ValueError, match="overlapping edits"):
        ApplyWorkspaceEditTool().invoke(
            _call(
                [
                    {"path": "sample.txt", "startLine": 1, "startCharacter": 1, "endLine": 1, "endCharacter": 5, "newText": "x"},
                    {"path": "sample.txt", "startLine": 1, "startCharacter": 3, "endLine": 1, "endCharacter": 7, "newText": "y"},
                ]
            ),
            workspace=tmp_path,
        )
    assert path.read_text(encoding="utf-8") == "abcdef"
