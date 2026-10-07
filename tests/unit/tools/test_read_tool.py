from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

import pytest

from voidcode.core.tool_context import ArtifactMissing, ToolContext
from voidcode.core.transcript import output_text
from voidcode.core.turns import report_call
from voidcode.runtime.context.rules import runtime_file_rule_contexts
from voidcode.runtime.execution.report_codec import parse_report_payload, report_payload
from voidcode.tools.contracts import ToolCall
from voidcode.tools.read import ReadTool


def test_read_tool_reads_text_file_with_offset_and_limit(tmp_path: Path) -> None:
    sample = tmp_path / "sample.txt"
    _ = sample.write_text("alpha\nbeta\ngamma\ndelta\n", encoding="utf-8")
    tool = ReadTool()

    result = tool.invoke(
        ToolCall(tool_name="read", arguments={"path": "sample.txt", "offset": 2, "limit": 2}),
        context=ToolContext(workspace=tmp_path),
    )

    assert result.status == "ok"
    output = output_text(result.output)
    assert output is not None
    assert "beta\ngamma" in output
    assert "alpha" not in output and "delta" not in output
    assert hashlib.sha256(sample.read_bytes()).hexdigest() in output


def test_read_tool_lists_directory_tree_and_marks_empty_directory(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    subdir = tmp_path / "subdir"
    subdir.mkdir()
    (subdir / "nested.txt").write_text("nested", encoding="utf-8")
    (tmp_path / "empty").mkdir()

    tool = ReadTool()

    result = tool.invoke(ToolCall(tool_name="read", arguments={"path": "."}), context=ToolContext(workspace=tmp_path))

    output = output_text(result.output)
    assert output is not None
    assert "a.txt" in output
    assert "subdir/" in output
    assert "nested.txt" in output
    assert "empty/" in output
    assert "(empty directory)" in output


def test_read_tool_rejects_archive_path_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "bundle.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        _ = handle.writestr("safe.txt", "safe\n")

    tool = ReadTool()

    with pytest.raises(ValueError, match=r"must not contain '\.\.'"):
        tool.invoke(ToolCall(tool_name="read", arguments={"path": "bundle.zip:../outside.txt"}), context=ToolContext(workspace=tmp_path))


def test_read_tool_lists_and_decodes_archive_members(tmp_path: Path) -> None:
    archive = tmp_path / "bundle.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        _ = handle.writestr("src/main.py", "line one\nline two\n")
        _ = handle.writestr("notes.txt", "note\n")

    tool = ReadTool()

    listing = tool.invoke(ToolCall(tool_name="read", arguments={"path": "bundle.zip"}), context=ToolContext(workspace=tmp_path))
    listing_output = output_text(listing.output)
    assert listing_output is not None
    assert "main.py" not in listing_output
    assert "src/" in listing_output
    assert "notes.txt" in listing_output

    member = tool.invoke(ToolCall(tool_name="read", arguments={"path": "bundle.zip:src/main.py"}), context=ToolContext(workspace=tmp_path))
    member_output = output_text(member.output)
    assert member_output is not None and "line one\nline two" in member_output


def test_read_tool_reports_non_utf8_archive_member_without_raising(tmp_path: Path) -> None:
    archive = tmp_path / "bundle.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        _ = handle.writestr("blob.bin", b"\xff\xfe\x00\x01")

    tool = ReadTool()

    result = tool.invoke(ToolCall(tool_name="read", arguments={"path": "bundle.zip:blob.bin"}), context=ToolContext(workspace=tmp_path))

    assert result.status == "ok"
    output = output_text(result.output)
    assert output is not None
    assert "not UTF-8 text" in output
    assert "4 bytes" in output


def test_read_tool_allows_workspace_escape_path_with_absolute_display(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-read.txt"
    outside.write_text("outside", encoding="utf-8")
    tool = ReadTool()

    result = tool.invoke(
        ToolCall(tool_name="read", arguments={"path": "../outside-read.txt"}),
        context=ToolContext(workspace=tmp_path),
    )

    output = output_text(result.output)
    assert output is not None and "outside" in output


def test_read_tool_reports_missing_file_path(tmp_path: Path) -> None:
    tool = ReadTool()

    with pytest.raises(ValueError):
        tool.invoke(ToolCall(tool_name="read", arguments={}), context=ToolContext(workspace=tmp_path))


class _FakeArtifactFacade:
    def read_artifact(
        self,
        *,
        caller_session_id: str,
        artifact_id: str,
        offset: int | None = None,
        limit: int | None = None,
    ) -> ArtifactMissing:
        _ = caller_session_id, artifact_id, offset, limit
        return ArtifactMissing()


def test_read_tool_rejects_unknown_artifact_id(tmp_path: Path) -> None:
    facade = _FakeArtifactFacade()

    context = ToolContext(workspace=tmp_path, session_id="session-1", artifact=facade)
    tool = ReadTool()
    with pytest.raises(ValueError):
        tool.invoke(
            ToolCall(
                tool_name="read",
                arguments={"path": "voidcode://artifact/artifact_ffffffffffffffffffffffff"},
            ),
            context=context,
        )


def test_replayed_read_selects_only_authorized_path_rule(tmp_path: Path) -> None:
    for directory, rule in (("first", "First package rule"), ("second", "Second package rule")):
        package = tmp_path / directory
        package.mkdir()
        (package / "AGENTS.md").write_text(rule, encoding="utf-8")
        (package / "sample.txt").write_text("observed\n", encoding="utf-8")
    for directory, rule in (("first", "First package rule"), ("second", "Second package rule")):
        arguments = {"path": f"{directory}/sample.txt"}
        call = ToolCall(tool_name="read", arguments=arguments, tool_call_id=f"read-{directory}")
        result = ReadTool().invoke(call, context=ToolContext(workspace=tmp_path))
        report = report_call(call, result, final_arguments=arguments, final_tool_name="read")
        restored = parse_report_payload(report_payload(report))
        contexts = runtime_file_rule_contexts(workspace=tmp_path, tool_results=(restored,), include_workspace_root=False)
        assert [(context.path, context.content) for context in contexts] == [(f"{directory}/AGENTS.md", rule)]
