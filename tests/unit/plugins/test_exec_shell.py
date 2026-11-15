"""Behavior tests for the exec plugin's shell tool on the core process lease.

The lease owns the machine, so these tests drive a fake lease: it records the call the tool made
and returns the scripted `ProcessResult` the host would have produced. What is under test is the
tool's contract — the argv/cwd/timeout it asks for, complete captured-text references,
and honest aggregate capture/timeout/cancellation flags.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from voidcode.capabilities.exec.shell_exec import ShellExecResultBody
from voidcode.plugins.exec import create_plugin
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.services import ArtifactReference, ProcessResult
from voidcode.sdk.tools import EmptyOutput, ToolCall, ToolFailure, ToolSuccess

_SHELL_WRAPPER = ["cmd.exe", "/c"] if os.name == "nt" else ["/bin/sh", "-c"]


class _AbortSignal:
    def __init__(self) -> None:
        self._cancelled = False
        self._reason: str | None = None

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    @property
    def reason(self) -> str | None:
        return self._reason

    def set_cancelled(self, value: bool, *, reason: str | None = None) -> None:
        self._cancelled = value
        self._reason = reason


class _FakeLease:
    """A scripted lease: records the call it received and returns the result it was given."""

    def __init__(self, result: ProcessResult | None = None) -> None:
        self.result = result if result is not None else ProcessResult(exit_code=0, stdout="", stderr="", duration_ms=1)
        self.calls: list[dict[str, object]] = []
        self.spills: list[dict[str, object]] = []

    def run_process(
        self,
        argv: object,
        *,
        cwd: object = None,
        env: object = None,
        stdin: object = None,
        timeout_seconds: object = None,
        max_output_bytes: object = 262144,
        on_output: object = None,
    ) -> ProcessResult:
        self.calls.append(
            {
                "argv": argv,
                "cwd": cwd,
                "env": env,
                "stdin": stdin,
                "timeout_seconds": timeout_seconds,
                "max_output_bytes": max_output_bytes,
                "on_output": on_output,
            }
        )
        return self.result

    def run_query_process(
        self,
        argv: object,
        *,
        cwd: object = None,
        env: object = None,
        stdin: object = None,
        timeout_seconds: object = None,
        max_output_bytes: object = 262144,
        on_output: object = None,
    ) -> ProcessResult:
        return self.run_process(
            argv,
            cwd=cwd,
            env=env,
            stdin=stdin,
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
            on_output=on_output,
        )

    def spill_artifact(self, data: bytes | str, *, media_type: str = "text/plain; charset=utf-8") -> ArtifactReference:
        payload = data.encode("utf-8") if isinstance(data, str) else bytes(data)
        self.spills.append({"data": payload, "media_type": media_type})
        return ArtifactReference(hashlib.sha256(payload).hexdigest(), len(payload), media_type)


def _context_with_progress(workspace: Path, chunks: list[dict[str, object]]) -> ToolContext:
    """The SDK context carries the progress sink as a field; append to a list to observe it."""
    return ToolContext(
        workspace=workspace,
        session_id="session-test",
        run_id="run-test",
        invocation_id="invocation-test",
        revision_id="rev-test",
        actor="model",
        emit_tool_progress=chunks.append,
    )


def _shell_tool():
    return create_plugin({}).tools[0]


def _context(workspace: Path, abort_signal: object | None = None) -> ToolContext:
    return ToolContext(
        workspace=workspace,
        session_id="session-test",
        run_id="run-test",
        invocation_id="invocation-test",
        revision_id="rev-test",
        actor="model",
        abort_signal=abort_signal,  # type: ignore[arg-type]
    )


def _invoke(command: str, tmp_path: Path, lease: _FakeLease, *, timeout: int | None = None, abort_signal: object | None = None):
    arguments: dict[str, object] = {"command": command}
    if timeout is not None:
        arguments["timeout"] = timeout
    return _shell_tool().invoke(
        ToolCall("shell_exec", arguments),
        context=_context(tmp_path, abort_signal),
        services=lease,  # type: ignore[arg-type]
    )


def test_shell_exec_runs_in_the_workspace_and_reports_success(tmp_path: Path) -> None:
    command = '"python" -c "import os; print(os.getcwd())"'
    lease = _FakeLease(ProcessResult(exit_code=0, stdout=f"{tmp_path.resolve()}\n", stderr="", duration_ms=12))

    result = _invoke(command, tmp_path, lease)

    assert isinstance(result, ToolSuccess)
    assert isinstance(result.output, EmptyOutput)
    assert result.output.presentation is not None and "Captured stdout:" in result.output.presentation
    body = result.body
    assert isinstance(body, ShellExecResultBody)
    assert body.command == command
    assert body.cwd == str(tmp_path.resolve())
    assert body.exit_code == 0
    assert body.timeout == 120
    assert body.truncated is False
    assert body.stdout.sha256 == hashlib.sha256(f"{tmp_path.resolve()}\n".encode()).hexdigest()
    assert body.stderr.bytes == 0
    assert lease.spills == [
        {"data": f"{tmp_path.resolve()}\n".encode(), "media_type": "text/plain; charset=utf-8"},
        {"data": b"", "media_type": "text/plain; charset=utf-8"},
    ]
    assert body.as_payload()["exit_code"] == 0
    call = lease.calls[0]
    assert call["argv"] == [*_SHELL_WRAPPER, command]
    assert call["cwd"] == tmp_path.resolve()
    assert call["timeout_seconds"] == 120.0
    assert call["max_output_bytes"] == 128 * 1024 * 1024


def test_shell_exec_nonzero_exit_keeps_streams_apart(tmp_path: Path) -> None:
    lease = _FakeLease(ProcessResult(exit_code=7, stdout="stdout\n", stderr="stderr\n", duration_ms=5))

    result = _invoke("exit 7", tmp_path, lease)

    assert isinstance(result, ToolFailure)
    assert isinstance(result.output, EmptyOutput)
    assert result.output.presentation is not None and "Captured stdout:" in result.output.presentation
    assert "Command exited with code 7" in result.error
    assert isinstance(result.body, ShellExecResultBody)
    assert result.body.exit_code == 7
    assert result.body.stdout.sha256 == hashlib.sha256(b"stdout\n").hexdigest()
    assert result.body.stderr.sha256 == hashlib.sha256(b"stderr\n").hexdigest()


def test_shell_exec_reports_a_zero_exit_without_streams(tmp_path: Path) -> None:
    lease = _FakeLease(ProcessResult(exit_code=3, stdout="", stderr="", duration_ms=5))

    result = _invoke("false", tmp_path, lease)

    assert isinstance(result, ToolFailure)
    assert result.error.startswith("Command exited with code 3; captured stderr:")


@pytest.mark.parametrize("command", ["cat file.txt", "grep foo file.txt", "sed -i 's/a/b/' file.txt", "find . -name x"])
def test_shell_exec_passes_commands_to_the_lease(tmp_path: Path, command: str) -> None:
    lease = _FakeLease()

    result = _invoke(command, tmp_path, lease)

    assert isinstance(result, ToolSuccess)
    assert lease.calls[0]["argv"] == [*_SHELL_WRAPPER, command]


def test_shell_exec_requires_a_command(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        _shell_tool().invoke(ToolCall("shell_exec", {}), context=_context(tmp_path), services=_FakeLease())  # type: ignore[arg-type]


def test_shell_exec_timeout_returns_a_partial_body_with_its_artifact(tmp_path: Path) -> None:
    lease = _FakeLease(ProcessResult(exit_code=None, stdout="partial\n", stderr="", duration_ms=1000, timed_out=True))

    result = _invoke("sleep 1", tmp_path, lease, timeout=1)

    assert isinstance(result, ToolFailure)
    body = result.body
    assert isinstance(body, ShellExecResultBody)
    assert body.timed_out is True
    assert body.exit_code is None
    assert body.timeout == 1
    assert body.stdout.sha256 == hashlib.sha256(b"partial\n").hexdigest()
    assert result.error == "shell_exec command timed out after 1s"
    assert result.timeout_seconds == 1
    assert lease.calls[0]["timeout_seconds"] == 1.0
    assert body.stderr.bytes == 0
    assert lease.spills == [
        {"data": b"partial\n", "media_type": "text/plain; charset=utf-8"},
        {"data": b"", "media_type": "text/plain; charset=utf-8"},
    ]


def test_shell_exec_cancellation_reports_the_abort_signal(tmp_path: Path) -> None:
    signal = _AbortSignal()
    signal.set_cancelled(True, reason="test abort")
    lease = _FakeLease(ProcessResult(exit_code=None, stdout="started\n", stderr="", duration_ms=20, cancelled=True))

    result = _invoke("sleep 1", tmp_path, lease, abort_signal=signal)

    assert isinstance(result, ToolFailure)
    assert result.error == "User aborted the command."
    assert isinstance(result.output, EmptyOutput)
    assert result.output.presentation is not None and result.output.presentation.startswith("User aborted the command.\n\nCaptured stdout:")
    body = result.body
    assert isinstance(body, ShellExecResultBody)
    assert body.cancelled is True
    assert body.reason == "test abort"
    assert body.timed_out is False
    assert body.stdout.sha256 == hashlib.sha256(b"started\n").hexdigest()
    assert body.as_payload()["reason"] == "test abort"


@pytest.mark.parametrize("timeout", [0, -1, 601, True, "5", 1.5])
def test_shell_exec_rejects_invalid_timeout_before_process_start(tmp_path: Path, timeout: object) -> None:
    lease = _FakeLease()
    with pytest.raises(ValueError):
        _shell_tool().invoke(
            ToolCall("shell_exec", {"command": "echo ok", "timeout": timeout}),
            context=_context(tmp_path),
            services=lease,
        )
    assert not lease.calls


def test_shell_exec_rejects_unknown_arguments(tmp_path: Path) -> None:
    lease = _FakeLease()
    with pytest.raises(ValueError):
        _shell_tool().invoke(
            ToolCall("shell_exec", {"command": "echo ok", "typo": True}),
            context=_context(tmp_path),
            services=lease,
        )
    assert not lease.calls


def test_shell_exec_reports_the_tool_timeout_with_partial_output(tmp_path: Path) -> None:
    lease = _FakeLease(ProcessResult(exit_code=None, stdout="partial\n", stderr="", duration_ms=30, timed_out=True))

    result = _invoke("sleep 1", tmp_path, lease, timeout=1)

    assert isinstance(result, ToolFailure)
    assert isinstance(result.body, ShellExecResultBody)
    assert result.body.timed_out is True
    assert result.body.timeout == 1
    assert result.body.stdout.sha256 == hashlib.sha256(b"partial\n").hexdigest()
    assert result.error == "shell_exec command timed out after 1s"
    assert lease.calls[0]["timeout_seconds"] == 1.0


def test_shell_exec_spills_the_full_stream_to_artifact(tmp_path: Path) -> None:
    full = "x" * (60 * 1024)
    result_value = ProcessResult(exit_code=0, stdout=full, stderr="", duration_ms=8)
    lease = _FakeLease(result_value)

    result = _invoke("generate", tmp_path, lease)

    assert isinstance(result, ToolSuccess)
    body = result.body
    assert isinstance(body, ShellExecResultBody)
    assert body.stdout.sha256 == hashlib.sha256(full.encode()).hexdigest()
    assert isinstance(result.output, EmptyOutput)
    assert lease.spills == [
        {"data": full.encode(), "media_type": "text/plain; charset=utf-8"},
        {"data": b"", "media_type": "text/plain; charset=utf-8"},
    ]
    assert result.output.bounds.reference == body.stdout
    assert result.output.presentation is not None and full not in result.output.presentation


def test_shell_exec_forwards_bounded_progress_chunks(tmp_path: Path) -> None:
    lease = _FakeLease(ProcessResult(exit_code=0, stdout="hi\n", stderr="", duration_ms=2))
    chunks: list[dict[str, object]] = []
    context = _context_with_progress(tmp_path, chunks)

    result = _shell_tool().invoke(ToolCall("shell_exec", {"command": "echo hi"}), context=context, services=lease)  # type: ignore[arg-type]

    assert isinstance(result, ToolSuccess)
    on_output = lease.calls[0]["on_output"]
    assert callable(on_output)
    on_output("stdout", b"hi\r\nthere")
    on_output("stderr", b"y" * 20_000)
    assert chunks == [
        {"stream": "stdout", "text": "hi\nthere", "run_id": "run-test", "invocation_id": "invocation-test"},
        {"stream": "stderr", "text": "y" * 12_000, "run_id": "run-test", "invocation_id": "invocation-test"},
        {"stream": "stderr", "text": "y" * 8_000, "run_id": "run-test", "invocation_id": "invocation-test"},
    ]


def test_shell_exec_without_a_progress_sink_asks_for_no_callback(tmp_path: Path) -> None:
    lease = _FakeLease()

    result = _invoke("echo hi", tmp_path, lease)

    assert isinstance(result, ToolSuccess)
    assert lease.calls[0]["on_output"] is None
