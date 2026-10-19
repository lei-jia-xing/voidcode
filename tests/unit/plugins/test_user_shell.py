"""Unit tests for the user_shell plugin, including real subprocess execution."""

from __future__ import annotations

from pathlib import Path

from voidcode.plugins.user_shell import (
    USER_SHELL_HELP_HINT,
    USER_SHELL_HELP_TITLE,
    USER_SHELL_TIMEOUT_SECONDS,
    USER_SHELL_TRUNCATION_BYTES,
    UserShellResult,
    execute_user_shell_command,
    format_user_shell_record,
    login_shell_args,
    parse_shell_escape,
    truncate_middle_chars,
)
from voidcode.plugins.user_shell import user_shell as user_shell_module


def test_constants_match_upstream() -> None:
    assert USER_SHELL_TIMEOUT_SECONDS == 3600
    assert USER_SHELL_TRUNCATION_BYTES == 10_000
    assert USER_SHELL_HELP_TITLE == "Prefix a command with ! to run it locally"
    assert USER_SHELL_HELP_HINT == "Example: !ls"


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


def test_truncate_middle_chars_short_text_unchanged() -> None:
    assert truncate_middle_chars("short", 10) == "short"


def test_truncate_middle_chars_cuts_middle_with_exact_marker() -> None:
    text = "a" * 100 + "b" * 100

    result = truncate_middle_chars(text, 40)

    assert result == "a" * 20 + "…160 chars truncated…" + "b" * 20


def test_truncate_middle_chars_zero_budget() -> None:
    assert truncate_middle_chars("abc", 0) == "…3 chars truncated…"
    assert truncate_middle_chars("", 0) == ""


def test_format_user_shell_record_exact_xml() -> None:
    result = UserShellResult(
        command="echo hi",
        exit_code=0,
        stdout="hi\n",
        stderr="",
        output="hi\n",
        truncated=False,
        duration_seconds=0.0123,
    )

    assert format_user_shell_record("echo hi", result) == (
        "<user_shell_command>\n"
        "\n<command>\n"
        "echo hi\n"
        "</command>\n<result>\n"
        "Exit code: 0\n"
        "Duration: 0.0123 seconds\n"
        "Output:\n"
        "hi\n"
        "\n</result>\n"
        "</user_shell_command>"
    )


def test_format_user_shell_record_includes_exit_and_duration() -> None:
    result = UserShellResult(
        command="false",
        exit_code=3,
        stdout="",
        stderr="boom\n",
        output="boom\n",
        truncated=False,
        duration_seconds=1.5,
    )

    record = format_user_shell_record("false", result)

    assert "Exit code: 3" in record
    assert "Duration: 1.5000 seconds" in record
    assert "Output:\nboom\n\n</result>" in record
    assert result.status == "failed"


def test_execute_echo_success(tmp_path: Path) -> None:
    result = execute_user_shell_command("echo hi", cwd=tmp_path)

    assert result.exit_code == 0
    assert result.stdout == "hi\n"
    assert result.stderr == ""
    assert result.output == "hi\n"
    assert result.truncated is False
    assert result.timed_out is False
    assert result.status == "completed"
    assert result.duration_seconds >= 0.0

    record = format_user_shell_record("echo hi", result)
    assert record.startswith("<user_shell_command>\n\n<command>\necho hi\n</command>\n<result>\n")
    assert "Exit code: 0" in record
    assert f"Duration: {result.duration_seconds:.4f} seconds" in record
    assert "Output:\nhi\n\n</result>\n</user_shell_command>" in record


def test_execute_captures_stderr_and_exit_code(tmp_path: Path) -> None:
    result = execute_user_shell_command("echo oops >&2; exit 7", cwd=tmp_path)

    assert result.exit_code == 7
    assert result.stdout == ""
    assert result.stderr == "oops\n"
    assert result.output == "oops\n"
    assert result.status == "failed"


def test_execute_middle_truncates_long_output(tmp_path: Path) -> None:
    command = "head -c 20000 /dev/zero | tr '\\0' a"

    result = execute_user_shell_command(command, cwd=tmp_path)

    assert result.exit_code == 0
    assert result.truncated is True
    assert result.stdout == "a" * 20000
    assert result.output == "a" * 5000 + "…10000 chars truncated…" + "a" * 5000


def test_execute_timeout_returns_124(tmp_path: Path) -> None:
    result = execute_user_shell_command("sleep 5", cwd=tmp_path, timeout_seconds=1)

    assert result.timed_out is True
    assert result.exit_code == 124
    assert result.output.startswith("command timed out after 1000 milliseconds\n")
    assert result.status == "failed"


def test_execute_spawn_failure_returns_minus_one(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        user_shell_module,
        "_default_user_shell",
        lambda: ("/nonexistent/definitely-not-a-shell", "sh"),
    )

    result = execute_user_shell_command("echo hi", cwd=tmp_path)

    assert result.exit_code == -1
    assert result.stdout == ""
    assert result.stderr.startswith("execution error:")
    assert result.output == result.stderr
    assert result.truncated is False
    assert result.timed_out is False
