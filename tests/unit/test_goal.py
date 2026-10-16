"""Unit tests for native /goal, runtime goal persistence, accounting, tools, TUI dispatch, and HTTP transport."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from starlette.testclient import TestClient

from voidcode.core.tool_context import ToolContext
from voidcode.core.turns import FinalTurn, ModelTurnFact, TurnPlan, TurnRequest
from voidcode.provider.protocol import ProviderTokenUsage
from voidcode.runtime import create_runtime_app
from voidcode.runtime.contracts import RuntimeRequest
from voidcode.runtime.goal import (
    GOAL_FILE_PREFIX,
    GOAL_FILE_SUFFIX,
    ThreadGoal,
    format_goal_elapsed_seconds,
    format_tokens_compact,
    goal_status_label,
    goal_usage_summary,
    objective_text_for_edit,
    should_confirm_before_replacing_goal,
    spill_goal_objective_if_needed,
)
from voidcode.runtime.service import VoidCodeRuntime
from voidcode.tools.contracts import ToolCall, ToolFailure, ToolSuccess
from voidcode.tools.goal import CreateGoalTool, GetGoalTool, UpdateGoalTool
from voidcode.tui.app import TuiApp
from voidcode.tui.overlay import GoalConfirmOverlay, OverlayOutcomeKind
from voidcode.tui.term import Terminal


def test_goal_format_helpers() -> None:
    assert format_goal_elapsed_seconds(30) == "30s"
    assert format_goal_elapsed_seconds(120) == "2m"
    assert format_goal_elapsed_seconds(3600) == "1h"
    assert format_goal_elapsed_seconds(3660) == "1h 1m"
    assert format_goal_elapsed_seconds(90000) == "1d 1h 0m"

    assert format_tokens_compact(500) == "500"
    assert format_tokens_compact(1500) == "1.5k"
    assert format_tokens_compact(2000) == "2k"
    assert format_tokens_compact(1500000) == "1.5M"

    assert goal_status_label("active") == "active"
    assert goal_status_label("blocked") == "stalled"
    assert goal_status_label("budget-limited") == "limited by budget"

    goal = ThreadGoal(
        thread_id="test",
        objective="Fix everything",
        status="active",
        token_budget=10000,
        tokens_used=2500,
        time_used_seconds=125,
    )
    summary = goal_usage_summary(goal)
    assert "Objective: Fix everything" in summary
    assert "Time: 2m." in summary
    assert "Tokens: 2.5k/10k." in summary

    assert should_confirm_before_replacing_goal(goal) is True
    complete_goal = ThreadGoal(thread_id="test", objective="done", status="complete")
    assert should_confirm_before_replacing_goal(complete_goal) is False


def test_goal_spilling(tmp_path: Path) -> None:
    short_obj = "short objective"
    spilled, path = spill_goal_objective_if_needed(tmp_path, short_obj)
    assert spilled == short_obj
    assert path is None

    long_obj = "x" * 4005
    spilled, path = spill_goal_objective_if_needed(tmp_path, long_obj)
    assert path is not None
    assert spilled.startswith(GOAL_FILE_PREFIX)
    assert spilled.endswith(GOAL_FILE_SUFFIX)
    assert path.is_file()
    assert path.read_text(encoding="utf-8") == long_obj

    # Test roundtrip reading for edit
    restored = objective_text_for_edit(tmp_path, spilled)
    assert restored == long_obj


class _DummyProducer:
    def __init__(self, token_usage: ProviderTokenUsage | None = None) -> None:
        self.usage = token_usage or ProviderTokenUsage(input_tokens=100, output_tokens=50)

    def produce(self, request: TurnRequest, tool_results: tuple[object, ...], *, session: object) -> TurnPlan:
        _ = (tool_results, session)
        return FinalTurn(
            facts=(ModelTurnFact(request.run_step, "deterministic", request.prompt),),
            output="finished task",
            provider_usage=self.usage,
        )


def test_goal_crud_and_persistence(tmp_path: Path) -> None:
    with VoidCodeRuntime(workspace=tmp_path, turn_producer=_DummyProducer()) as runtime:
        resp = runtime.run(RuntimeRequest(prompt="initial task"))
        session_id = resp.session.session.id

        # Initial: no goal
        assert runtime.get_goal(session_id=session_id) is None

        # Set goal
        goal = runtime.set_goal(
            session_id=session_id,
            objective="Build native /goal",
            token_budget=5000,
        )
        assert goal["objective"] == "Build native /goal"
        assert goal["status"] == "active"
        assert goal["token_budget"] == 5000

        # Verify persistence across session reload
        stored = runtime._repositories.sessions.load_session(workspace=tmp_path, session_id=session_id)
        assert stored.session.metadata.get("thread_goal") == goal
        goal_events = [e for e in stored.events if e.event_type == "runtime.thread_goal_updated"]
        assert len(goal_events) == 1
        assert goal_events[0].payload["goal"] == goal

        # Mutate status: pause
        paused = runtime.set_goal(session_id=session_id, status="paused")
        assert paused["status"] == "paused"
        assert paused["objective"] == "Build native /goal"

        # Mutate status: resume
        resumed = runtime.set_goal(session_id=session_id, status="active")
        assert resumed["status"] == "active"

        # Clear goal
        cleared = runtime.clear_goal(session_id=session_id)
        assert cleared is True
        assert runtime.get_goal(session_id=session_id) is None

        stored_after_clear = runtime._repositories.sessions.load_session(workspace=tmp_path, session_id=session_id)
        assert "thread_goal" not in stored_after_clear.session.metadata
        clear_events = [e for e in stored_after_clear.events if e.event_type == "runtime.thread_goal_cleared"]
        assert len(clear_events) == 1


def test_goal_accounting_and_plan_mode_skip(tmp_path: Path) -> None:
    producer = _DummyProducer(token_usage=ProviderTokenUsage(input_tokens=200, output_tokens=100))
    with VoidCodeRuntime(workspace=tmp_path, turn_producer=producer) as runtime:
        resp = runtime.run(RuntimeRequest(prompt="turn 1"))
        session_id = resp.session.session.id

        # Set active goal
        runtime.set_goal(session_id=session_id, objective="Pursue goal", token_budget=1000)

        # Normal run charges the active goal
        _ = runtime.run(RuntimeRequest(session_id=session_id, prompt="turn 2"))
        goal_after = runtime.get_goal(session_id=session_id)
        assert goal_after is not None
        assert goal_after["tokens_used"] == 300

        # Switch to Plan mode
        runtime.set_collaboration_mode(session_id=session_id, mode="plan")

        # Plan-mode run must be excluded from accounting
        _ = runtime.run(RuntimeRequest(session_id=session_id, prompt="planning turn"))
        goal_plan = runtime.get_goal(session_id=session_id)
        assert goal_plan is not None
        assert goal_plan["tokens_used"] == 300  # unchanged!


def test_agent_tools_rules(tmp_path: Path) -> None:
    get_tool = GetGoalTool()
    create_tool = CreateGoalTool()
    update_tool = UpdateGoalTool()

    # 1. get_goal when no goal
    ctx_empty = ToolContext(session_id="s1", goal=None)
    res_get = get_tool.invoke(ToolCall(tool_name="get_goal"), context=ctx_empty)
    assert isinstance(res_get, ToolSuccess)
    parsed = json.loads(res_get.output.text)
    assert parsed["goal"] is None
    assert parsed["remainingTokens"] is None

    # 2. create_goal
    call_create = ToolCall(tool_name="create_goal", arguments={"objective": "Test objective", "token_budget": 500})
    res_create = create_tool.invoke(call_create, context=ctx_empty)
    assert isinstance(res_create, ToolSuccess)
    created_goal = res_create.body.goal if res_create.body else None
    assert created_goal is not None
    assert created_goal["objective"] == "Test objective"
    assert created_goal["status"] == "active"
    assert created_goal["token_budget"] == 500

    # 3. create_goal fails if unfinished goal exists
    ctx_active = ToolContext(session_id="s1", goal=created_goal)
    res_create_fail = create_tool.invoke(call_create, context=ctx_active)
    assert isinstance(res_create_fail, ToolFailure)
    assert "unfinished goal" in res_create_fail.error

    # 4. update_goal validation: illegal status
    call_bad_status = ToolCall(tool_name="update_goal", arguments={"status": "paused"})
    res_bad = update_tool.invoke(call_bad_status, context=ctx_active)
    assert isinstance(res_bad, ToolFailure)
    assert "only mark the existing goal complete or blocked" in res_bad.error

    # 5. update_goal blocked check: fails if < 3 consecutive turns
    ctx_blocked_2 = ToolContext(session_id="s1", goal=created_goal, goal_consecutive_blocked_turns=2)
    call_block = ToolCall(tool_name="update_goal", arguments={"status": "blocked"})
    res_block_fail = update_tool.invoke(call_block, context=ctx_blocked_2)
    assert isinstance(res_block_fail, ToolFailure)
    assert "at least 3 consecutive goal turns" in res_block_fail.error

    # 6. update_goal blocked check: succeeds at 3 consecutive turns
    ctx_blocked_3 = ToolContext(session_id="s1", goal=created_goal, goal_consecutive_blocked_turns=3)
    res_block_ok = update_tool.invoke(call_block, context=ctx_blocked_3)
    assert isinstance(res_block_ok, ToolSuccess)
    assert res_block_ok.body.goal["status"] == "blocked"

    # 7. update_goal complete
    call_complete = ToolCall(tool_name="update_goal", arguments={"status": "complete"})
    res_complete = update_tool.invoke(call_complete, context=ctx_active)
    assert isinstance(res_complete, ToolSuccess)
    assert res_complete.body.goal["status"] == "complete"
    assert "completionBudgetReport" in json.loads(res_complete.output.text)


def test_http_contract(tmp_path: Path) -> None:
    with VoidCodeRuntime(workspace=tmp_path, turn_producer=_DummyProducer()) as runtime:
        init_resp = runtime.run(RuntimeRequest(prompt="hi"))
        session_id = init_resp.session.session.id
        app = create_runtime_app(workspace=tmp_path, runtime_factory=lambda: runtime)
        client = TestClient(app)
        # GET /goal when unset
        get_res = client.get(f"/api/sessions/{session_id}/goal")
        assert get_res.status_code == 200
        assert get_res.json()["goal"] is None

        # POST /goal set
        post_res = client.post(
            f"/api/sessions/{session_id}/goal",
            json={"objective": "HTTP Goal test", "token_budget": 2000},
        )
        assert post_res.status_code == 200
        goal = post_res.json()["goal"]
        assert goal["objective"] == "HTTP Goal test"
        assert goal["token_budget"] == 2000

        # POST /goal pause
        pause_res = client.post(
            f"/api/sessions/{session_id}/goal",
            json={"status": "paused"},
        )
        assert pause_res.status_code == 200
        assert pause_res.json()["goal"]["status"] == "paused"

        # DELETE /goal clear
        del_res = client.delete(f"/api/sessions/{session_id}/goal")
        assert del_res.status_code == 200
        assert del_res.json()["cleared"] is True


class _MockTuiRuntime:
    def __init__(self) -> None:
        self.goal: dict[str, object] | None = None
        self.started_prompts: list[str] = []

    def current_lsp_state(self) -> SimpleNamespace:
        return SimpleNamespace(mode="disabled", servers={})

    def provider_readiness(self, *, session_id: str | None) -> SimpleNamespace:
        _ = session_id
        return SimpleNamespace(context_window=None)

    def get_goal(self, *, session_id: str) -> dict[str, object] | None:
        _ = session_id
        return self.goal

    def set_goal(
        self,
        *,
        session_id: str,
        objective: str | None = None,
        status: str | None = None,
        token_budget: int | None = None,
        mode: str | None = None,
    ) -> dict[str, object]:
        _ = (session_id, token_budget, mode)
        if self.goal is None:
            self.goal = {
                "thread_id": "t1",
                "objective": objective or "",
                "status": status or "active",
                "token_budget": None,
                "tokens_used": 0,
                "time_used_seconds": 0,
                "created_at": 1000,
                "updated_at": 1000,
            }
        else:
            if objective is not None:
                self.goal["objective"] = objective
            if status is not None:
                self.goal["status"] = status
        return dict(self.goal)

    def clear_goal(self, *, session_id: str) -> bool:
        _ = session_id
        if self.goal is None:
            return False
        self.goal = None
        return True

    def __exit__(self, *_args: object) -> None:
        return None


def test_tui_goal_dispatch_and_confirm(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = _MockTuiRuntime()
    app = TuiApp(workspace, runtime=runtime)
    log_file = (tmp_path / "terminal.log").open("wb", buffering=0)
    term = Terminal(output_fd=log_file.fileno(), is_tty=False, size=(80, 24), color_system=None)
    app._term = term
    app._setup(term)

    try:
        # Bare /goal before session started
        app._submit("/goal")
        assert app._overlay is None

        # Start session
        app._session_id = "session-1"

        # Bare /goal when no goal is set
        app._submit("/goal")
        assert runtime.goal is None

        # Set goal
        app._submit("/goal Ship the goal feature")
        assert runtime.goal is not None
        assert runtime.goal["objective"] == "Ship the goal feature"
        assert runtime.goal["status"] == "active"
        assert app._goal_status == "active"

        # Mutate status: pause
        app._submit("/goal pause")
        assert runtime.goal["status"] == "paused"
        assert app._goal_status == "paused"

        # Mutate status: resume
        app._submit("/goal resume")
        assert runtime.goal["status"] == "active"
        assert app._goal_status == "active"

        # Replace goal while active -> triggers GoalConfirmOverlay
        app._submit("/goal A different objective")
        assert isinstance(app._overlay, GoalConfirmOverlay)
        assert app._overlay._objective == "A different objective"

        # Simulate confirm 'y'
        outcome = app._overlay.handle_key(SimpleNamespace(name="y"))
        assert outcome.kind is OverlayOutcomeKind.DONE
        assert outcome.payload == "replace"
        app._handle_overlay_key(SimpleNamespace(name="y"))
        assert app._overlay is None
        assert runtime.goal["objective"] == "A different objective"

        # Clear goal
        app._submit("/goal clear")
        assert runtime.goal is None
        assert app._goal_status is None
    finally:
        app._shutdown(term)
        log_file.close()
