from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from voidcode.core.tool_context import ToolContext
from voidcode.core.turns import ReportedCall
from voidcode.runtime.execution.report_codec import parse_report_payload, report_payload
from voidcode.tools._repair import ToolDiagnosticError
from voidcode.tools.apply_patch import ApplyPatchTool
from voidcode.tools.contracts import ToolCall
from voidcode.tools.edit import EditTool
from voidcode.tools.guards import ReadTracking, read_tracking_for_tool_results
from voidcode.tools.multi_edit import MultiEditTool
from voidcode.tools.read import MAX_BYTES, MAX_LINE_LENGTH, ReadResultBody, ReadTool
from voidcode.tools.write import WriteTool


def _content_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_read_paths_for_tool_results_collects_successful_workspace_reads(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("sample", encoding="utf-8")

    paths = read_tracking_for_tool_results(
        tool_results=(_read_result(workspace=tmp_path, path="sample.txt"),),
        workspace=tmp_path,
    ).read_paths

    assert paths == frozenset({target.resolve().as_posix()})


_UPDATE_PATCH = "\n".join(
    [
        "*** Begin Patch",
        "*** Update File: sample.txt",
        "@@",
        "-old",
        "+new",
        "*** End Patch",
    ]
)


@pytest.mark.parametrize(
    ("tool_name", "tool", "arguments"),
    (
        pytest.param("write", WriteTool(), {"path": "sample.txt", "content": "new"}, id="write"),
        pytest.param(
            "edit",
            EditTool(),
            {"path": "sample.txt", "oldString": "old", "newString": "new"},
            id="edit",
        ),
        pytest.param(
            "multi_edit",
            MultiEditTool(),
            {"path": "sample.txt", "edits": [{"oldString": "old", "newString": "new"}]},
            id="multi-edit",
        ),
        pytest.param("apply_patch", ApplyPatchTool(), {"patch": _UPDATE_PATCH}, id="apply-patch"),
    ),
)
def test_mutating_tools_reject_modify_without_prior_read(
    tmp_path: Path,
    tool_name: str,
    tool: Any,
    arguments: dict[str, object],
) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("old", encoding="utf-8")

    context = ToolContext(workspace=tmp_path, session_id="test")
    with pytest.raises(ValueError, match="requires reading the current file before modifying it"):
        tool.invoke(ToolCall(tool_name=tool_name, arguments=arguments), context=context)


def test_write_tool_allows_overwrite_after_prior_read(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("old", encoding="utf-8")
    tool = WriteTool()
    content_hash = _content_hash(target)
    observation = (target.resolve().as_posix(), content_hash)
    read_lines = {observation: frozenset({1})}

    context = ToolContext(
        workspace=tmp_path,
        session_id="test",
        read_paths=frozenset({observation[0]}),
        read_lines=read_lines,
        read_whole_files=frozenset({observation}),
        read_hash=content_hash,
    )
    result = tool.invoke(
        ToolCall(
            tool_name="write",
            arguments={"path": "sample.txt", "content": "new", "expectedHash": _content_hash(target)},
        ),
        context=context,
    )

    assert result.status == "ok"
    assert target.read_text(encoding="utf-8") == "new"


def test_write_tool_allows_new_file_without_prior_read_with_explicit_context(
    tmp_path: Path,
) -> None:
    tool = WriteTool()

    result = tool.invoke(
        ToolCall(
            tool_name="write",
            arguments={"path": "new-file.txt", "content": "hello"},
        ),
        context=ToolContext(workspace=tmp_path, session_id="test"),
    )

    assert result.status == "ok"
    assert (tmp_path / "new-file.txt").read_text(encoding="utf-8") == "hello"


def _read_result(*, workspace: Path, path: str, offset: int | None = None, limit: int | None = None) -> ReportedCall:
    arguments: dict[str, object] = {"path": path}
    if offset is not None:
        arguments["offset"] = offset
    if limit is not None:
        arguments["limit"] = limit
    result = ReadTool().invoke(ToolCall(tool_name="read", arguments=arguments), context=ToolContext(workspace=workspace))
    return ReportedCall("read-call", "read", arguments, result)


def _seen_lines(tracking: ReadTracking, target: Path) -> frozenset[int]:
    path = target.resolve().as_posix()
    keys = [key for key in tracking.read_lines if key[0] == path]
    assert len(keys) == 1
    return tracking.read_lines[keys[0]]


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
    assert tracking.read_whole_files == frozenset({(target.resolve().as_posix(), _content_hash(target))})


def test_read_tracking_ignores_attachment_reads_for_line_data(tmp_path: Path) -> None:
    image = tmp_path / "image.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    tracking = read_tracking_for_tool_results(
        tool_results=(_read_result(workspace=tmp_path, path="image.png"),),
        workspace=tmp_path,
    )

    assert tracking.read_paths == frozenset({image.resolve().as_posix()})
    assert all(path != image.resolve().as_posix() for path, _ in tracking.read_lines)


def test_read_tracking_does_not_reuse_lines_from_another_content_hash(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("alpha\nbeta\n", encoding="utf-8")
    tracking = read_tracking_for_tool_results(
        tool_results=(_read_result(workspace=tmp_path, path="sample.txt"),),
        workspace=tmp_path,
    )
    target.write_text("gamma\ndelta\n", encoding="utf-8")
    new_hash = _content_hash(target)

    context = ToolContext(
        workspace=tmp_path,
        session_id="test",
        read_paths=tracking.read_paths,
        read_lines=tracking.read_lines,
        read_hash=new_hash,
    )
    with pytest.raises(ToolDiagnosticError, match="never revealed by read"):
        EditTool().invoke(
            ToolCall(
                tool_name="edit",
                arguments={
                    "path": "sample.txt",
                    "oldString": "gamma",
                    "newString": "GAMMA",
                    "expectedHash": new_hash,
                },
            ),
            context=context,
        )
    assert target.read_text(encoding="utf-8") == "gamma\ndelta\n"


def test_write_tool_rejects_partial_read_overwrite_with_unseen_range(
    tmp_path: Path,
) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    tool = WriteTool()
    resolved = target.resolve().as_posix()

    read_hash = _content_hash(target)
    context = ToolContext(
        workspace=tmp_path,
        session_id="test",
        read_paths=frozenset({resolved}),
        read_lines={(resolved, read_hash): frozenset({1})},
        read_hash=read_hash,
    )
    with pytest.raises(ToolDiagnosticError, match="never revealed by read") as exc_info:
        tool.invoke(
            ToolCall(
                tool_name="write",
                arguments={"path": "sample.txt", "content": "new", "expectedHash": _content_hash(target)},
            ),
            context=context,
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

    context = ToolContext(workspace=tmp_path, session_id="test", read_paths=frozenset({resolved}))
    with pytest.raises(ToolDiagnosticError, match="never revealed by read") as exc_info:
        tool.invoke(
            ToolCall(
                tool_name="write",
                arguments={"path": "sample.txt", "content": "new", "expectedHash": _content_hash(target)},
            ),
            context=context,
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
    read_hash = _content_hash(target)
    context = ToolContext(
        workspace=tmp_path,
        session_id="test",
        read_paths=tracking.read_paths,
        read_lines=tracking.read_lines,
        read_hash=read_hash,
    )
    result = tool.invoke(
        ToolCall(
            tool_name="edit",
            arguments={"path": "sample.txt", "oldString": "beta", "newString": "BETA", "expectedHash": read_hash},
        ),
        context=context,
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
    read_hash = _content_hash(target)
    assert tracking.read_whole_files == frozenset({(target.resolve().as_posix(), read_hash)})

    tool = EditTool()
    context = ToolContext(
        workspace=tmp_path,
        session_id="test",
        read_paths=tracking.read_paths,
        read_lines=tracking.read_lines,
        read_whole_files=tracking.read_whole_files,
        read_hash=read_hash,
    )
    result = tool.invoke(
        ToolCall(
            tool_name="edit",
            arguments={"path": "sample.txt", "oldString": "gamma", "newString": "GAMMA", "expectedHash": _content_hash(target)},
        ),
        context=context,
    )

    assert result.status == "ok"
    assert target.read_text(encoding="utf-8") == "alpha\nbeta\nGAMMA\n"


def test_replayed_clipped_read_refuses_overwrite(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    original = "x" * (MAX_LINE_LENGTH + 1) + "\ntail\n"
    target.write_text(original, encoding="utf-8")
    report = _read_result(workspace=tmp_path, path="sample.txt")
    restored = parse_report_payload(report_payload(report))
    tracking = read_tracking_for_tool_results(tool_results=(restored,), workspace=tmp_path)
    assert _seen_lines(tracking, target) == frozenset({2})
    context = ToolContext(
        workspace=tmp_path,
        session_id="test",
        read_paths=tracking.read_paths,
        read_lines=tracking.read_lines,
        read_whole_files=tracking.read_whole_files,
        read_hash=_content_hash(target),
    )
    with pytest.raises(ToolDiagnosticError):
        WriteTool().invoke(
            ToolCall(tool_name="write", arguments={"path": "sample.txt", "content": "replacement", "expectedHash": _content_hash(target)}),
            context=context,
        )
    assert target.read_text(encoding="utf-8") == original


def test_byte_bounded_replayed_pages_grant_write_only_after_full_coverage(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    original = ("x" * MAX_LINE_LENGTH + "\n") * (MAX_BYTES // (MAX_LINE_LENGTH + 1) + 2)
    target.write_text(original, encoding="utf-8")
    first = parse_report_payload(report_payload(_read_result(workspace=tmp_path, path="sample.txt")))
    assert isinstance(first.result.body, ReadResultBody)
    next_offset = first.result.body.next_offset
    assert next_offset is not None
    last = parse_report_payload(report_payload(_read_result(workspace=tmp_path, path="sample.txt", offset=next_offset)))
    digest = _content_hash(target)
    for reports, allowed in (((first,), False), ((first, last), True)):
        tracking = read_tracking_for_tool_results(tool_results=reports, workspace=tmp_path)
        context = ToolContext(
            workspace=tmp_path,
            session_id="test",
            read_paths=tracking.read_paths,
            read_lines=tracking.read_lines,
            read_whole_files=tracking.read_whole_files,
            read_hash=digest,
        )
        call = ToolCall(tool_name="write", arguments={"path": "sample.txt", "content": "replacement", "expectedHash": digest})
        if allowed:
            WriteTool().invoke(call, context=context)
            assert target.read_text(encoding="utf-8") == "replacement"
        else:
            with pytest.raises(ToolDiagnosticError):
                WriteTool().invoke(call, context=context)
            assert target.read_text(encoding="utf-8") == original
