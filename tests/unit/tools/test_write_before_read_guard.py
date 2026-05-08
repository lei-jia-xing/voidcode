from __future__ import annotations

from pathlib import Path

import pytest

from voidcode.tools import ApplyPatchTool, EditTool, MultiEditTool, ToolCall, WriteFileTool
from voidcode.tools.contracts import ToolResult
from voidcode.tools.guards import read_paths_for_tool_results
from voidcode.tools.runtime_context import RuntimeToolInvocationContext, bind_runtime_tool_context


def test_read_paths_for_tool_results_collects_successful_workspace_reads(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("sample", encoding="utf-8")

    paths = read_paths_for_tool_results(
        tool_results=(
            ToolResult(
                tool_name="read_file",
                status="ok",
                content="sample",
                data={"path": "sample.txt", "arguments": {"filePath": "sample.txt"}},
            ),
        ),
        workspace=tmp_path,
    )

    assert paths == frozenset({target.resolve().as_posix()})


def test_write_file_tool_rejects_overwrite_without_prior_read(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("old", encoding="utf-8")
    tool = WriteFileTool()

    with bind_runtime_tool_context(RuntimeToolInvocationContext(session_id="test")):
        with pytest.raises(
            ValueError, match="requires reading the current file before modifying it"
        ):
            tool.invoke(
                ToolCall(
                    tool_name="write_file", arguments={"path": "sample.txt", "content": "new"}
                ),
                workspace=tmp_path,
            )


def test_write_file_tool_allows_overwrite_after_prior_read(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("old", encoding="utf-8")
    tool = WriteFileTool()
    read_paths = frozenset({target.resolve().as_posix()})

    with bind_runtime_tool_context(
        RuntimeToolInvocationContext(session_id="test", read_paths=read_paths)
    ):
        result = tool.invoke(
            ToolCall(tool_name="write_file", arguments={"path": "sample.txt", "content": "new"}),
            workspace=tmp_path,
        )

    assert result.status == "ok"
    assert target.read_text(encoding="utf-8") == "new"


def test_write_file_tool_allows_new_file_without_prior_read_even_with_runtime_context(
    tmp_path: Path,
) -> None:
    tool = WriteFileTool()

    with bind_runtime_tool_context(RuntimeToolInvocationContext(session_id="test")):
        result = tool.invoke(
            ToolCall(
                tool_name="write_file",
                arguments={"path": "new-file.txt", "content": "hello"},
            ),
            workspace=tmp_path,
        )

    assert result.status == "ok"
    assert (tmp_path / "new-file.txt").read_text(encoding="utf-8") == "hello"


def test_edit_tool_rejects_modify_without_prior_read(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("old", encoding="utf-8")
    tool = EditTool()

    with bind_runtime_tool_context(RuntimeToolInvocationContext(session_id="test")):
        with pytest.raises(
            ValueError, match="requires reading the current file before modifying it"
        ):
            tool.invoke(
                ToolCall(
                    tool_name="edit",
                    arguments={"path": "sample.txt", "oldString": "old", "newString": "new"},
                ),
                workspace=tmp_path,
            )


def test_multi_edit_rejects_modify_without_prior_read(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("old", encoding="utf-8")
    tool = MultiEditTool()

    with bind_runtime_tool_context(RuntimeToolInvocationContext(session_id="test")):
        with pytest.raises(
            ValueError, match="requires reading the current file before modifying it"
        ):
            tool.invoke(
                ToolCall(
                    tool_name="multi_edit",
                    arguments={
                        "path": "sample.txt",
                        "edits": [{"oldString": "old", "newString": "new"}],
                    },
                ),
                workspace=tmp_path,
            )


def test_apply_patch_rejects_modify_without_prior_read(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("old\n", encoding="utf-8")
    tool = ApplyPatchTool()
    patch = "\n".join(
        [
            "*** Begin Patch",
            "*** Update File: sample.txt",
            "@@",
            "-old",
            "+new",
            "*** End Patch",
        ]
    )

    with bind_runtime_tool_context(RuntimeToolInvocationContext(session_id="test")):
        with pytest.raises(
            ValueError, match="requires reading the current file before modifying it"
        ):
            tool.invoke(
                ToolCall(tool_name="apply_patch", arguments={"patch": patch}), workspace=tmp_path
            )
