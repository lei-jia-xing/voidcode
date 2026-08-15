from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from voidcode.tools import ApplyPatchTool, EditTool, MultiEditTool, ReadTool, ToolCall, WriteTool
from voidcode.tools._repair import ToolDiagnosticError
from voidcode.tools.contracts import ToolResult
from voidcode.tools.guards import ReadTracking, read_paths_for_tool_results, read_tracking_for_tool_results
from voidcode.tools.runtime_context import RuntimeToolInvocationContext, bind_runtime_tool_context


def _content_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_read_paths_for_tool_results_collects_successful_workspace_reads(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("sample", encoding="utf-8")

    paths = read_paths_for_tool_results(
        tool_results=(
            ToolResult(
                tool_name="read",
                status="ok",
                content="sample",
                data={"path": "sample.txt", "arguments": {"path": "sample.txt"}},
            ),
        ),
        workspace=tmp_path,
    )

    assert paths == frozenset({target.resolve().as_posix()})


def test_write_tool_rejects_overwrite_without_prior_read(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("old", encoding="utf-8")
    tool = WriteTool()

    with bind_runtime_tool_context(RuntimeToolInvocationContext(session_id="test")):
        with pytest.raises(ValueError, match="requires reading the current file before modifying it"):
            tool.invoke(
                ToolCall(tool_name="write", arguments={"path": "sample.txt", "content": "new"}),
                workspace=tmp_path,
            )


def test_write_tool_allows_overwrite_after_prior_read(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("old", encoding="utf-8")
    tool = WriteTool()
    read_paths = frozenset({target.resolve().as_posix()})
    read_lines = {target.resolve().as_posix(): frozenset({1})}

    with bind_runtime_tool_context(RuntimeToolInvocationContext(session_id="test", read_paths=read_paths, read_lines=read_lines)):
        result = tool.invoke(
            ToolCall(
                tool_name="write",
                arguments={"path": "sample.txt", "content": "new", "expectedHash": _content_hash(target)},
            ),
            workspace=tmp_path,
        )

    assert result.status == "ok"
    assert target.read_text(encoding="utf-8") == "new"


def test_write_tool_allows_new_file_without_prior_read_even_with_runtime_context(
    tmp_path: Path,
) -> None:
    tool = WriteTool()

    with bind_runtime_tool_context(RuntimeToolInvocationContext(session_id="test")):
        result = tool.invoke(
            ToolCall(
                tool_name="write",
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
        with pytest.raises(ValueError, match="requires reading the current file before modifying it"):
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
        with pytest.raises(ValueError, match="requires reading the current file before modifying it"):
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
        with pytest.raises(ValueError, match="requires reading the current file before modifying it"):
            tool.invoke(ToolCall(tool_name="apply_patch", arguments={"patch": patch}), workspace=tmp_path)


def _read_result(*, workspace: Path, path: str, offset: int | None = None, limit: int | None = None) -> ToolResult:
    arguments: dict[str, object] = {"path": path}
    if offset is not None:
        arguments["offset"] = offset
    if limit is not None:
        arguments["limit"] = limit
    return ReadTool().invoke(ToolCall(tool_name="read", arguments=arguments), workspace=workspace)


def _seen_lines(tracking: ReadTracking, target: Path) -> frozenset[int]:
    return tracking.read_lines[target.resolve().as_posix()]


def test_read_tracking_collects_exact_seen_line_numbers(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("alpha\nbeta\ngamma\ndelta\nepsilon\n", encoding="utf-8")

    tracking = read_tracking_for_tool_results(
        tool_results=(
            _read_result(workspace=tmp_path, path="sample.txt", offset=2, limit=2),
            _read_result(workspace=tmp_path, path="sample.txt"),
        ),
        workspace=tmp_path,
    )

    assert tracking.read_paths == frozenset({target.resolve().as_posix()})
    assert _seen_lines(tracking, target) == frozenset({1, 2, 3, 4, 5})


def test_read_tracking_unions_multiple_partial_reads(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("\n".join(f"line-{index}" for index in range(1, 7)), encoding="utf-8")

    tracking = read_tracking_for_tool_results(
        tool_results=(
            _read_result(workspace=tmp_path, path="sample.txt", offset=1, limit=3),
            _read_result(workspace=tmp_path, path="sample.txt", offset=4, limit=3),
        ),
        workspace=tmp_path,
    )

    assert _seen_lines(tracking, target) == frozenset({1, 2, 3, 4, 5, 6})


def test_read_tracking_ignores_attachment_reads_for_line_data(tmp_path: Path) -> None:
    image = tmp_path / "image.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"fake")

    tracking = read_tracking_for_tool_results(
        tool_results=(_read_result(workspace=tmp_path, path="image.png"),),
        workspace=tmp_path,
    )

    assert tracking.read_paths == frozenset({image.resolve().as_posix()})
    assert image.resolve().as_posix() not in tracking.read_lines


def test_write_tool_rejects_partial_read_overwrite_with_unseen_range(
    tmp_path: Path,
) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    tool = WriteTool()
    resolved = target.resolve().as_posix()

    with bind_runtime_tool_context(
        RuntimeToolInvocationContext(
            session_id="test",
            read_paths=frozenset({resolved}),
            read_lines={resolved: frozenset({1})},
        )
    ):
        with pytest.raises(ToolDiagnosticError, match="never revealed by read") as exc_info:
            tool.invoke(
                ToolCall(
                    tool_name="write",
                    arguments={"path": "sample.txt", "content": "new", "expectedHash": _content_hash(target)},
                ),
                workspace=tmp_path,
            )

    diagnostic = exc_info.value
    assert diagnostic.error_kind == "tool_input_mismatch"
    assert diagnostic.error_details["reason"] == "unseen_range"
    assert diagnostic.error_details["unseen_line_ranges"] == [{"start": 2, "end": 3}]
    assert "read" in (diagnostic.retry_guidance or "")
    assert target.read_text(encoding="utf-8") == "alpha\nbeta\ngamma\n"


def test_write_tool_rejects_fail_closed_when_path_read_but_no_line_data(
    tmp_path: Path,
) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("alpha\n", encoding="utf-8")
    tool = WriteTool()
    resolved = target.resolve().as_posix()

    with bind_runtime_tool_context(
        RuntimeToolInvocationContext(
            session_id="test",
            read_paths=frozenset({resolved}),
        )
    ):
        with pytest.raises(ToolDiagnosticError, match="never revealed by read") as exc_info:
            tool.invoke(
                ToolCall(
                    tool_name="write",
                    arguments={"path": "sample.txt", "content": "new", "expectedHash": _content_hash(target)},
                ),
                workspace=tmp_path,
            )

    diagnostic = exc_info.value
    assert diagnostic.error_kind == "tool_input_mismatch"
    assert diagnostic.error_details["reason"] == "unseen_range"
    assert target.read_text(encoding="utf-8") == "alpha\n"


def test_runtime_flow_read_tracking_grants_edit_of_seen_line(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    tracking = read_tracking_for_tool_results(
        tool_results=(_read_result(workspace=tmp_path, path="sample.txt", offset=1, limit=2),),
        workspace=tmp_path,
    )
    assert _seen_lines(tracking, target) == frozenset({1, 2})

    tool = EditTool()
    with bind_runtime_tool_context(
        RuntimeToolInvocationContext(
            session_id="test",
            read_paths=tracking.read_paths,
            read_lines=tracking.read_lines,
        )
    ):
        result = tool.invoke(
            ToolCall(
                tool_name="edit",
                arguments={"path": "sample.txt", "oldString": "beta", "newString": "BETA", "expectedHash": _content_hash(target)},
            ),
            workspace=tmp_path,
        )

    assert result.status == "ok"
    assert target.read_text(encoding="utf-8") == "alpha\nBETA\ngamma\n"


def test_runtime_flow_full_file_read_grants_full_file_edit(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    tracking = read_tracking_for_tool_results(
        tool_results=(_read_result(workspace=tmp_path, path="sample.txt"),),
        workspace=tmp_path,
    )
    assert _seen_lines(tracking, target) == frozenset({1, 2, 3})

    tool = EditTool()
    with bind_runtime_tool_context(
        RuntimeToolInvocationContext(
            session_id="test",
            read_paths=tracking.read_paths,
            read_lines=tracking.read_lines,
        )
    ):
        result = tool.invoke(
            ToolCall(
                tool_name="edit",
                arguments={"path": "sample.txt", "oldString": "gamma", "newString": "GAMMA", "expectedHash": _content_hash(target)},
            ),
            workspace=tmp_path,
        )

    assert result.status == "ok"
    assert target.read_text(encoding="utf-8") == "alpha\nbeta\nGAMMA\n"
