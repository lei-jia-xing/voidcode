from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from voidcode.core.tool_context import LspDocumentState, ToolContext
from voidcode.tools._repair import ToolDiagnosticError
from voidcode.tools.apply_workspace_edit import ApplyWorkspaceEditTool
from voidcode.tools.contracts import ToolCall


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
        context=ToolContext(workspace=tmp_path),
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
            context=ToolContext(workspace=tmp_path),
        )
    diagnostic = exc_info.value
    assert diagnostic.error_kind == "stale_edit"
    assert diagnostic.error_details["reason"] == "content_hash_mismatch"
    assert diagnostic.error_details["path"] == "second.txt"
    assert diagnostic.error_details["expected_hash"] == "stale"
    assert diagnostic.error_details["actual_hash"] == hashlib.sha256(b"second").hexdigest()
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
            context=ToolContext(workspace=tmp_path),
        )
    assert path.read_text(encoding="utf-8") == "abcdef"


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

    read_hash = _content_hash(path)
    context = ToolContext(
        workspace=tmp_path,
        session_id="test",
        read_paths=frozenset({resolved}),
        read_lines={(resolved, read_hash): frozenset({1})},
        read_hash=read_hash,
    )
    with pytest.raises(ToolDiagnosticError, match="never revealed by read") as exc_info:
        ApplyWorkspaceEditTool().invoke(_call([edit]), context=context)

    diagnostic = exc_info.value
    assert diagnostic.error_kind == "tool_input_mismatch"
    assert diagnostic.error_details["reason"] == "unseen_range"
    assert diagnostic.error_details["unseen_line_ranges"] == [{"start": 3, "end": 3}]
    assert path.read_text(encoding="utf-8") == "alpha\nbeta\ngamma\n"


def test_apply_workspace_edit_handles_non_bmp_utf16_positions(tmp_path: Path) -> None:
    path = tmp_path / "emoji.txt"
    # 'hello ' (6 UTF-16 units) + '🚀' (2 UTF-16 units, 7-8) + ' world\n' (9-15)
    path.write_text("hello 🚀 world\n", encoding="utf-8")
    edit = {
        "path": "emoji.txt",
        "startLine": 1,
        "startCharacter": 10,
        "endLine": 1,
        "endCharacter": 15,
        "newText": "earth",
        "expectedHash": _content_hash(path),
    }
    result = ApplyWorkspaceEditTool().invoke(
        ToolCall(tool_name="apply_workspace_edit", arguments={"edits": [edit], "positionEncoding": "utf-16"}),
        context=ToolContext(workspace=tmp_path),
    )
    assert result.status == "ok"
    assert path.read_text(encoding="utf-8") == "hello 🚀 earth\n"


def test_apply_workspace_edit_rejects_mid_surrogate_position(tmp_path: Path) -> None:
    path = tmp_path / "emoji.txt"
    path.write_text("hello 🚀 world\n", encoding="utf-8")
    # Character 8 is the second (low) surrogate of 🚀 in UTF-16
    edit = {
        "path": "emoji.txt",
        "startLine": 1,
        "startCharacter": 8,
        "endLine": 1,
        "endCharacter": 9,
        "newText": "x",
        "expectedHash": _content_hash(path),
    }
    with pytest.raises(ValueError, match="mid-surrogate"):
        ApplyWorkspaceEditTool().invoke(
            ToolCall(tool_name="apply_workspace_edit", arguments={"edits": [edit], "positionEncoding": "utf-16"}),
            context=ToolContext(workspace=tmp_path),
        )


def test_apply_workspace_edit_rejects_requested_version_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "versioned.txt"
    path.write_text("sample content\n", encoding="utf-8")
    edit = {
        "path": "versioned.txt",
        "startLine": 1,
        "startCharacter": 1,
        "endLine": 1,
        "endCharacter": 7,
        "newText": "new",
        "expectedHash": _content_hash(path),
        "expectedVersion": 42,
    }
    with pytest.raises(ValueError, match="fail-closed"):
        ApplyWorkspaceEditTool().invoke(
            _call([edit]),
            context=ToolContext(workspace=tmp_path),
        )


@pytest.mark.parametrize(
    ("encoding", "start_character", "end_character"),
    [("utf-8", 6, 7), ("utf-16", 4, 5), ("utf-32", 3, 4)],
)
def test_apply_workspace_edit_uses_open_document_encoding_and_preserves_crlf(
    tmp_path: Path,
    encoding: str,
    start_character: int,
    end_character: int,
) -> None:
    path = tmp_path / "emoji.txt"
    original = "A🚀B\r\n".encode()
    path.write_bytes(original)
    state = LspDocumentState(version=7, content_hash=hashlib.sha256(original).hexdigest(), position_encoding=encoding)
    edit = {
        "path": "emoji.txt",
        "startLine": 1,
        "startCharacter": start_character,
        "endLine": 1,
        "endCharacter": end_character,
        "newText": "X",
        "expectedHash": state.content_hash,
        "expectedVersion": 7,
    }

    result = ApplyWorkspaceEditTool().invoke(
        _call([edit]),
        context=ToolContext(
            workspace=tmp_path,
            lsp_document_state=lambda *, file_path, workspace: state,
        ),
    )

    expected = "A🚀X\r\n".encode()
    assert path.read_bytes() == expected
    assert result.body is not None
    file_result = result.body.as_payload()["files"][0]
    assert isinstance(file_result, dict)
    assert file_result["byte_count"] == len(expected)
    assert file_result["content_hash"] == hashlib.sha256(expected).hexdigest()
    assert "-A🚀B\r\n" in file_result["diff"]
    assert "+A🚀X\r\n" in file_result["diff"]


def test_apply_workspace_edit_rejects_stale_open_document_version(tmp_path: Path) -> None:
    path = tmp_path / "versioned.txt"
    original = b"sample content\n"
    path.write_bytes(original)
    edit = {
        "path": "versioned.txt",
        "startLine": 1,
        "startCharacter": 1,
        "endLine": 1,
        "endCharacter": 7,
        "newText": "new",
        "expectedHash": hashlib.sha256(original).hexdigest(),
        "expectedVersion": 6,
    }
    state = LspDocumentState(version=7, content_hash=hashlib.sha256(original).hexdigest(), position_encoding="utf-16")

    with pytest.raises(ToolDiagnosticError, match="not requested version 6"):
        ApplyWorkspaceEditTool().invoke(
            _call([edit]),
            context=ToolContext(
                workspace=tmp_path,
                lsp_document_state=lambda *, file_path, workspace: state,
            ),
        )
    assert path.read_bytes() == original
