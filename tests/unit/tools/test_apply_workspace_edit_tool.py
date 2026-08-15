from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from voidcode.tools._repair import ToolDiagnosticError
from voidcode.tools.apply_workspace_edit import ApplyWorkspaceEditTool
from voidcode.tools.contracts import ToolCall
from voidcode.tools.runtime_context import RuntimeToolInvocationContext, bind_runtime_tool_context


def _call(edits: list[dict[str, object]]) -> ToolCall:
    return ToolCall(tool_name="apply_workspace_edit", arguments={"edits": edits})


def _content_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _edit(path: str, *, start: int, end: int, new_text: str, file_path: Path) -> dict[str, object]:
    return {
        "path": path,
        "startLine": 1,
        "startCharacter": start,
        "endLine": 1,
        "endCharacter": end,
        "newText": new_text,
        "expectedHash": _content_hash(file_path),
    }


def test_applies_same_file_edits_against_original_ranges(tmp_path: Path) -> None:
    path = tmp_path / "sample.py"
    path.write_text("alpha beta gamma\n", encoding="utf-8")
    result = ApplyWorkspaceEditTool().invoke(
        _call(
            [
                _edit("sample.py", start=1, end=6, new_text="a", file_path=path),
                _edit("sample.py", start=12, end=17, new_text="g", file_path=path),
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
    with pytest.raises(ToolDiagnosticError, match="stale edit") as exc_info:
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
    diagnostic = exc_info.value
    assert diagnostic.error_kind == "stale_edit"
    assert diagnostic.error_details["reason"] == "content_hash_mismatch"
    assert diagnostic.error_details["path"] == "second.txt"
    assert diagnostic.error_details["expected_hash"] == "stale"
    assert diagnostic.error_details["actual_hash"] == hashlib.sha256(b"second").hexdigest()
    assert "data.content_hash" in (diagnostic.retry_guidance or "")
    assert first.read_text(encoding="utf-8") == "first"
    assert second.read_text(encoding="utf-8") == "second"


def test_rejects_overlapping_edits(tmp_path: Path) -> None:
    path = tmp_path / "sample.txt"
    path.write_text("abcdef", encoding="utf-8")
    with pytest.raises(ValueError, match="overlapping edits"):
        ApplyWorkspaceEditTool().invoke(
            _call(
                [
                    {
                        "path": "sample.txt",
                        "startLine": 1,
                        "startCharacter": 1,
                        "endLine": 1,
                        "endCharacter": 5,
                        "newText": "x",
                        "expectedHash": _content_hash(path),
                    },
                    {
                        "path": "sample.txt",
                        "startLine": 1,
                        "startCharacter": 3,
                        "endLine": 1,
                        "endCharacter": 7,
                        "newText": "y",
                        "expectedHash": _content_hash(path),
                    },
                ]
            ),
            workspace=tmp_path,
        )
    assert path.read_text(encoding="utf-8") == "abcdef"


def test_rejects_missing_expected_hash_with_structured_diagnostic(tmp_path: Path) -> None:
    path = tmp_path / "sample.txt"
    path.write_text("abcdef", encoding="utf-8")
    with pytest.raises(ToolDiagnosticError, match="expectedHash") as exc_info:
        ApplyWorkspaceEditTool().invoke(
            _call(
                [
                    {
                        "path": "sample.txt",
                        "startLine": 1,
                        "startCharacter": 1,
                        "endLine": 1,
                        "endCharacter": 5,
                        "newText": "x",
                    },
                ]
            ),
            workspace=tmp_path,
        )
    diagnostic = exc_info.value
    assert diagnostic.error_kind == "tool_input_mismatch"
    assert diagnostic.error_details["reason"] == "missing_expected_hash"
    assert diagnostic.error_details["edit_index"] == 1
    assert diagnostic.error_details["path"] == "sample.txt"
    assert "read" in (diagnostic.retry_guidance or "")
    assert path.read_text(encoding="utf-8") == "abcdef"


def test_schema_requires_expected_hash_on_every_edit() -> None:
    schema = ApplyWorkspaceEditTool.definition.input_schema
    edits_schema = schema["edits"]
    assert edits_schema["items"]["required"] == [
        "path",
        "startLine",
        "startCharacter",
        "endLine",
        "endCharacter",
        "newText",
        "expectedHash",
    ]
    assert "data.content_hash" in str(edits_schema["description"])


def test_rejects_edit_on_line_outside_read_window(tmp_path: Path) -> None:
    path = tmp_path / "sample.py"
    path.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    resolved = path.resolve().as_posix()
    edit = {
        "path": "sample.py",
        "startLine": 3,
        "startCharacter": 1,
        "endLine": 3,
        "endCharacter": 6,
        "newText": "GAMMA",
        "expectedHash": _content_hash(path),
    }

    with bind_runtime_tool_context(
        RuntimeToolInvocationContext(
            session_id="test",
            read_paths=frozenset({resolved}),
            read_lines={resolved: frozenset({1})},
        )
    ):
        with pytest.raises(ToolDiagnosticError, match="never revealed by read") as exc_info:
            ApplyWorkspaceEditTool().invoke(_call([edit]), workspace=tmp_path)

    diagnostic = exc_info.value
    assert diagnostic.error_kind == "tool_input_mismatch"
    assert diagnostic.error_details["reason"] == "unseen_range"
    assert diagnostic.error_details["unseen_line_ranges"] == [{"start": 3, "end": 3}]
    assert path.read_text(encoding="utf-8") == "alpha\nbeta\ngamma\n"


def test_applies_edit_when_target_lines_are_seen(tmp_path: Path) -> None:
    path = tmp_path / "sample.py"
    path.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    resolved = path.resolve().as_posix()
    edit = {
        "path": "sample.py",
        "startLine": 2,
        "startCharacter": 1,
        "endLine": 2,
        "endCharacter": 5,
        "newText": "BETA",
        "expectedHash": _content_hash(path),
    }

    with bind_runtime_tool_context(
        RuntimeToolInvocationContext(
            session_id="test",
            read_paths=frozenset({resolved}),
            read_lines={resolved: frozenset({1, 2, 3})},
        )
    ):
        result = ApplyWorkspaceEditTool().invoke(_call([edit]), workspace=tmp_path)

    assert result.status == "ok"
    assert path.read_text(encoding="utf-8") == "alpha\nBETA\ngamma\n"
