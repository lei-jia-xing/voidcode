from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest

from voidcode.runtime.service import ToolRegistry
from voidcode.tools import ShellExecTool, ToolCall
from voidcode.tools.output import MAX_TOOL_OUTPUT_BYTES, cap_tool_result_output
from voidcode.tools.runtime_context import RuntimeToolInvocationContext, bind_runtime_tool_context
from voidcode.tools.shell_exec import kill_timed_out_process


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
    assert result.data.get("timeout") == 30
    assert result.data.get("truncated") is False
    assert result.data.get("stdout_truncated") is False
    assert result.data.get("stderr_truncated") is False


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
    assert isinstance(result.content, str)
    assert result.content.strip() == "alpha"
    assert (tmp_path / "sample.txt").read_text(encoding="utf-8").strip() == "alpha"


def test_shell_exec_tool_rejects_invalid_command_arguments(tmp_path: Path) -> None:
    tool = ShellExecTool()
    command_type_error = (
        r"shell_exec invalid arguments: command: "
        r"Input should be a valid string \(received int\)"
    )

    with pytest.raises(ValueError, match=command_type_error):
        tool.invoke(
            ToolCall(tool_name="shell_exec", arguments={"command": 123}),
            workspace=tmp_path,
        )

    command_empty_error = (
        r"shell_exec invalid arguments: command: Value error, "
        r"command must not be empty \(received str\)"
    )
    with pytest.raises(ValueError, match=command_empty_error):
        tool.invoke(
            ToolCall(tool_name="shell_exec", arguments={"command": "   "}),
            workspace=tmp_path,
        )

    description_error = (
        r"shell_exec invalid arguments: description: Value error, "
        r"description must not be empty when provided \(received str\)"
    )
    with pytest.raises(ValueError, match=description_error):
        tool.invoke(
            ToolCall(
                tool_name="shell_exec",
                arguments={"command": "pwd", "description": "   "},
            ),
            workspace=tmp_path,
        )


def test_shell_exec_tool_reports_missing_command(tmp_path: Path) -> None:
    tool = ShellExecTool()
    missing_command_error = (
        r"shell_exec invalid arguments: command: "
        r"Input should be a valid string \(received NoneType\)"
    )

    with pytest.raises(ValueError, match=missing_command_error):
        tool.invoke(ToolCall(tool_name="shell_exec", arguments={}), workspace=tmp_path)


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


def test_shell_exec_windows_timeout_cleanup_uses_successful_taskkill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    class _FakeProcess:
        pid = 1234

        def kill(self) -> None:
            raise AssertionError("process.kill should not run after successful taskkill")

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("voidcode.tools.shell_exec.os.name", "nt")
    monkeypatch.setattr("voidcode.tools.shell_exec.subprocess.run", fake_run)

    kill_timed_out_process(cast(subprocess.Popen[str], _FakeProcess()))

    assert calls == [["taskkill", "/PID", "1234", "/T", "/F"]]


def test_shell_exec_windows_timeout_cleanup_falls_back_after_taskkill_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    killed = False

    class _FakeProcess:
        pid = 5678

        def kill(self) -> None:
            nonlocal killed
            killed = True

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 5, "", "not found")

    monkeypatch.setattr("voidcode.tools.shell_exec.os.name", "nt")
    monkeypatch.setattr("voidcode.tools.shell_exec.subprocess.run", fake_run)

    kill_timed_out_process(cast(subprocess.Popen[str], _FakeProcess()))

    assert calls == [["taskkill", "/PID", "5678", "/T", "/F"]]
    assert killed is True


def test_shell_exec_tool_returns_full_output_for_large_subprocess(tmp_path: Path) -> None:
    tool = ShellExecTool()
    command = f'"{sys.executable}" -c "import sys; sys.stdout.write(chr(120)*250000)"'

    result = tool.invoke(
        ToolCall(tool_name="shell_exec", arguments={"command": command}),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert isinstance(result.content, str)
    assert len(result.content) == 250000
    assert result.data.get("truncated") is False
    assert result.data.get("stdout_truncated") is False
    assert result.data.get("stderr_truncated") is False
    assert result.data.get("output_char_count") == 250000


def test_shell_exec_waits_for_pipe_readers_before_final_output(tmp_path: Path) -> None:
    tool = ShellExecTool()
    stdout_size = 700_000
    stderr_size = 650_000
    command = (
        f'"{sys.executable}" -c "import sys; '
        f"sys.stdout.write('o' * {stdout_size}); sys.stdout.flush(); "
        f"sys.stderr.write('e' * {stderr_size}); sys.stderr.flush()"
        '"'
    )

    result = tool.invoke(
        ToolCall(tool_name="shell_exec", arguments={"command": command}),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert result.data["stdout"] == "o" * stdout_size
    assert result.data["stderr"] == "e" * stderr_size
    assert result.content == ("o" * stdout_size) + ("e" * stderr_size)


def test_shell_exec_emits_incremental_stdout_progress_and_preserves_final_output(
    tmp_path: Path,
) -> None:
    tool = ShellExecTool()
    progress: list[dict[str, object]] = []
    command = (
        f'"{sys.executable}" -c "import sys, time; '
        "sys.stdout.write('alpha\\n'); sys.stdout.flush(); "
        "time.sleep(0.2); "
        "sys.stdout.write('omega\\n'); sys.stdout.flush()"
        '"'
    )

    with bind_runtime_tool_context(
        RuntimeToolInvocationContext(
            session_id="shell-progress",
            emit_tool_progress=lambda payload: progress.append(dict(payload)),
        )
    ):
        result = tool.invoke(
            ToolCall(tool_name="shell_exec", arguments={"command": command}),
            workspace=tmp_path,
        )

    assert result.status == "ok"
    assert result.content == "alpha\nomega\n"
    assert result.data["stdout"] == "alpha\nomega\n"
    assert [item["stream"] for item in progress] == ["stdout", "stdout"]
    assert "alpha\n" in {item["chunk"] for item in progress}
    assert "omega\n" in {item["chunk"] for item in progress}


def test_shell_exec_progress_callback_failure_does_not_break_final_result(
    tmp_path: Path,
) -> None:
    tool = ShellExecTool()

    def failing_progress_callback(_payload: object) -> None:
        raise RuntimeError("consumer disconnected")

    with bind_runtime_tool_context(
        RuntimeToolInvocationContext(
            session_id="shell-progress-failure",
            emit_tool_progress=failing_progress_callback,
        )
    ):
        result = tool.invoke(
            ToolCall(
                tool_name="shell_exec",
                arguments={"command": f'"{sys.executable}" -c "print(\'done\')"'},
            ),
            workspace=tmp_path,
        )

    assert result.status == "ok"
    assert result.content == "done\n"


def test_shell_exec_large_output_spills_full_payload_via_central_cap(tmp_path: Path) -> None:
    payload_size = MAX_TOOL_OUTPUT_BYTES + 10_000
    tool = ShellExecTool()
    command = f'"{sys.executable}" -c "import sys; sys.stdout.write(chr(120)*{payload_size})"'

    result = tool.invoke(
        ToolCall(tool_name="shell_exec", arguments={"command": command}),
        workspace=tmp_path,
    )
    capped = cap_tool_result_output(result, workspace=tmp_path)

    assert capped.truncated is True
    assert capped.partial is True
    assert capped.reference is not None
    assert isinstance(capped.content, str)
    assert "[Tool output truncated:" in capped.content
    assert "artifact_id=" in capped.content
    assert "read/search the full output" in capped.content
    assert capped.reference.startswith("artifact:")

    artifact = capped.data["artifact"]
    assert isinstance(artifact, dict)
    typed_artifact = cast(dict[str, object], artifact)
    reference_path = Path(str(typed_artifact["path"]))
    assert reference_path.exists()
    assert len(reference_path.read_text(encoding="utf-8")) == payload_size
    assert not (tmp_path / ".voidcode" / "tool-output").exists()


# ── Target contract: ShellExecArgs.description ──────────────────────────
# These tests encode the expected behaviour BEFORE the field exists.
# They are expected to fail (RED) until T2 adds `description` support.


def test_shell_exec_args_supports_description_field() -> None:
    """ShellExecArgs must accept an optional human-readable description."""
    from voidcode.tools._pydantic_args import ShellExecArgs

    args = ShellExecArgs.model_validate(
        {"command": "ls -la", "description": "List directory contents"}
    )
    assert args.description == "List directory contents"


def test_shell_exec_args_description_optional() -> None:
    """description is optional; command alone must remain valid."""
    from voidcode.tools._pydantic_args import ShellExecArgs

    args = ShellExecArgs.model_validate({"command": "ls"})
    assert args.description is None


def test_shell_exec_args_description_non_empty_when_provided() -> None:
    """An explicitly empty description must be rejected (like command)."""
    from pydantic import ValidationError

    from voidcode.tools._pydantic_args import ShellExecArgs

    with pytest.raises(ValidationError, match="description must not be empty"):
        ShellExecArgs.model_validate({"command": "ls", "description": "  "})
