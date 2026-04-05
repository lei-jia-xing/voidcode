from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from voidcode.runtime.service import ToolRegistry
from voidcode.tools import ShellExecTool, ToolCall


def test_shell_exec_tool_runs_command_in_workspace(tmp_path: Path) -> None:
    tool = ShellExecTool()

    result = tool.invoke(
        ToolCall(tool_name="shell_exec", arguments={"command": "pwd"}),
        workspace=tmp_path,
    )

    assert result.tool_name == "shell_exec"
    assert result.status == "ok"
    assert result.content == f"{tmp_path.resolve()}\n"
    assert result.data == {
        "command": "pwd",
        "exit_code": 0,
        "stdout": f"{tmp_path.resolve()}\n",
        "stderr": "",
    }


def test_shell_exec_tool_rejects_invalid_command_arguments(tmp_path: Path) -> None:
    tool = ShellExecTool()

    with pytest.raises(ValueError, match="string command"):
        tool.invoke(
            ToolCall(tool_name="shell_exec", arguments={"command": 123}),
            workspace=tmp_path,
        )

    with pytest.raises(ValueError, match="must not be empty"):
        tool.invoke(
            ToolCall(tool_name="shell_exec", arguments={"command": "   "}),
            workspace=tmp_path,
        )


def test_tools_package_and_default_registry_export_shell_exec_tool() -> None:
    registry = ToolRegistry.with_defaults()

    assert "ShellExecTool" in __import__("voidcode.tools", fromlist=["__all__"]).__all__
    assert registry.resolve("shell_exec").definition.name == "shell_exec"
    assert registry.resolve("shell_exec").definition.read_only is False
