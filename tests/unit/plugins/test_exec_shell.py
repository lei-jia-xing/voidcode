"""Behavior tests for the exec plugin's shell tool on the kernel process lease.

The lease owns the machine, so these tests drive a fake lease: it records the call the tool made
and returns the scripted `ProcessResult` the host would have produced. What is under test is the
tool's contract — interception before the lease, the argv/cwd/timeout it asks for, the
preview/artifact split of each stream, and the flags on each failure shape.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from voidcode.plugins.exec import create_plugin
from voidcode.plugins.exec.shell_exec import ShellExecResultBody
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.services import ProcessResult
from voidcode.sdk.tools import RuntimeToolTimeoutError, TextOutput, ToolCall, ToolFailure, ToolSuccess

_PREVIEW_BYTES = 25 * 1024
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


@dataclass(frozen=True, slots=True)
class _ArtifactReference:
    sha256: str
    bytes: int
    media_type: str


class _FakeLease:
    """A scripted lease: records the call it received and returns the result it was given."""

    def __init__(self, result: ProcessResult | None = None, *, spill: bool = True) -> None:
        self.result = result if result is not None else ProcessResult(exit_code=0, stdout="", stderr="", duration_ms=1)
        self.calls: list[dict[str, object]] = []
        self.spills: list[dict[str, object]] = []
        self.spill_enabled = spill

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

    def spill_artifact(self, data: bytes | str, *, media_type: str = "text/plain; charset=utf-8") -> _ArtifactReference | None:
        payload = data.encode("utf-8") if isinstance(data, str) else bytes(data)
        self.spills.append({"data": payload, "media_type": media_type})
        if not self.spill_enabled:
            return None
        return _ArtifactReference(hashlib.sha256(payload).hexdigest(), len(payload), media_type)


class _RefusingLease(_FakeLease):
    """A lease whose store refuses the spill; the tool keeps inline output and no reference."""

    def spill_artifact(self, data: bytes | str, *, media_type: str = "text/plain; charset=utf-8") -> None:
        return None


class _UnspilledLease(_FakeLease):
    """A lease with no artifact service at all (a handler that cannot spill)."""

    spill_artifact = None  # type: ignore[assignment]


def _context_with_progress(workspace: Path, chunks: list[dict[str, object]]) -> ToolContext:
    """The SDK context carries the progress sink as a field; append to a list to observe it."""
    return ToolContext(
        workspace=workspace,
        session_id="session-test",
        run_id="run-test",
        invocation_id="invocation-test",
        revision_id="rev-test",
        actor="model",
        capabilities=frozenset({"workspace.write"}),
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
        capabilities=frozenset({"workspace.write"}),
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
    assert isinstance(result.output, TextOutput)
    assert result.output.text == f"{tmp_path.resolve()}\n"
    body = result.body
    assert isinstance(body, ShellExecResultBody)
    assert body.command == command
    assert body.cwd == str(tmp_path.resolve())
    assert body.exit_code == 0
    assert body.timeout == 120
    assert body.truncated is False
    assert body.stdout_truncated is False
    assert body.stderr_truncated is False
    # Every non-empty stream is spilled, truncated or not; only the preview stays inline.
    assert body.stdout_artifact is not None
    assert body.stdout_artifact["artifact_id"] == hashlib.sha256(result.output.text.encode()).hexdigest()
    assert body.stderr_artifact is None
    assert lease.spills == [{"data": result.output.text.encode(), "media_type": "text/plain; charset=utf-8"}]
    assert body.output_char_count == len(result.output.text)
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
    assert isinstance(result.output, TextOutput)
    assert result.output.text == "stdout\n"
    assert result.error == "stderr\n"
    assert isinstance(result.body, ShellExecResultBody)
    assert result.body.exit_code == 7
    assert result.body.stdout == "stdout\n"
    assert result.body.stderr == "stderr\n"


def test_shell_exec_reports_a_zero_exit_without_streams(tmp_path: Path) -> None:
    lease = _FakeLease(ProcessResult(exit_code=3, stdout="", stderr="", duration_ms=5))

    result = _invoke("false", tmp_path, lease)

    assert isinstance(result, ToolFailure)
    assert result.error == "Command exited with code 3."


@pytest.mark.parametrize("command", ["cat file.txt", "grep foo file.txt", "sed -i 's/a/b/' file.txt", "find . -name x"])
def test_shell_exec_refuses_misused_shell_patterns(tmp_path: Path, command: str) -> None:
    lease = _FakeLease()

    with pytest.raises(ValueError, match="Blocked:") as exc:
        _invoke(command, tmp_path, lease)

    assert f"Original command: {command}" in str(exc.value)
    # Interception is pre-flight: a refused command never reaches the lease.
    assert lease.calls == []


def test_shell_exec_requires_a_command(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="shell_exec Validation error"):
        _shell_tool().invoke(ToolCall("shell_exec", {}), context=_context(tmp_path), services=_FakeLease())  # type: ignore[arg-type]


def test_shell_exec_without_a_lease_fails_visibly(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="shell_exec requires the kernel process lease"):
        _shell_tool().invoke(ToolCall("shell_exec", {"command": "echo hi"}), context=_context(tmp_path), services=None)  # type: ignore[arg-type]


def test_shell_exec_timeout_returns_a_partial_body_with_its_artifact(tmp_path: Path) -> None:
    lease = _FakeLease(ProcessResult(exit_code=None, stdout="partial\n", stderr="", duration_ms=1000, timed_out=True))

    result = _invoke("sleep 30", tmp_path, lease, timeout=1)

    assert isinstance(result, ToolFailure)
    body = result.body
    assert isinstance(body, ShellExecResultBody)
    assert body.timed_out is True
    assert body.interrupted is True
    assert body.stop_confirmed is True
    assert body.exit_code is None
    assert body.timeout == 1
    assert body.stdout == "partial\n"
    assert result.error == "shell_exec command timed out after 1s"
    assert result.timeout_seconds == 1
    assert lease.calls[0]["timeout_seconds"] == 1.0
    assert body.stdout_artifact is not None
    assert body.stdout_artifact["artifact_id"] == hashlib.sha256(b"partial\n").hexdigest()
    assert body.stdout_artifact["kind"] == "stdout"
    assert body.stdout_artifact["source"] == "process_output"
    assert body.stderr_artifact is None
    assert lease.spills == [{"data": b"partial\n", "media_type": "text/plain; charset=utf-8"}]


def test_shell_exec_cancellation_reports_the_abort_signal(tmp_path: Path) -> None:
    signal = _AbortSignal()
    signal.set_cancelled(True, reason="test abort")
    lease = _FakeLease(ProcessResult(exit_code=None, stdout="started\n", stderr="", duration_ms=20, cancelled=True))

    result = _invoke("sleep 30", tmp_path, lease, abort_signal=signal)

    assert isinstance(result, ToolFailure)
    assert result.error == "User aborted the command."
    assert isinstance(result.output, TextOutput)
    assert result.output.text == "User aborted the command.\n\nstarted\n"
    body = result.body
    assert isinstance(body, ShellExecResultBody)
    assert body.interrupted is True
    assert body.cancelled is True
    assert body.reason == "test abort"
    assert body.stop_confirmed is True
    assert body.timed_out is False
    assert body.stdout == "started\n"
    assert body.as_payload()["reason"] == "test abort"


def test_shell_exec_clamps_the_timeout_to_the_maximum(tmp_path: Path) -> None:
    lease = _FakeLease(ProcessResult(exit_code=0, stdout="ok\n", stderr="", duration_ms=3))

    result = _invoke("echo ok", tmp_path, lease, timeout=5000)

    assert isinstance(result, ToolSuccess)
    assert isinstance(result.body, ShellExecResultBody)
    assert result.body.timeout == 600
    assert lease.calls[0]["timeout_seconds"] == 600.0


def test_shell_exec_uses_the_smaller_runtime_timeout(tmp_path: Path) -> None:
    lease = _FakeLease(ProcessResult(exit_code=None, stdout="partial\n", stderr="", duration_ms=30, timed_out=True))

    with pytest.raises(RuntimeToolTimeoutError) as exc:
        _shell_tool().invoke_with_runtime_timeout(
            ToolCall("shell_exec", {"command": "sleep 30"}),
            context=_context(tmp_path),
            services=lease,  # type: ignore[arg-type]
            timeout_seconds=3,
        )

    assert exc.value.execution_stopped is True
    partial = exc.value.partial_result
    assert isinstance(partial, ToolFailure)
    assert isinstance(partial.body, ShellExecResultBody)
    assert partial.body.timed_out is True
    assert partial.body.stop_confirmed is True
    assert partial.body.timeout == 3
    assert partial.body.stdout == "partial\n"
    assert partial.error == "tool 'shell_exec' exceeded runtime timeout of 3s"
    assert lease.calls[0]["timeout_seconds"] == 3.0


@pytest.mark.parametrize("lease_kind", ["spilling", "refusing", "absent"])
def test_shell_exec_preview_overflow_spills_the_full_stream(tmp_path: Path, lease_kind: str) -> None:
    full = "x" * (60 * 1024)
    result_value = ProcessResult(exit_code=0, stdout=full, stderr="", duration_ms=8)
    lease = {"spilling": _FakeLease, "refusing": _RefusingLease, "absent": _UnspilledLease}[lease_kind](result_value)

    result = _invoke("generate", tmp_path, lease)

    assert isinstance(result, ToolSuccess)
    body = result.body
    assert isinstance(body, ShellExecResultBody)
    assert body.truncated is True
    assert body.stdout_truncated is True
    assert body.stderr_truncated is False
    assert body.stdout == full[: 25 * 1024] + "\n... output preview omitted ...\n" + full[-25 * 1024 :]
    assert isinstance(result.output, TextOutput)
    assert result.output.bounds.truncated is True
    assert result.output.bounds.partial is True
    if lease_kind == "spilling":
        assert lease.spills == [{"data": full.encode(), "media_type": "text/plain; charset=utf-8"}]
        assert body.stdout_artifact is not None
        assert body.stdout_artifact["artifact_id"] == hashlib.sha256(full.encode()).hexdigest()
        reference = result.output.bounds.reference
        assert reference is not None
        assert reference.uri == f"voidcode://artifact/{body.stdout_artifact['artifact_id']}"
        assert result.output.text == f"Full process output: {reference.uri}\n\n{body.stdout}"
    else:
        assert body.stdout_artifact is None
        assert result.output.bounds.reference is None
        assert result.output.text == body.stdout


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
    ]


def test_shell_exec_without_a_progress_sink_asks_for_no_callback(tmp_path: Path) -> None:
    lease = _FakeLease()

    result = _invoke("echo hi", tmp_path, lease)

    assert isinstance(result, ToolSuccess)
    assert lease.calls[0]["on_output"] is None
