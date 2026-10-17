from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from tests.integration.test_http_transport import _run_app
from voidcode.core.turns import FinalTurn, ModelTurnFact, TurnRequest, TurnSession
from voidcode.runtime.active_session import ACTIVE_SESSION_REGISTRY
from voidcode.runtime.contracts import RuntimeRequest
from voidcode.runtime.service import VoidCodeRuntime
from voidcode.runtime.transport.http import create_runtime_app


class _FinishingTurnProducer:
    def produce(self, request: TurnRequest, tool_results: tuple[object, ...], *, session: TurnSession) -> FinalTurn:
        fact = ModelTurnFact(request.run_step, "deterministic", request.prompt)
        return FinalTurn(facts=(fact,), output="done")


def _seed(workspace: Path, session_id: str, title: str | None = None) -> None:
    with VoidCodeRuntime(workspace=workspace, turn_producer=_FinishingTurnProducer()) as runtime:
        runtime.run(RuntimeRequest(prompt="hello", session_id=session_id))
        if title:
            runtime.rename_session(session_id=session_id, title=title)


def test_http_create_session(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    app = create_runtime_app(workspace=workspace)

    # 1. POST /api/sessions without payload
    res = _run_app(app, method="POST", path="/api/sessions")
    assert res.status == 201
    payload = cast(dict[str, object], res.json())
    assert "session" in payload
    session_data = cast(dict[str, object], payload["session"])
    assert str(session_data["id"]).startswith("session-")
    assert payload["title"] is None

    # 2. POST /api/sessions with name
    body = json.dumps({"name": "New HTTP Session"}).encode("utf-8")
    res = _run_app(app, method="POST", path="/api/sessions", body=body)
    assert res.status == 201
    payload = cast(dict[str, object], res.json())
    assert payload["title"] == "New HTTP Session"

    # 3. POST /api/sessions with from_session_id when busy -> 409
    _seed(workspace, "busy-session")
    run_id = "test-run"
    ACTIVE_SESSION_REGISTRY.register(
        workspace=workspace,
        session_id="busy-session",
        run_id=run_id,
        metadata={"running": True},
    )
    try:
        body = json.dumps({"from_session_id": "busy-session"}).encode("utf-8")
        res = _run_app(app, method="POST", path="/api/sessions", body=body)
        assert res.status == 409
    finally:
        ACTIVE_SESSION_REGISTRY.unregister(
            workspace=workspace,
            session_id="busy-session",
            run_id=run_id,
        )


def test_http_fork_session(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _seed(workspace, "parent-session", title="Original Title")
    app = create_runtime_app(workspace=workspace)

    # 1. Successful fork with rename
    body = json.dumps({"name": "Forked Branch"}).encode("utf-8")
    res = _run_app(app, method="POST", path="/api/sessions/parent-session/fork", body=body)
    assert res.status == 201
    payload = cast(dict[str, object], res.json())
    session_data = cast(dict[str, object], payload["session"])
    assert session_data["id"] != "parent-session"
    assert payload["title"] == "Forked Branch"

    # 2. Fork unknown session -> 404
    res = _run_app(app, method="POST", path="/api/sessions/unknown-session/fork")
    assert res.status == 404

    # 3. Fork active/busy session -> 409
    run_id = "test-fork-busy"
    ACTIVE_SESSION_REGISTRY.register(
        workspace=workspace,
        session_id="parent-session",
        run_id=run_id,
        metadata={"running": True},
    )
    try:
        res = _run_app(app, method="POST", path="/api/sessions/parent-session/fork")
        assert res.status == 409
    finally:
        ACTIVE_SESSION_REGISTRY.unregister(
            workspace=workspace,
            session_id="parent-session",
            run_id=run_id,
        )


def test_http_rename_session(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _seed(workspace, "named-session")
    app = create_runtime_app(workspace=workspace)

    # 1. POST /api/sessions/{id}/rename with title
    body = json.dumps({"title": "POST Renamed"}).encode("utf-8")
    res = _run_app(app, method="POST", path="/api/sessions/named-session/rename", body=body)
    assert res.status == 200
    payload = cast(dict[str, object], res.json())
    assert payload["title"] == "POST Renamed"

    # 2. PATCH /api/sessions/{id} with name
    body = json.dumps({"name": "PATCH Renamed"}).encode("utf-8")
    res = _run_app(app, method="PATCH", path="/api/sessions/named-session", body=body)
    assert res.status == 200
    payload = cast(dict[str, object], res.json())
    assert payload["title"] == "PATCH Renamed"

    # 3. Empty name -> 400 "Thread name cannot be empty."
    body = json.dumps({"title": "   "}).encode("utf-8")
    res = _run_app(app, method="POST", path="/api/sessions/named-session/rename", body=body)
    assert res.status == 400
    err_payload = cast(dict[str, object], res.json())
    assert "Thread name cannot be empty." in str(err_payload["error"])

    # 4. Unknown session -> 404
    body = json.dumps({"title": "New Title"}).encode("utf-8")
    res = _run_app(app, method="POST", path="/api/sessions/missing-session/rename", body=body)
    assert res.status == 404


def test_http_resume_busy_gate(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _seed(workspace, "resume-busy-session")
    app = create_runtime_app(workspace=workspace)

    run_id = "test-resume-busy"
    ACTIVE_SESSION_REGISTRY.register(
        workspace=workspace,
        session_id="resume-busy-session",
        run_id=run_id,
        metadata={"running": True},
    )
    try:
        res = _run_app(app, method="POST", path="/api/sessions/resume-busy-session/resume")
        assert res.status == 409
    finally:
        ACTIVE_SESSION_REGISTRY.unregister(
            workspace=workspace,
            session_id="resume-busy-session",
            run_id=run_id,
        )


def test_http_agents_overview_and_switch_via_resume(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _seed(workspace, "session-1", title="Primary Session")
    _seed(workspace, "session-2", title="Secondary Session")

    app = create_runtime_app(workspace=workspace)

    # 1. Inspect overview from session-1
    res = _run_app(app, method="GET", path="/api/sessions/session-1/info/agents")
    assert res.status == 200
    payload = cast(dict[str, object], res.json())
    assert payload["session_id"] == "session-1"
    assert payload["current_session_id"] == "session-1"
    session_rows = cast(list[dict[str, object]], payload["sessions"])
    session_ids = [r["session_id"] for r in session_rows]
    assert "session-1" in session_ids
    assert "session-2" in session_ids

    # 2. Switch to session-2 via existing resume endpoint
    res_switch = _run_app(app, method="POST", path="/api/sessions/session-2/resume")
    assert res_switch.status == 200
    switch_payload = cast(dict[str, object], res_switch.json())
    assert switch_payload.get("output") == "done"

    # 3. Switching is busy-gated: when target is busy, returns 409
    run_id = "test-switch-busy"
    ACTIVE_SESSION_REGISTRY.register(
        workspace=workspace,
        session_id="session-2",
        run_id=run_id,
        metadata={"running": True},
    )
    try:
        res_busy = _run_app(app, method="POST", path="/api/sessions/session-2/resume")
        assert res_busy.status == 409
    finally:
        ACTIVE_SESSION_REGISTRY.unregister(
            workspace=workspace,
            session_id="session-2",
            run_id=run_id,
        )
