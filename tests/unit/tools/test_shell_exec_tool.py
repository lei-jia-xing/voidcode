from __future__ import annotations

import sys
from pathlib import Path

import pytest

from voidcode.runtime.service import ToolRegistry
from voidcode.tools import ShellExecTool, ToolCall


def _cwd_command() -> str:
    return f'"{sys.executable}" -c "import os; print(os.getcwd())"'


def test_shell_exec_tool_runs_command_in_workspace(tmp_path: Path) -> None:
    tool = ShellExecTool()
    command = _cwd_command()

    result = tool.invoke(
        ToolCall(tool_name="shell_exec", arguments={"command": command}),
        workspace=tmp_path,
    )

    assert result.tool_name == "shell_exec"
    assert result.status == "ok"
    assert result.content == f"{tmp_path.resolve()}\n"
    assert result.data.get("command") == command
    assert result.data.get("exit_code") == 0
    assert result.data.get("stdout") == f"{tmp_path.resolve()}\n"
    assert result.data.get("stderr") == ""
    assert result.data.get("timeout") == 30
    assert result.data.get("truncated") is False


def test_shell_exec_tool_supports_shell_operators(tmp_path: Path) -> None:
    tool = ShellExecTool()
    command = "printf 'alpha\\n' > sample.txt && cat sample.txt"
    if sys.platform.startswith("win"):
        command = "echo alpha>sample.txt && type sample.txt"

    result = tool.invoke(
        ToolCall(
            tool_name="shell_exec",
            arguments={"command": command},
        ),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert result.content.strip() == "alpha"
    assert (tmp_path / "sample.txt").read_text(encoding="utf-8").strip() == "alpha"


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


def test_shell_exec_tool_respects_timeout(tmp_path: Path) -> None:
    tool = ShellExecTool()

    with pytest.raises(ValueError, match="timed out"):
        tool.invoke(
            ToolCall(
                tool_name="shell_exec",
                arguments={
                    "command": f'"{sys.executable}" -c "import time; time.sleep(2)"',
                    "timeout": 1,
                },
            ),
            workspace=tmp_path,
        )


def test_shell_exec_timeout_cleanup_falls_back_without_killpg(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = ShellExecTool()

    if not hasattr(__import__("os"), "killpg"):
        pytest.skip("killpg unavailable on this platform")

    def unavailable_killpg(_pid: int, _signal_value: int) -> None:
        raise AttributeError("killpg unavailable")

    monkeypatch.setattr("voidcode.tools.shell_exec.os.killpg", unavailable_killpg)

    with pytest.raises(ValueError, match="shell_exec command timed out after 1s"):
        tool.invoke(
            ToolCall(
                tool_name="shell_exec",
                arguments={
                    "command": f'"{sys.executable}" -c "import time; time.sleep(2)"',
                    "timeout": 1,
                },
            ),
            workspace=tmp_path,
        )


def test_shell_exec_tool_truncates_large_output(tmp_path: Path) -> None:
    tool = ShellExecTool()
    command = f'"{sys.executable}" -c "import sys; sys.stdout.write(chr(120)*250000)"'

    result = tool.invoke(
        ToolCall(tool_name="shell_exec", arguments={"command": command}),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert isinstance(result.content, str)
    assert len(result.content) == 200000
    assert result.data.get("truncated") is True
