"""End-to-end ``!`` contract: bounded artifacts, explicit failures, and truthful cancellation."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from pathlib import Path

from tests.fixtures.core_factory import register_core
from voidcode.bootstrap import open_core
from voidcode.core.engine import Core
from voidcode.plugins.user_shell.user_shell import MAX_SHELL_EVENT_BYTES, PREVIEW_BYTES
from voidcode.sdk.json_values import json_wire_object

_SHELL_PLUGIN_ROOT = Path(__file__).resolve().parents[2] / "src" / "voidcode" / "plugins" / "user_shell"


def _write_config(tmp_path: Path) -> tuple[Path, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    config = {
        "schema_version": 1,
        "policy": {"approval_mode": "ask", "read_only": False},
        "plugins": {
            "files": {"enabled": False},
            "user_shell": {"path": str(_SHELL_PLUGIN_ROOT)},
        },
    }
    (workspace / "voidcode.json").write_text(json.dumps(config), encoding="utf-8")
    return workspace, state_dir


def _core(tmp_path: Path, *, read_only: bool = False, approval_mode: str = "ask") -> Core:
    workspace, state_dir = _write_config(tmp_path)
    config = json.loads((workspace / "voidcode.json").read_text(encoding="utf-8"))
    config["policy"] = {"approval_mode": approval_mode, "read_only": read_only}
    (workspace / "voidcode.json").write_text(json.dumps(config), encoding="utf-8")
    core = open_core(workspace, state_dir=state_dir)
    register_core(core)
    return core


def _invoke(core: Core, session_id: str, name: str, arguments: dict[str, object]):
    return core.invoke_command(session_id, name, arguments, expected_revision=core.command_catalog().revision_id)


def _shell_events(core: Core, session_id: str):
    return [event for event in core.events_after(session_id) if event.event_type == "plugin.user_shell.shell_executed"]


def _command_events(core: Core, session_id: str):
    return [event for event in core.events_after(session_id) if event.event_type == "core.command_completed"]


def _artifact_text(core: Core, payload: Mapping[str, object], key: str) -> str:
    reference = payload.get(key)
    assert isinstance(reference, Mapping), reference
    page = core.read_artifact(str(reference["sha256"]))
    return page.data.decode("utf-8")


def test_shell_escape_runs_and_records_one_compact_fact(tmp_path: Path) -> None:
    core = _core(tmp_path)
    session_id = core.create_session().session_id

    descriptor = {command.name: command for command in core.command_catalog().commands}["shell"]
    assert descriptor.client_syntax == "!"
    assert "execute" in {effect.value for effect in descriptor.effects}

    result = _invoke(core, session_id, "shell", {"command": "echo hello-from-shell"})
    assert result.status == "ok"
    assert result.output is not None and "hello-from-shell" in result.output
    assert result.data is not None
    assert result.data["exit_code"] == 0
    assert result.data["status"] == "completed"
    assert result.data["capture_truncated"] is False
    assert result.data["preview_truncated"] is False
    assert "record" not in result.data
    assert "approval" not in {event.event_type for event in core.events_after(session_id)}

    shell_events = _shell_events(core, session_id)
    assert len(shell_events) == 1
    payload = shell_events[0].payload
    assert payload["command"] == "echo hello-from-shell"
    assert payload["exit_code"] == 0
    assert payload["status"] == "completed"
    assert "stdout" not in payload and "stderr" not in payload
    assert "record" not in payload and "preview" not in payload
    assert payload["stdout_artifact"]["bytes"] == len("hello-from-shell\n")
    assert _artifact_text(core, payload, "stdout_artifact") == "hello-from-shell\n"


def test_shell_escape_is_denied_read_only_with_no_workspace_mutation(tmp_path: Path) -> None:
    core = _core(tmp_path, read_only=True)
    session_id = core.create_session().session_id
    target = tmp_path / "workspace" / "shell-should-not-write.txt"

    result = _invoke(core, session_id, "shell", {"command": f"echo mutated > {target.name}"})
    assert result.status == "error"
    assert "read-only" in (result.error or "")
    assert not target.exists()
    assert not _shell_events(core, session_id)


def test_shell_nonzero_exit_is_a_command_error_and_records_the_fact(tmp_path: Path) -> None:
    core = _core(tmp_path, approval_mode="write")
    session_id = core.create_session().session_id
    result = _invoke(core, session_id, "shell", {"command": "printf 'oops\\n' >&2; exit 7"})

    assert result.status == "error"
    assert result.error is not None and "exit" in result.error and "7" in result.error
    assert result.data is not None
    assert result.data["error"] == "nonzero_exit"
    assert result.data["status"] == "failed"
    assert result.data["exit_code"] == 7
    assert result.output == "oops\n"
    shell_events = _shell_events(core, session_id)
    assert len(shell_events) == 1
    assert shell_events[0].payload["error"] == "nonzero_exit"
    completed = _command_events(core, session_id)[-1].payload["result"]
    assert completed["status"] == "error"
    assert completed["data"]["error"] == "nonzero_exit"


def test_shell_timeout_is_a_command_error_with_captured_prefix(tmp_path: Path) -> None:
    core = _core(tmp_path)
    session_id = core.create_session().session_id
    result = _invoke(core, session_id, "shell", {"command": "printf 'before\\n'; sleep 5", "timeout": 1})

    assert result.status == "error"
    assert result.error is not None and "timed out" in result.error
    assert result.data is not None
    assert result.data["error"] == "timed_out"
    assert result.data["timed_out"] is True
    assert result.data["status"] == "timed_out"
    assert result.output is not None and "before" in result.output
    assert result.data["stdout_artifact"] is not None
    assert _artifact_text(core, result.data, "stdout_artifact") == "before\n"
    assert _shell_events(core, session_id)[-1].payload["timed_out"] is True


def test_shell_requires_a_command_argument(tmp_path: Path) -> None:
    core = _core(tmp_path)
    session_id = core.create_session().session_id
    empty = _invoke(core, session_id, "shell", {"command": "   "})
    assert empty.status == "error"
    assert empty.data is not None and empty.data["error"] == "empty_command"
    assert not _shell_events(core, session_id)


def test_large_output_is_spilled_once_and_preview_is_bounded(tmp_path: Path) -> None:
    core = _core(tmp_path)
    session_id = core.create_session().session_id
    stdout = "a" * 60000
    result = _invoke(core, session_id, "shell", {"command": "head -c 60000 /dev/zero | tr '\\0' a"})

    assert result.status == "ok"
    assert result.data is not None
    assert result.data["preview_truncated"] is True
    assert result.data["capture_truncated"] is False
    assert result.output is not None
    assert len(result.output.encode("utf-8")) <= PREVIEW_BYTES + 1024
    assert _artifact_text(core, result.data, "stdout_artifact") == stdout
    assert result.data["stdout_artifact"]["bytes"] == len(stdout)

    shell_payload = _shell_events(core, session_id)[-1].payload
    assert "stdout" not in shell_payload and "stderr" not in shell_payload
    assert "record" not in shell_payload
    assert shell_payload["stdout_artifact"]["bytes"] == len(stdout)
    assert len(json.dumps(json_wire_object(shell_payload), ensure_ascii=False, sort_keys=True).encode("utf-8")) <= MAX_SHELL_EVENT_BYTES

    completed = _command_events(core, session_id)[-1].payload["result"]
    assert completed["output"] is not None
    assert len(completed["output"].encode("utf-8")) <= PREVIEW_BYTES + 1024
    assert completed["data"]["stdout_artifact"]["bytes"] == len(stdout)


def test_cancelled_command_preserves_bounded_partial_preview_and_fact(tmp_path: Path) -> None:
    core = _core(tmp_path)
    session_id = core.create_session().session_id
    marker = tmp_path / "workspace" / "cancelled-started"
    command = f"printf 'partial\\n'; touch {shlex.quote(str(marker))}; sleep 30"
    results = []

    worker = threading.Thread(target=lambda: results.append(_invoke(core, session_id, "shell", {"command": command})))
    worker.start()
    deadline = time.monotonic() + 10
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert marker.exists()
    assert core.cancel_session(session_id) is True
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert len(results) == 1
    result = results[0]

    assert result.status == "error"
    assert result.error is not None and "cancelled" in result.error
    assert result.data is not None
    assert result.data["error"] == "cancelled"
    assert result.data["status"] == "cancelled"
    assert result.data["cancelled"] is True
    assert result.data["stdout_artifact"] is None
    assert result.output is not None and result.output.startswith("command cancelled\npartial")
    shell_events = _shell_events(core, session_id)
    assert len(shell_events) == 1
    assert shell_events[0].payload["cancelled"] is True
    assert "stdout" not in shell_events[0].payload
    completed = _command_events(core, session_id)[-1].payload["result"]
    assert completed["status"] == "error"
    assert completed["data"]["error"] == "cancelled"
    assert "partial" in completed["output"]


def test_cli_reports_nonzero_exit_and_timeout_as_failure(tmp_path: Path) -> None:
    if os.name == "nt":
        raise RuntimeError("POSIX shell command expected")
    workspace, state_dir = _write_config(tmp_path)
    root_args = [sys.executable, "-m", "voidcode", "--workspace", str(workspace), "--state-dir", str(state_dir)]
    created = subprocess.run([*root_args, "create-session", "--session-id", "cli-shell"], capture_output=True, text=True, timeout=60)
    assert created.returncode == 0, created.stdout + created.stderr
    catalog = subprocess.run([*root_args, "catalog"], capture_output=True, text=True, timeout=60)
    assert catalog.returncode == 0, catalog.stdout + catalog.stderr
    revision = json.loads(catalog.stdout)["revision_id"]

    for arguments, code in (({"command": "exit 7"}, "nonzero_exit"), ({"command": "sleep 2", "timeout": 1}, "timed_out")):
        invoked = subprocess.run(
            [
                *root_args,
                "invoke",
                "cli-shell",
                "shell",
                "--arguments",
                json.dumps(arguments),
                "--expected-revision",
                revision,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert invoked.returncode == 1, invoked.stdout + invoked.stderr
        payload = json.loads(invoked.stdout)
        assert payload["status"] == "error"
        assert payload["data"]["error"] == code
