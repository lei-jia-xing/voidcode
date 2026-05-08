from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from voidcode.runtime.service import ToolRegistry
from voidcode.tools import InteractiveShellTool, ToolCall


def test_tools_package_and_default_registry_export_interactive_shell_tool() -> None:
    registry = ToolRegistry.with_defaults()

    assert "InteractiveShellTool" in __import__("voidcode.tools", fromlist=["__all__"]).__all__
    assert "interactive_shell" not in registry.tools


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux unavailable")
def test_interactive_shell_creates_and_captures_tmux_session(tmp_path: Path) -> None:
    tool = InteractiveShellTool()
    session_name = "vc-it-shell-test"
    subprocess.run(["tmux", "kill-session", "-t", session_name], check=False, capture_output=True)
    try:
        create_result = tool.invoke(
            ToolCall(
                tool_name="interactive_shell",
                arguments={"tmux_command": f"new-session -d -s {session_name}"},
            ),
            workspace=tmp_path,
        )
        assert create_result.status == "ok"

        send_result = tool.invoke(
            ToolCall(
                tool_name="interactive_shell",
                arguments={"tmux_command": f'send-keys -t {session_name} "printf hello" Enter'},
            ),
            workspace=tmp_path,
        )
        assert send_result.status == "ok"

        capture_result = tool.invoke(
            ToolCall(
                tool_name="interactive_shell",
                arguments={"tmux_command": f"capture-pane -p -t {session_name}"},
            ),
            workspace=tmp_path,
        )
        assert capture_result.status == "ok"
        assert isinstance(capture_result.content, str)
        assert "hello" in capture_result.content
        assert capture_result.data["subcommand"] == "capture-pane"
    finally:
        subprocess.run(
            ["tmux", "kill-session", "-t", session_name], check=False, capture_output=True
        )


def test_interactive_shell_blocks_risky_tmux_subcommands(tmp_path: Path) -> None:
    tool = InteractiveShellTool()
    with pytest.raises(ValueError, match="blocks risky tmux subcommand 'kill-session'"):
        tool.invoke(
            ToolCall(
                tool_name="interactive_shell", arguments={"tmux_command": "kill-session -t demo"}
            ),
            workspace=tmp_path,
        )


@pytest.mark.skipif(os.name != "nt", reason="windows-only behavior")
def test_interactive_shell_not_registered_on_windows() -> None:
    registry = ToolRegistry.with_defaults()
    assert "interactive_shell" not in registry.tools


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux unavailable")
def test_interactive_shell_reports_tmux_failures(tmp_path: Path) -> None:
    tool = InteractiveShellTool()
    result = tool.invoke(
        ToolCall(
            tool_name="interactive_shell",
            arguments={"tmux_command": "has-session -t definitely-missing-session"},
        ),
        workspace=tmp_path,
    )
    assert result.status == "error"
    assert result.error is not None
    assert result.data["subcommand"] == "has-session"
