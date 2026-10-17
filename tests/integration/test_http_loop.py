from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from tests.integration.test_http_transport import _run_app
from voidcode.command.loop import USAGE_MESSAGE
from voidcode.core.turns import FinalTurn, ModelTurnFact, TurnRequest, TurnSession
from voidcode.runtime.contracts import RuntimeRequest
from voidcode.runtime.service import VoidCodeRuntime
from voidcode.runtime.transport.http import create_runtime_app


class _FinishingTurnProducer:
    def produce(self, request: TurnRequest, tool_results: tuple[object, ...], *, session: TurnSession) -> FinalTurn:
        fact = ModelTurnFact(request.run_step, "deterministic", request.prompt)
        return FinalTurn(facts=(fact,), output="done")


def _seed(workspace: Path, session_id: str) -> None:
    with VoidCodeRuntime(workspace=workspace, turn_producer=_FinishingTurnProducer()) as runtime:
        runtime.run(RuntimeRequest(prompt="hello", session_id=session_id))


def test_http_loop_empty_input_returns_usage(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    app = create_runtime_app(workspace=workspace)
    _seed(workspace, "session-loop-1")

    # POST with empty input -> 200 with usage message
    body = json.dumps({"input": ""}).encode("utf-8")
    res = _run_app(app, method="POST", path="/api/sessions/session-loop-1/loop", body=body)
    assert res.status == 200
    data = cast(dict[str, object], res.json())
    assert data["session_id"] == "session-loop-1"
    assert data["message"] == USAGE_MESSAGE
    assert data["is_usage"] is True
    assert data["schedule_id"] is None


def test_http_loop_schedule_success(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    app = create_runtime_app(workspace=workspace)
    _seed(workspace, "session-loop-2")

    # POST with valid input
    body = json.dumps({"input": "5m check deploy status"}).encode("utf-8")
    res = _run_app(app, method="POST", path="/api/sessions/session-loop-2/loop", body=body)
    assert res.status == 200
    data = cast(dict[str, object], res.json())
    assert data["session_id"] == "session-loop-2"
    assert data["is_usage"] is False
    assert data["schedule"] == "every 5m"
    assert data["human_schedule"] == "every 5m"
    assert data["prompt"] == "check deploy status"
    assert data["schedule_id"] is not None
    assert "job ID" in str(data["message"])

    # Verify schedule exists in runtime
    with VoidCodeRuntime(workspace=workspace) as runtime:
        sched = runtime.get_schedule(schedule_id=str(data["schedule_id"]))
        assert sched is not None
        assert sched.prompt == "check deploy status"
        assert sched.human_schedule == "every 5m"


def test_http_loop_explicit_prompt_and_interval(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    app = create_runtime_app(workspace=workspace)
    _seed(workspace, "session-loop-3")

    body = json.dumps({"prompt": "run tests", "interval": "30m"}).encode("utf-8")
    res = _run_app(app, method="POST", path="/api/sessions/session-loop-3/loop", body=body)
    assert res.status == 200
    data = cast(dict[str, object], res.json())
    assert data["schedule"] == "every 30m"
    assert data["human_schedule"] == "every 30m"
    assert data["prompt"] == "run tests"
