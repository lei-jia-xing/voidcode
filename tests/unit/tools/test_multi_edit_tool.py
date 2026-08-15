from __future__ import annotations

import hashlib
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest

from voidcode.formatter import RuntimeFormatterPresetConfig
from voidcode.hook.config import RuntimeHooksConfig
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


def test_multi_edit_rejects_missing_path(tmp_path: Path) -> None:
    tool = MultiEditTool()

    with pytest.raises(ValueError, match="string path"):
        tool.invoke(
            ToolCall(tool_name="multi_edit", arguments={"edits": []}),
            workspace=tmp_path,
        )


def test_multi_edit_rejects_non_list_edits(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("alpha\n", encoding="utf-8")
    tool = MultiEditTool()

    with pytest.raises(ValueError, match="array edits"):
        tool.invoke(
            ToolCall(
                tool_name="multi_edit",
                arguments={"path": "sample.txt", "edits": "bad"},
            ),
            workspace=tmp_path,
        )


def test_multi_edit_rejects_empty_edits(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("alpha\n", encoding="utf-8")
    tool = MultiEditTool()

    with pytest.raises(ValueError, match="at least one edit"):
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


def test_multi_edit_formats_once_after_all_edits(tmp_path: Path) -> None:
    target = tmp_path / "sample.py"
    target.write_text("value = 'a'\nother = 'b'\n", encoding="utf-8")
    formatter_script = tmp_path / "formatter.py"
    formatter_script.write_text(
        textwrap.dedent(
            """
            import pathlib
            import sys

            pathlib.Path(sys.argv[-1]).write_text("VALUE='A'\\nOTHER='B'\\n", encoding="utf-8")
            """
        ),
        encoding="utf-8",
    )

    tool = MultiEditTool(
        hooks_config=RuntimeHooksConfig(
            format_on_write=True,
            formatter_presets={
                "python": RuntimeFormatterPresetConfig(
                    command=(sys.executable, str(formatter_script)),
                    extensions=(".py",),
                )
            },
        )
    )
    result = tool.invoke(
        ToolCall(
            tool_name="multi_edit",
            arguments={
                "path": "sample.py",
                "expectedHash": _content_hash(target),
                "edits": [
                    {"oldString": "'a'", "newString": "'A'"},
                    {"oldString": "'b'", "newString": "'B'"},
                ],
            },
        ),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert target.read_text(encoding="utf-8") == "VALUE='A'\nOTHER='B'\n"
    assert result.data["applied"] == 2
    assert result.data["formatter"] == {
        "status": "formatted",
        "language": "python",
        "cwd": str(tmp_path),
        "command": [sys.executable, str(formatter_script), str(target)],
        "attempted_commands": [[sys.executable, str(formatter_script), str(target)]],
    }
    assert "VALUE='A'" in str(result.data["diff"])


def test_multi_edit_skips_formatter_when_hooks_are_disabled(tmp_path: Path) -> None:
    target = tmp_path / "sample.py"
    target.write_text("value = 'a'\nother = 'b'\n", encoding="utf-8")

    tool = MultiEditTool(
        hooks_config=RuntimeHooksConfig(
            enabled=False,
            formatter_presets={
                "python": RuntimeFormatterPresetConfig(
                    command=("missing-formatter-binary",),
                    extensions=(".py",),
                )
            },
        )
    )
    result = tool.invoke(
        ToolCall(
            tool_name="multi_edit",
            arguments={
                "path": "sample.py",
                "expectedHash": _content_hash(target),
                "edits": [
                    {"oldString": "'a'", "newString": "'A'"},
                    {"oldString": "'b'", "newString": "'B'"},
                ],
            },
        ),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert result.data["applied"] == 2
    assert "formatter" not in result.data
    assert "diagnostics" not in result.data
    assert target.read_text(encoding="utf-8") == "value = 'A'\nother = 'B'\n"


def test_multi_edit_keeps_edits_successful_when_formatter_is_missing(
    tmp_path: Path,
) -> None:
    target = tmp_path / "sample.py"
    target.write_text("value = 'a'\nother = 'b'\n", encoding="utf-8")
    tool = MultiEditTool(
        hooks_config=RuntimeHooksConfig(
            format_on_write=True,
            formatter_presets={
                "python": RuntimeFormatterPresetConfig(
                    command=("missing-formatter-binary",),
                    extensions=(".py",),
                )
            },
        )
    )

    result = tool.invoke(
        ToolCall(
            tool_name="multi_edit",
            arguments={
                "path": "sample.py",
                "expectedHash": _content_hash(target),
                "edits": [
                    {"oldString": "'a'", "newString": "'A'"},
                    {"oldString": "'b'", "newString": "'B'"},
                ],
            },
        ),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert target.read_text(encoding="utf-8") == "value = 'A'\nother = 'B'\n"
    assert "Formatter warning:" in (result.content or "")
    formatter = cast(dict[str, object], result.data["formatter"])
    assert formatter["status"] == "missing_executable"
    diagnostics = result.data["diagnostics"]
    assert isinstance(diagnostics, list)
    assert "No formatter executable was available" in str(diagnostics[0])


def test_multi_edit_skips_formatter_when_formatter_is_disabled_by_hooks_config(
    tmp_path: Path,
) -> None:
    target = tmp_path / "sample.py"
    target.write_text("value = 'a'\nother = 'b'\n", encoding="utf-8")
    formatter_marker = tmp_path / "formatter-ran.txt"
    formatter_script = tmp_path / "formatter.py"
    formatter_script.write_text(
        textwrap.dedent(
            f"""
            import pathlib

            pathlib.Path({str(formatter_marker)!r}).write_text("ran", encoding="utf-8")
            """
        ),
        encoding="utf-8",
    )
    tool = MultiEditTool(
        hooks_config=RuntimeHooksConfig(
            enabled=False,
            formatter_presets={
                "python": RuntimeFormatterPresetConfig(
                    command=(sys.executable, str(formatter_script)),
                    extensions=(".py",),
                )
            },
        )
    )

    result = tool.invoke(
        ToolCall(
            tool_name="multi_edit",
            arguments={
                "path": "sample.py",
                "expectedHash": _content_hash(target),
                "edits": [
                    {"oldString": "'a'", "newString": "'A'"},
                    {"oldString": "'b'", "newString": "'B'"},
                ],
            },
        ),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert not formatter_marker.exists()
    assert "formatter" not in result.data
    assert "diagnostics" not in result.data
    assert target.read_text(encoding="utf-8") == "value = 'A'\nother = 'B'\n"


def test_multi_edit_keeps_edits_successful_when_formatter_times_out(tmp_path: Path) -> None:
    target = tmp_path / "sample.py"
    target.write_text("value = 'a'\nother = 'b'\n", encoding="utf-8")

    tool = MultiEditTool(
        hooks_config=RuntimeHooksConfig(
            format_on_write=True,
            formatter_presets={
                "python": RuntimeFormatterPresetConfig(
                    command=("slow-formatter",),
                    extensions=(".py",),
                )
            },
        )
    )

    with patch(
        "voidcode.formatter.executor.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["slow-formatter"], timeout=10.0),
    ):
        result = tool.invoke(
            ToolCall(
                tool_name="multi_edit",
                arguments={
                    "path": "sample.py",
                    "expectedHash": _content_hash(target),
                    "edits": [
                        {"oldString": "'a'", "newString": "'A'"},
                        {"oldString": "'b'", "newString": "'B'"},
                    ],
                },
            ),
            workspace=tmp_path,
        )

    assert result.status == "ok"
    assert target.read_text(encoding="utf-8") == "value = 'A'\nother = 'B'\n"
    diagnostics = result.data["diagnostics"]
    assert isinstance(diagnostics, list)
    first_diagnostic = cast(dict[str, object], diagnostics[0])
    assert "timed out after 30.0s" in str(first_diagnostic["message"])


def test_multi_edit_rejects_missing_expected_hash_with_structured_diagnostic(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("alpha\n", encoding="utf-8")
    tool = MultiEditTool()

    with pytest.raises(ToolDiagnosticError, match="expectedHash") as exc_info:
        tool.invoke(
            ToolCall(
                tool_name="multi_edit",
                arguments={
                    "path": "sample.txt",
                    "edits": [{"oldString": "alpha", "newString": "ALPHA"}],
                },
            ),
            workspace=tmp_path,
        )

    diagnostic = exc_info.value
    assert diagnostic.error_kind == "tool_input_mismatch"
    assert diagnostic.error_details["reason"] == "missing_expected_hash"
    assert diagnostic.error_details["path"] == "sample.txt"
    assert "read" in (diagnostic.retry_guidance or "")
    assert target.read_text(encoding="utf-8") == "alpha\n"


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


def test_multi_edit_schema_requires_expected_hash() -> None:
    schema = MultiEditTool.definition.input_schema
    assert "expectedHash" in schema
    assert schema["required"] == ["path", "edits", "expectedHash"]
    assert "data.content_hash" in str(schema["expectedHash"]["description"])


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


def test_multi_edit_applies_edits_when_all_lines_are_seen(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    tool = MultiEditTool()
    resolved = target.resolve().as_posix()

    with bind_runtime_tool_context(
        RuntimeToolInvocationContext(
            session_id="test",
            read_paths=frozenset({resolved}),
            read_lines={resolved: frozenset({1, 2, 3})},
        )
    ):
        result = tool.invoke(
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

    assert result.status == "ok"
    assert result.data["applied"] == 2
    assert target.read_text(encoding="utf-8") == "ALPHA\nbeta\nGAMMA\n"
