from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

from voidcode.hook.config import RuntimeFormatterPresetConfig, RuntimeHooksConfig
from voidcode.runtime.service import ToolRegistry
from voidcode.tools import ToolCall, WriteFileTool


def test_write_file_tool_writes_utf8_content_inside_workspace(tmp_path: Path) -> None:
    tool = WriteFileTool()

    result = tool.invoke(
        ToolCall(
            tool_name="write_file",
            arguments={"path": "nested/output.txt", "content": "hello utf8 π"},
        ),
        workspace=tmp_path,
    )

    assert (tmp_path / "nested" / "output.txt").read_text(encoding="utf-8") == "hello utf8 π"
    assert result.tool_name == "write_file"
    assert result.status == "ok"
    assert result.content == "Wrote file successfully: nested/output.txt"
    assert result.data == {
        "path": "nested/output.txt",
        "byte_count": len("hello utf8 π".encode()),
        "diff": "--- a/nested/output.txt\n+++ b/nested/output.txt\n@@ -0,0 +1 @@\n+hello utf8 π",
    }


def test_write_file_tool_returns_diff_for_rewrite(tmp_path: Path) -> None:
    (tmp_path / "note.txt").write_text("old\n", encoding="utf-8")
    tool = WriteFileTool()

    result = tool.invoke(
        ToolCall(
            tool_name="write_file",
            arguments={"path": "note.txt", "content": "new\n"},
        ),
        workspace=tmp_path,
    )

    assert result.data["diff"] == ("--- a/note.txt\n+++ b/note.txt\n@@ -1 +1 @@\n-old\n+new\n")


def test_write_file_tool_rejects_non_string_arguments(tmp_path: Path) -> None:
    tool = WriteFileTool()

    with pytest.raises(ValueError, match="string path"):
        tool.invoke(
            ToolCall(tool_name="write_file", arguments={"path": 123, "content": "x"}),
            workspace=tmp_path,
        )

    with pytest.raises(ValueError, match="string content"):
        tool.invoke(
            ToolCall(tool_name="write_file", arguments={"path": "out.txt", "content": 123}),
            workspace=tmp_path,
        )


def test_write_file_tool_rejects_paths_outside_workspace(tmp_path: Path) -> None:
    tool = WriteFileTool()

    with pytest.raises(ValueError, match="inside the workspace"):
        tool.invoke(
            ToolCall(
                tool_name="write_file",
                arguments={"path": "../escape.txt", "content": "nope"},
            ),
            workspace=tmp_path,
        )


def test_write_file_tool_rejects_symlink_escape(tmp_path: Path) -> None:
    outside_dir = tmp_path.parent / "outside_write_escape"
    outside_dir.mkdir(exist_ok=True)
    link_dir = tmp_path / "linkdir"
    try:
        link_dir.symlink_to(outside_dir, target_is_directory=True)
    except OSError:
        pytest.skip("symlink is not available on this platform")

    tool = WriteFileTool()
    with pytest.raises(ValueError, match="inside the workspace"):
        tool.invoke(
            ToolCall(
                tool_name="write_file",
                arguments={"path": "linkdir/escape.txt", "content": "nope"},
            ),
            workspace=tmp_path,
        )


def test_write_file_tool_runs_formatter_after_writing(tmp_path: Path) -> None:
    formatter_script = tmp_path / "formatter.py"
    formatter_script.write_text(
        textwrap.dedent(
            """
            import pathlib
            import sys

            pathlib.Path(sys.argv[-1]).write_text("print( 'formatted' )\\n", encoding="utf-8")
            """
        ),
        encoding="utf-8",
    )
    tool = WriteFileTool(
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
            tool_name="write_file",
            arguments={"path": "main.py", "content": "print('raw')\n"},
        ),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert (tmp_path / "main.py").read_text(encoding="utf-8") == "print( 'formatted' )\n"
    assert result.data["formatter"] == {
        "status": "formatted",
        "language": "python",
        "cwd": str(tmp_path),
        "command": [sys.executable, str(formatter_script), str(tmp_path / "main.py")],
        "attempted_commands": [[sys.executable, str(formatter_script), str(tmp_path / "main.py")]],
    }
    assert result.data["byte_count"] == len(b"print( 'formatted' )\n")


def test_tools_package_and_default_registry_export_write_file_tool() -> None:
    registry = ToolRegistry.with_defaults()

    assert "WriteFileTool" in __import__("voidcode.tools", fromlist=["__all__"]).__all__
    assert registry.resolve("write_file").definition.name == "write_file"
    assert registry.resolve("write_file").definition.read_only is False
