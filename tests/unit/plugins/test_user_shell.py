"""Shell selection, output presentation, and truthful process outcomes."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from voidcode.plugins.user_shell import user_shell as user_shell_module
from voidcode.plugins.user_shell.user_shell import login_shell_args, parse_shell_escape
from voidcode.plugins.user_shell.user_shell_plugin import ShellCommand
from voidcode.sdk.commands import CommandInvocation
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.services import ArtifactReference, PluginServices, ProcessResult
from voidcode.sdk.tools import TOOL_OUTPUT_BYTES


def test_parse_shell_escape_detects_bang_prefix() -> None:
    assert parse_shell_escape("!ls -la") == "ls -la"
    assert parse_shell_escape("  !echo hi  ") == "echo hi"


def test_parse_shell_escape_strips_only_one_bang() -> None:
    assert parse_shell_escape("!!foo") == "!foo"


def test_parse_shell_escape_bare_bang_is_empty_command() -> None:
    assert parse_shell_escape("!") == ""


def test_parse_shell_escape_ignores_plain_input() -> None:
    assert parse_shell_escape("ls") is None
    assert parse_shell_escape("") is None
    assert parse_shell_escape("echo !") is None


def test_login_shell_args_posix_uses_lc() -> None:
    argv = login_shell_args("echo hi")

    assert argv[1] == "-lc"
    assert argv[2] == "echo hi"
    assert "/" in argv[0]


def test_login_shell_args_powershell_and_cmd(monkeypatch) -> None:
    monkeypatch.setattr(user_shell_module, "_default_user_shell", lambda: ("pwsh", "powershell"))
    assert login_shell_args("Get-Date") == ["pwsh", "-Command", "Get-Date"]

    monkeypatch.setattr(user_shell_module, "_default_user_shell", lambda: ("cmd.exe", "cmd"))
    assert login_shell_args("dir") == ["cmd.exe", "/c", "dir"]


def test_shell_preserves_large_output_without_custom_middle_slicing(tmp_path: Path) -> None:
    large_text = "x" * 20_000
    services = ProcessServices(ProcessResult(0, large_text, "", 10))
    result = _invoke(tmp_path, services)
    assert result.output == large_text
    assert result.data is not None
    assert result.data["truncated"] is False
    assert "artifact" not in result.data
    assert services.artifacts == {}


def test_shell_output_exceeding_budget_spills_artifact(tmp_path: Path) -> None:
    overflow_text = "y" * (TOOL_OUTPUT_BYTES + 500)
    services = ProcessServices(ProcessResult(0, overflow_text, "", 10))
    result = _invoke(tmp_path, services)
    assert result.output == overflow_text
    assert result.data is not None
    assert result.data["truncated"] is True
    expected_sha = hashlib.sha256(overflow_text.encode("utf-8")).hexdigest()
    assert result.data["artifact"] == {"sha256": expected_sha, "bytes": len(overflow_text.encode("utf-8")), "media_type": "text/plain; charset=utf-8"}
    assert result.data["artifact_uri"] == f"artifact://{expected_sha}"
    assert expected_sha in services.artifacts


class ProcessServices(PluginServices):
    def __init__(self, result: ProcessResult) -> None:
        self.result = result
        self.events: list[Mapping[str, object]] = []
        self.artifacts: dict[str, bytes] = {}

    def run_process(self, argv: Sequence[str], **kwargs: object) -> ProcessResult:
        return self.result

    def run_query_process(self, argv: Sequence[str], **kwargs: object) -> ProcessResult:
        return self.run_process(argv, **kwargs)

    def stage_event(self, event_type: str, payload: Mapping[str, object]) -> None:
        assert event_type == "shell_executed"
        self.events.append(payload)

    def spill_artifact(self, data: bytes | str, *, media_type: str = "text/plain; charset=utf-8") -> ArtifactReference:
        raw = data.encode("utf-8") if isinstance(data, str) else data
        sha = hashlib.sha256(raw).hexdigest()
        self.artifacts[sha] = raw
        return ArtifactReference(sha256=sha, bytes=len(raw), media_type=media_type)


def _invoke(tmp_path: Path, services: ProcessServices, command: str = "echo hi"):
    context = ToolContext(
        workspace=tmp_path,
        session_id="session",
        run_id="run",
        invocation_id="invocation",
        revision_id="revision",
        actor="user",
        logical_turn="turn",
    )
    return ShellCommand().invoke(CommandInvocation(context=context, name="shell", arguments={"command": command}), services)


@pytest.mark.parametrize("flag", ["timed_out", "cancelled"])
def test_stopped_process_preserves_unknown_exit_and_partial_output(tmp_path: Path, flag: str) -> None:
    services = ProcessServices(ProcessResult(None, "partial\n", "", 25, timed_out=flag == "timed_out", cancelled=flag == "cancelled"))
    result = _invoke(tmp_path, services)
    assert result.status == "ok"
    assert result.data is not None
    assert result.data["exit_code"] is None
    assert result.data["status"] == flag
    assert result.data[flag] is True
    assert result.output == f"command {flag}\npartial\n"
    assert "Exit code: unknown" in str(result.data["record"])
    assert services.events[0]["exit_code"] is None


def test_capture_truncation_remains_visible_even_with_short_preview(tmp_path: Path) -> None:
    services = ProcessServices(ProcessResult(0, "retained", "", 0, truncated=True))
    result = _invoke(tmp_path, services)
    assert result.data is not None
    assert result.data["truncated"] is True
    assert result.output == "retained\n[process output exceeded capture limit]"


@pytest.mark.parametrize("command", ["", " ", "!", "!  "])
def test_empty_escape_never_starts_a_process(tmp_path: Path, command: str) -> None:
    services = ProcessServices(ProcessResult(0, "unexpected process output", "", 0))
    result = _invoke(tmp_path, services, command)
    assert result.status == "error"
    assert services.events == []


def test_login_shell_preserves_the_configured_executable_path(monkeypatch) -> None:
    monkeypatch.setenv("SHELL", "/custom/bin/bash")
    monkeypatch.setattr(user_shell_module.shutil, "which", lambda name: name if name == "/custom/bin/bash" else "/bin/bash")
    assert login_shell_args("pwd") == ["/custom/bin/bash", "-lc", "pwd"]
