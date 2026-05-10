from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path
from typing import cast

import pytest

from voidcode.formatter import RuntimeFormatterPresetConfig
from voidcode.hook.config import RuntimeHooksConfig
from voidcode.runtime.config import load_runtime_config
from voidcode.runtime.service import ToolRegistry
from voidcode.tools import ToolCall, WriteFileTool
from voidcode.tools.runtime_context import RuntimeToolInvocationContext, bind_runtime_tool_context


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

    with pytest.raises(
        ValueError,
        match=(
            r"write_file Validation error: path: Input should be a valid string \(received int\)\. "
            r"Please retry with corrected arguments that satisfy the tool schema\."
        ),
    ):
        tool.invoke(
            ToolCall(tool_name="write_file", arguments={"path": 123, "content": "x"}),
            workspace=tmp_path,
        )

    with pytest.raises(
        ValueError,
        match=(
            r"write_file Validation error: content: Input should be a valid string "
            r"\(received int\)\. "
            r"Please retry with corrected arguments that satisfy the tool schema\."
        ),
    ):
        tool.invoke(
            ToolCall(tool_name="write_file", arguments={"path": "out.txt", "content": 123}),
            workspace=tmp_path,
        )


def test_write_file_tool_allows_empty_content_for_new_file(tmp_path: Path) -> None:
    tool = WriteFileTool()

    result = tool.invoke(
        ToolCall(tool_name="write_file", arguments={"path": "shader.frag", "content": ""}),
        workspace=tmp_path,
    )

    assert (tmp_path / "shader.frag").read_text(encoding="utf-8") == ""
    assert result.status == "ok"
    assert result.content == "Wrote file successfully: shader.frag"
    assert result.data == {"path": "shader.frag", "byte_count": 0, "diff": ""}


def test_write_file_tool_allows_empty_content_for_existing_file(tmp_path: Path) -> None:
    (tmp_path / "shader.frag").write_text("void main() {}\n", encoding="utf-8")
    tool = WriteFileTool()

    result = tool.invoke(
        ToolCall(tool_name="write_file", arguments={"path": "shader.frag", "content": ""}),
        workspace=tmp_path,
    )

    assert (tmp_path / "shader.frag").read_text(encoding="utf-8") == ""
    assert result.status == "ok"
    assert result.data["byte_count"] == 0
    assert result.data["diff"] == (
        "--- a/shader.frag\n+++ b/shader.frag\n@@ -1 +0,0 @@\n-void main() {}\n"
    )


def test_write_file_tool_allows_absolute_paths_outside_workspace(tmp_path: Path) -> None:
    tool = WriteFileTool()
    outside = tmp_path.parent / "outside-write.txt"

    result = tool.invoke(
        ToolCall(
            tool_name="write_file",
            arguments={"path": str(outside), "content": "ok"},
        ),
        workspace=tmp_path,
    )

    assert outside.read_text(encoding="utf-8") == "ok"
    assert result.data["path"] == str(outside.resolve())


def test_write_file_tool_allows_symlink_escape_when_runtime_permission_allows(
    tmp_path: Path,
) -> None:
    outside_dir = tmp_path.parent / "outside_write_escape"
    outside_dir.mkdir(exist_ok=True)
    link_dir = tmp_path / "linkdir"
    try:
        link_dir.symlink_to(outside_dir, target_is_directory=True)
    except OSError:
        pytest.skip("symlink is not available on this platform")

    tool = WriteFileTool()
    result = tool.invoke(
        ToolCall(
            tool_name="write_file",
            arguments={"path": "linkdir/escape.txt", "content": "ok"},
        ),
        workspace=tmp_path,
    )
    assert (outside_dir / "escape.txt").read_text(encoding="utf-8") == "ok"
    assert result.data["path"] == str((outside_dir / "escape.txt").resolve())


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


def test_write_file_tool_keeps_write_successful_when_formatter_is_missing(
    tmp_path: Path,
) -> None:
    tool = WriteFileTool(
        hooks_config=RuntimeHooksConfig(
            formatter_presets={
                "python": RuntimeFormatterPresetConfig(
                    command=("missing-formatter-binary",),
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


def test_write_file_tool_appends_runtime_lsp_diagnostics_when_available(tmp_path: Path) -> None:
    tool = WriteFileTool()

    class _FakeLspFacade:
        def request_diagnostics(self, *, file_path: str, workspace: str) -> dict[str, object]:
            assert file_path == "main.py"
            assert workspace == str(tmp_path.resolve())
            return {
                "lsp_response": {
                    "result": {
                        "items": [
                            {
                                "message": "Example type error",
                                "severity": 1,
                                "code": "example",
                                "range": {
                                    "start": {"line": 0, "character": 4},
                                    "end": {"line": 0, "character": 9},
                                },
                            }
                        ]
                    }
                }
            }

    with bind_runtime_tool_context(
        RuntimeToolInvocationContext(
            session_id="session-1",
            lsp=_FakeLspFacade(),
        )
    ):
        result = tool.invoke(
            ToolCall(
                tool_name="write_file",
                arguments={"path": "main.py", "content": "print('raw')\n"},
            ),
            workspace=tmp_path,
        )

    diagnostics = cast(list[dict[str, object]], result.data["diagnostics"])
    lsp_diagnostic = diagnostics[-1]
    assert lsp_diagnostic["source"] == "lsp"
    assert lsp_diagnostic["path"] == "main.py"
    assert lsp_diagnostic["message"] == "Example type error"
    assert lsp_diagnostic["line"] == 1
    assert lsp_diagnostic["character"] == 5
    assert (tmp_path / "main.py").read_text(encoding="utf-8") == "print('raw')\n"
    assert result.content == "Wrote file successfully: main.py"


def test_write_file_tool_skips_formatter_when_hooks_are_disabled(tmp_path: Path) -> None:
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
    tool = WriteFileTool(
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
            tool_name="write_file",
            arguments={"path": "main.py", "content": "print('raw')\n"},
        ),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert not formatter_marker.exists()
    assert "formatter" not in result.data
    assert (tmp_path / "main.py").read_text(encoding="utf-8") == "print('raw')\n"


def test_write_file_tool_skips_formatter_when_formatter_is_disabled_in_runtime_config(
    tmp_path: Path,
) -> None:
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
    (tmp_path / ".voidcode.json").write_text(
        textwrap.dedent(
            f"""
            {{
              "formatter": {{
                "enabled": false,
                "languages": {{
                  "python": {{
                    "command": {json.dumps([sys.executable, str(formatter_script)])},
                    "extensions": [".py"]
                  }}
                }}
              }}
            }}
            """
        ),
        encoding="utf-8",
    )
    config = load_runtime_config(
        tmp_path,
        env={"XDG_CONFIG_HOME": str(tmp_path / "xdg-config")},
    )
    tool = WriteFileTool(hooks_config=config.hooks)

    result = tool.invoke(
        ToolCall(
            tool_name="write_file",
            arguments={"path": "main.py", "content": "print('raw')\n"},
        ),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert not formatter_marker.exists()
    assert "formatter" not in result.data
    assert (tmp_path / "main.py").read_text(encoding="utf-8") == "print('raw')\n"


def test_tools_package_and_default_registry_export_write_file_tool() -> None:
    registry = ToolRegistry.with_defaults()

    assert "WriteFileTool" in __import__("voidcode.tools", fromlist=["__all__"]).__all__
    assert registry.resolve("write_file").definition.name == "write_file"
    assert registry.resolve("write_file").definition.read_only is False
