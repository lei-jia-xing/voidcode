"""CLI contract tests for ``voidcode tasks``.

Covers the gaps left by ``test_cli_delegated_parity.py``: the real-process task
listing payload, the unknown-task error path for every lifecycle subcommand, and
the copyable ``next_steps`` guidance contract for a task awaiting a decision.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from voidcode.cli import app
from voidcode.cli_support import EXIT_RUNTIME_ERROR, EXIT_SUCCESS

from ._cli_harness import StubRuntime, cli_boundary, deterministic_config, run_cli


class _TaskStateRuntime(StubRuntime):
    """Stub runtime that serves one fixed background-task state."""

    def __init__(self, state: SimpleNamespace) -> None:
        super().__init__()
        self._state = state

    def load_background_task(self, task_id: str) -> Any:
        return self._state


def _task_state(**overrides: object) -> SimpleNamespace:
    fields: dict[str, object] = {
        "task": SimpleNamespace(id="task-1"),
        "status": "waiting",
        "parent_session_id": "leader-session",
        "request": SimpleNamespace(session_id="requested-child"),
        "child_session_id": "child-session",
        "approval_request_id": "approval-1",
        "question_request_id": None,
        "result_available": False,
        "keep_alive": False,
        "steer_prompt": None,
        "cancellation_cause": None,
        "error": None,
        "routing_identity": None,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


# ---------------------------------------------------------------------------
# tasks list
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("parent_session", [None, "leader-session"])
def test_tasks_list_json_reports_scope_and_empty_task_set(tmp_path: Path, parent_session: str | None) -> None:
    args = ["tasks", "list", "--workspace", str(tmp_path), "--json"]
    if parent_session is not None:
        args += ["--parent-session", parent_session]

    result = run_cli(*args, cwd=tmp_path)

    payload = json.loads(result.stdout)
    assert result.returncode == EXIT_SUCCESS
    assert result.stderr == ""
    assert payload["workspace"] == str(tmp_path)
    assert payload["parent_session_id"] == parent_session
    assert payload["tasks"] == []


# ---------------------------------------------------------------------------
# unknown task id
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("subcommand", ["status", "output", "cancel", "retry"])
def test_tasks_unknown_task_is_clean_runtime_error(tmp_path: Path, subcommand: str) -> None:
    result = run_cli("tasks", subcommand, "missing-task", "--workspace", str(tmp_path), cwd=tmp_path)

    assert result.returncode == EXIT_RUNTIME_ERROR
    assert result.stdout == ""
    assert result.stderr.startswith("error:")
    assert "unknown background task" in result.stderr
    assert "missing-task" in result.stderr
    assert "Traceback" not in result.stderr


# ---------------------------------------------------------------------------
# next_steps guidance contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "expected_blocked", "expected_commands"),
    [
        (
            _task_state(status="waiting", approval_request_id="approval-1", question_request_id=None),
            True,
            [
                "voidcode sessions resume child-session",
                "--approval-request-id approval-1 --approval-decision allow",
                "voidcode tasks cancel task-1",
            ],
        ),
        (
            _task_state(status="waiting", approval_request_id=None, question_request_id="question-1"),
            False,
            [
                "voidcode sessions answer child-session",
                "--question-request-id question-1",
                "voidcode sessions debug child-session",
                "voidcode tasks cancel task-1",
            ],
        ),
        (
            _task_state(status="running", approval_request_id=None, question_request_id=None),
            False,
            [
                "voidcode tasks status task-1",
                "voidcode tasks output task-1",
                "voidcode tasks cancel task-1",
            ],
        ),
    ],
)
def test_tasks_blocked_on_decision_next_steps_are_copyable_commands(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    state: SimpleNamespace,
    expected_blocked: bool,
    expected_commands: list[str],
) -> None:
    runtime = _TaskStateRuntime(state)

    with cli_boundary(config=deterministic_config(), runtime=runtime):
        result = app.main(["tasks", "status", "task-1", "--workspace", str(tmp_path), "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert result == EXIT_SUCCESS
    assert payload["workspace"] == str(tmp_path)
    assert payload["task"]["task_id"] == "task-1"
    assert payload["task"]["child_session_id"] == "child-session"
    assert payload["task"]["approval_blocked"] is expected_blocked

    steps = payload["task"]["next_steps"]
    assert steps
    # Every step is a runnable CLI command scoped to this workspace, not prose.
    assert all("voidcode " in step for step in steps)
    assert all(str(tmp_path) in step for step in steps)
    for expected in expected_commands:
        assert any(expected in step for step in steps), (expected, steps)
