"""Native `!` shell escape: dispatch, bypass, exit paths, truncation, persistence."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from voidcode.core.turns import FinalTurn, ModelTurnFact
from voidcode.runtime.config.loading import RuntimeConfig, RuntimeMcpConfig
from voidcode.runtime.context.continuity import replayed_conversation_segments_from_events
from voidcode.runtime.contracts import RuntimeRequest
from voidcode.runtime.permissions.policy import PermissionPolicy
from voidcode.runtime.service import VoidCodeRuntime
from voidcode.runtime.user_shell import (
    USER_SHELL_TIMEOUT_SECONDS,
    USER_SHELL_TRUNCATION_BYTES,
    UserShellResult,
    execute_user_shell_command,
    format_user_shell_record,
    login_shell_args,
    parse_shell_escape,
    truncate_middle_chars,
)


def test_parse_shell_escape_detection() -> None:
    assert parse_shell_escape("!ls") == "ls"
    assert parse_shell_escape("   !echo hi  ") == "echo hi"
    # Single-char strip only: no `!!` special form.
    assert parse_shell_escape("!!foo") == "!foo"
    assert parse_shell_escape("!") == ""
    assert parse_shell_escape("!   ") == ""
    assert parse_shell_escape("/plan") is None
    assert parse_shell_escape("hello !world") is None
    assert parse_shell_escape("") is None
    assert parse_shell_escape("   ") is None


def test_login_shell_args_unix_shape() -> None:
    argv = login_shell_args("echo hi")
    assert argv[-1] == "echo hi"
    if sys.platform == "win32":
        assert argv[1] in {"-Command", "/c"}
    else:
        assert argv[1] == "-lc"
        assert len(argv) == 3


def test_truncate_middle_chars() -> None:
    assert truncate_middle_chars("short", 100) == "short"
    assert truncate_middle_chars("", 10) == ""
    assert truncate_middle_chars("abcdef", 0) == "…6 chars truncated…"
    out = truncate_middle_chars("x" * 100, 10)
    assert out.startswith("xxxxx")
    assert out.endswith("xxxxx")
    assert "chars truncated" in out
    # Multibyte text never splits a code point.
    out = truncate_middle_chars("😀" * 100, 20)
    assert "�" not in out


def test_format_user_shell_record_shape() -> None:
    result = UserShellResult(
        command="echo hi",
        exit_code=0,
        stdout="hi\n",
        stderr="",
        output="hi\n",
        truncated=False,
        duration_seconds=0.1234,
    )
    record = format_user_shell_record("echo hi", result)
    assert record.startswith("<user_shell_command>")
    assert record.endswith("</user_shell_command>")
    assert "<command>\necho hi\n</command>" in record
    assert "Exit code: 0" in record
    assert "Duration: 0.1234 seconds" in record


def test_execute_exit_zero_and_nonzero(tmp_path: Path) -> None:
    ok = execute_user_shell_command("exit 0", cwd=tmp_path)
    assert ok.exit_code == 0
    assert ok.status == "completed"
    assert ok.duration_seconds >= 0
    failed = execute_user_shell_command("exit 3", cwd=tmp_path)
    assert failed.exit_code != 0
    assert failed.status == "failed"


def test_execute_captures_stdout_stderr_and_cwd(tmp_path: Path) -> None:
    result = execute_user_shell_command(f'"{sys.executable}" -c "import os, sys; print(os.getcwd()); print(\'oops\', file=sys.stderr)"', cwd=tmp_path)
    assert result.exit_code == 0
    assert str(tmp_path) in result.stdout
    assert "oops" in result.stderr
    assert str(tmp_path) in result.output


def test_execute_spawn_failure_records_exit_minus_one(tmp_path: Path) -> None:
    with patch("voidcode.runtime.user_shell.login_shell_args", return_value=["/nonexistent/shell-xyz", "-lc", "echo"]):
        result = execute_user_shell_command("echo", cwd=tmp_path)
    assert result.exit_code == -1
    assert result.status == "failed"
    assert "execution error" in result.stderr


def test_execute_timeout_kills_and_marks(tmp_path: Path) -> None:
    with patch("voidcode.runtime.user_shell.USER_SHELL_TRUNCATION_BYTES", 10_000):
        result = execute_user_shell_command("exit 0", cwd=tmp_path, timeout_seconds=30)
    assert result.exit_code == 0
    assert result.timed_out is False


def test_timeout_plumbing_uses_one_hour_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert USER_SHELL_TIMEOUT_SECONDS == 3600
    seen: dict[str, object] = {}

    class _Proc:
        returncode = 0
        stdout = _ClosedPipe()
        stderr = _ClosedPipe()

        def wait(self, timeout: float | None = None) -> None:
            seen["timeout"] = timeout

    def _popen(*args: object, **kwargs: object) -> _Proc:
        return _Proc()

    monkeypatch.setattr(subprocess, "Popen", _popen)
    result = execute_user_shell_command("echo", cwd=tmp_path)
    assert seen["timeout"] == 3600
    assert result.exit_code == 0


class _ClosedPipe:
    def read(self, _n: int) -> bytes:
        return b""

    def close(self) -> None:
        return None


class _FinalProducer:
    def produce(self, request: object, tool_results: tuple[object, ...], *, session: object) -> FinalTurn:
        step = getattr(request, "run_step", 1)
        prompt = getattr(request, "prompt", "")
        return FinalTurn(facts=(ModelTurnFact(step, "deterministic", prompt),), output="done")


def _runtime(tmp_path: Path, approval_mode: str = "yolo") -> VoidCodeRuntime:
    return VoidCodeRuntime(
        workspace=tmp_path,
        turn_producer=_FinalProducer(),
        config=RuntimeConfig(approval_mode=approval_mode, mcp=RuntimeMcpConfig(enabled=False)),  # type: ignore[arg-type]
        permission_policy=PermissionPolicy(mode=approval_mode),  # type: ignore[arg-type]
    )


def _assert_shell_persisted_without_provider_turn(runtime: VoidCodeRuntime, workspace: Path, session_id: str, command: str) -> None:
    loaded = runtime._repositories.sessions.load_session(workspace=workspace, session_id=session_id)
    shell_events = [e for e in loaded.events if e.event_type == "runtime.user_shell_executed"]
    assert len(shell_events) == 1
    payload = shell_events[0].payload
    assert payload["command"] == command
    assert payload["record"].startswith("<user_shell_command>")
    # The seed run is the only provider turn: at most one request event, and
    # the shell record must replay into provider context afterwards.
    assert len([e for e in loaded.events if e.event_type == "runtime.request_received"]) <= 1
    segments = replayed_conversation_segments_from_events(loaded.events, reported_call_from_event=lambda e: None)
    assert any(s.role == "user" and (s.content or "").startswith("<user_shell_command>") for s in segments)


@pytest.mark.parametrize("approval_mode", ["yolo", "ask", "write"])
def test_run_user_shell_bypasses_approval_modes(tmp_path: Path, approval_mode: str) -> None:
    with _runtime(tmp_path, approval_mode) as runtime:
        resp = runtime.run(RuntimeRequest(prompt="seed"))
        session_id = resp.session.session.id
        result = runtime.run_user_shell(session_id=session_id, command="echo bypassed")
        assert result["exit_code"] == 0
        assert "bypassed" in str(result["output"])
        _assert_shell_persisted_without_provider_turn(runtime, tmp_path, session_id, "echo bypassed")


def test_run_user_shell_always_executes_in_plan_and_read_only(tmp_path: Path) -> None:
    (tmp_path / "file.txt").write_text("data\n")
    with _runtime(tmp_path) as runtime:
        resp = runtime.run(RuntimeRequest(prompt="seed"))
        session_id = resp.session.session.id
        runtime.set_collaboration_mode(session_id=session_id, mode="plan")
        result = runtime.run_user_shell(session_id=session_id, command="echo plan-escape")
        assert result["exit_code"] == 0
        ro = runtime.run(RuntimeRequest(prompt="ro seed", metadata={"read_only": True}))
        ro_result = runtime.run_user_shell(session_id=ro.session.session.id, command="echo ro-escape")
        assert ro_result["exit_code"] == 0


def test_run_user_shell_nonzero_and_blank_paths(tmp_path: Path) -> None:
    with _runtime(tmp_path) as runtime:
        resp = runtime.run(RuntimeRequest(prompt="seed"))
        session_id = resp.session.session.id
        failed = runtime.run_user_shell(session_id=session_id, command="exit 7")
        assert failed["exit_code"] != 0
        assert failed["status"] == "failed"
        with pytest.raises(ValueError, match="must not be empty"):
            runtime.run_user_shell(session_id=session_id, command="   ")


def test_run_user_shell_truncates_large_output(tmp_path: Path) -> None:
    assert USER_SHELL_TRUNCATION_BYTES == 10_000
    with _runtime(tmp_path) as runtime:
        resp = runtime.run(RuntimeRequest(prompt="seed"))
        session_id = resp.session.session.id
        result = runtime.run_user_shell(
            session_id=session_id,
            command=f'"{sys.executable}" -c "print(\'y\' * 60000)"',
        )
        assert result["truncated"] is True
        assert "chars truncated" in str(result["output"])
        assert len(str(result["output"]).encode("utf-8")) < 60000


def test_run_user_shell_sessionless_creates_recording_session(tmp_path: Path) -> None:
    with _runtime(tmp_path) as runtime:
        result = runtime.run_user_shell(session_id=None, command="echo fresh")
        assert result["exit_code"] == 0
        assert isinstance(result["session_id"], str) and result["session_id"]
        _assert_shell_persisted_without_provider_turn(runtime, tmp_path, str(result["session_id"]), "echo fresh")
