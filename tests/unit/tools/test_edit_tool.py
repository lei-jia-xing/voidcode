from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest

from voidcode.hook.config import RuntimeFormatterPresetConfig, RuntimeHooksConfig
from voidcode.runtime.service import ToolRegistry
from voidcode.tools import EditTool, ToolCall


def test_edit_tool_replaces_exact_text(tmp_path: Path) -> None:
    file_path = tmp_path / "test.txt"
    file_path.write_text("hello world", encoding="utf-8")

    tool = EditTool()

    result = tool.invoke(
        ToolCall(
            tool_name="edit",
            arguments={
                "path": "test.txt",
                "oldString": "world",
                "newString": "voidcode",
            },
        ),
        workspace=tmp_path,
    )

    assert result.tool_name == "edit"
    assert result.status == "ok"
    assert result.content == "Edit applied successfully."
    assert file_path.read_text(encoding="utf-8") == "hello voidcode"
    assert result.data["additions"] == 1
    assert result.data["deletions"] == 1


def test_edit_tool_replaces_all_occurrences(tmp_path: Path) -> None:
    file_path = tmp_path / "test.txt"
    file_path.write_text("foo bar foo baz foo", encoding="utf-8")

    tool = EditTool()

    result = tool.invoke(
        ToolCall(
            tool_name="edit",
            arguments={
                "path": "test.txt",
                "oldString": "foo",
                "newString": "qux",
                "replaceAll": True,
            },
        ),
        workspace=tmp_path,
    )

    assert file_path.read_text(encoding="utf-8") == "qux bar qux baz qux"
    assert result.content is not None
    assert "3 occurrences replaced" in result.content


def test_edit_tool_rejects_multiple_exact_matches_without_replace_all(tmp_path: Path) -> None:
    file_path = tmp_path / "test.txt"
    file_path.write_text("foo bar foo", encoding="utf-8")

    tool = EditTool()

    with pytest.raises(ValueError, match="Multiple matches found"):
        tool.invoke(
            ToolCall(
                tool_name="edit",
                arguments={
                    "path": "test.txt",
                    "oldString": "foo",
                    "newString": "qux",
                },
            ),
            workspace=tmp_path,
        )


def test_edit_tool_rejects_non_string_arguments(tmp_path: Path) -> None:
    tool = EditTool()

    with pytest.raises(ValueError, match="string path"):
        tool.invoke(
            ToolCall(tool_name="edit", arguments={"path": 123, "oldString": "a", "newString": "b"}),
            workspace=tmp_path,
        )

    with pytest.raises(ValueError, match="string oldString"):
        tool.invoke(
            ToolCall(
                tool_name="edit", arguments={"path": "f.txt", "oldString": 123, "newString": "b"}
            ),
            workspace=tmp_path,
        )

    with pytest.raises(ValueError, match="string newString"):
        tool.invoke(
            ToolCall(
                tool_name="edit", arguments={"path": "f.txt", "oldString": "a", "newString": 123}
            ),
            workspace=tmp_path,
        )


def test_edit_tool_allows_path_outside_workspace(tmp_path: Path) -> None:
    tool = EditTool()
    outside = tmp_path.parent / "outside-edit.txt"
    outside.write_text("alpha", encoding="utf-8")

    result = tool.invoke(
        ToolCall(
            tool_name="edit",
            arguments={"path": str(outside), "oldString": "alpha", "newString": "beta"},
        ),
        workspace=tmp_path,
    )
    assert result.status == "ok"
    assert outside.read_text(encoding="utf-8") == "beta"


def test_edit_tool_allows_symlink_escape_when_runtime_permission_allows(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside_edit_escape.txt"
    outside.write_text("alpha", encoding="utf-8")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink is not available on this platform")

    tool = EditTool()
    result = tool.invoke(
        ToolCall(
            tool_name="edit",
            arguments={"path": "link.txt", "oldString": "alpha", "newString": "beta"},
        ),
        workspace=tmp_path,
    )
    assert result.status == "ok"
    assert outside.read_text(encoding="utf-8") == "beta"


def test_edit_tool_rejects_nonexistent_file(tmp_path: Path) -> None:
    tool = EditTool()

    with pytest.raises(ValueError, match="does not exist"):
        tool.invoke(
            ToolCall(
                tool_name="edit",
                arguments={"path": "missing.txt", "oldString": "a", "newString": "b"},
            ),
            workspace=tmp_path,
        )


def test_edit_tool_rejects_identical_old_and_new(tmp_path: Path) -> None:
    file_path = tmp_path / "test.txt"
    file_path.write_text("hello", encoding="utf-8")

    tool = EditTool()

    with pytest.raises(ValueError, match="identical"):
        tool.invoke(
            ToolCall(
                tool_name="edit",
                arguments={"path": "test.txt", "oldString": "hello", "newString": "hello"},
            ),
            workspace=tmp_path,
        )


def test_edit_tool_rejects_when_old_string_not_found(tmp_path: Path) -> None:
    file_path = tmp_path / "test.txt"
    file_path.write_text("hello", encoding="utf-8")

    tool = EditTool()

    with pytest.raises(ValueError, match="Could not find oldString") as exc_info:
        tool.invoke(
            ToolCall(
                tool_name="edit",
                arguments={"path": "test.txt", "oldString": "missing", "newString": "b"},
            ),
            workspace=tmp_path,
        )

    message = str(exc_info.value)
    assert "Replacers attempted:" in message
    assert "SimpleReplacer" in message
    assert "ContextAwareReplacer" in message
    assert "No nearby text match found" in message


def test_edit_tool_no_match_with_unindented_old_string_keeps_diagnostics(
    tmp_path: Path,
) -> None:
    file_path = tmp_path / "test.txt"
    file_path.write_text("hello", encoding="utf-8")

    tool = EditTool()

    with pytest.raises(ValueError, match="Could not find oldString") as exc_info:
        tool.invoke(
            ToolCall(
                tool_name="edit",
                arguments={"path": "test.txt", "oldString": "missing", "newString": "b"},
            ),
            workspace=tmp_path,
        )

    assert "Replacers attempted:" in str(exc_info.value)


def test_edit_tool_reports_near_match_context_when_old_string_is_stale(
    tmp_path: Path,
) -> None:
    file_path = tmp_path / "test.py"
    file_path.write_text(
        "def greet():\n    message = 'hello'\n    return message\n",
        encoding="utf-8",
    )

    tool = EditTool()

    with pytest.raises(ValueError, match="Near-match hints") as exc_info:
        tool.invoke(
            ToolCall(
                tool_name="edit",
                arguments={
                    "path": "test.py",
                    "oldString": "def greet():\n    message = 'hullo'\n    return value",
                    "newString": "def greet():\n    message = 'hi'\n    return message",
                },
            ),
            workspace=tmp_path,
        )

    message = str(exc_info.value)
    assert "Replacers attempted:" in message
    assert "BlockAnchorReplacer" in message
    assert "L1" in message
    assert "message = 'hello'" in message
    assert "Diff (- oldString, + current):" in message
    assert "-    message = 'hullo'" in message
    assert "+    message = 'hello'" in message
    assert " def greet():" in message
    assert "+    return message" in message
    assert "first block anchor is close" in message
    assert "retry with exact current text" in message


def test_edit_tool_warns_when_old_string_includes_read_line_prefixes(
    tmp_path: Path,
) -> None:
    file_path = tmp_path / "test.py"
    file_path.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")

    tool = EditTool()

    with pytest.raises(ValueError, match="line prefixes") as exc_info:
        tool.invoke(
            ToolCall(
                tool_name="edit",
                arguments={
                    "path": "test.py",
                    "oldString": "1: alpha\n2: beta",
                    "newString": "alpha\nupdated",
                },
            ),
            workspace=tmp_path,
        )

    message = str(exc_info.value)
    assert "oldString appears to include read output line prefixes" in message
    assert "remove those prefixes" in message


def test_edit_tool_preserves_line_endings(tmp_path: Path) -> None:
    file_path = tmp_path / "test.txt"
    file_path.write_bytes(b"line1\r\nline2\r\nline3")

    tool = EditTool()

    tool.invoke(
        ToolCall(
            tool_name="edit",
            arguments={"path": "test.txt", "oldString": "line2", "newString": "modified"},
        ),
        workspace=tmp_path,
    )

    content = file_path.read_bytes()
    assert content == b"line1\r\nmodified\r\nline3"


def test_edit_tool_matches_block_anchors_with_small_typos(tmp_path: Path) -> None:
    file_path = tmp_path / "test.txt"
    file_path.write_text(
        "alpha\nstart block\nkeep middle\nend block\nomega\n",
        encoding="utf-8",
    )

    tool = EditTool()

    result = tool.invoke(
        ToolCall(
            tool_name="edit",
            arguments={
                "path": "test.txt",
                "oldString": "start blok\nkeep middle\nend block",
                "newString": "start block\nupdated middle\nend block",
            },
        ),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert file_path.read_text(encoding="utf-8") == (
        "alpha\nstart block\nupdated middle\nend block\nomega\n"
    )


def test_edit_tool_skips_formatter_when_no_matching_preset(tmp_path: Path) -> None:
    file_path = tmp_path / "note.txt"
    file_path.write_text("hello world\n", encoding="utf-8")

    tool = EditTool(
        hooks_config=RuntimeHooksConfig(
            formatter_presets={
                "python": RuntimeFormatterPresetConfig(
                    command=("missing-formatter",),
                    extensions=(".py",),
                )
            }
        )
    )

    result = tool.invoke(
        ToolCall(
            tool_name="edit",
            arguments={"path": "note.txt", "oldString": "world", "newString": "voidcode"},
        ),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert result.content == "Edit applied successfully."
    assert "diagnostics" not in result.data
    assert "formatter" not in result.data
    assert file_path.read_text(encoding="utf-8") == "hello voidcode\n"


def test_edit_tool_skips_formatter_when_hooks_are_disabled(tmp_path: Path) -> None:
    file_path = tmp_path / "main.py"
    file_path.write_text("print('hi')\n", encoding="utf-8")

    tool = EditTool(
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
            tool_name="edit",
            arguments={"path": "main.py", "oldString": "'hi'", "newString": "'bye'"},
        ),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert result.content == "Edit applied successfully."
    assert "diagnostics" not in result.data
    assert "formatter" not in result.data
    assert file_path.read_text(encoding="utf-8") == "print('bye')\n"


def test_edit_tool_surfaces_warning_when_formatter_executable_is_missing(tmp_path: Path) -> None:
    file_path = tmp_path / "main.py"
    file_path.write_text("print('hi')\n", encoding="utf-8")

    tool = EditTool(
        hooks_config=RuntimeHooksConfig(
            formatter_presets={
                "python": RuntimeFormatterPresetConfig(
                    command=("missing-formatter-binary",),
                    extensions=(".py",),
                    fallback_commands=(("also-missing",),),
                )
            }
        )
    )

    result = tool.invoke(
        ToolCall(
            tool_name="edit",
            arguments={"path": "main.py", "oldString": "'hi'", "newString": "'bye'"},
        ),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert "Formatter warning:" in (result.content or "")
    assert file_path.read_text(encoding="utf-8") == "print('bye')\n"
    diagnostics = result.data["diagnostics"]
    assert diagnostics == [
        {
            "source": "formatter",
            "severity": "warning",
            "message": (
                "No formatter executable was available for preset 'python'. "
                "Tried: missing-formatter-binary, also-missing. Install one of them or override "
                "hooks.formatter_presets.python.command in .voidcode.json."
            ),
            "language": "python",
            "cwd": str(tmp_path),
            "attempted_commands": [
                ["missing-formatter-binary", str(file_path)],
                ["also-missing", str(file_path)],
            ],
        }
    ]


def test_edit_tool_re_reads_after_successful_formatter_rewrite(tmp_path: Path) -> None:
    file_path = tmp_path / "main.py"
    file_path.write_text("print('hi')\n", encoding="utf-8")
    formatter_script = tmp_path / "formatter.py"
    formatter_script.write_text(
        textwrap.dedent(
            """
            import pathlib
            import sys

            pathlib.Path(sys.argv[-1]).write_text("print( 'bye' )\\n", encoding="utf-8")
            """
        ),
        encoding="utf-8",
    )

    tool = EditTool(
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
            tool_name="edit",
            arguments={"path": "main.py", "oldString": "'hi'", "newString": "'bye'"},
        ),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert file_path.read_text(encoding="utf-8") == "print( 'bye' )\n"
    assert result.data["formatter"] == {
        "status": "formatted",
        "language": "python",
        "cwd": str(tmp_path),
        "command": [sys.executable, str(formatter_script), str(file_path)],
        "attempted_commands": [[sys.executable, str(formatter_script), str(file_path)]],
    }
    assert "print( 'bye' )" in str(result.data["diff"])
    assert "diagnostics" not in result.data


def test_edit_tool_keeps_edit_successful_when_formatter_returns_non_zero(tmp_path: Path) -> None:
    file_path = tmp_path / "main.py"
    file_path.write_text("print('hi')\n", encoding="utf-8")
    formatter_script = tmp_path / "broken_formatter.py"
    formatter_script.write_text(
        textwrap.dedent(
            """
            import pathlib
            import sys

            pathlib.Path(sys.argv[-1]).write_text("print('partial')\\n", encoding="utf-8")
            raise SystemExit(1)
            """
        ),
        encoding="utf-8",
    )

    tool = EditTool(
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
            tool_name="edit",
            arguments={"path": "main.py", "oldString": "'hi'", "newString": "'bye'"},
        ),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert "Formatter warning:" in (result.content or "")
    assert file_path.read_text(encoding="utf-8") == "print('partial')\n"
    diagnostics = result.data["diagnostics"]
    assert isinstance(diagnostics, list)
    first_diagnostic = cast(dict[str, object], diagnostics[0])
    assert first_diagnostic["source"] == "formatter"
    assert "Format failed for main.py" in str(first_diagnostic["message"])
    assert "print('partial')" in str(result.data["diff"])


def test_edit_tool_keeps_edit_successful_when_formatter_times_out(tmp_path: Path) -> None:
    file_path = tmp_path / "main.py"
    file_path.write_text("print('hi')\n", encoding="utf-8")

    tool = EditTool(
        hooks_config=RuntimeHooksConfig(
            formatter_presets={
                "python": RuntimeFormatterPresetConfig(
                    command=("slow-formatter",),
                    extensions=(".py",),
                )
            }
        )
    )

    with patch(
        "voidcode.tools._formatter.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["slow-formatter"], timeout=10.0),
    ):
        result = tool.invoke(
            ToolCall(
                tool_name="edit",
                arguments={"path": "main.py", "oldString": "'hi'", "newString": "'bye'"},
            ),
            workspace=tmp_path,
        )

    assert result.status == "ok"
    assert "Formatter warning:" in (result.content or "")
    diagnostics = result.data["diagnostics"]
    assert isinstance(diagnostics, list)
    first_diagnostic = cast(dict[str, object], diagnostics[0])
    assert "timed out after 30.0s" in str(first_diagnostic["message"])
    assert file_path.read_text(encoding="utf-8") == "print('bye')\n"


def test_tools_package_and_default_registry_export_edit_tool() -> None:
    registry = ToolRegistry.with_defaults()

    assert "EditTool" in __import__("voidcode.tools", fromlist=["__all__"]).__all__
    assert registry.resolve("edit").definition.name == "edit"
    assert registry.resolve("edit").definition.read_only is False
