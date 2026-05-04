"""Executable parity inventory for CLI delegated-lifecycle day-1 surface.

These tests assert that the CLI exposes (or is expected to expose) every
delegated-lifecycle capability that the runtime already supports.  Each test
pins one parity requirement against the runtime truth in ``VoidCodeRuntime``.

Day-1 delegated lifecycle surfaces covered:
- create (via ``task`` tool inside ``run``)
- status / inspect (load background task)
- output (load background task result)
- cancel (cancel background task)
- list (list background tasks by parent session)
- approval resolution (via ``sessions resume``)
- restart / resume (via ``sessions resume`` after process restart)
- parent-visible lifecycle events (emitted during run/resume streams)
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import pytest

from voidcode.runtime.task import BackgroundTaskStatus, is_background_task_terminal
from voidcode.tools import ToolCall

from .._paths import with_src_pythonpath

pytestmark = pytest.mark.usefixtures("_force_deterministic_engine_default")


@pytest.fixture
def _force_deterministic_engine_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOIDCODE_EXECUTION_ENGINE", "deterministic")


def _run_module_cli(
    *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "voidcode", *args],
        capture_output=True,
        text=True,
        check=False,
        env=with_src_pythonpath(env),
    )


def _wait_for_background_task(
    runtime: Any,
    task_id: str,
    *,
    predicate: Any,
    timeout: float = 3.0,
) -> Any:
    deadline = time.monotonic() + timeout
    last_state: Any = None
    while time.monotonic() < deadline:
        last_state = runtime.load_background_task(task_id)
        if predicate(last_state):
            return last_state
        time.sleep(0.01)
    raise AssertionError(
        f"background task {task_id} did not reach expected state; last_status="
        f"{getattr(last_state, 'status', None)!r}"
    )


def _is_terminal_background_task(task: Any) -> bool:
    return is_background_task_terminal(cast(BackgroundTaskStatus, task.status))


def _is_waiting_approval_background_task(task: Any) -> bool:
    return getattr(task, "approval_request_id", None) is not None


class _QuestionThenDoneGraph:
    def step(self, request: Any, tool_results: tuple[object, ...], *, session: Any) -> Any:
        _ = request, session
        if not tool_results:
            return SimpleNamespace(
                tool_call=ToolCall(
                    tool_name="question",
                    arguments={
                        "questions": [
                            {
                                "question": "Which runtime path should we use?",
                                "header": "Runtime path",
                                "options": [{"label": "Reuse existing", "description": ""}],
                                "multiple": False,
                            }
                        ]
                    },
                ),
                output=None,
                events=(),
                is_finished=False,
            )
        return SimpleNamespace(tool_call=None, output="done", events=(), is_finished=True)


# ---------------------------------------------------------------------------
# 1. CLI parser surface: delegated-lifecycle subcommands must exist
# ---------------------------------------------------------------------------


def test_cli_parser_has_sessions_list_subcommand() -> None:
    """``voidcode sessions list`` must exist for session enumeration."""
    cli = importlib.import_module("voidcode.cli")
    parser = cli.build_parser()
    actions = {action.dest for action in parser._subparsers._group_actions[0]._choices_actions}
    assert "sessions" in actions


def test_cli_parser_has_sessions_resume_subcommand() -> None:
    """``voidcode sessions resume`` must exist for approval/resume paths."""
    cli = importlib.import_module("voidcode.cli")
    parser = cli.build_parser()
    sessions_parser = None
    for action in parser._subparsers._group_actions:
        if hasattr(action, "_parser_class") and "sessions" in (action.choices or {}):
            sessions_parser = action.choices["sessions"]
            break
    assert sessions_parser is not None
    sub_actions = {a.dest for a in sessions_parser._subparsers._group_actions[0]._choices_actions}
    assert "resume" in sub_actions


def test_cli_parser_has_sessions_answer_subcommand() -> None:
    """``voidcode sessions answer`` must exist for pending-question waits."""
    cli = importlib.import_module("voidcode.cli")
    parser = cli.build_parser()
    sessions_parser = None
    for action in parser._subparsers._group_actions:
        if hasattr(action, "_parser_class") and "sessions" in (action.choices or {}):
            sessions_parser = action.choices["sessions"]
            break
    assert sessions_parser is not None
    sub_actions = {a.dest for a in sessions_parser._subparsers._group_actions[0]._choices_actions}
    assert "answer" in sub_actions


def test_cli_parser_has_run_subcommand() -> None:
    """``voidcode run`` must exist as the primary delegated-task entry point."""
    cli = importlib.import_module("voidcode.cli")
    parser = cli.build_parser()
    actions = {action.dest for action in parser._subparsers._group_actions[0]._choices_actions}
    assert "run" in actions


def test_cli_parser_has_serve_subcommand() -> None:
    """``voidcode serve`` must exist to start the HTTP transport."""
    cli = importlib.import_module("voidcode.cli")
    parser = cli.build_parser()
    actions = {action.dest for action in parser._subparsers._group_actions[0]._choices_actions}
    assert "serve" in actions


def test_cli_parser_has_continuation_loop_subcommands() -> None:
    """``voidcode loops`` must expose the runtime-owned loop lifecycle surface."""
    cli = importlib.import_module("voidcode.cli")
    parser = cli.build_parser()
    loops_parser = None
    for action in parser._subparsers._group_actions:
        if hasattr(action, "_parser_class") and "loops" in (action.choices or {}):
            loops_parser = action.choices["loops"]
            break
    assert loops_parser is not None
    sub_actions = {a.dest for a in loops_parser._subparsers._group_actions[0]._choices_actions}
    assert {"start", "status", "cancel", "list"}.issubset(sub_actions)


# ---------------------------------------------------------------------------
# 2. CLI run: delegated task creation via task tool inside run
# ---------------------------------------------------------------------------


def test_cli_run_delegates_task_via_task_tool_in_graph(tmp_path: Path) -> None:
    cli = importlib.import_module("voidcode.cli")
    workspace = tmp_path
    config = SimpleNamespace(approval_mode="allow")

    class _StubSession:
        def __init__(self, id_: str, status: str) -> None:
            self.session = SimpleNamespace(id=id_)
            self.status = status
            self.turn = 1
            self.metadata: dict[str, object] = {}

    class _StubEvent:
        def __init__(self, event_type: str, sequence: int, payload: dict[str, object]) -> None:
            self.event_type = event_type
            self.sequence = sequence
            self.source = "runtime"
            self.payload = payload

    class _StubChunk:
        def __init__(
            self,
            kind: str,
            session: _StubSession,
            event: _StubEvent | None = None,
            output: str | None = None,
        ) -> None:
            self.kind = kind
            self.session = session
            self.event = event
            self.output = output

    chunks = (
        _StubChunk(
            kind="event",
            session=_StubSession("run-session", "running"),
            event=_StubEvent("runtime.request_received", 1, {"prompt": "delegate this"}),
        ),
        _StubChunk(
            kind="output",
            session=_StubSession("run-session", "completed"),
            output="delegation started\n",
        ),
    )

    with patch.object(cli, "load_runtime_config", autospec=True, return_value=config):
        with patch.object(cli, "VoidCodeRuntime", autospec=True) as runtime_class:
            runtime_class.return_value.run_stream.return_value = iter(chunks)
            result = cli.main(
                ["run", "delegate this", "--workspace", str(workspace), "--approval-mode", "allow"]
            )

    assert result == 0
    runtime_class.return_value.run_stream.assert_called_once()


# ---------------------------------------------------------------------------
# 3. CLI sessions resume: approval resolution path
# ---------------------------------------------------------------------------


def test_cli_sessions_resume_resolves_approval_allow() -> None:
    """``voidcode sessions resume --approval-request-id X --approval-decision allow``
    must resolve a pending approval and continue the session."""
    cli = importlib.import_module("voidcode.cli")
    workspace = Path("/tmp/demo-workspace")
    response = SimpleNamespace(
        session=SimpleNamespace(
            session=SimpleNamespace(id="waiting-session"),
            status="completed",
            turn=1,
            metadata={"workspace": str(workspace)},
        ),
        events=(
            SimpleNamespace(
                session_id="waiting-session",
                sequence=3,
                event_type="runtime.approval_resolved",
                source="runtime",
                payload={"request_id": "req-1", "decision": "allow"},
            ),
            SimpleNamespace(
                session_id="waiting-session",
                sequence=4,
                event_type="graph.response_ready",
                source="graph",
                payload={},
            ),
        ),
        output="done\n",
    )

    with patch.object(cli, "VoidCodeRuntime", autospec=True) as runtime_class:
        runtime_class.return_value.resume.return_value = response
        result = cli.main(
            [
                "sessions",
                "resume",
                "waiting-session",
                "--workspace",
                str(workspace),
                "--approval-request-id",
                "req-1",
                "--approval-decision",
                "allow",
            ]
        )

    assert result == 0
    runtime_class.return_value.resume.assert_called_once_with(
        "waiting-session",
        approval_request_id="req-1",
        approval_decision="allow",
    )


def test_cli_sessions_resume_resolves_approval_deny() -> None:
    """``voidcode sessions resume --approval-decision deny`` must deny the
    pending approval and mark the session as failed."""
    cli = importlib.import_module("voidcode.cli")
    workspace = Path("/tmp/demo-workspace")
    response = SimpleNamespace(
        session=SimpleNamespace(
            session=SimpleNamespace(id="waiting-session"),
            status="failed",
            turn=1,
            metadata={"workspace": str(workspace)},
        ),
        events=(
            SimpleNamespace(
                session_id="waiting-session",
                sequence=3,
                event_type="runtime.approval_resolved",
                source="runtime",
                payload={"request_id": "req-1", "decision": "deny"},
            ),
            SimpleNamespace(
                session_id="waiting-session",
                sequence=4,
                event_type="runtime.failed",
                source="runtime",
                payload={"error": "permission denied"},
            ),
        ),
        output=None,
    )

    with patch.object(cli, "VoidCodeRuntime", autospec=True) as runtime_class:
        runtime_class.return_value.resume.return_value = response
        result = cli.main(
            [
                "sessions",
                "resume",
                "waiting-session",
                "--workspace",
                str(workspace),
                "--approval-request-id",
                "req-1",
                "--approval-decision",
                "deny",
            ]
        )

    assert result == 0
    assert runtime_class.return_value.resume.call_args.kwargs["approval_decision"] == "deny"


def test_cli_sessions_answer_resolves_pending_question() -> None:
    """``voidcode sessions answer`` must call the runtime-owned question path."""
    cli = importlib.import_module("voidcode.cli")
    workspace = Path("/tmp/demo-workspace")
    response = SimpleNamespace(
        session=SimpleNamespace(
            session=SimpleNamespace(id="question-session"),
            status="completed",
            turn=1,
            metadata={"workspace": str(workspace)},
        ),
        events=(
            SimpleNamespace(
                session_id="question-session",
                sequence=3,
                event_type="runtime.question_answered",
                source="runtime",
                payload={"request_id": "question-1"},
            ),
        ),
        output="answered\n",
    )

    with patch.object(cli, "VoidCodeRuntime", autospec=True) as runtime_class:
        runtime_class.return_value.answer_question.return_value = response
        result = cli.main(
            [
                "sessions",
                "answer",
                "question-session",
                "--workspace",
                str(workspace),
                "--question-request-id",
                "question-1",
                "--response",
                "yes",
            ]
        )

    assert result == 0
    runtime_class.return_value.answer_question.assert_called_once()
    call = runtime_class.return_value.answer_question.call_args
    assert call.args == ("question-session",)
    assert call.kwargs["question_request_id"] == "question-1"
    responses = call.kwargs["responses"]
    assert len(responses) == 1
    assert responses[0].header == "response"
    assert responses[0].answers == ("yes",)


def test_cli_sessions_answer_accepts_json_responses() -> None:
    cli = importlib.import_module("voidcode.cli")
    workspace = Path("/tmp/demo-workspace")
    response = SimpleNamespace(
        session=SimpleNamespace(
            session=SimpleNamespace(id="question-session"),
            status="completed",
            turn=1,
            metadata={"workspace": str(workspace)},
        ),
        events=(),
        output=None,
    )

    with patch.object(cli, "VoidCodeRuntime", autospec=True) as runtime_class:
        runtime_class.return_value.answer_question.return_value = response
        result = cli.main(
            [
                "sessions",
                "answer",
                "question-session",
                "--workspace",
                str(workspace),
                "--question-request-id",
                "question-1",
                "--response-json",
                '[{"header":"Confirm","answers":["yes","ship it"]}]',
            ]
        )

    assert result == 0
    responses = runtime_class.return_value.answer_question.call_args.kwargs["responses"]
    assert responses[0].header == "Confirm"
    assert responses[0].answers == ("yes", "ship it")


def test_cli_sessions_answer_completes_real_question_wait(tmp_path: Path) -> None:
    cli = importlib.import_module("voidcode.cli")
    runtime_module = importlib.import_module("voidcode.runtime")
    config_module = importlib.import_module("voidcode.runtime.config")

    runtime = runtime_module.VoidCodeRuntime(
        workspace=tmp_path,
        graph=_QuestionThenDoneGraph(),
        config=config_module.RuntimeConfig(approval_mode="ask"),
    )
    waiting = runtime.run(
        runtime_module.RuntimeRequest(prompt="ask", session_id="question-session")
    )
    question_request_id = waiting.events[-1].payload["request_id"]
    runtime.__exit__(None, None, None)

    def _runtime_factory(*, workspace: Path) -> Any:
        return runtime_module.VoidCodeRuntime(
            workspace=workspace,
            graph=_QuestionThenDoneGraph(),
            config=config_module.RuntimeConfig(approval_mode="ask"),
        )

    with patch.object(cli, "VoidCodeRuntime", side_effect=_runtime_factory):
        result = cli.main(
            [
                "sessions",
                "answer",
                "question-session",
                "--workspace",
                str(tmp_path),
                "--question-request-id",
                str(question_request_id),
                "--response-json",
                '[{"header":"Runtime path","answers":["Reuse existing"]}]',
            ]
        )

    resumed = runtime_module.VoidCodeRuntime(workspace=tmp_path).resume("question-session")
    assert result == 0
    assert resumed.session.status == "completed"
    assert resumed.output == "done"


# ---------------------------------------------------------------------------
# 4. CLI sessions resume: restart / resume after process restart
# ---------------------------------------------------------------------------


def test_cli_sessions_resume_replays_completed_session_after_restart(tmp_path: Path) -> None:
    """After a process restart, ``voidcode sessions resume`` must replay a
    completed session from the SQLite store without re-executing the graph."""
    env = with_src_pythonpath(os.environ.copy())

    _ = (tmp_path / "sample.txt").write_text("restart replay\n", encoding="utf-8")

    run_result = _run_module_cli(
        "run",
        "read sample.txt",
        "--workspace",
        str(tmp_path),
        "--session-id",
        "restart-session",
        env=env,
    )
    assert run_result.returncode == 0

    resume_result = _run_module_cli(
        "sessions",
        "resume",
        "restart-session",
        "--workspace",
        str(tmp_path),
        env=env,
    )

    assert resume_result.returncode == 0
    assert "RESULT" in resume_result.stdout
    assert "restart replay" in resume_result.stdout


# ---------------------------------------------------------------------------
# 5. CLI sessions list: parent-visible lifecycle events
# ---------------------------------------------------------------------------


def test_cli_sessions_list_shows_delegated_child_sessions(tmp_path: Path) -> None:
    cli = importlib.import_module("voidcode.cli")
    workspace = tmp_path
    runtime_module = importlib.import_module("voidcode.runtime")
    (workspace / "sample.txt").write_text("hello\n", encoding="utf-8")

    runtime = runtime_module.VoidCodeRuntime(workspace=workspace)
    _ = runtime.run(
        runtime_module.RuntimeRequest(prompt="read sample.txt", session_id="leader-session")
    )
    _ = runtime.run(
        runtime_module.RuntimeRequest(
            prompt="read sample.txt",
            parent_session_id="leader-session",
        )
    )

    result = cli.main(["sessions", "list", "--workspace", str(workspace)])

    assert result == 0
    listed = runtime.list_sessions()
    child_ids = {s.session.id for s in listed if s.session.parent_id == "leader-session"}
    assert len(child_ids) >= 1


# ---------------------------------------------------------------------------
# 6. CLI run: inline approval loop (interactive)
# ---------------------------------------------------------------------------


def test_cli_run_inline_approval_loop_emits_events(capsys: Any) -> None:
    """Interactive ``voidcode run`` must emit approval_requested events and
    wait for user input before resuming."""
    cli = importlib.import_module("voidcode.cli")
    config = SimpleNamespace(approval_mode="ask")

    class _StubSession:
        def __init__(self, id_: str, status: str) -> None:
            self.session = SimpleNamespace(id=id_)
            self.status = status
            self.turn = 1
            self.metadata: dict[str, object] = {}

    class _StubEvent:
        def __init__(
            self,
            event_type: str,
            sequence: int,
            payload: dict[str, object],
            source: str = "runtime",
        ) -> None:
            self.event_type = event_type
            self.sequence = sequence
            self.source = source
            self.payload = payload

    class _StubChunk:
        def __init__(
            self,
            kind: str,
            session: _StubSession,
            event: _StubEvent | None = None,
            output: str | None = None,
        ) -> None:
            self.kind = kind
            self.session = session
            self.event = event
            self.output = output

    first_stream = (
        _StubChunk(
            kind="event",
            session=_StubSession("demo-session", "running"),
            event=_StubEvent("runtime.request_received", 1, {"prompt": "write x.txt hi"}),
        ),
        _StubChunk(
            kind="event",
            session=_StubSession("demo-session", "waiting"),
            event=_StubEvent(
                "runtime.approval_requested",
                2,
                {
                    "request_id": "req-1",
                    "tool": "write_file",
                    "target_summary": "x.txt",
                },
            ),
        ),
    )

    resume_stream = (
        _StubChunk(
            kind="event",
            session=_StubSession("demo-session", "running"),
            event=_StubEvent(
                "runtime.approval_resolved",
                3,
                {
                    "request_id": "req-1",
                    "decision": "allow",
                },
            ),
        ),
        _StubChunk(
            kind="output",
            session=_StubSession("demo-session", "completed"),
            output="done\n",
        ),
    )

    class _StubStdin:
        def __init__(self) -> None:
            self._responses = ["y\n"]

        def isatty(self) -> bool:
            return True

        def readline(self) -> str:
            return self._responses.pop(0)

    class _StubStderr:
        def __init__(self) -> None:
            self.writes: list[str] = []

        def isatty(self) -> bool:
            return True

        def write(self, text: str) -> int:
            self.writes.append(text)
            return len(text)

        def flush(self) -> None:
            pass

    with patch.object(cli, "load_runtime_config", autospec=True, return_value=config):
        with patch.object(cli, "VoidCodeRuntime", autospec=True) as runtime_class:
            runtime = runtime_class.return_value
            runtime.run_stream.return_value = iter(first_stream)
            runtime.resume_stream.return_value = iter(resume_stream)
            with patch.object(cli.sys, "stdin", _StubStdin()):
                with patch.object(cli.sys, "stderr", _StubStderr()):
                    result = cli.main(
                        ["run", "write x.txt hi", "--workspace", "/tmp/demo-workspace"]
                    )

    assert result == 0
    captured = capsys.readouterr()
    assert "EVENT runtime.approval_requested" in captured.out
    assert "EVENT runtime.approval_resolved" in captured.out


# ---------------------------------------------------------------------------
# 7. CLI run: non-interactive mode skips approval loop
# ---------------------------------------------------------------------------


def test_cli_run_non_interactive_skips_approval_loop(capsys: Any) -> None:
    """Non-interactive ``voidcode run`` must NOT enter the approval loop and
    must return immediately after the initial stream ends."""
    cli = importlib.import_module("voidcode.cli")
    config = SimpleNamespace(approval_mode="ask")

    class _StubSession:
        def __init__(self, id_: str, status: str) -> None:
            self.session = SimpleNamespace(id=id_)
            self.status = status
            self.turn = 1
            self.metadata: dict[str, object] = {}

    class _StubEvent:
        def __init__(
            self,
            event_type: str,
            sequence: int,
            payload: dict[str, object],
            source: str = "runtime",
        ) -> None:
            self.event_type = event_type
            self.sequence = sequence
            self.source = source
            self.payload = payload

    class _StubChunk:
        def __init__(
            self,
            kind: str,
            session: _StubSession,
            event: _StubEvent | None = None,
            output: str | None = None,
        ) -> None:
            self.kind = kind
            self.session = session
            self.event = event
            self.output = output

    first_stream = (
        _StubChunk(
            kind="event",
            session=_StubSession("demo-session", "waiting"),
            event=_StubEvent(
                "runtime.approval_requested",
                1,
                {
                    "request_id": "req-1",
                    "tool": "write_file",
                    "target_summary": "x.txt",
                },
            ),
        ),
    )

    class _StubStdin:
        def isatty(self) -> bool:
            return False

        def readline(self) -> str:
            raise AssertionError("readline should not be called")

    class _StubStderr:
        def isatty(self) -> bool:
            return False

        def write(self, text: str) -> int:
            return len(text)

        def flush(self) -> None:
            pass

    with patch.object(cli, "load_runtime_config", autospec=True, return_value=config):
        with patch.object(cli, "VoidCodeRuntime", autospec=True) as runtime_class:
            runtime = runtime_class.return_value
            runtime.run_stream.return_value = iter(first_stream)
            with patch.object(cli.sys, "stdin", _StubStdin()):
                with patch.object(cli.sys, "stderr", _StubStderr()):
                    result = cli.main(
                        ["run", "write x.txt hi", "--workspace", "/tmp/demo-workspace"]
                    )

    assert result == 13
    runtime.resume_stream.assert_not_called()
    captured = capsys.readouterr()
    assert captured.out == ""


# ---------------------------------------------------------------------------
# 8. CLI delegated background-task subcommands
# ---------------------------------------------------------------------------


def test_cli_parser_has_tasks_subcommand() -> None:
    cli = importlib.import_module("voidcode.cli")
    parser = cli.build_parser()
    actions = {action.dest for action in parser._subparsers._group_actions[0]._choices_actions}
    assert "tasks" in actions


def test_cli_parser_has_tasks_lifecycle_subcommands() -> None:
    cli = importlib.import_module("voidcode.cli")
    parser = cli.build_parser()
    tasks_parser = None
    for action in parser._subparsers._group_actions:
        if hasattr(action, "choices") and "tasks" in (action.choices or {}):
            tasks_parser = action.choices["tasks"]
            break
    assert tasks_parser is not None
    sub_actions = {a.dest for a in tasks_parser._subparsers._group_actions[0]._choices_actions}
    assert sub_actions == {"status", "output", "cancel", "retry", "list"}


def test_cli_tasks_status_delegates_to_runtime_load_background_task(capsys: Any) -> None:
    cli = importlib.import_module("voidcode.cli")
    workspace = Path("/tmp/demo-workspace")
    task_state = SimpleNamespace(
        task=SimpleNamespace(id="task-1"),
        status="waiting",
        parent_session_id="leader-session",
        request=SimpleNamespace(session_id="child-requested"),
        child_session_id="child-session",
        approval_request_id="approval-1",
        question_request_id=None,
        result_available=False,
        cancellation_cause=None,
        error=None,
        routing_identity=SimpleNamespace(
            mode="background",
            category="quick",
            subagent_type=None,
            description="Investigate",
            command=None,
        ),
    )

    with patch.object(cli, "VoidCodeRuntime", autospec=True) as runtime_class:
        runtime_class.return_value.load_background_task.return_value = task_state
        result = cli.main(["tasks", "status", "task-1", "--workspace", str(workspace)])

    captured = capsys.readouterr()
    assert result == 0
    runtime_class.assert_called_once_with(workspace=workspace)
    runtime_class.return_value.load_background_task.assert_called_once_with("task-1")
    assert "TASK id=task-1 status=waiting" in captured.out
    assert "parent_session_id=leader-session" in captured.out
    assert "requested_child_session_id=child-requested" in captured.out
    assert "child_session_id=child-session" in captured.out
    assert "approval_request_id=approval-1" in captured.out
    assert "delegation_mode=background" in captured.out
    assert "category=quick" in captured.out
    assert "NEXT" in captured.out
    assert "voidcode sessions resume child-session" in captured.out
    assert "--approval-request-id approval-1 --approval-decision allow" in captured.out


def test_cli_tasks_status_supports_json_guidance(capsys: Any) -> None:
    cli = importlib.import_module("voidcode.cli")
    workspace = Path("/tmp/demo-workspace")
    task_state = SimpleNamespace(
        task=SimpleNamespace(id="task-json"),
        status="failed",
        parent_session_id="leader-session",
        request=SimpleNamespace(session_id="requested-child"),
        child_session_id="child-session",
        approval_request_id=None,
        question_request_id=None,
        result_available=True,
        cancellation_cause=None,
        error="provider execution requires a configured provider/model",
        routing_identity=SimpleNamespace(
            mode="background",
            category=None,
            subagent_type="worker",
            description="Run child",
            command=None,
        ),
    )

    with patch.object(cli, "VoidCodeRuntime", autospec=True) as runtime_class:
        runtime_class.return_value.load_background_task.return_value = task_state
        result = cli.main(["tasks", "status", "task-json", "--workspace", str(workspace), "--json"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert result == 0
    assert payload["workspace"] == str(workspace)
    assert payload["task"]["task_id"] == "task-json"
    assert payload["task"]["parent_session_id"] == "leader-session"
    assert payload["task"]["requested_child_session_id"] == "requested-child"
    assert payload["task"]["child_session_id"] == "child-session"
    assert payload["task"]["error_type"] == "provider"
    assert payload["task"]["routing"]["subagent_type"] == "worker"
    assert any("provider inspect" in step for step in payload["task"]["next_steps"])


def test_cli_tasks_guidance_quotes_workspace_with_spaces(capsys: Any) -> None:
    cli = importlib.import_module("voidcode.cli")
    workspace = Path("/tmp/demo workspace")
    task_state = SimpleNamespace(
        task=SimpleNamespace(id="task-spaces"),
        status="running",
        parent_session_id="leader-session",
        request=SimpleNamespace(session_id="requested-child"),
        child_session_id="child-session",
        approval_request_id="approval-1",
        question_request_id=None,
        result_available=False,
        cancellation_cause=None,
        error=None,
        routing_identity=None,
    )

    with patch.object(cli, "VoidCodeRuntime", autospec=True) as runtime_class:
        runtime_class.return_value.load_background_task.return_value = task_state
        result = cli.main(["tasks", "status", "task-spaces", "--workspace", str(workspace)])

    captured = capsys.readouterr()
    assert result == 0
    assert "--workspace '/tmp/demo workspace'" in captured.out


def test_cli_tasks_json_guidance_quotes_workspace_with_shell_metacharacters(
    capsys: Any,
) -> None:
    cli = importlib.import_module("voidcode.cli")
    workspace = Path("/tmp/demo repo; rm -rf no")
    task_state = SimpleNamespace(
        task=SimpleNamespace(id="task-shell"),
        status="completed",
        parent_session_id="leader-session",
        request=SimpleNamespace(session_id="requested-child"),
        child_session_id="child-session",
        approval_request_id=None,
        question_request_id=None,
        result_available=True,
        cancellation_cause=None,
        error=None,
        routing_identity=None,
    )

    with patch.object(cli, "VoidCodeRuntime", autospec=True) as runtime_class:
        runtime_class.return_value.load_background_task.return_value = task_state
        result = cli.main(
            ["tasks", "status", "task-shell", "--workspace", str(workspace), "--json"]
        )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert result == 0
    assert payload["task"]["next_steps"] == [
        "Read output: voidcode tasks output task-shell --workspace '/tmp/demo repo; rm -rf no'",
        "Replay child session: voidcode sessions resume child-session "
        "--workspace '/tmp/demo repo; rm -rf no'",
    ]


def test_cli_tasks_surfaces_real_runtime_completed_delegated_lifecycle(
    tmp_path: Path, capsys: Any
) -> None:
    cli = importlib.import_module("voidcode.cli")
    runtime_module = importlib.import_module("voidcode.runtime")
    (tmp_path / "sample.txt").write_text("hello\n", encoding="utf-8")

    runtime = runtime_module.VoidCodeRuntime(workspace=tmp_path)
    _ = runtime.run(
        runtime_module.RuntimeRequest(prompt="read sample.txt", session_id="leader-session")
    )
    started = runtime.start_background_task(
        runtime_module.RuntimeRequest(
            prompt="read sample.txt",
            parent_session_id="leader-session",
        )
    )
    completed = _wait_for_background_task(
        runtime,
        started.task.id,
        predicate=_is_terminal_background_task,
    )

    status_result = cli.main(["tasks", "status", started.task.id, "--workspace", str(tmp_path)])
    status_output = capsys.readouterr().out
    output_result = cli.main(["tasks", "output", started.task.id, "--workspace", str(tmp_path)])
    task_output = capsys.readouterr().out
    list_result = cli.main(["tasks", "list", "--workspace", str(tmp_path)])
    list_output = capsys.readouterr().out
    filtered_result = cli.main(
        [
            "tasks",
            "list",
            "--workspace",
            str(tmp_path),
            "--parent-session",
            "leader-session",
        ]
    )
    filtered_output = capsys.readouterr().out

    assert status_result == 0
    assert output_result == 0
    assert list_result == 0
    assert filtered_result == 0
    assert f"TASK id={started.task.id} status=completed" in status_output
    assert "parent_session_id=leader-session" in status_output
    assert f"child_session_id={completed.child_session_id}" in status_output
    assert "result_available=True" in status_output
    assert f"TASK id={started.task.id} status=completed" in task_output
    assert "summary_output='Completed child session " in task_output
    assert "summary_output='Completed: hello'" not in task_output
    assert "RESULT\nhello\n" in task_output
    assert f"TASK id={started.task.id} status=completed" in list_output
    assert f"TASK id={started.task.id} status=completed" in filtered_output


def test_cli_tasks_output_delegates_to_runtime_and_prints_child_result(capsys: Any) -> None:
    cli = importlib.import_module("voidcode.cli")
    workspace = Path("/tmp/demo-workspace")
    task_result = SimpleNamespace(
        task_id="task-1",
        status="completed",
        parent_session_id="leader-session",
        requested_child_session_id="child-requested",
        child_session_id="child-session",
        approval_request_id=None,
        question_request_id=None,
        approval_blocked=False,
        result_available=True,
        summary_output="Completed: delegated work",
        error=None,
        routing=SimpleNamespace(
            mode="background",
            category=None,
            subagent_type="explore",
            description="Inspect logs",
            command=None,
        ),
    )
    session_result = SimpleNamespace(output="child output\n")

    with patch.object(cli, "VoidCodeRuntime", autospec=True) as runtime_class:
        runtime = runtime_class.return_value
        runtime.load_background_task_result.return_value = task_result
        runtime.session_result.return_value = session_result
        result = cli.main(["tasks", "output", "task-1", "--workspace", str(workspace)])

    captured = capsys.readouterr()
    assert result == 0
    runtime.load_background_task_result.assert_called_once_with("task-1")
    runtime.session_result.assert_called_once_with(session_id="child-session")
    assert "TASK id=task-1 status=completed" in captured.out
    assert "subagent_type=explore" in captured.out
    assert "NEXT" in captured.out
    assert "voidcode sessions resume child-session" in captured.out
    assert "RESULT\nchild output\n" in captured.out


def test_cli_tasks_output_supports_json_failure_guidance(capsys: Any) -> None:
    cli = importlib.import_module("voidcode.cli")
    workspace = Path("/tmp/demo-workspace")
    task_result = SimpleNamespace(
        task_id="task-failed-json",
        status="failed",
        parent_session_id="leader-session",
        requested_child_session_id="requested-child",
        child_session_id="child-session",
        approval_request_id=None,
        question_request_id=None,
        approval_blocked=False,
        result_available=True,
        summary_output="Failed: child tool failed",
        error="tool write_file failed",
        cancellation_cause=None,
        routing=None,
    )
    session_result = SimpleNamespace(output="tool failure details\n")

    with patch.object(cli, "VoidCodeRuntime", autospec=True) as runtime_class:
        runtime = runtime_class.return_value
        runtime.load_background_task_result.return_value = task_result
        runtime.session_result.return_value = session_result
        result = cli.main(
            ["tasks", "output", "task-failed-json", "--workspace", str(workspace), "--json"]
        )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert result == 0
    assert payload["task"]["task_id"] == "task-failed-json"
    assert payload["task"]["error_type"] == "tool"
    assert payload["task"]["parent_session_id"] == "leader-session"
    assert payload["task"]["child_session_id"] == "child-session"
    assert payload["output"] == "tool failure details\n"
    assert any("child session events" in step for step in payload["task"]["next_steps"])


def test_cli_tasks_surfaces_real_runtime_waiting_approval_and_cancel(
    tmp_path: Path, capsys: Any
) -> None:
    cli = importlib.import_module("voidcode.cli")
    runtime_module = importlib.import_module("voidcode.runtime")
    config_module = importlib.import_module("voidcode.runtime.config")
    permission_module = importlib.import_module("voidcode.runtime.permission")

    runtime = runtime_module.VoidCodeRuntime(
        workspace=tmp_path,
        config=config_module.RuntimeConfig(
            approval_mode="ask",
            execution_engine="deterministic",
        ),
        permission_policy=permission_module.PermissionPolicy(mode="ask"),
    )
    (tmp_path / "sample.txt").write_text("leader\n", encoding="utf-8")
    _ = runtime.run(
        runtime_module.RuntimeRequest(prompt="read sample.txt", session_id="leader-session")
    )
    started = runtime.start_background_task(
        runtime_module.RuntimeRequest(
            prompt="write child.txt delegated",
            parent_session_id="leader-session",
        )
    )
    waiting = _wait_for_background_task(
        runtime,
        started.task.id,
        predicate=_is_waiting_approval_background_task,
    )

    status_result = cli.main(["tasks", "status", started.task.id, "--workspace", str(tmp_path)])
    status_output = capsys.readouterr().out
    output_result = cli.main(["tasks", "output", started.task.id, "--workspace", str(tmp_path)])
    task_output = capsys.readouterr().out
    cancel_result = cli.main(["tasks", "cancel", started.task.id, "--workspace", str(tmp_path)])
    cancel_output = capsys.readouterr().out

    assert status_result == 0
    assert output_result == 0
    assert cancel_result == 0
    assert f"TASK id={started.task.id} status=running" in status_output
    assert f"child_session_id={waiting.child_session_id}" in status_output
    assert f"approval_request_id={waiting.approval_request_id}" in status_output
    assert f"TASK id={started.task.id} status=running" in task_output
    assert f"approval_request_id={waiting.approval_request_id}" in task_output
    assert "approval_blocked=True" in task_output
    assert "Approval blocked on write_file: write_file child.txt" in task_output
    assert f"TASK id={started.task.id} status=cancelled" in cancel_output
    assert "cancellation_cause=cancelled by parent while child session was waiting" in cancel_output
    assert "cancelled by parent while child session was waiting" in cancel_output


def test_cli_tasks_output_falls_back_to_summary_without_child_result(capsys: Any) -> None:
    cli = importlib.import_module("voidcode.cli")
    workspace = Path("/tmp/demo-workspace")
    task_result = SimpleNamespace(
        task_id="task-2",
        status="failed",
        parent_session_id="leader-session",
        requested_child_session_id=None,
        child_session_id=None,
        approval_request_id=None,
        question_request_id=None,
        approval_blocked=False,
        result_available=False,
        summary_output="Failed: delegated work",
        error="boom",
        routing=None,
    )

    with patch.object(cli, "VoidCodeRuntime", autospec=True) as runtime_class:
        runtime = runtime_class.return_value
        runtime.load_background_task_result.return_value = task_result
        result = cli.main(["tasks", "output", "task-2", "--workspace", str(workspace)])

    captured = capsys.readouterr()
    assert result == 0
    runtime.load_background_task_result.assert_called_once_with("task-2")
    runtime.session_result.assert_not_called()
    assert "TASK id=task-2 status=failed" in captured.out
    assert "error=boom" in captured.out
    assert "RESULT\nFailed: delegated work" in captured.out


def test_cli_tasks_output_falls_back_when_child_session_lookup_fails(capsys: Any) -> None:
    cli = importlib.import_module("voidcode.cli")
    workspace = Path("/tmp/demo-workspace")
    task_result = SimpleNamespace(
        task_id="task-3",
        status="failed",
        parent_session_id="leader-session",
        requested_child_session_id="child-requested",
        child_session_id="child-missing",
        approval_request_id=None,
        question_request_id=None,
        approval_blocked=False,
        result_available=True,
        summary_output="Failed: delegated work",
        error="provider execution requires a configured provider/model",
        routing=None,
    )

    with patch.object(cli, "VoidCodeRuntime", autospec=True) as runtime_class:
        runtime = runtime_class.return_value
        runtime.load_background_task_result.return_value = task_result
        runtime.session_result.side_effect = ValueError("unknown session: child-missing")
        result = cli.main(["tasks", "output", "task-3", "--workspace", str(workspace)])

    captured = capsys.readouterr()
    assert result == 0
    runtime.load_background_task_result.assert_called_once_with("task-3")
    runtime.session_result.assert_called_once_with(session_id="child-missing")
    assert "TASK id=task-3 status=failed" in captured.out
    assert "result_available=True" in captured.out
    assert "error=provider execution requires a configured provider/model" in captured.out
    assert "RESULT\nFailed: delegated work" in captured.out


def test_cli_tasks_output_preserves_empty_child_session_output(capsys: Any) -> None:
    cli = importlib.import_module("voidcode.cli")
    workspace = Path("/tmp/demo-workspace")
    task_result = SimpleNamespace(
        task_id="task-empty",
        status="completed",
        parent_session_id="leader-session",
        requested_child_session_id="child-requested",
        child_session_id="child-session",
        approval_request_id=None,
        question_request_id=None,
        approval_blocked=False,
        result_available=True,
        summary_output="Completed: delegated work",
        error=None,
        routing=None,
    )
    session_result = SimpleNamespace(output="")

    with patch.object(cli, "VoidCodeRuntime", autospec=True) as runtime_class:
        runtime = runtime_class.return_value
        runtime.load_background_task_result.return_value = task_result
        runtime.session_result.return_value = session_result
        result = cli.main(["tasks", "output", "task-empty", "--workspace", str(workspace)])

    captured = capsys.readouterr()

    assert result == 0
    runtime.load_background_task_result.assert_called_once_with("task-empty")
    runtime.session_result.assert_called_once_with(session_id="child-session")
    assert "TASK id=task-empty status=completed" in captured.out
    assert "summary_output='Completed: delegated work'" in captured.out
    assert captured.out.endswith("RESULT\n")
    assert "Completed: delegated work" not in captured.out.split("RESULT\n", 1)[1]


def test_cli_tasks_cancel_delegates_to_runtime_cancel_background_task(capsys: Any) -> None:
    cli = importlib.import_module("voidcode.cli")
    workspace = Path("/tmp/demo-workspace")
    task_state = SimpleNamespace(
        task=SimpleNamespace(id="task-1"),
        status="cancelled",
        parent_session_id="leader-session",
        request=SimpleNamespace(session_id="child-requested"),
        child_session_id="child-session",
        approval_request_id=None,
        question_request_id=None,
        result_available=False,
        cancellation_cause="parent_cancelled",
        error="cancelled before start",
        routing_identity=None,
    )

    with patch.object(cli, "VoidCodeRuntime", autospec=True) as runtime_class:
        runtime_class.return_value.cancel_background_task.return_value = task_state
        result = cli.main(["tasks", "cancel", "task-1", "--workspace", str(workspace)])

    captured = capsys.readouterr()
    assert result == 0
    runtime_class.return_value.cancel_background_task.assert_called_once_with("task-1")
    assert "TASK id=task-1 status=cancelled" in captured.out
    assert "cancellation_cause=parent_cancelled" in captured.out
    assert "error=cancelled before start" in captured.out


def test_cli_tasks_retry_delegates_to_runtime_retry_background_task(capsys: Any) -> None:
    cli = importlib.import_module("voidcode.cli")
    workspace = Path("/tmp/demo-workspace")
    task_state = SimpleNamespace(
        task=SimpleNamespace(id="task-retried"),
        status="queued",
        parent_session_id="leader-session",
        request=SimpleNamespace(session_id="child-requested"),
        child_session_id=None,
        approval_request_id=None,
        question_request_id=None,
        result_available=False,
        cancellation_cause=None,
        error=None,
        routing_identity=None,
    )

    with patch.object(cli, "VoidCodeRuntime", autospec=True) as runtime_class:
        runtime_class.return_value.retry_background_task.return_value = task_state
        result = cli.main(["tasks", "retry", "task-1", "--workspace", str(workspace)])

    captured = capsys.readouterr()
    assert result == 0
    runtime_class.return_value.retry_background_task.assert_called_once_with("task-1")
    assert "TASK id=task-retried status=queued" in captured.out
    assert "RETRY previous_task_id=task-1 new_task_id=task-retried" in captured.out


def test_cli_tasks_list_lists_all_background_tasks(capsys: Any) -> None:
    cli = importlib.import_module("voidcode.cli")
    workspace = Path("/tmp/demo-workspace")
    tasks = (
        SimpleNamespace(
            task=SimpleNamespace(id="task-1"),
            status="queued",
            session_id=None,
            created_at=1,
            updated_at=2,
            prompt="Investigate",
        ),
        SimpleNamespace(
            task=SimpleNamespace(id="task-2"),
            status="completed",
            session_id="child-session",
            created_at=3,
            updated_at=4,
            prompt="Summarize",
        ),
    )

    with patch.object(cli, "VoidCodeRuntime", autospec=True) as runtime_class:
        runtime_class.return_value.list_background_tasks.return_value = tasks
        result = cli.main(["tasks", "list", "--workspace", str(workspace)])

    captured = capsys.readouterr()
    assert result == 0
    runtime_class.return_value.list_background_tasks.assert_called_once_with()
    assert "TASK id=task-1 status=queued" in captured.out
    assert "TASK id=task-2 status=completed" in captured.out
    assert "prompt='Investigate'" in captured.out
    assert "prompt='Summarize'" in captured.out


def test_cli_tasks_list_supports_json(capsys: Any) -> None:
    cli = importlib.import_module("voidcode.cli")
    workspace = Path("/tmp/demo-workspace")
    tasks = (
        SimpleNamespace(
            task=SimpleNamespace(id="task-1"),
            status="queued",
            session_id=None,
            created_at=1,
            updated_at=2,
            prompt="Investigate",
            error=None,
        ),
        SimpleNamespace(
            task=SimpleNamespace(id="task-2"),
            status="failed",
            session_id="child-session",
            created_at=3,
            updated_at=4,
            prompt="Summarize",
            error="runtime failed",
        ),
    )

    with patch.object(cli, "VoidCodeRuntime", autospec=True) as runtime_class:
        runtime_class.return_value.list_background_tasks.return_value = tasks
        result = cli.main(["tasks", "list", "--workspace", str(workspace), "--json"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert result == 0
    assert payload["workspace"] == str(workspace)
    assert payload["parent_session_id"] is None
    assert payload["tasks"][0]["task_id"] == "task-1"
    assert payload["tasks"][1]["child_session_id"] == "child-session"
    assert payload["tasks"][1]["error_type"] == "runtime"


def test_cli_tasks_list_filters_by_parent_session(capsys: Any) -> None:
    cli = importlib.import_module("voidcode.cli")
    workspace = Path("/tmp/demo-workspace")
    tasks = (
        SimpleNamespace(
            task=SimpleNamespace(id="task-3"),
            status="running",
            session_id="child-session",
            created_at=5,
            updated_at=6,
            prompt="Review",
        ),
    )

    with patch.object(cli, "VoidCodeRuntime", autospec=True) as runtime_class:
        runtime_class.return_value.list_background_tasks_by_parent_session.return_value = tasks
        result = cli.main(
            [
                "tasks",
                "list",
                "--workspace",
                str(workspace),
                "--parent-session",
                "leader-session",
            ]
        )

    captured = capsys.readouterr()
    assert result == 0
    runtime_class.return_value.list_background_tasks_by_parent_session.assert_called_once_with(
        parent_session_id="leader-session"
    )
    runtime_class.return_value.list_background_tasks.assert_not_called()
    assert "TASK id=task-3 status=running" in captured.out


# ---------------------------------------------------------------------------
# 9. Runtime truth: delegated lifecycle methods exist and are callable
# ---------------------------------------------------------------------------


def test_runtime_exposes_start_background_task(tmp_path: Path) -> None:
    """Runtime truth: ``start_background_task`` must exist and return a task
    with a valid ID and queued status."""
    runtime_module = importlib.import_module("voidcode.runtime")
    runtime = runtime_module.VoidCodeRuntime(workspace=tmp_path)
    started = runtime.start_background_task(runtime_module.RuntimeRequest(prompt="background work"))
    assert started.task.id.startswith("task-")
    assert started.status in ("queued", "running", "completed")


def test_runtime_exposes_load_background_task(tmp_path: Path) -> None:
    """Runtime truth: ``load_background_task`` must return the current state
    of a background task."""
    runtime_module = importlib.import_module("voidcode.runtime")
    runtime = runtime_module.VoidCodeRuntime(workspace=tmp_path)
    started = runtime.start_background_task(runtime_module.RuntimeRequest(prompt="background work"))
    loaded = runtime.load_background_task(started.task.id)
    assert loaded.task.id == started.task.id


def test_runtime_exposes_cancel_background_task(tmp_path: Path) -> None:
    runtime_module = importlib.import_module("voidcode.runtime")
    runtime = runtime_module.VoidCodeRuntime(workspace=tmp_path)
    runtime._background_tasks_reconciled = True
    store = runtime._session_store
    task_module = importlib.import_module("voidcode.runtime.task")
    store.create_background_task(
        workspace=tmp_path,
        task=task_module.BackgroundTaskState(
            task=task_module.BackgroundTaskRef(id="task-cancel-test"),
            request=task_module.BackgroundTaskRequestSnapshot(
                prompt="background work",
            ),
            created_at=1,
            updated_at=1,
        ),
    )

    cancelled = runtime.cancel_background_task("task-cancel-test")
    assert cancelled.status == "cancelled"


def test_runtime_exposes_retry_background_task(tmp_path: Path) -> None:
    runtime_module = importlib.import_module("voidcode.runtime")
    runtime = runtime_module.VoidCodeRuntime(workspace=tmp_path)
    runtime._background_tasks_reconciled = True
    store = runtime._session_store
    task_module = importlib.import_module("voidcode.runtime.task")
    store.create_background_task(
        workspace=tmp_path,
        task=task_module.BackgroundTaskState(
            task=task_module.BackgroundTaskRef(id="task-retry-test"),
            request=task_module.BackgroundTaskRequestSnapshot(prompt="background work"),
            created_at=1,
            updated_at=1,
        ),
    )
    cancelled = runtime.cancel_background_task("task-retry-test")

    retried = runtime.retry_background_task(cancelled.task.id)
    _ = _wait_for_background_task(
        runtime,
        retried.task.id,
        predicate=_is_terminal_background_task,
    )

    assert retried.task.id != cancelled.task.id
    assert retried.request.prompt == "background work"


def test_runtime_exposes_list_background_tasks(tmp_path: Path) -> None:
    """Runtime truth: ``list_background_tasks`` must return all tasks."""
    runtime_module = importlib.import_module("voidcode.runtime")
    runtime = runtime_module.VoidCodeRuntime(workspace=tmp_path)
    started = runtime.start_background_task(runtime_module.RuntimeRequest(prompt="background work"))
    listed = runtime.list_background_tasks()
    assert any(t.task.id == started.task.id for t in listed)


def test_runtime_exposes_list_background_tasks_by_parent_session(tmp_path: Path) -> None:
    runtime_module = importlib.import_module("voidcode.runtime")
    (tmp_path / "sample.txt").write_text("hello\n", encoding="utf-8")
    runtime = runtime_module.VoidCodeRuntime(workspace=tmp_path)
    _ = runtime.run(
        runtime_module.RuntimeRequest(prompt="read sample.txt", session_id="leader-session")
    )
    started = runtime.start_background_task(
        runtime_module.RuntimeRequest(
            prompt="child work",
            parent_session_id="leader-session",
        )
    )
    listed = runtime.list_background_tasks_by_parent_session(parent_session_id="leader-session")
    assert any(t.task.id == started.task.id for t in listed)
