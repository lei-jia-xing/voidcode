from __future__ import annotations

import hashlib
import os
import shlex
import sys
import threading
import time
from collections.abc import Mapping
from pathlib import Path

import pytest

from voidcode.core.tool_context import ToolContext
from voidcode.tools.contracts import TextOutput, ToolCall, ToolFailure
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
    assert result.body.cwd == result.body.stdout.strip()
    assert result.body.exit_code == 0
    assert result.body.stdout.strip() == str(tmp_path.resolve())
    assert result.body.stderr == ""
    assert result.body.timeout == 120
    assert result.body.truncated is False
    assert result.body.stdout_truncated is False
    assert result.body.stderr_truncated is False


def test_shell_exec_nonzero_exit_preserves_separate_streams_as_failure(tmp_path: Path) -> None:
    command = f"\"{sys.executable}\" -c \"import sys; sys.stdout.write('stdout\\\\n'); sys.stderr.write('stderr\\\\n'); sys.exit(7)\""

    result = ShellExecTool().invoke(
        ToolCall(tool_name="shell_exec", arguments={"command": command}),
        context=ToolContext(workspace=tmp_path),
    )

    assert isinstance(result, ToolFailure)
    assert result.error == "stderr\n"
    assert isinstance(result.output, TextOutput)
    assert result.output.text == "stdout\n"
    assert isinstance(result.body, ShellExecResultBody)
    assert result.body.exit_code == 7


def test_shell_exec_keeps_stderr_visible_on_success(tmp_path: Path) -> None:
    result = ShellExecTool().invoke(
        ToolCall(
            tool_name="shell_exec",
            arguments={"command": _shell_command("import sys; print('stdout'); print('warning', file=sys.stderr)")},
        ),
        context=ToolContext(workspace=tmp_path),
    )

    assert result.status == "ok"
    assert isinstance(result.body, ShellExecResultBody)
    assert result.body.exit_code == 0
    assert result.body.stderr == "warning\n"
    assert isinstance(result.output, TextOutput)
    assert "warning" in result.output.text


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


def test_shell_exec_timeout_returns_partial_failure(tmp_path: Path) -> None:
    result = ShellExecTool().invoke(
        ToolCall(
            tool_name="shell_exec",
            arguments={
                "command": f'"{sys.executable}" -c "import time; time.sleep(2)"',
                "timeout": 1,
            },
        ),
        context=ToolContext(workspace=tmp_path),
    )

    assert isinstance(result, ToolFailure)
    assert isinstance(result.body, ShellExecResultBody)
    assert result.body.timed_out
    assert result.body.interrupted


def test_shell_exec_keeps_full_split_stream_bytes_on_nonzero_exit(tmp_path: Path) -> None:
    stdout = b"x" * 8191 + "雪".encode() + b"\xff\n" + b"stdout-line\n" * 12_000
    stderr = b"stderr-line\n" * 12_000
    script = (
        "import sys; "
        "sys.stdout.buffer.write(b'x'*8191 + '雪'.encode() + b'\\xff\\n' + b'stdout-line\\n'*12000); "
        "sys.stderr.buffer.write(b'stderr-line\\n'*12000); "
        "sys.exit(7)"
    )
    result = ShellExecTool().invoke(
        ToolCall(
            tool_name="shell_exec",
            tool_call_id="native-capture",
            arguments={"command": _shell_command(script)},
        ),
        context=ToolContext(workspace=tmp_path, session_id="native-capture-session"),
    )

    assert isinstance(result, ToolFailure)
    assert isinstance(result.body, ShellExecResultBody)
    assert result.body.exit_code == 7
    assert result.body.stdout_truncated is True
    assert result.body.stderr_truncated is True
    assert stdout.count(b"\n") >= 1_000
    assert len(stdout) > 50_000 and len(stderr) > 50_000
    for artifact, expected in ((result.body.stdout_artifact, stdout), (result.body.stderr_artifact, stderr)):
        assert artifact is not None
        path = Path(str(artifact["path"]))
        assert path.read_bytes() == expected
        assert artifact["byte_count"] == len(expected)
        assert artifact["sha256"] == hashlib.sha256(expected).hexdigest()


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


class _AbortSignal:
    cancelled = False
    reason: str | None = "native smoke cancellation"

    def set_cancelled(self, value: bool, *, reason: str | None = None) -> None:
        self.cancelled = value
        self.reason = reason


def _shell_command(script: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"


def test_shell_timeout_stops_child_group_and_keeps_partial_log(tmp_path: Path) -> None:
    marker = tmp_path / "child-survived-timeout"
    child = f"import time; time.sleep(1.5); open({str(marker)!r}, 'w').write('alive')"
    script = f"import subprocess,sys,time; subprocess.Popen([sys.executable, '-c', {child!r}]); print('partial', flush=True); time.sleep(30)"
    result = ShellExecTool().invoke(
        ToolCall(
            tool_name="shell_exec",
            arguments={"command": _shell_command(script), "timeout": 1},
        ),
        context=ToolContext(workspace=tmp_path),
    )

    assert isinstance(result, ToolFailure)
    assert isinstance(result.body, ShellExecResultBody)
    assert result.body.timed_out
    assert result.body.stdout == "partial\n"
    assert result.body.stdout_artifact is not None
    assert result.body.stop_confirmed in {True, False}
    time.sleep(1.7)
    assert not marker.exists()


def test_shell_runtime_timeout_stops_descendant_and_keeps_partial_output(tmp_path: Path) -> None:
    from voidcode.tools.contracts import RuntimeToolTimeoutError

    marker = tmp_path / "child-survived-runtime-timeout"
    child = f"import time; time.sleep(1.5); open({str(marker)!r}, 'w').write('alive')"
    script = f"import subprocess,sys,time; subprocess.Popen([sys.executable, '-c', {child!r}]); print('partial', flush=True); time.sleep(30)"
    try:
        ShellExecTool().invoke_with_runtime_timeout(
            ToolCall(tool_name="shell_exec", arguments={"command": _shell_command(script)}),
            context=ToolContext(workspace=tmp_path),
            timeout_seconds=1,
        )
    except RuntimeToolTimeoutError as exc:
        assert isinstance(exc.partial_result, ToolFailure)
        assert isinstance(exc.partial_result.body, ShellExecResultBody)
        assert exc.partial_result.body.timed_out
        assert exc.partial_result.body.stdout == "partial\n"
        artifact = exc.partial_result.body.stdout_artifact
        assert artifact is not None
        assert Path(str(artifact["path"])).read_bytes() == b"partial\n"
    else:
        raise AssertionError("runtime timeout returned a normal result")
    time.sleep(1.7)
    assert not marker.exists()


def test_shell_cancel_stops_child_group_and_preserves_raw_output(tmp_path: Path) -> None:
    marker = tmp_path / "child-survived-cancel"
    child = f"import time; time.sleep(1.5); open({str(marker)!r}, 'w').write('alive')"
    script = f"import subprocess,sys,time; subprocess.Popen([sys.executable, '-c', {child!r}]); print('started', flush=True); time.sleep(30)"
    signal = _AbortSignal()
    progress_seen = threading.Event()
    timer = threading.Thread(
        target=lambda: (progress_seen.wait(5), signal.set_cancelled(True)),
        daemon=True,
    )
    timer.start()
    result = ShellExecTool().invoke(
        ToolCall(tool_name="shell_exec", arguments={"command": _shell_command(script)}),
        context=ToolContext(
            workspace=tmp_path,
            abort_signal=signal,  # type: ignore[arg-type]
            emit_tool_progress=lambda _: progress_seen.set(),
        ),
    )

    assert isinstance(result, ToolFailure)
    assert isinstance(result.body, ShellExecResultBody)
    assert result.body.cancelled
    assert result.body.stdout_artifact is not None
    assert result.body.stop_confirmed in {True, False}
    time.sleep(1.7)
    assert not marker.exists()


def test_shell_capture_limit_returns_partial_artifact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from voidcode.tools import process_output

    monkeypatch.setattr(process_output, "_MAX_CAPTURE_BYTES", 1024)
    script = "import sys; sys.stdout.buffer.write(b'x'*4096); sys.stderr.buffer.write(b'y'*4096)"
    result = ShellExecTool().invoke(
        ToolCall(tool_name="shell_exec", arguments={"command": _shell_command(script)}),
        context=ToolContext(workspace=tmp_path),
    )

    assert isinstance(result, ToolFailure)
    assert isinstance(result.body, ShellExecResultBody)
    assert result.body.output_limit_exceeded
    assert result.body.stdout_artifact is not None
    assert Path(str(result.body.stdout_artifact["path"])).read_bytes() == b"x" * 1024


def test_shell_exec_preserves_command_and_inherited_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    capture = tmp_path / "capture"
    shim = bin_dir / "npm"
    shim.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "base = os.environ['CAPTURE']\n"
        "open(base + '.args', 'wb').write(b'\\0'.join(arg.encode() for arg in sys.argv[1:]))\n"
        "open(base + '.env', 'wb').write((os.environ.get('CI', '') + '\\0' + os.environ.get('NPM_CONFIG_YES', '')).encode())\n"
        "open(base + '.stdin', 'wb').write(sys.stdin.buffer.read())\n"
    )
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("CAPTURE", str(capture))
    monkeypatch.setenv("CI", "caller-value")
    monkeypatch.setenv("NPM_CONFIG_YES", "caller-no")
    command = "\nnpm 'arg with spaces' <<'EOF'\nfirst\nsecond\nEOF\n"

    result = ShellExecTool().invoke(
        ToolCall(tool_name="shell_exec", arguments={"command": command}),
        context=ToolContext(workspace=tmp_path),
    )

    assert isinstance(result.body, ShellExecResultBody)
    assert result.body.command == command
    assert hashlib.sha256(result.body.command.encode()).hexdigest() == hashlib.sha256(command.encode()).hexdigest()
    assert result.body.exit_code == 0
    assert (tmp_path / "capture.args").read_bytes() == b"arg with spaces"
    assert (tmp_path / "capture.env").read_bytes() == b"caller-value\0caller-no"
    assert (tmp_path / "capture.stdin").read_bytes() == b"first\nsecond\n"


@pytest.mark.parametrize(
    ("command", "suggested_tool"),
    [
        ("cat README.md", "read"),
        ("echo hi && grep foo bar.txt", "grep"),
        ("rg pattern .", "grep"),
        ("sed -i 's/a/b/' file.txt", "edit"),
        ("find . -name '*.py'", "glob"),
    ],
)
def test_shell_exec_blocks_misused_shell_patterns(tmp_path: Path, command: str, suggested_tool: str) -> None:
    with pytest.raises(ValueError, match=suggested_tool):
        ShellExecTool().invoke(
            ToolCall(tool_name="shell_exec", arguments={"command": command}),
            context=ToolContext(workspace=tmp_path),
        )


def test_shell_exec_allows_unmatched_commands(tmp_path: Path) -> None:
    echo_result = ShellExecTool().invoke(
        ToolCall(tool_name="shell_exec", arguments={"command": "echo intercepted"}),
        context=ToolContext(workspace=tmp_path),
    )
    assert echo_result.body.exit_code == 0
