from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from voidcode.tools import MultiEditTool, ToolCall
from voidcode.tools._repair import ToolDiagnosticError
from voidcode.tools.runtime_context import RuntimeToolInvocationContext, bind_runtime_tool_context


def _content_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_multi_edit_applies_multiple_edits_in_order(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("alpha\nbeta\nalpha\n", encoding="utf-8")

    tool = MultiEditTool()
    result = tool.invoke(
        ToolCall(
            tool_name="multi_edit",
            arguments={
                "path": "sample.txt",
                "expectedHash": _content_hash(target),
                "edits": [
                    {"oldString": "alpha", "newString": "ALPHA", "replaceAll": True},
                    {"oldString": "beta", "newString": "BETA"},
                ],
            },
        ),
        workspace=tmp_path,
    )

    content = target.read_text(encoding="utf-8")
    assert "ALPHA" in content
    assert "BETA" in content
    assert result.status == "ok"
    assert result.data["applied"] == 2


def test_multi_edit_rejects_empty_edits(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("alpha\n", encoding="utf-8")
    tool = MultiEditTool()

    with pytest.raises(ValueError, match="Validation error"):
        tool.invoke(
            ToolCall(
                tool_name="multi_edit",
                arguments={"path": "sample.txt", "edits": []},
            ),
            workspace=tmp_path,
        )


def test_multi_edit_reports_failing_edit_index_with_underlying_diagnostic(
    tmp_path: Path,
) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    tool = MultiEditTool()

    with pytest.raises(ValueError, match="failed at edit #2") as exc_info:
        tool.invoke(
            ToolCall(
                tool_name="multi_edit",
                arguments={
                    "path": "sample.txt",
                    "expectedHash": _content_hash(target),
                    "edits": [
                        {"oldString": "alpha", "newString": "ALPHA"},
                        {"oldString": "2: beta", "newString": "BETA"},
                    ],
                },
            ),
            workspace=tmp_path,
        )

    message = str(exc_info.value)
    assert "Applied edits before failure: 1" in message
    assert "Underlying edit diagnostic" in message
    assert "oldString appears to include read output line prefixes" in message
    assert target.read_text(encoding="utf-8") == "ALPHA\nbeta\ngamma\n"


def test_multi_edit_rejects_stale_expected_hash_before_any_edit(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("alpha\nbeta\n", encoding="utf-8")
    tool = MultiEditTool()

    with pytest.raises(ToolDiagnosticError, match="stale edit") as exc_info:
        tool.invoke(
            ToolCall(
                tool_name="multi_edit",
                arguments={
                    "path": "sample.txt",
                    "expectedHash": "0" * 64,
                    "edits": [{"oldString": "alpha", "newString": "ALPHA"}],
                },
            ),
            workspace=tmp_path,
        )

    diagnostic = exc_info.value
    assert diagnostic.error_kind == "stale_edit"
    assert diagnostic.error_details["reason"] == "content_hash_mismatch"
    assert diagnostic.error_details["expected_hash"] == "0" * 64
    assert diagnostic.error_details["actual_hash"] == _content_hash(target)
    assert diagnostic.error_details["path"] == "sample.txt"
    assert "data.content_hash" in (diagnostic.retry_guidance or "")
    assert target.read_text(encoding="utf-8") == "alpha\nbeta\n"


def test_multi_edit_rejects_edit_of_line_outside_read_window(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    tool = MultiEditTool()
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
                    tool_name="multi_edit",
                    arguments={
                        "path": "sample.txt",
                        "expectedHash": _content_hash(target),
                        "edits": [
                            {"oldString": "alpha", "newString": "ALPHA"},
                            {"oldString": "gamma", "newString": "GAMMA"},
                        ],
                    },
                ),
                workspace=tmp_path,
            )

    diagnostic = exc_info.value
    assert diagnostic.error_kind == "tool_input_mismatch"
    assert diagnostic.error_details["reason"] == "unseen_range"
    assert diagnostic.error_details["unseen_line_ranges"] == [{"start": 3, "end": 3}]
    # The earlier (seen) edit applied before the unseen edit was rejected.
    assert target.read_text(encoding="utf-8") == "ALPHA\nbeta\ngamma\n"
