from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

from voidcode.hook.config import RuntimeFormatterPresetConfig, RuntimeHooksConfig
from voidcode.tools import MultiEditTool, ToolCall


def test_multi_edit_applies_multiple_edits_in_order(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("alpha\nbeta\nalpha\n", encoding="utf-8")

    tool = MultiEditTool()
    result = tool.invoke(
        ToolCall(
            tool_name="multi_edit",
            arguments={
                "path": "sample.txt",
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
            formatter_presets={
                "python": RuntimeFormatterPresetConfig(
                    command=(sys.executable, str(formatter_script)),
                    extensions=(".py",),
                )
            }
        )
    )
    result = tool.invoke(
        ToolCall(
            tool_name="multi_edit",
            arguments={
                "path": "sample.py",
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
