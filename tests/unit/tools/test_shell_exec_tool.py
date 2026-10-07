from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

from voidcode.core.tool_context import ToolContext
from voidcode.tools.contracts import TextOutput, ToolCall
from voidcode.tools.output import MAX_TOOL_OUTPUT_BYTES, cap_tool_result_output
from voidcode.tools.shell_exec import ShellExecResultBody, ShellExecTool


def _cwd_command() -> str:
    return f'"{sys.executable}" -c "import os; print(os.getcwd())"'


def test_shell_exec_tool_runs_command_in_workspace(tmp_path: Path) -> None:
    tool = ShellExecTool()
    command = _cwd_command()

    result = tool.invoke(
        ToolCall(tool_name="shell_exec", arguments={"command": command}),
        context=ToolContext(workspace=tmp_path),
    )

    assert result.tool_name == "shell_exec"
    assert result.status == "ok"
    assert isinstance(result.output, TextOutput)
    assert result.output.text.strip() == str(tmp_path.resolve())
    assert isinstance(result.body, ShellExecResultBody)
    assert result.body.command == command
    assert result.body.cwd == str(tmp_path.resolve())
    assert result.body.exit_code == 0
    assert result.body.stdout.strip() == str(tmp_path.resolve())
    assert result.body.stderr == ""
    assert result.body.timeout == 120
    assert result.body.truncated is False
    assert result.body.stdout_truncated is False
    assert result.body.stderr_truncated is False
    assert result.body.injected_env_keys == ()


def test_shell_exec_tool_rejects_invalid_command_arguments(tmp_path: Path) -> None:
    tool = ShellExecTool()

    with pytest.raises(ValueError):
        tool.invoke(
            ToolCall(tool_name="shell_exec", arguments={"command": 123}),
            context=ToolContext(workspace=tmp_path),
        )

    with pytest.raises(ValueError):
        tool.invoke(
            ToolCall(tool_name="shell_exec", arguments={"command": "   "}),
            context=ToolContext(workspace=tmp_path),
        )

    with pytest.raises(ValueError):
        tool.invoke(
            ToolCall(
                tool_name="shell_exec",
                arguments={"command": "pwd", "description": "   "},
            ),
            context=ToolContext(workspace=tmp_path),
        )


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
            context=ToolContext(workspace=tmp_path),
        )


def test_shell_exec_tool_caps_explicit_timeout_at_production_max(tmp_path: Path) -> None:
    tool = ShellExecTool()

    result = tool.invoke(
        ToolCall(
            tool_name="shell_exec",
            arguments={"command": _cwd_command(), "timeout": 9999},
        ),
        context=ToolContext(workspace=tmp_path),
    )

    assert result.status == "ok"
    assert isinstance(result.body, ShellExecResultBody)
    assert result.body.timeout == 600


def test_shell_exec_large_output_spills_full_payload_via_central_cap(tmp_path: Path) -> None:
    payload_size = MAX_TOOL_OUTPUT_BYTES + 10_000
    tool = ShellExecTool()
    command = f'"{sys.executable}" -c "import sys; sys.stdout.write(chr(120)*{payload_size})"'

    result = tool.invoke(
        ToolCall(tool_name="shell_exec", arguments={"command": command}),
        context=ToolContext(workspace=tmp_path),
    )
    capped = cap_tool_result_output(result)

    assert isinstance(capped.output, TextOutput)
    bounds = capped.output.bounds
    assert bounds.truncated is True
    assert bounds.partial is True
    assert bounds.reference is not None
    assert bounds.reference.uri in capped.output.text
    assert bounds.reference.uri.startswith("voidcode://artifact/")
    artifact = bounds.reference.artifact
    assert isinstance(artifact, Mapping)
    reference_path = Path(str(artifact["path"]))
    assert reference_path.exists()
    assert reference_path.read_text(encoding="utf-8") == "x" * payload_size
    assert not (tmp_path / ".voidcode" / "tool-output").exists()
