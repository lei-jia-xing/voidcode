"""End-to-end integration test for the User shell plugin on the real core stack.

Proves the ``!`` contract on the new seams: an ordinary EXECUTE command (declarative client
syntax) that works normally, is denied under a frozen read-only session with no workspace
mutation, and records exactly one durable event with the exit code and truncation facts.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from voidcode.core.bootstrap import open_core
from voidcode.core.engine import Core

_SHELL_PLUGIN_ROOT = Path(__file__).resolve().parents[2] / "src" / "voidcode" / "plugins" / "user_shell"
_CREATED_CORES: list[Core] = []


@pytest.fixture(autouse=True)
def _close_created_cores() -> Iterator[None]:
    yield
    while _CREATED_CORES:
        _CREATED_CORES.pop().close()


def _core(tmp_path: Path, *, read_only: bool, approval_mode: str) -> Core:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    config = {
        "schema_version": 1,
        "policy": {"approval_mode": approval_mode, "read_only": read_only},
        "plugins": {
            # The bundled files plugin is not under test here and is rewritten by another P4 lane.
            "files": {"enabled": False},
            "user_shell": {"path": str(_SHELL_PLUGIN_ROOT)},
        },
    }
    (workspace / "voidcode.json").write_text(json.dumps(config), encoding="utf-8")
    core = open_core(workspace, state_dir=state_dir)
    _CREATED_CORES.append(core)
    return core


def _invoke(core: Core, session_id: str, name: str, arguments: dict[str, object]):
    return core.invoke_command(session_id, name, arguments, expected_revision=core.command_catalog().revision_id)


def test_shell_escape_runs_and_records_the_fact(tmp_path: Path) -> None:
    core = _core(tmp_path, read_only=False, approval_mode="ask")
    session_id = core.create_session().session_id

    descriptor = {command.name: command for command in core.command_catalog().commands}["shell"]
    assert descriptor.client_syntax == "!"
    assert "execute" in {effect.value for effect in descriptor.effects}

    result = _invoke(core, session_id, "shell", {"command": "echo hello-from-shell"})
    assert result.status == "ok"
    assert result.output is not None
    assert "hello-from-shell" in result.output
    assert result.data is not None
    assert result.data["exit_code"] == 0
    assert result.data["status"] == "completed"
    assert result.data["timed_out"] is False
    assert "<user_shell_command>" in str(result.data["record"])

    events = core.events_after(session_id)
    shell_events = [event for event in events if event.event_type == "plugin.user_shell.shell_executed"]
    assert len(shell_events) == 1
    assert shell_events[0].payload["command"] == "echo hello-from-shell"
    assert shell_events[0].payload["exit_code"] == 0


def test_shell_escape_is_denied_read_only_with_no_workspace_mutation(tmp_path: Path) -> None:
    core = _core(tmp_path, read_only=True, approval_mode="ask")
    session_id = core.create_session().session_id
    target = tmp_path / "workspace" / "shell-should-not-write.txt"

    result = _invoke(core, session_id, "shell", {"command": f"echo mutated > {target.name}"})
    assert result.status == "error"
    assert "read-only" in (result.error or "")
    assert not target.exists()
    assert not [event for event in core.events_after(session_id) if event.event_type == "plugin.user_shell.shell_executed"]


def test_shell_failure_records_a_nonzero_exit_code(tmp_path: Path) -> None:
    core = _core(tmp_path, read_only=False, approval_mode="write")
    session_id = core.create_session().session_id
    result = _invoke(core, session_id, "shell", {"command": "exit 7"})
    assert result.status == "ok"
    assert result.data is not None
    assert result.data["exit_code"] == 7
    assert result.data["status"] == "failed"
    events = [event for event in core.events_after(session_id) if event.event_type == "plugin.user_shell.shell_executed"]
    assert len(events) == 1
    assert events[0].payload["exit_code"] == 7


def test_shell_requires_a_command_argument(tmp_path: Path) -> None:
    core = _core(tmp_path, read_only=False, approval_mode="ask")
    session_id = core.create_session().session_id
    empty = _invoke(core, session_id, "shell", {"command": "   "})
    assert empty.status == "error"
    assert empty.error == "shell requires a non-empty command"
    # The failure is durable as the command outcome, but nothing was executed.
    assert not [event for event in core.events_after(session_id) if event.event_type == "plugin.user_shell.shell_executed"]
