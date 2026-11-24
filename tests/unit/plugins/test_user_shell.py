"""Bounded output, stable failures, and cancellation behavior for the ``!`` command."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from voidcode.capabilities.exec.shell_exec import SHELL_CAPTURE_BYTES, shell_argv
from voidcode.plugins.user_shell.user_shell import (
    DEFAULT_TIMEOUT_SECONDS,
    MAX_SHELL_EVENT_BYTES,
    PREVIEW_BYTES,
    ShellErrorCode,
    clip_text,
    event_payload_size,
)
from voidcode.plugins.user_shell.user_shell_plugin import ShellCommand
from voidcode.sdk.commands import CommandInvocation
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.services import ArtifactReference, PluginServices, ProcessFailure, ProcessResult
from voidcode.sdk.tools import ToolEffect


class _Abort:
    def __init__(self, cancelled: bool = False) -> None:
        self._cancelled = cancelled

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    @property
    def reason(self) -> str | None:
        return "test cancel" if self._cancelled else None

    def set_cancelled(self, value: bool, *, reason: str | None = None) -> None:
        _ = reason
        self._cancelled = value


class ProcessServices(PluginServices):
    def __init__(
        self,
        result: ProcessResult | None = None,
        *,
        error: BaseException | None = None,
        artifact_error: BaseException | None = None,
    ) -> None:
        self.result = result or ProcessResult(exit_code=0, stdout="", stderr="", duration_ms=0)
        self.error = error
        self.artifact_error = artifact_error
        self.calls: list[tuple[list[str], dict[str, object]]] = []
        self.events: list[tuple[str, Mapping[str, object]]] = []
        self.artifacts: dict[str, bytes] = {}
        self.spills: list[tuple[str, str]] = []

    def run_process(self, argv: Sequence[str], **kwargs: object) -> ProcessResult:
        self.calls.append((list(argv), dict(kwargs)))
        if self.error is not None:
            raise self.error
        return self.result

    def run_query_process(self, argv: Sequence[str], **kwargs: object) -> ProcessResult:
        return self.run_process(argv, **kwargs)

    def spill_artifact(self, data: bytes | str, *, media_type: str = "text/plain; charset=utf-8") -> ArtifactReference:
        if self.artifact_error is not None:
            raise self.artifact_error
        raw = data.encode("utf-8") if isinstance(data, str) else data
        self.spills.append((media_type, raw.decode("utf-8", errors="replace")))
        sha = hashlib.sha256(raw).hexdigest()
        self.artifacts[sha] = raw
        return ArtifactReference(sha256=sha, bytes=len(raw), media_type=media_type)

    def stage_event(self, event_type: str, payload: Mapping[str, object]) -> None:
        self.events.append((event_type, payload))


def _invoke(
    tmp_path: Path,
    services: ProcessServices,
    command: str = "echo hi",
    *,
    timeout: object = None,
    abort: _Abort | None = None,
):
    arguments: dict[str, object] = {"command": command}
    if timeout is not None:
        arguments["timeout"] = timeout
    context = ToolContext(
        workspace=tmp_path,
        session_id="session",
        run_id="run",
        invocation_id="invocation",
        revision_id="revision",
        actor="user",
        logical_turn="turn",
        abort_signal=abort,  # type: ignore[arg-type]
    )
    return ShellCommand().invoke(CommandInvocation(context=context, name="shell", arguments=arguments), services)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _event(services: ProcessServices) -> Mapping[str, object]:
    assert len(services.events) == 1
    event_type, payload = services.events[0]
    assert event_type == "shell_executed"
    return payload


def test_definition_declares_execute_timeout_and_cancellation_opt_in() -> None:
    definition = ShellCommand.definition

    assert definition.effects == frozenset({ToolEffect.EXECUTE})
    assert definition.busy_gate is False
    assert definition.client_syntax == "!"
    assert definition.slash_visible is False
    assert definition.usage == "!<command>"
    assert definition.examples == ("!git status",)
    assert definition.preserve_cancelled_result is True
    assert definition.argument_schema["properties"]["timeout"]["maximum"] == 600
    assert definition.argument_schema["properties"]["timeout"]["default"] == DEFAULT_TIMEOUT_SECONDS


def test_shell_argv_is_non_login_and_does_not_read_profiles() -> None:
    if os.name == "nt":
        pytest.skip("POSIX shell assertion")
    argv = shell_argv("echo hi")

    assert argv == ["/bin/sh", "-c", "echo hi"]
    assert "-l" not in argv
    assert "-lc" not in argv


def test_run_process_contract_uses_core_ceiling_and_workspace(tmp_path: Path) -> None:
    services = ProcessServices()
    result = _invoke(tmp_path, services, "printf hi", timeout=5)

    assert result.status == "ok"
    argv, kwargs = services.calls[0]
    assert argv == ["/bin/sh", "-c", "printf hi"]
    assert kwargs["cwd"] == tmp_path
    assert kwargs["timeout_seconds"] == 5
    assert kwargs["max_output_bytes"] == SHELL_CAPTURE_BYTES


def test_command_is_passed_verbatim() -> None:
    # Direct command invocation is not the TUI `!` route: no prefix stripping happens here.
    services = ProcessServices(ProcessResult(0, "ok", "", 1))
    _invoke(Path("/tmp"), services, "  printf '  x  '  ")

    assert services.calls[0][0][2] == "  printf '  x  '  "


@pytest.mark.parametrize("command", ["", " ", "!", "!  "])
def test_empty_command_never_starts_a_process(tmp_path: Path, command: str) -> None:
    services = ProcessServices()
    result = _invoke(tmp_path, services, command)

    assert result.status == "error"
    assert result.data is not None and result.data["error"] == ShellErrorCode.EMPTY_COMMAND.value
    assert services.calls == []
    assert services.events == []


@pytest.mark.parametrize("timeout", [0, -1, 601, True, 1.5, "5"])
def test_invalid_timeout_is_a_stable_error(tmp_path: Path, timeout: object) -> None:
    services = ProcessServices()
    result = _invoke(tmp_path, services, "true", timeout=timeout)

    assert result.status == "error"
    assert result.data is not None and result.data["error"] == ShellErrorCode.INVALID_TIMEOUT.value
    assert "guidance" in result.data
    assert services.calls == []
    assert services.events == []


def test_default_timeout_is_600(tmp_path: Path) -> None:
    services = ProcessServices()
    _invoke(tmp_path, services, "true")
    assert services.calls[0][1]["timeout_seconds"] == 600


def test_success_spills_both_streams_and_bounds_preview(tmp_path: Path) -> None:
    stdout = "o" * (PREVIEW_BYTES + 500)
    stderr = "e" * 100
    services = ProcessServices(ProcessResult(0, stdout, stderr, 12))
    result = _invoke(tmp_path, services, "echo big")

    assert result.status == "ok"
    assert result.data is not None
    assert result.data["status"] == "completed"
    assert result.data["capture_truncated"] is False
    assert result.data["preview_truncated"] is True
    assert result.data["stdout_artifact"] == {
        "sha256": _sha(stdout),
        "bytes": len(stdout.encode("utf-8")),
        "media_type": "text/plain; charset=utf-8",
    }
    assert result.data["stderr_artifact"] == {
        "sha256": _sha(stderr),
        "bytes": len(stderr.encode("utf-8")),
        "media_type": "text/plain; charset=utf-8",
    }
    assert result.data["stdout_uri"] == f"artifact://{_sha(stdout)}"
    assert result.output is not None
    assert "preview truncated" in result.output
    assert f"stdout artifact://{_sha(stdout)}" in result.output
    assert result.data["command"] == "echo big"
    assert services.artifacts[_sha(stdout)] == stdout.encode("utf-8")
    assert services.artifacts[_sha(stderr)] == stderr.encode("utf-8")

    event = _event(services)
    assert event["status"] == "completed"
    assert event["capture_truncated"] is False
    assert event["stdout_artifact"] == result.data["stdout_artifact"]
    assert "stdout" not in event and "stderr" not in event
    assert "record" not in event and "preview" not in event
    assert event_payload_size(dict(event)) <= MAX_SHELL_EVENT_BYTES


def test_empty_streams_are_still_recoverable_artifacts(tmp_path: Path) -> None:
    services = ProcessServices(ProcessResult(0, "", "", 1))
    result = _invoke(tmp_path, services, "true")

    assert result.status == "ok"
    assert result.data is not None
    empty_sha = _sha("")
    assert result.data["stdout_artifact"] == {"sha256": empty_sha, "bytes": 0, "media_type": "text/plain; charset=utf-8"}
    assert result.data["stderr_artifact"] == {"sha256": empty_sha, "bytes": 0, "media_type": "text/plain; charset=utf-8"}
    assert result.output == "(no output; exit code 0)"
    assert services.artifacts[empty_sha] == b""


def test_capture_truncation_is_separate_from_artifact(tmp_path: Path) -> None:
    services = ProcessServices(ProcessResult(0, "retained", "", 0, truncated=True))
    result = _invoke(tmp_path, services)

    assert result.status == "ok"
    assert result.data is not None
    assert result.data["capture_truncated"] is True
    assert result.data["preview_truncated"] is False
    assert result.data["stdout_artifact"] is not None
    assert result.output is not None and "capture limit" in result.output
    event = _event(services)
    assert event["capture_truncated"] is True
    assert event["stdout_artifact"] is not None


def test_nonzero_exit_is_a_command_error_with_recoverable_output(tmp_path: Path) -> None:
    services = ProcessServices(ProcessResult(7, "stdout\n", "stderr\n", 25))
    result = _invoke(tmp_path, services)

    assert result.status == "error"
    assert result.error is not None
    assert result.error.startswith("shell command exited with code 7; full output: stdout artifact://")
    assert result.data is not None
    assert result.data["error"] == ShellErrorCode.NONZERO_EXIT.value
    assert result.data["status"] == "failed"
    assert result.data["exit_code"] == 7
    assert result.data["guidance"]
    assert result.output == "stdout\nstderr\n"
    assert services.events and services.artifacts


def test_timeout_is_a_command_error_with_captured_prefix(tmp_path: Path) -> None:
    services = ProcessServices(ProcessResult(None, "partial\n", "", 25, timed_out=True))
    result = _invoke(tmp_path, services, timeout=2)

    assert result.status == "error"
    assert result.error is not None
    assert result.error.startswith("shell command timed out after 2s; captured prefix: stdout artifact://")
    assert result.data is not None
    assert result.data["error"] == ShellErrorCode.TIMED_OUT.value
    assert result.data["status"] == "timed_out"
    assert result.data["timed_out"] is True
    assert result.data["exit_code"] is None
    assert result.output is not None and result.output.startswith("command timed out\npartial")
    assert result.data["stdout_artifact"] is not None
    assert _event(services)["timed_out"] is True


def test_cancelled_result_keeps_partial_preview_without_artifacts(tmp_path: Path) -> None:
    services = ProcessServices(ProcessResult(None, "partial\n", "", 25, cancelled=True))
    result = _invoke(tmp_path, services)

    assert result.status == "error"
    assert "cancelled" in (result.error or "")
    assert result.data is not None
    assert result.data["error"] == ShellErrorCode.CANCELLED.value
    assert result.data["status"] == "cancelled"
    assert result.data["cancelled"] is True
    assert result.data["stdout_artifact"] is None
    assert result.data["stderr_artifact"] is None
    assert result.output is not None and result.output.startswith("command cancelled\npartial")
    assert services.spills == []
    assert _event(services)["cancelled"] is True
    assert "stdout" not in _event(services)


def test_abort_signal_after_process_completion_is_cancelled(tmp_path: Path) -> None:
    services = ProcessServices(ProcessResult(0, "done\n", "", 1))
    result = _invoke(tmp_path, services, abort=_Abort(cancelled=True))

    assert result.status == "error"
    assert result.data is not None and result.data["error"] == ShellErrorCode.CANCELLED.value
    assert result.data["stdout_artifact"] is None
    assert services.spills == []
    assert _event(services)["cancelled"] is True


@pytest.mark.parametrize(
    ("error", "code"),
    [
        # Deliberately misleading text proves classification uses only the typed kind.
        (ProcessFailure("start_failed", "sandbox helper failed and while reading output"), ShellErrorCode.START_FAILED),
        (ProcessFailure("capture_failed", "failed to start process"), ShellErrorCode.CAPTURE_FAILED),
        (ProcessFailure("sandbox_failed", "failed while reading process output"), ShellErrorCode.SANDBOX_FAILED),
        (ProcessFailure("process_failed", "sandbox helper failed"), ShellErrorCode.PROCESS_FAILED),
        (ValueError("failed to start process: boom"), ShellErrorCode.PROCESS_FAILED),
        (RuntimeError("unexpected"), ShellErrorCode.PROCESS_FAILED),
    ],
)
def test_process_errors_have_stable_codes(tmp_path: Path, error: BaseException, code: ShellErrorCode) -> None:
    services = ProcessServices(error=error)
    result = _invoke(tmp_path, services)

    assert result.status == "error"
    assert result.error == (
        "shell command could not start"
        if code is ShellErrorCode.START_FAILED
        else "shell command failed while capturing its output"
        if code is ShellErrorCode.CAPTURE_FAILED
        else "shell command sandbox failed"
        if code is ShellErrorCode.SANDBOX_FAILED
        else "shell command failed before producing a result"
    )
    assert result.data is not None
    assert result.data["error"] == code.value
    assert result.data["guidance"]
    assert result.data["detail"]
    assert services.events == []


def test_artifact_failure_keeps_bounded_preview_and_reports_code(tmp_path: Path) -> None:
    services = ProcessServices(ProcessResult(0, "visible\n", "", 2), artifact_error=OSError("disk full"))
    result = _invoke(tmp_path, services)

    assert result.status == "error"
    assert result.error == "shell command finished but its output artifact could not be stored"
    assert result.data is not None
    assert result.data["error"] == ShellErrorCode.ARTIFACT_FAILED.value
    assert result.data["status"] == "failed"
    assert result.data["stdout_artifact"] is None
    assert result.data["detail"] == "disk full"
    assert result.output == "visible\n"
    assert _event(services)["error"] == ShellErrorCode.ARTIFACT_FAILED.value


def test_event_payload_is_bounded_and_command_metadata_truncated(tmp_path: Path) -> None:
    services = ProcessServices(ProcessResult(0, "ok", "", 1))
    result = _invoke(Path("/tmp"), services, "\x00" * 5000)

    assert result.status == "ok"
    event = _event(services)
    assert event["command_truncated"] is True
    assert len(str(event["command"]).encode("utf-8")) <= 4096
    assert event_payload_size(dict(event)) <= MAX_SHELL_EVENT_BYTES
    assert event_payload_size(result.as_payload()) <= MAX_SHELL_EVENT_BYTES


def test_control_heavy_preview_and_result_stay_under_the_plugin_bound(tmp_path: Path) -> None:
    services = ProcessServices(ProcessResult(0, "\x00" * (PREVIEW_BYTES + 100), "", 1))
    result = _invoke(tmp_path, services)

    assert result.status == "ok"
    assert result.data is not None and result.data["preview_truncated"] is True
    assert event_payload_size(result.as_payload()) <= MAX_SHELL_EVENT_BYTES
    assert event_payload_size(dict(_event(services))) <= MAX_SHELL_EVENT_BYTES
    assert result.data["stdout_artifact"]["bytes"] == PREVIEW_BYTES + 100


def test_clip_text_never_splits_utf8() -> None:
    text = "é" * 10
    clipped, truncated = clip_text(text, 5)

    assert truncated is True
    assert len(clipped.encode("utf-8")) <= 5
    assert clipped == "éé"
