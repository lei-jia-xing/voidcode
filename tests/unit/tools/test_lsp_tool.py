from __future__ import annotations

from pathlib import Path

import pytest

from voidcode.core.tool_context import LspResponse, ToolContext
from voidcode.tools.contracts import ToolCall, ToolFailure
from voidcode.tools.lsp import LspOperation, LspTool, _render_lsp_output


def test_rename_preview_paginates_all_edits(tmp_path: Path) -> None:
    edits = [
        {
            "range": {
                "start": {"line": index, "character": 0},
                "end": {"line": index, "character": 1},
            },
            "newText": "renamed",
        }
        for index in range(51)
    ]
    payload = {"documentChanges": [{"textDocument": {"uri": (tmp_path / "sample.py").as_uri(), "version": 9}, "edits": edits}]}
    first_page = _render_lsp_output(
        LspOperation.RENAME,
        payload,
        file_path="sample.py",
        workspace_root=tmp_path,
        new_name="renamed",
    )
    second_page = _render_lsp_output(
        LspOperation.RENAME,
        payload,
        file_path="sample.py",
        workspace_root=tmp_path,
        new_name="renamed",
        offset=51,
        limit=50,
    )

    assert first_page[1:] == (51, True)
    assert first_page[0].count(" -> renamed") == 50
    assert "document version 9" in first_page[0]
    assert second_page[1:] == (51, False)
    assert second_page[0].count(" -> renamed") == 1
    assert "line 51:1-51:2 -> renamed" in second_page[0]


@pytest.mark.parametrize(
    ("operation", "payload", "expected"),
    [
        (LspOperation.DIAGNOSTICS, [{"message": "first"}, {"message": "second"}], "second"),
        (
            LspOperation.GO_TO_DEFINITION,
            [
                {"uri": "file:///tmp/first.py", "range": {"start": {"line": 0, "character": 0}}},
                {"uri": "file:///tmp/second.py", "range": {"start": {"line": 1, "character": 0}}},
            ],
            "second.py",
        ),
        (
            LspOperation.FIND_REFERENCES,
            [
                {"uri": "file:///tmp/first.py", "range": {"start": {"line": 0, "character": 0}}},
                {"uri": "file:///tmp/second.py", "range": {"start": {"line": 1, "character": 0}}},
            ],
            "second.py",
        ),
        (LspOperation.DOCUMENT_SYMBOL, [{"name": "first"}, {"name": "second"}], "second"),
        (LspOperation.WORKSPACE_SYMBOL, [{"name": "first"}, {"name": "second"}], "second"),
        (LspOperation.CODE_ACTION, [{"title": "first"}, {"title": "second"}], "second"),
        (
            LspOperation.WILL_RENAME_FILES,
            {"changes": {"file:///tmp/sample.py": [{"newText": "first"}, {"newText": "second"}]}},
            "second",
        ),
        (
            LspOperation.INCOMING_CALLS,
            [{"from": {"name": "first"}}, {"from": {"name": "second"}}],
            "second",
        ),
        (
            LspOperation.OUTGOING_CALLS,
            [{"to": {"name": "first"}}, {"to": {"name": "second"}}],
            "second",
        ),
    ],
)
def test_list_lsp_results_page_by_offset(
    tmp_path: Path,
    operation: LspOperation,
    payload: object,
    expected: str,
) -> None:
    text, item_count, truncated = _render_lsp_output(
        operation,
        payload,
        file_path="sample.py",
        workspace_root=tmp_path,
        line=1,
        character=1,
        offset=2,
        limit=1,
    )

    assert item_count == 2
    assert truncated is False
    assert expected in text


def test_unavailable_lsp_request_returns_tool_failure(tmp_path: Path) -> None:
    (tmp_path / "sample.py").write_text("value = 1\n", encoding="utf-8")

    def no_server(
        *,
        server_name: str | None,
        method: str,
        params: dict[str, object],
        workspace: Path,
    ) -> LspResponse:
        _ = server_name, method, params, workspace
        raise ValueError("no LSP server is configured")

    result = LspTool().invoke(
        ToolCall(
            tool_name="lsp",
            arguments={"operation": "hover", "path": "sample.py", "line": 1, "character": 1},
        ),
        context=ToolContext(workspace=tmp_path, lsp_request=no_server),
    )

    assert isinstance(result, ToolFailure)
    assert result.error == "no LSP server is configured"
