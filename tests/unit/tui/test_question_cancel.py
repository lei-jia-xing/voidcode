import os
import time
from pathlib import Path

import pytest

from voidcode.core.questions import QuestionCancellation
from voidcode.core.turns import FinalTurn, ToolTurn, TurnRequest, TurnSession
from voidcode.runtime.config import RuntimeConfig
from voidcode.runtime.contracts import NoPendingQuestionError
from voidcode.runtime.service import VoidCodeRuntime
from voidcode.tools.contracts import ToolCall
from voidcode.tui.app import TuiApp
from voidcode.tui.keys import Key
from voidcode.tui.overlay import QuestionOverlay
from voidcode.tui.term import Terminal


class _QuestionThenFinish:
    def produce(
        self,
        request: TurnRequest,
        tool_results: tuple[object, ...],
        *,
        session: TurnSession,
    ) -> ToolTurn | FinalTurn:
        if not tool_results:
            return ToolTurn(
                calls=(
                    ToolCall(
                        tool_name="question",
                        arguments={
                            "questions": [
                                {
                                    "question": "Choose a path",
                                    "header": "Path",
                                    "options": [],
                                    "multiple": False,
                                    "allow_freeform": True,
                                }
                            ]
                        },
                    ),
                )
            )
        return FinalTurn(output="cancelled")


def test_question_escape_cancels_and_persists_public_runtime_answer(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    config_home = tmp_path / "config"
    local_app_data = tmp_path / "localappdata"
    for directory in (workspace, home, config_home, local_app_data):
        directory.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("APPDATA", str(config_home))
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("VOIDCODE_EXECUTION_ENGINE", "deterministic")
    for key in tuple(os.environ):
        if key.endswith("_API_KEY"):
            monkeypatch.delenv(key, raising=False)

    runtime = VoidCodeRuntime(
        workspace=workspace,
        turn_producer=_QuestionThenFinish(),
        config=RuntimeConfig(execution_engine="deterministic", approval_mode="yolo"),
    )
    output = (tmp_path / "terminal.log").open("wb", buffering=0)
    terminal = Terminal(output_fd=output.fileno(), is_tty=False, size=(80, 24), color_system=None)
    runtime_entered = False
    app: TuiApp | None = None
    try:
        runtime.__enter__()
        runtime_entered = True
        app = TuiApp(workspace, runtime=runtime)
        app._term = terminal
        app._setup(terminal)
        app._start_prompt("ask")

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            app._drain_queue()
            if isinstance(app._overlay, QuestionOverlay):
                break
            time.sleep(0.01)
        assert isinstance(app._overlay, QuestionOverlay)
        app._overlay.viewport(80, 24)
        assert app._session_id is not None
        session_id = app._session_id
        requested = runtime.replay_session(session_id=session_id)
        request_id = next(event.payload["request_id"] for event in requested.events if event.event_type == "runtime.question_requested")

        app._handle_key(Key("escape"))
        deadline = time.monotonic() + 30
        while app._streaming and time.monotonic() < deadline:
            app._drain_queue()
            time.sleep(0.01)
        app._drain_queue()
        assert not app._streaming
        assert app._overlay is None

        replay = runtime.replay_session(session_id=session_id)
        cancelled = [event for event in replay.events if event.event_type == "runtime.question_answered"]
        assert len(cancelled) == 1
        assert cancelled[0].payload == {"request_id": request_id, "cancelled": True}
        assert replay.session.status == "completed"
        with pytest.raises(NoPendingQuestionError):
            runtime.answer_question(
                session_id,
                question_request_id=request_id,
                responses=QuestionCancellation(),
            )
    finally:
        if app is not None:
            app._shutdown(terminal)
        elif runtime_entered:
            runtime.__exit__(None, None, None)
        else:
            terminal.close()
        output.close()
