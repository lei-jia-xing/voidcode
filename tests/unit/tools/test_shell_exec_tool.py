from __future__ import annotations

import sys
from pathlib import Path
from typing import cast

import pytest

from voidcode.tools import ShellExecTool, ToolCall
from voidcode.tools.output import MAX_TOOL_OUTPUT_BYTES, cap_tool_result_output


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
    assert isinstance(result.content, str)
    assert result.content.strip() == str(tmp_path.resolve())
    assert result.data.get("command") == command
    assert result.data.get("cwd") == str(tmp_path.resolve())
    assert result.data.get("exit_code") == 0
    stdout = result.data.get("stdout")
    assert isinstance(stdout, str)
    assert stdout.strip() == str(tmp_path.resolve())
    assert result.data.get("stderr") == ""
    assert result.data.get("timeout") == 120
    assert result.data.get("truncated") is False
    assert result.data.get("stdout_truncated") is False
    assert result.data.get("stderr_truncated") is False
    assert result.data.get("injected_env_keys") == ()


def test_shell_exec_tool_rejects_invalid_command_arguments(tmp_path: Path) -> None:
    tool = ShellExecTool()
    command_type_error = (
        r"shell_exec Validation error: command: "
        r"Input should be a valid string \(received int\)"
        r"\. Please retry with corrected arguments that satisfy the tool schema\."
    )

    with pytest.raises(ValueError, match=command_type_error):
        tool.invoke(
            ToolCall(tool_name="shell_exec", arguments={"command": 123}),
            workspace=tmp_path,
        )

    command_empty_error = (
        r"shell_exec Validation error: command: Value error, "
        r"command must not be empty \(received str\)"
        r"\. Please retry with corrected arguments that satisfy the tool schema\."
    )
    with pytest.raises(ValueError, match=command_empty_error):
        tool.invoke(
            ToolCall(tool_name="shell_exec", arguments={"command": "   "}),
            workspace=tmp_path,
        )

    description_error = (
        r"shell_exec Validation error: description: Value error, "
        r"description must not be empty when provided \(received str\)"
        r"\. Please retry with corrected arguments that satisfy the tool schema\."
    )
    with pytest.raises(ValueError, match=description_error):
        tool.invoke(
            ToolCall(
                tool_name="shell_exec",
                arguments={"command": "pwd", "description": "   "},
            ),
            workspace=tmp_path,
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
            workspace=tmp_path,
        )


def test_shell_exec_tool_caps_explicit_timeout_at_production_max(tmp_path: Path) -> None:
    tool = ShellExecTool()

    result = tool.invoke(
        ToolCall(
            tool_name="shell_exec",
            arguments={"command": _cwd_command(), "timeout": 9999},
        ),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert result.data.get("timeout") == 600


def test_shell_exec_large_output_spills_full_payload_via_central_cap(tmp_path: Path) -> None:
    payload_size = MAX_TOOL_OUTPUT_BYTES + 10_000
    tool = ShellExecTool()
    command = f'"{sys.executable}" -c "import sys; sys.stdout.write(chr(120)*{payload_size})"'

    result = tool.invoke(
        ToolCall(tool_name="shell_exec", arguments={"command": command}),
        workspace=tmp_path,
    )
    capped = cap_tool_result_output(result)

    assert capped.truncated is True
    assert capped.partial is True
    assert capped.reference is not None
    assert isinstance(capped.content, str)
    assert "[Tool output truncated:" in capped.content
    assert "artifact_id=" in capped.content
    assert 'read(path="voidcode://artifact/' in capped.content
    assert capped.reference.startswith("voidcode://artifact/")

    artifact = capped.data["artifact"]
    assert isinstance(artifact, dict)
    typed_artifact = cast(dict[str, object], artifact)
    reference_path = Path(str(typed_artifact["path"]))
    assert reference_path.exists()
    assert len(reference_path.read_text(encoding="utf-8")) == payload_size
    assert not (tmp_path / ".voidcode" / "tool-output").exists()
