from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from voidcode.runtime.contracts import (
    ProviderInspectResult,
    ProviderModelsResult,
    ProviderReadinessResult,
    ProviderSummary,
    ProviderValidationResult,
)
from voidcode.runtime.question import QuestionResponse


class _Tty:
    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.writes: list[str] = []

    def isatty(self) -> bool:
        return True

    def readline(self) -> str:
        return self.answers.pop(0) if self.answers else ""

    def write(self, text: str) -> int:
        self.writes.append(text)
        return len(text)

    def flush(self) -> None:
        return None


class _Event:
    def __init__(self, event_type: str, payload: dict[str, object]) -> None:
        self.event_type = event_type
        self.source = "runtime"
        self.payload = payload
        self.sequence = 1


class _Session:
    def __init__(self, session_id: str, status: str, metadata: dict[str, object] | None = None) -> None:
        self.session = SimpleNamespace(id=session_id, parent_id=None)
        self.status = status
        self.turn = 1
        self.metadata = metadata or {}


class _Chunk:
    def __init__(self, session: _Session, event: _Event | None = None, output: str | None = None) -> None:
        self.kind = "output" if output is not None else "event"
        self.session = session
        self.event = event
        self.output = output


def test_interactive_question_uses_answer_question_stream(capsys: pytest.CaptureFixture[str]) -> None:
    from voidcode.cli import app

    waiting = _Session("question-session", "waiting")
    completed = _Session("question-session", "completed")
    question = _Event(
        "runtime.question_requested",
        {
            "request_id": "question-1",
            "tool": "question",
            "question_count": 1,
            "questions": [{"header": "choice", "question": "Pick one", "options": [{"label": "yes"}]}],
        },
    )
    runtime = MagicMock()
    runtime.run_stream.return_value = iter([_Chunk(waiting, question)])
    runtime.answer_question_stream.return_value = iter([_Chunk(completed, output="done")])
    config = SimpleNamespace(approval_mode="ask")
    stdin = _Tty("yes\n")
    stderr = _Tty()
    with patch.object(app, "load_runtime_config", return_value=config):
        with patch.object(app, "VoidCodeRuntime", return_value=runtime):
            with patch.object(app.sys, "stdin", stdin), patch.object(app.sys, "stderr", stderr):
                assert app.main(["run", "ask", "--workspace", "/tmp/question-workspace"]) == 0
    runtime.answer_question_stream.assert_called_once()
    assert runtime.answer_question_stream.call_args.kwargs["question_request_id"] == "question-1"
    responses = runtime.answer_question_stream.call_args.kwargs["responses"]
    assert responses == (QuestionResponse(header="choice", answers=("yes",)),)
    assert "Pick one" in "".join(stderr.writes)
    assert "RESULT" in capsys.readouterr().out


def test_sessions_answer_uses_stream_and_returns_failed_status() -> None:
    from voidcode.cli import app

    runtime = MagicMock()
    runtime.answer_question_stream.return_value = iter([_Chunk(_Session("s", "failed"), output="failed")])
    with patch.object(app, "VoidCodeRuntime", return_value=runtime):
        result = app.main(["sessions", "answer", "s", "--question-request-id", "q", "--response", "no", "--workspace", "/tmp/ws"])
    assert result == app.EXIT_RUNTIME_ERROR
    runtime.answer_question.assert_not_called()
    runtime.answer_question_stream.assert_called_once()


def test_provider_inspect_unready_returns_provider_exit() -> None:
    from voidcode.cli import app

    inspect = ProviderInspectResult(
        summary=ProviderSummary(name="openai", label="OpenAI", configured=True),
        models=ProviderModelsResult(provider="openai", configured=True),
        validation=ProviderValidationResult(provider="openai", configured=True, ok=True, status="ok", message="ok"),
        readiness=ProviderReadinessResult(
            provider="openai",
            model="gpt",
            configured=True,
            ok=False,
            status="missing_auth",
            guidance="set OPENAI_API_KEY",
        ),
    )
    runtime = MagicMock()
    runtime.inspect_provider.return_value = inspect
    with patch.object(app, "VoidCodeRuntime", return_value=runtime), patch.object(app, "print"):
        result = app.main(["provider", "inspect", "openai", "--workspace", "/tmp/ws"])
    assert result == app.EXIT_PROVIDER_ERROR


def test_export_oserror_is_stable_cli_error(tmp_path: Path) -> None:
    from voidcode.cli import app

    runtime = MagicMock()
    runtime.export_session_bundle.return_value = SimpleNamespace(to_payload=lambda: {"schema": "x", "manifest": {}})
    with patch.object(app, "VoidCodeRuntime", return_value=runtime), patch.object(app, "write_session_bundle", side_effect=OSError("read-only")):
        result = app.main(["sessions", "export", "s", "--workspace", str(tmp_path), "--output", str(tmp_path / "out.zip")])
    assert result == app.EXIT_RUNTIME_ERROR


def test_task_question_guidance_has_copyable_answer_command(tmp_path: Path) -> None:
    from voidcode.cli import app

    steps = app._background_task_next_steps(
        task_id="task-1",
        status="running",
        workspace=tmp_path,
        child_session_id="child-1",
        approval_request_id=None,
        question_request_id="question-1",
        result_available=False,
        error=None,
    )
    assert any("voidcode sessions answer child-1" in step for step in steps)
