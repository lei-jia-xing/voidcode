from __future__ import annotations

import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

from voidcode.hook.config import RuntimeFormatterPresetConfig, RuntimeHooksConfig
from voidcode.tools.contracts import ToolCall
from voidcode.tools.lsp import FormatTool


def test_format_tool_uses_nearest_formatter_root_for_cwd(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    project_root = workspace / "project"
    nested = project_root / "src"
    nested.mkdir(parents=True)
    (project_root / "pyproject.toml").write_text("[tool.ruff]\n", encoding="utf-8")
    target = nested / "main.py"
    target.write_text("print('hi')\n", encoding="utf-8")
    formatter_script = workspace / "record_formatter.py"
    observed_cwd = workspace / "formatter-cwd.txt"
    formatter_script.write_text(
        textwrap.dedent(
            f"""
            import pathlib
            import sys

            pathlib.Path(r"{observed_cwd}").write_text(str(pathlib.Path.cwd()), encoding="utf-8")
            pathlib.Path(sys.argv[-1]).write_text("formatted\\n", encoding="utf-8")
            """
        ),
        encoding="utf-8",
    )

    tool = FormatTool(
        RuntimeHooksConfig(
            formatter_presets={
                "python": RuntimeFormatterPresetConfig(
                    command=(sys.executable, str(formatter_script)),
                    extensions=(".py",),
                    root_markers=("pyproject.toml",),
                )
            }
        ),
        workspace,
    )

    result = tool.invoke(
        ToolCall(tool_name="format_file", arguments={"path": "project/src/main.py"}),
        workspace=workspace,
    )

    assert result.status == "ok"
    assert observed_cwd.read_text(encoding="utf-8") == str(project_root)
    assert result.data["cwd"] == str(project_root)


def test_format_tool_uses_fallback_formatter_command_when_primary_is_missing(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "example.py"
    target.write_text("print('hi')\n", encoding="utf-8")
    formatter_script = workspace / "fallback_formatter.py"
    formatter_script.write_text(
        textwrap.dedent(
            """
            import pathlib
            import sys

            pathlib.Path(sys.argv[-1]).write_text("formatted\\n", encoding="utf-8")
            """
        ),
        encoding="utf-8",
    )

    tool = FormatTool(
        RuntimeHooksConfig(
            formatter_presets={
                "python": RuntimeFormatterPresetConfig(
                    command=("missing-formatter-binary",),
                    extensions=(".py",),
                    fallback_commands=((sys.executable, str(formatter_script)),),
                )
            }
        ),
        workspace,
    )

    result = tool.invoke(
        ToolCall(tool_name="format_file", arguments={"path": "example.py"}),
        workspace=workspace,
    )

    assert result.status == "ok"
    assert result.data["command"] == [sys.executable, str(formatter_script), str(target)]
    assert target.read_text(encoding="utf-8") == "formatted\n"


def test_format_tool_reports_attempted_commands_when_formatter_is_missing(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "example.py"
    target.write_text("print('hi')\n", encoding="utf-8")

    tool = FormatTool(
        RuntimeHooksConfig(
            formatter_presets={
                "python": RuntimeFormatterPresetConfig(
                    command=("missing-formatter-binary",),
                    extensions=(".py",),
                    fallback_commands=(("also-missing",),),
                )
            }
        ),
        workspace,
    )

    result = tool.invoke(
        ToolCall(tool_name="format_file", arguments={"path": "example.py"}),
        workspace=workspace,
    )

    assert result.status == "error"
    assert "hooks.formatter_presets.python.command" in (result.error or "")
    assert result.data["attempted_commands"] == [
        ["missing-formatter-binary", str(target)],
        ["also-missing", str(target)],
    ]


def test_format_tool_supports_custom_user_defined_formatter_presets(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "example.php"
    target.write_text("<?php echo 'hi';\n", encoding="utf-8")
    formatter_script = workspace / "php_formatter.py"
    formatter_script.write_text(
        textwrap.dedent(
            """
            import pathlib
            import sys

            pathlib.Path(sys.argv[-1]).write_text("<?php echo 'formatted';\\n", encoding="utf-8")
            """
        ),
        encoding="utf-8",
    )

    tool = FormatTool(
        RuntimeHooksConfig(
            formatter_presets={
                "php": RuntimeFormatterPresetConfig(
                    command=(sys.executable, str(formatter_script)),
                    extensions=(".php",),
                    root_markers=("composer.json",),
                )
            }
        ),
        workspace,
    )

    result = tool.invoke(
        ToolCall(tool_name="format_file", arguments={"path": "example.php"}),
        workspace=workspace,
    )

    assert result.status == "ok"
    assert result.data["language"] == "php"
    assert result.data["command"] == [sys.executable, str(formatter_script), str(target)]
    assert target.read_text(encoding="utf-8") == "<?php echo 'formatted';\n"


def test_format_tool_treats_non_enoent_launch_failure_as_formatter_attempt_failure(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "example.py"
    target.write_text("print('hi')\n", encoding="utf-8")

    tool = FormatTool(
        RuntimeHooksConfig(
            formatter_presets={
                "python": RuntimeFormatterPresetConfig(
                    command=("custom-formatter",),
                    extensions=(".py",),
                    fallback_commands=((sys.executable, "-c", "print('fallback')"),),
                )
            }
        ),
        workspace,
    )

    with patch(
        "voidcode.tools.lsp.subprocess.run", side_effect=PermissionError("permission denied")
    ):
        result = tool.invoke(
            ToolCall(tool_name="format_file", arguments={"path": "example.py"}),
            workspace=workspace,
        )

    assert result.status == "error"
    assert "permission denied" in (result.error or "")
    assert result.data["command"] == [sys.executable, "-c", "print('fallback')", str(target)]
    assert result.data["attempted_commands"] == [
        ["custom-formatter", str(target)],
        [sys.executable, "-c", "print('fallback')", str(target)],
    ]
