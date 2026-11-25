"""Unit tests for the Goal plugin models, tools, commands, views, and observers."""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.fixtures.fake_services import FakeServices
from voidcode.core.activation import plan_state_copies, validate_state_payload
from voidcode.core.storage.store import PluginStateRow
from voidcode.plugins.goal import create_plugin, goal_plugin
from voidcode.plugins.goal.goal_plugin import (
    SCHEMA_VERSION,
    ActiveWindow,
    CreateGoalTool,
    GetGoalTool,
    GoalClearCommand,
    GoalDetailsView,
    GoalEditCommand,
    GoalLoadObjectiveCommand,
    GoalPauseCommand,
    GoalResumeCommand,
    GoalSetCommand,
    GoalState,
    GoalStatus,
    UpdateGoalTool,
    goal_observer,
    prepare_goal_deactivation,
    read_full_objective,
    spill_objective_if_needed,
)
from voidcode.sdk.commands import CommandInvocation, ConfirmationRequest
from voidcode.sdk.hooks import InvocationFinishedFact, RunFinishedFact, UsageFact
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.provider import ProviderTokenUsage
from voidcode.sdk.services import BudgetExhaustion, SessionUsageReport
from voidcode.sdk.tools import ToolCall, ToolFailure, ToolSuccess


def _loaded(payload: dict[str, object] | None) -> GoalState | None:
    services = FakeServices()
    if payload is not None:
        services.seed_state(payload)
    return goal_plugin._store(services).load()[0]


def _make_context(workspace: Path, *, actor: str = "client", logical_turn: str = "turn-1") -> ToolContext:
    return ToolContext(
        workspace=workspace,
        session_id="session-1",
        run_id="run-1",
        invocation_id="inv-1",
        revision_id="rev-1",
        actor=actor,
        logical_turn=logical_turn,
    )


# --- 1. State Models and Strict Parsing ---


def test_models_state_transitions() -> None:
    now = int(time.time() * 1000)
    # Valid statuses
    for status in ("active", "paused", "blocked", "usage-limited", "budget-limited", "complete"):
        state = GoalState(goal_id="g1", status=status, objective="Task objective", created_at=now, updated_at=now)
        assert state.status == status

    # Invalid status raises ValueError
    with pytest.raises(ValueError, match="Invalid goal status"):
        GoalState(goal_id="g1", status="unknown", objective="Task", created_at=now, updated_at=now)

    # Invalid budget types raise ValueError
    with pytest.raises(ValueError, match="positive integer"):
        GoalState(goal_id="g1", status="active", objective="Task", token_budget=-10, created_at=now, updated_at=now)
    with pytest.raises(ValueError, match="positive integer"):
        GoalState(goal_id="g1", status="active", objective="Task", token_budget=True, created_at=now, updated_at=now)  # type: ignore

    # Empty objective raises ValueError
    with pytest.raises(ValueError, match="non-empty string"):
        GoalState(goal_id="g1", status="active", objective="   ", created_at=now, updated_at=now)

    # Roundtrip serialization with nullable metrics
    orig = GoalState(
        goal_id="g123",
        status="active",
        objective="Ship feature",
        created_at=now,
        updated_at=now,
        token_budget=5000,
        tokens_used=1200,
        time_used_seconds=42.5,
        cost_usd=0.05,
        active_windows=(ActiveWindow(start_sequence=0, end_sequence=5), ActiveWindow(start_sequence=10)),
        blocked_turns_count=1,
        last_blocked_logical_turn="turn-4",
    )
    serialized = orig.to_dict()
    restored = GoalState.from_dict(serialized)
    assert restored is not None
    assert restored.goal_id == "g123"
    assert restored.tokens_used == 1200
    assert restored.remaining_tokens == 3800
    assert restored.time_used_seconds == 42.5
    assert restored.cost_usd == 0.05
    assert len(restored.active_windows) == 2
    assert any(window.contains(3) for window in restored.active_windows) is True  # (0, 5]
    assert any(window.contains(0) for window in restored.active_windows) is False  # <= 0 is outside
    assert any(window.contains(7) for window in restored.active_windows) is False  # outside windows
    assert any(window.contains(15) for window in restored.active_windows) is True  # (10, inf)

    # Strict parsing: extra keys rejected!
    bad_dict = dict(serialized)
    bad_dict["unknown_hack"] = 123
    with pytest.raises(ValueError, match="unknown extra fields"):
        _loaded(bad_dict)

    # Unreported metrics remain None without fake 0 defaults
    unreported_state = GoalState(
        goal_id="g_unrep",
        status="active",
        objective="Task",
        created_at=now,
        updated_at=now,
        token_budget=None,
        tokens_used=None,
        time_used_seconds=None,
        cost_usd=None,
    )
    unrep_dict = unreported_state.to_dict()
    unrep_restored = GoalState.from_dict(unrep_dict)
    assert unrep_restored is not None
    assert unrep_restored.tokens_used is None
    assert unrep_restored.time_used_seconds is None
    assert unrep_restored.remaining_tokens is None


# --- 2. Long Objective Spilling & Verification ---


def test_spill_and_read_long_objective(tmp_path: Path) -> None:
    services = FakeServices()
    now = int(time.time() * 1000)
    # Short objective: not spilled
    short_text = "Fix the login button bug."
    prompt, ref, digest = spill_objective_if_needed(services, short_text)
    assert prompt == short_text
    assert ref is None
    assert digest is None

    # Long objective (> 4000 chars) with multiline and Unicode
    long_text = ("🚀 Goal Objective: 必须完全验证所有边界条件。\n" * 150) + "Final acceptance."
    assert len(long_text) > 4000

    prompt, ref, digest = spill_objective_if_needed(services, long_text)
    assert ref is not None
    assert ref.startswith("artifact://")
    assert digest is not None
    assert f"Read the goal objective at {ref}" in prompt
    # Workspace remains clean - no unapproved writes
    assert not (tmp_path / ".voidcode").exists()

    state = GoalState(
        goal_id="g1",
        status="active",
        objective=prompt,
        created_at=now,
        updated_at=now,
        objective_file_ref=ref,
        objective_file_sha256=digest,
    )

    # Read back complete via services.read_artifact
    read_back = read_full_objective(state, services=services)
    assert read_back == long_text

    # Non-artifact references are rejected: artifact:// only, no workspace files.
    bad_state = GoalState(
        goal_id="g1",
        status="active",
        objective="Escaping",
        created_at=now,
        updated_at=now,
        objective_file_ref="../escaped.txt",
    )
    with pytest.raises(ValueError, match="must be an artifact URI"):
        read_full_objective(bad_state, services=services)

    # Reference/hash mismatch is rejected.
    assert digest is not None
    mismatched = GoalState(
        goal_id="g1",
        status="active",
        objective=prompt,
        created_at=now,
        updated_at=now,
        objective_file_ref=ref,
        objective_file_sha256="0" * 64,
    )
    with pytest.raises(ValueError, match="reference/hash mismatch"):
        read_full_objective(mismatched, services=services)

    # Tampered artifact detection.
    assert ref is not None
    services.artifacts[ref[len("artifact://") :]] += b" Tampered addition!"
    with pytest.raises(ValueError, match="tampered or corrupted"):
        read_full_objective(state, services=services)


# --- 3. Tools (Get, Create, Update) ---


def test_get_goal_tool(tmp_path: Path) -> None:
    tool = GetGoalTool()
    services = FakeServices()
    ctx = _make_context(tmp_path)
    now = int(time.time() * 1000)

    # No goal yet: the tool still succeeds with an explicit absence.
    res = tool.invoke(ToolCall(tool_name="get_goal"), context=ctx, services=services)
    assert isinstance(res, ToolSuccess)
    assert res.body.as_payload()["goal"] is None

    # Remaining budget is derived truthfully from budget minus charged usage.
    state = GoalState(goal_id="g1", status="active", objective="Build auth", created_at=now, updated_at=now, token_budget=1000, tokens_used=300)
    services.replace_state(state.to_dict(), expected_version=0)

    res2 = tool.invoke(ToolCall(tool_name="get_goal"), context=ctx, services=services)
    assert isinstance(res2, ToolSuccess)
    body_goal = res2.body.as_payload()["goal"]
    assert body_goal["status"] == "active"
    assert body_goal["tokens_used"] == 300
    assert body_goal["token_budget"] == 1000

    # Unknown charged usage yields unknown remaining, never a fabricated full budget.
    unknown = GoalState(goal_id="g1", status="active", objective="Build auth", created_at=now, updated_at=now, token_budget=1000, tokens_used=None)
    _, ver = services.get_state("session")
    services.replace_state(unknown.to_dict(), expected_version=ver)
    res3 = tool.invoke(ToolCall(tool_name="get_goal"), context=ctx, services=services)
    assert isinstance(res3, ToolSuccess)
    assert res3.body.as_payload()["goal"]["tokens_used"] is None


def test_create_goal_tool(tmp_path: Path) -> None:
    tool = CreateGoalTool()
    services = FakeServices(through_sequence=3)
    ctx = _make_context(tmp_path)
    now = int(time.time() * 1000)

    # Refuses when unfinished goal exists
    state = GoalState(goal_id="g1", status="active", objective="Existing", created_at=now, updated_at=now)
    services.replace_state(state.to_dict(), expected_version=0)

    res = tool.invoke(ToolCall(tool_name="create_goal", arguments={"objective": "New"}), context=ctx, services=services)
    assert isinstance(res, ToolFailure)
    assert "unfinished goal" in res.error

    # Allows when existing is complete
    complete_state = GoalState(goal_id="g1", status="complete", objective="Done", created_at=now, updated_at=now)
    services.replace_state(complete_state.to_dict(), expected_version=1)

    res2 = tool.invoke(
        ToolCall(tool_name="create_goal", arguments={"objective": "New Task", "token_budget": 5000}),
        context=ctx,
        services=services,
    )
    assert isinstance(res2, ToolSuccess)
    new_state_data, ver = services.get_state("session")
    assert new_state_data is not None
    assert new_state_data["objective"] == "New Task"
    assert new_state_data["token_budget"] == 5000
    assert services.budget is not None
    assert services.budget.max_total_tokens == 5000
    assert services.budget.since_sequence == 3
    # The new goal owns a fresh accounting window starting at the current tip.
    assert new_state_data["active_windows"] == [{"start_sequence": 3, "end_sequence": None}]


@pytest.mark.parametrize("repetitions", [120, 250, 5000])
def test_create_goal_tool_preserves_unicode_objective_without_workspace_mutation(tmp_path: Path, repetitions: int) -> None:
    services = FakeServices(through_sequence=5)
    ctx = _make_context(tmp_path)
    objective = " \n" + ("🎯 完全自动化端到端测试与真实验证。\n" * repetitions) + "Final acceptance.\n\t"
    res = CreateGoalTool().invoke(
        ToolCall(tool_name="create_goal", arguments={"objective": objective}),
        context=ctx,
        services=services,
    )
    assert isinstance(res, ToolSuccess)
    state, _ = services.get_state("session")
    assert state is not None
    if repetitions > 120:
        assert state["objective_file_ref"].startswith("artifact://")
    loaded = GoalLoadObjectiveCommand().invoke(CommandInvocation(context=ctx, name="goal.load_objective"), services)
    assert loaded.status == "ok", loaded.error
    assert loaded.data["objective"] == objective
    assert list(tmp_path.iterdir()) == []


def test_update_goal_tool_complete(tmp_path: Path) -> None:
    tool = UpdateGoalTool()
    services = FakeServices(through_sequence=12)
    ctx = _make_context(tmp_path)
    now = int(time.time() * 1000)

    state = GoalState(
        goal_id="g1",
        status="active",
        objective="Deploy app",
        created_at=now,
        updated_at=now,
        token_budget=2000,
        tokens_used=500,
        active_windows=(ActiveWindow(start_sequence=0),),
    )
    services.replace_state(state.to_dict(), expected_version=0)

    res = tool.invoke(ToolCall(tool_name="update_goal", arguments={"status": "complete"}), context=ctx, services=services)
    assert isinstance(res, ToolSuccess)
    updated, _ = services.get_state("session")
    assert updated is not None
    assert updated["status"] == "complete"
    # Completion releases the core budget constraint for this owner.
    assert services.budget is None


def test_update_goal_tool_3_turn_blocked_discipline(tmp_path: Path) -> None:
    tool = UpdateGoalTool()
    services = FakeServices(through_sequence=10)
    now = int(time.time() * 1000)

    state = GoalState(goal_id="g1", status="active", objective="Hard task", created_at=now, updated_at=now)
    services.replace_state(state.to_dict(), expected_version=0)

    # Attempt 1: logical turn 1 -> returns ToolFailure, increments audit count to 1
    ctx1 = _make_context(tmp_path, logical_turn="turn-1")
    res1 = tool.invoke(ToolCall(tool_name="update_goal", arguments={"status": "blocked"}), context=ctx1, services=services)
    assert isinstance(res1, ToolFailure)
    assert "currently 1" in res1.error
    st1, _ = services.get_state("session")
    assert st1["blocked_turns_count"] == 1
    assert st1["status"] == "active"  # Staged audit proposal committed, not blocked yet

    # Repeated native call in same logical turn (turn-1) -> does NOT increment!
    res1_repeat = tool.invoke(ToolCall(tool_name="update_goal", arguments={"status": "blocked"}), context=ctx1, services=services)
    assert isinstance(res1_repeat, ToolFailure)
    assert "currently 1" in res1_repeat.error
    st1_repeat, _ = services.get_state("session")
    assert st1_repeat["blocked_turns_count"] == 1

    # Attempt 2: logical turn 2 -> increments to 2
    ctx2 = _make_context(tmp_path, logical_turn="turn-2")
    res2 = tool.invoke(ToolCall(tool_name="update_goal", arguments={"status": "blocked"}), context=ctx2, services=services)
    assert isinstance(res2, ToolFailure)
    assert "currently 2" in res2.error

    # Attempt 3: logical turn 3 -> genuine 3 distinct turns verified! Transitions to blocked.
    ctx3 = _make_context(tmp_path, logical_turn="turn-3")
    res3 = tool.invoke(ToolCall(tool_name="update_goal", arguments={"status": "blocked"}), context=ctx3, services=services)
    assert isinstance(res3, ToolSuccess)
    st3, _ = services.get_state("session")
    assert st3["status"] == "blocked"
    assert services.budget is None


# --- 4. Commands and Confirmation Policy ---


def test_commands_set_edit_pause_resume_clear(tmp_path: Path) -> None:
    services = FakeServices(through_sequence=5)
    ctx_client = _make_context(tmp_path, actor="client")
    ctx_user = _make_context(tmp_path, actor="user")

    # 1. goal.set: initial creation
    set_cmd = GoalSetCommand()
    inv_set = CommandInvocation(context=ctx_client, name="goal.set", arguments={"objective": "Test Goal", "token_budget": 3000})
    res_set = set_cmd.invoke(inv_set, services)
    assert res_set.status == "ok"
    st, ver = services.get_state("session")
    assert st["status"] == "active"
    assert services.budget is not None
    assert services.budget.since_sequence == 5

    # 2. goal.set replacement of UNFINISHED goal by non-user -> returns pending_confirmation!
    inv_replace_client = CommandInvocation(context=ctx_client, name="goal.set", arguments={"objective": "Replace Goal"})
    res_conf = set_cmd.invoke(inv_replace_client, services)
    assert res_conf.status == "pending_confirmation"
    assert res_conf.confirmation is not None
    assert isinstance(res_conf.confirmation, ConfirmationRequest)
    assert "Replace it" in res_conf.confirmation.ui_data["message"]
    # Verify state was NOT modified yet!
    st_unchanged, _ = services.get_state("session")
    assert st_unchanged["objective"] == "Test Goal"

    # Authorized user confirmation path (actor == 'user') executes replacement!
    inv_replace_user = CommandInvocation(context=ctx_user, name="goal.set", arguments={"objective": "Replace Goal"})
    res_replace_ok = set_cmd.invoke(inv_replace_user, services)
    assert res_replace_ok.status == "ok"
    st_replaced, ver = services.get_state("session")
    assert st_replaced["objective"] == "Replace Goal"

    # 3. goal.edit
    edit_cmd = GoalEditCommand()
    inv_edit = CommandInvocation(context=ctx_client, name="goal.edit", arguments={"objective": "Edited Objective"})
    res_edit = edit_cmd.invoke(inv_edit, services)
    assert res_edit.status == "ok"
    st_edited, ver = services.get_state("session")
    assert st_edited["objective"] == "Edited Objective"

    # 4. goal.pause
    pause_cmd = GoalPauseCommand()
    res_pause = pause_cmd.invoke(CommandInvocation(context=ctx_client, name="goal.pause"), services)
    assert res_pause.status == "ok"
    st_paused, ver = services.get_state("session")
    assert st_paused["status"] == "paused"
    assert services.budget is None

    # 5. goal.resume
    resume_cmd = GoalResumeCommand()
    res_resume = resume_cmd.invoke(CommandInvocation(context=ctx_client, name="goal.resume"), services)
    assert res_resume.status == "ok"
    st_resumed, ver = services.get_state("session")
    assert st_resumed["status"] == "active"

    # 6. goal.clear: explicit clear with NO extra confirmation prompt!
    clear_cmd = GoalClearCommand()
    res_clear = clear_cmd.invoke(CommandInvocation(context=ctx_client, name="goal.clear"), services)
    assert res_clear.status == "ok"
    assert res_clear.confirmation is None
    st_cleared, _ = services.get_state("session")
    assert st_cleared is None


def test_commands_edge_cases(tmp_path: Path) -> None:
    services = FakeServices(through_sequence=5)
    ctx = _make_context(tmp_path)
    now = int(time.time() * 1000)

    # Pause on non-existent goal -> error
    pause_cmd = GoalPauseCommand()
    res = pause_cmd.invoke(CommandInvocation(context=ctx, name="goal.pause"), services)
    assert res.status == "error"
    assert "No goal exists" in res.error

    # Complete goal
    complete_goal = GoalState(goal_id="g1", status="complete", objective="Done task", created_at=now, updated_at=now)
    services.replace_state(complete_goal.to_dict(), expected_version=0)

    # Resume on complete goal -> error
    resume_cmd = GoalResumeCommand()
    res_res = resume_cmd.invoke(CommandInvocation(context=ctx, name="goal.resume"), services)
    assert res_res.status == "error"
    assert "Cannot resume" in res_res.error

    # Pause on complete goal -> error
    res_p2 = pause_cmd.invoke(CommandInvocation(context=ctx, name="goal.pause"), services)
    assert res_p2.status == "error"
    assert "Cannot pause" in res_p2.error

    # Replacement of COMPLETE goal does NOT require confirmation even from client actor
    set_cmd = GoalSetCommand()
    res_replace = set_cmd.invoke(
        CommandInvocation(context=ctx, name="goal.set", arguments={"objective": "Next task"}),
        services,
    )
    assert res_replace.status == "ok"
    assert res_replace.confirmation is None
    st, _ = services.get_state("session")
    assert st["objective"] == "Next task"
    assert st["status"] == "active"

    # Load objective on unspilled goal -> returns objective text directly
    load_cmd = GoalLoadObjectiveCommand()
    res_load = load_cmd.invoke(CommandInvocation(context=ctx, name="goal.load_objective"), services)
    assert res_load.status == "ok"
    assert res_load.data["objective"] == "Next task"

    # Clear goal
    clear_cmd = GoalClearCommand()
    res_clear = clear_cmd.invoke(CommandInvocation(context=ctx, name="goal.clear"), services)
    assert res_clear.status == "ok"
    st_cleared, _ = services.get_state("session")
    assert st_cleared is None

    # Load objective on empty session -> error
    res_load_empty = load_cmd.invoke(CommandInvocation(context=ctx, name="goal.load_objective"), services)
    assert res_load_empty.status == "error"
    assert "No goal exists" in res_load_empty.error


def test_goal_edit_with_long_objective_and_budget_adjust(tmp_path: Path) -> None:
    services = FakeServices(through_sequence=1)
    ctx = _make_context(tmp_path)
    now = int(time.time() * 1000)

    state = GoalState(goal_id="g1", status="active", objective="Short objective", created_at=now, updated_at=now, token_budget=1000)
    services.replace_state(state.to_dict(), expected_version=0)

    edit_cmd = GoalEditCommand()

    # 1. Edit with long objective (>4000 chars) -> spills to unique attachment
    huge_objective = "A" * 5000
    res_edit_huge = edit_cmd.invoke(
        CommandInvocation(context=ctx, name="goal.edit", arguments={"objective": huge_objective}),
        services,
    )
    assert res_edit_huge.status == "ok"
    st, _ = services.get_state("session")
    assert st["objective_file_ref"].startswith("artifact://")
    assert not (tmp_path / ".voidcode").exists()
    # 2. Query edit form without arguments -> returns objective and initial values
    res_query = edit_cmd.invoke(
        CommandInvocation(context=ctx, name="goal.edit"),
        services,
    )
    assert res_query.status == "ok"
    # The full objective is served only by the dedicated READ loader; the form descriptor
    # stays bounded and never duplicates it.
    assert "objective" not in res_query.data["initial_values"]
    assert res_query.data["load_action"] == "goal.load_objective"

    load_cmd = GoalLoadObjectiveCommand()
    loaded = load_cmd.invoke(CommandInvocation(context=ctx, name="goal.load_objective"), services)
    assert loaded.status == "ok"
    assert loaded.output is None  # full text crosses the wire exactly once
    assert loaded.data["objective"] == huge_objective

    # 3. Adjust budget to uncap
    res_uncap = edit_cmd.invoke(
        CommandInvocation(context=ctx, name="goal.edit", arguments={"token_budget": None}),
        services,
    )
    assert res_uncap.status == "ok"
    st2, _ = services.get_state("session")
    assert st2["token_budget"] is None
    assert services.budget is None


def test_edit_budget_unknown_lifetime_refuses_finite_raise_and_allows_uncap(tmp_path: Path) -> None:
    """When closed-window usage is unknown or pending, finite remaining cannot be known.
    Setting a finite budget must fail with a visible error until the user explicitly uncaps (null).
    """
    services = FakeServices(through_sequence=9)
    ctx = _make_context(tmp_path)
    now = int(time.time() * 1000)

    limited = GoalState(
        goal_id="g1",
        status="usage-limited",
        objective="Task",
        created_at=now,
        updated_at=now,
        token_budget=1000,
        tokens_used=None,
        window_tokens_used=None,
        window_allowance=1000,
        has_unknown_usage=True,
        active_windows=(ActiveWindow(start_sequence=0),),
    )
    services.replace_state(limited.to_dict(), expected_version=0)

    # 1. Finite budget raise on unknown spend is refused!
    res = GoalEditCommand().invoke(
        CommandInvocation(context=ctx, name="goal.edit", arguments={"token_budget": 4000}),
        services,
    )
    assert res.status == "error"
    assert "prior closed-window usage is unknown" in res.error

    # 2. Explicit uncap (token_budget=null) succeeds!
    res_uncap = GoalEditCommand().invoke(
        CommandInvocation(context=ctx, name="goal.edit", arguments={"token_budget": None}),
        services,
    )
    assert res_uncap.status == "ok"
    st, _ = services.get_state("session")
    assert st["token_budget"] is None
    assert st["tokens_used"] is None  # lifetime unknown is preserved, not faked
    assert st["has_unknown_usage"] is True  # history stays visible


# --- 5. Observer Fact Processing & Budget Constraints ---


def test_observer_usage_accounting_and_windows(tmp_path: Path) -> None:
    services = FakeServices(through_sequence=10)
    ctx = _make_context(tmp_path)
    now = int(time.time() * 1000)

    # Active goal with window (10, None] and a fresh allowance
    state = GoalState(
        goal_id="g1",
        status="active",
        objective="Run pipeline",
        created_at=now,
        updated_at=now,
        token_budget=1000,
        tokens_used=0,
        window_tokens_used=0,
        window_allowance=1000,
        active_windows=(ActiveWindow(start_sequence=10),),
    )
    services.replace_state(state.to_dict(), expected_version=0)

    # Usage fact dispatched before goal (dispatch_sequence=5 <= 10) -> IGNORED!
    fact_prior = UsageFact(
        session_id="s1",
        run_id="r1",
        revision_id="rev1",
        logical_turn="turn-1",
        event_sequence=1,
        dispatch_sequence=5,
        provider_name="test",
        model_name="m1",
        usage=ProviderTokenUsage(input_tokens=100, output_tokens=50),
    )
    goal_observer(fact_prior, ctx, services)
    st, _ = services.get_state("session")
    assert st["tokens_used"] == 0  # Not charged!

    # Usage fact dispatched during active window (dispatch_sequence=12 > 10) -> CHARGED!
    fact_active = UsageFact(
        session_id="s1",
        run_id="r1",
        revision_id="rev1",
        logical_turn="turn-1",
        event_sequence=2,
        dispatch_sequence=12,
        provider_name="test",
        model_name="m1",
        usage=ProviderTokenUsage(input_tokens=200, output_tokens=100),
    )
    goal_observer(fact_active, ctx, services)
    st, _ = services.get_state("session")
    assert st["tokens_used"] == 300

    # Pause goal at dispatch sequence 20
    services.through_sequence = 20
    pause_cmd = GoalPauseCommand()
    pause_cmd.invoke(CommandInvocation(context=ctx, name="goal.pause"), services)

    # Late usage arriving after pause, but dispatched at seq 15 (within closed window (10, 20]) -> STILL CHARGED!
    fact_late = UsageFact(
        session_id="s1",
        run_id="r1",
        revision_id="rev1",
        logical_turn="turn-1",
        event_sequence=3,
        dispatch_sequence=15,
        provider_name="test",
        model_name="m1",
        usage=ProviderTokenUsage(input_tokens=50, output_tokens=50),
    )
    goal_observer(fact_late, ctx, services)
    st, _ = services.get_state("session")
    assert st["tokens_used"] == 400

    # Unreported usage arrives for active window -> transitions to usage-limited!
    fact_unknown = UsageFact(
        session_id="s1",
        run_id="r1",
        revision_id="rev1",
        logical_turn="turn-1",
        event_sequence=4,
        dispatch_sequence=16,
        provider_name="test",
        model_name="m1",
        usage=None,
    )
    goal_observer(fact_unknown, ctx, services)
    st, _ = services.get_state("session")
    assert st["has_unknown_usage"] is True
    # While PAUSED count lifetime only, never rearm/global block (status remains paused)
    assert st["status"] == "paused"

    # But later resume is refused because closed-window usage is unknown!
    resume_cmd = GoalResumeCommand()
    res_resume = resume_cmd.invoke(CommandInvocation(context=ctx, name="goal.resume"), services)
    assert res_resume.status == "error"
    assert "prior usage is unknown or pending" in res_resume.error


def test_observer_unreported_metrics_never_become_zero(tmp_path: Path) -> None:
    """Unreported dispatch: token metrics go unknown and a known later fact cannot resurrect them as zero."""
    services = FakeServices(through_sequence=5)
    ctx = _make_context(tmp_path)
    now = int(time.time() * 1000)

    state = GoalState(
        goal_id="g1",
        status="active",
        objective="Task",
        created_at=now,
        updated_at=now,
        tokens_used=0,
        window_tokens_used=0,
        window_allowance=None,
        active_windows=(ActiveWindow(start_sequence=5),),
    )
    services.replace_state(state.to_dict(), expected_version=0)

    # Unreported dispatch still carries measured elapsed (accounted) but unknown cost.
    fact_unknown = UsageFact(
        session_id="s1",
        run_id="r1",
        revision_id="rev1",
        logical_turn="turn-1",
        event_sequence=1,
        dispatch_sequence=6,
        provider_name="test",
        model_name="m1",
        usage=None,
        elapsed_seconds=1.5,
        cost_usd=None,
    )
    goal_observer(fact_unknown, ctx, services)
    st, _ = services.get_state("session")
    assert st["tokens_used"] is None
    assert st["has_unknown_usage"] is True
    assert st["time_used_seconds"] == 1.5  # real measured time, not 0
    assert st["cost_usd"] is None  # unknown cost stays unknown

    # A later known fact cannot turn the unknown totals back into numbers.
    fact_known = UsageFact(
        session_id="s1",
        run_id="r1",
        revision_id="rev1",
        logical_turn="turn-1",
        event_sequence=2,
        dispatch_sequence=7,
        provider_name="test",
        model_name="m1",
        usage=ProviderTokenUsage(input_tokens=10, output_tokens=5),
        elapsed_seconds=2.0,
        cost_usd=0.25,
    )
    goal_observer(fact_known, ctx, services)
    st2, _ = services.get_state("session")
    assert st2["tokens_used"] is None
    assert st2["time_used_seconds"] == 3.5  # 1.5 + 2.0
    assert st2["cost_usd"] is None  # unknown cost never becomes a partial total


def test_observer_unknown_cost_does_not_poison_tokens(tmp_path: Path) -> None:
    """A missing cost or cache metric must not falsify known billable tokens."""
    services = FakeServices(through_sequence=5)
    ctx = _make_context(tmp_path)
    now = int(time.time() * 1000)

    state = GoalState(
        goal_id="g1",
        status="active",
        objective="Task",
        created_at=now,
        updated_at=now,
        tokens_used=0,
        window_tokens_used=0,
        window_allowance=None,
        active_windows=(ActiveWindow(start_sequence=5),),
    )
    services.replace_state(state.to_dict(), expected_version=0)

    fact = UsageFact(
        session_id="s1",
        run_id="r1",
        revision_id="rev1",
        logical_turn="turn-1",
        event_sequence=1,
        dispatch_sequence=6,
        provider_name="test",
        model_name="m1",
        usage=ProviderTokenUsage(input_tokens=10, output_tokens=5, cost_usd=None),
        elapsed_seconds=0.5,
        cost_usd=None,
    )
    goal_observer(fact, ctx, services)
    st, _ = services.get_state("session")
    assert st["tokens_used"] == 15
    assert st["cost_usd"] is None
    assert st["has_unknown_usage"] is False


def test_run_finished_missing_elapsed_keeps_time_unknown(tmp_path: Path) -> None:
    """Run-finished never fabricates a zero elapsed and never double counts measured dispatch time."""
    services = FakeServices()
    ctx = _make_context(tmp_path)
    now = int(time.time() * 1000)

    unknown_time = GoalState(
        goal_id="g1",
        status="active",
        objective="Task",
        created_at=now,
        updated_at=now,
        time_used_seconds=None,
    )
    services.replace_state(unknown_time.to_dict(), expected_version=0)
    goal_observer(
        RunFinishedFact(
            session_id="s1",
            run_id="r1",
            revision_id="rev1",
            logical_turn="turn-1",
            event_sequence=1,
            status="completed",
            stop_cause="completed",
            elapsed_seconds=None,
        ),
        ctx,
        services,
    )
    st, _ = services.get_state("session")
    assert st["time_used_seconds"] is None  # not 0.0

    # Measured dispatch time is not inflated by the run-level elapsed.
    known_time = GoalState(
        goal_id="g1",
        status="active",
        objective="Task",
        created_at=now,
        updated_at=now,
        time_used_seconds=4.0,
    )
    _, ver = services.get_state("session")
    services.replace_state(known_time.to_dict(), expected_version=ver)
    goal_observer(
        RunFinishedFact(
            session_id="s1",
            run_id="r1",
            revision_id="rev1",
            logical_turn="turn-2",
            event_sequence=2,
            status="completed",
            stop_cause="completed",
            elapsed_seconds=9.0,
        ),
        ctx,
        services,
    )
    st2, _ = services.get_state("session")
    assert st2["time_used_seconds"] == 4.0


def test_state_parse_rejects_coercion_and_missing_keys() -> None:
    now = int(time.time() * 1000)
    base = GoalState(goal_id="g1", status="active", objective="Task", created_at=now, updated_at=now).to_dict()

    coerced = dict(base)
    coerced["has_unknown_usage"] = 1
    with pytest.raises(ValueError, match="boolean"):
        GoalState.from_dict(coerced)

    coerced_count = dict(base)
    coerced_count["blocked_turns_count"] = True
    with pytest.raises(ValueError, match="non-negative integer"):
        GoalState.from_dict(coerced_count)

    missing = dict(base)
    del missing["tokens_used"]
    with pytest.raises(ValueError, match="tokens_used"):
        GoalState.from_dict(missing)

    missing_window = dict(base)
    del missing_window["window_allowance"]
    with pytest.raises(ValueError, match="window_allowance"):
        GoalState.from_dict(missing_window)

    non_artifact = dict(base)
    non_artifact["objective_file_ref"] = ".voidcode/attachments/old/goal-objective.md"
    non_artifact["objective_file_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="artifact:// URI"):
        GoalState.from_dict(non_artifact)

    uncorrelated = dict(base)
    uncorrelated["objective_file_ref"] = "artifact://" + "0" * 64
    with pytest.raises(ValueError, match="both be set or both be null"):
        GoalState.from_dict(uncorrelated)

    null_is_kept = dict(base)
    null_is_kept["tokens_used"] = None
    null_is_kept["time_used_seconds"] = None
    parsed = GoalState.from_dict(null_is_kept)
    assert parsed is not None
    assert parsed.tokens_used is None
    assert parsed.time_used_seconds is None


def test_observer_reset_blocked_count_on_tool_success(tmp_path: Path) -> None:
    services = FakeServices()
    ctx = _make_context(tmp_path)
    now = int(time.time() * 1000)

    state = GoalState(
        goal_id="g1",
        status="active",
        objective="Task",
        created_at=now,
        updated_at=now,
        blocked_turns_count=2,
        last_blocked_logical_turn="turn-2",
    )
    services.replace_state(state.to_dict(), expected_version=0)

    # Own tool invocation (e.g. get_goal) does NOT reset blocked count
    fact_get_goal = InvocationFinishedFact(
        session_id="s1",
        run_id="r1",
        revision_id="rev1",
        logical_turn="turn-3",
        event_sequence=1,
        tool_name="get_goal",
        tool_call_id="call-1",
        status="ok",
    )
    goal_observer(fact_get_goal, ctx, services)
    st, _ = services.get_state("session")
    assert st["blocked_turns_count"] == 2

    # Meaningful execution tool (e.g. write) success RESETS blocked count to 0!
    fact_write = InvocationFinishedFact(
        session_id="s1",
        run_id="r1",
        revision_id="rev1",
        logical_turn="turn-3",
        event_sequence=2,
        tool_name="write",
        tool_call_id="call-2",
        status="ok",
    )
    goal_observer(fact_write, ctx, services)
    st, _ = services.get_state("session")
    assert st["blocked_turns_count"] == 0
    assert st["last_blocked_logical_turn"] is None


def test_observer_run_finished_stop_causes(tmp_path: Path) -> None:
    services = FakeServices()
    ctx = _make_context(tmp_path)
    now = int(time.time() * 1000)

    # Active goal
    state = GoalState(goal_id="g1", status="active", objective="Task", created_at=now, updated_at=now)
    services.replace_state(state.to_dict(), expected_version=0)

    # 1. user_stop: pauses goal and does NOT auto-enqueue continuation
    fact_user_stop = RunFinishedFact(
        session_id="s1",
        run_id="r1",
        revision_id="rev1",
        logical_turn="turn-1",
        event_sequence=1,
        status="interrupted",
        stop_cause="user_stop",
    )
    goal_observer(fact_user_stop, ctx, services)
    st, _ = services.get_state("session")
    assert st["status"] == "paused"

    # 2. shutdown: keeps active status (does not fake blocked) and does not auto-enqueue
    _, ver = services.get_state("session")
    state2 = GoalState(goal_id="g1", status="active", objective="Task", created_at=now, updated_at=now)
    services.replace_state(state2.to_dict(), expected_version=ver)
    fact_shutdown = RunFinishedFact(
        session_id="s1",
        run_id="r1",
        revision_id="rev1",
        logical_turn="turn-2",
        event_sequence=2,
        status="interrupted",
        stop_cause="shutdown",
    )
    goal_observer(fact_shutdown, ctx, services)
    st2, _ = services.get_state("session")
    assert st2["status"] == "active"  # Preserved active for core recovery!

    # 3. completed: auto-enqueues continuation
    fact_completed = RunFinishedFact(
        session_id="s1",
        run_id="r1",
        revision_id="rev1",
        logical_turn="turn-3",
        event_sequence=3,
        status="completed",
        stop_cause="completed",
    )
    goal_observer(fact_completed, ctx, services)
    st3, _ = services.get_state("session")
    assert st3["status"] == "active"  # a completed turn on an active goal keeps pursuing it


# --- 6. Context Contribution & Views ---


def test_consumer_edit_flow_preserves_identity_and_usage(tmp_path: Path) -> None:
    """The real edit flow: registered descriptor -> the plugin's own action -> loader round trip.

    The form action is dispatched through the plugin instance the host would register, so a
    mis-registered action fails here rather than matching a hardcoded class name. Editing a
    large Unicode objective must preserve identity, creation time and the usage counters the
    goal already carries (charged usage and measured time are seeded here; their truthfulness
    against core facts is an integration concern, not this unit boundary).
    """
    services = FakeServices(through_sequence=4)
    ctx = _make_context(tmp_path)
    plugin = create_plugin({})
    commands = {command.definition.name: command for command in plugin.commands}

    original = "\U0001f680 原始目标：验证多行与 Unicode 保持完整。\n" * 200 + "尾部标记"
    assert len(original) > 4000

    set_res = commands["goal.set"].invoke(
        CommandInvocation(context=ctx, name="goal.set", arguments={"objective": original, "token_budget": 9000}),
        services,
    )
    assert set_res.status == "ok"
    created_id = set_res.data["goal"]["goal_id"]
    created_at = set_res.data["goal"]["created_at"]

    # Seed the counters an already-charged goal would carry after its first turn.
    state, _ = goal_plugin._store(services).load()
    assert state is not None
    charged = replace(
        state,
        tokens_used=1500,
        window_tokens_used=1500,
        time_used_seconds=12.5,
        cost_usd=0.02,
    )
    _, ver = services.get_state("session")
    services.replace_state(charged.to_dict(), expected_version=ver)

    # Dispatch exactly what the registered descriptor advertises: a mis-registered action
    # raises here rather than matching a hardcoded class name.
    form = next(view for view in plugin.views if view.name == "goal.form").render("s1", services)
    action = str(form.data["action"])
    load_action = str(form.data["load_action"])

    loaded = commands[load_action].invoke(CommandInvocation(context=ctx, name=load_action), services)
    assert loaded.data["objective"] == original

    edited_text = loaded.data["objective"] + "\n附加要求：保持全部原始约束。"
    edit_res = commands[action].invoke(
        CommandInvocation(context=ctx, name=action, arguments={"objective": edited_text}),
        services,
    )
    assert edit_res.status == "ok"
    edited = edit_res.data["goal"]

    assert edited["goal_id"] == created_id
    assert edited["created_at"] == created_at
    assert edited["tokens_used"] == 1500
    assert edited["time_used_seconds"] == 12.5
    assert edited["cost_usd"] == 0.02
    assert edited["token_budget"] == 9000
    assert edited["objective_file_ref"] != state.objective_file_ref

    reloaded = commands[load_action].invoke(CommandInvocation(context=ctx, name=load_action), services)
    assert reloaded.data["objective"] == edited_text


def test_update_goal_blocked_without_turn_identity_never_credits_a_turn(tmp_path: Path) -> None:
    """A missing logical turn identity must not be credited as an impasse turn.

    A native batch that repeats `blocked` without identifiable turns must never accumulate
    the audit counter; the tool refuses explicitly instead of fabricating turns.
    """
    tool = UpdateGoalTool()
    services = FakeServices(through_sequence=3)
    now = int(time.time() * 1000)
    state = GoalState(goal_id="g1", status="active", objective="Noisy batch", created_at=now, updated_at=now)
    services.replace_state(state.to_dict(), expected_version=0)

    ctx = _make_context(tmp_path, logical_turn="")
    for _ in range(3):
        res = tool.invoke(ToolCall(tool_name="update_goal", arguments={"status": "blocked"}), context=ctx, services=services)
        assert isinstance(res, ToolFailure)

    st, _ = services.get_state("session")
    assert st["blocked_turns_count"] == 0
    assert st["status"] == "active"


def test_prepare_deactivation_pauses_live_goal_at_the_authentic_tip() -> None:
    """A disable pauses a live goal and closes its open window, rewriting nothing else."""
    now = int(time.time() * 1000)
    goal = GoalState(
        goal_id="g-live",
        status="active",
        objective="Ship the cutover",
        created_at=now - 60_000,
        updated_at=now,
        token_budget=5000,
        tokens_used=1234,
        window_tokens_used=234,
        window_allowance=3766,
        has_unknown_usage=True,
        time_used_seconds=42.5,
        cost_usd=0.75,
        active_windows=(ActiveWindow(start_sequence=7, end_sequence=11), ActiveWindow(start_sequence=11)),
        blocked_turns_count=2,
        last_blocked_logical_turn="turn-7",
        objective_edited=True,
    )

    prepared = prepare_goal_deactivation(goal.to_dict(), "session", "session-1", 42)

    assert prepared["status"] == "paused"
    assert prepared["active_windows"] == [
        {"start_sequence": 7, "end_sequence": 11},
        {"start_sequence": 11, "end_sequence": 42},
    ]
    # No clock in the transform: the paused copy carries the timestamps the state already had.
    assert prepared["updated_at"] == goal.updated_at
    assert prepared["created_at"] == goal.created_at

    decoded = GoalState.from_dict(prepared)
    assert decoded is not None
    assert decoded == replace(
        goal,
        status="paused",
        active_windows=(ActiveWindow(start_sequence=7, end_sequence=11), ActiveWindow(start_sequence=11, end_sequence=42)),
    )


def test_prepare_deactivation_status_and_empty_state_semantics() -> None:
    """Live statuses become paused; `complete` and an empty state are never rewritten."""
    now = int(time.time() * 1000)
    live_statuses: tuple[GoalStatus, ...] = ("active", "blocked", "usage-limited", "budget-limited")
    for status in live_statuses:
        goal = GoalState(
            goal_id=f"g-{status}",
            status=status,
            objective="Task",
            created_at=now,
            updated_at=now,
            active_windows=(ActiveWindow(start_sequence=3),),
        )
        prepared = prepare_goal_deactivation(goal.to_dict(), "session", "session-1", 9)
        assert prepared["status"] == "paused"
        assert prepared["goal_id"] == goal.goal_id
        assert prepared["active_windows"] == [{"start_sequence": 3, "end_sequence": 9}]

    # A tip equal to the window start closes the window the way `goal.pause` does.
    just_opened = GoalState(
        goal_id="g-boundary",
        status="active",
        objective="Task",
        created_at=now,
        updated_at=now,
        active_windows=(ActiveWindow(start_sequence=3),),
    )
    at_start = prepare_goal_deactivation(just_opened.to_dict(), "session", "session-1", 3)
    assert at_start["active_windows"] == [{"start_sequence": 3, "end_sequence": 3}]

    complete = GoalState(
        goal_id="g-complete",
        status="complete",
        objective="Done",
        created_at=now,
        updated_at=now,
        active_windows=(ActiveWindow(start_sequence=1, end_sequence=5),),
    )
    assert prepare_goal_deactivation(complete.to_dict(), "session", "session-1", 9) == complete.to_dict()

    # An owner without a goal must not gain one from a disable.
    assert prepare_goal_deactivation({}, "session", "session-1", 9) == {}
    assert prepare_goal_deactivation({}, "workspace", "workspace-1", None) == {}


def test_prepare_deactivation_rejects_malformed_input_instead_of_resetting() -> None:
    """A malformed scope, tip or state fails visibly; it never silently resets the goal."""
    now = int(time.time() * 1000)
    base = GoalState(
        goal_id="g1",
        status="active",
        objective="Task",
        created_at=now,
        updated_at=now,
        active_windows=(ActiveWindow(start_sequence=4),),
    ).to_dict()

    with pytest.raises(ValueError, match="scope"):
        prepare_goal_deactivation(base, "galaxy", "session-1", 8)
    # The scope/tip contract holds regardless of status, windows or an empty state.
    with pytest.raises(ValueError, match="non-negative integer"):
        prepare_goal_deactivation(base, "session", "session-1", True)
    with pytest.raises(ValueError, match="non-negative integer"):
        prepare_goal_deactivation(base, "session", "session-1", None)
    with pytest.raises(ValueError, match="non-negative integer"):
        prepare_goal_deactivation(base, "session", "session-1", -1)
    with pytest.raises(ValueError, match="non-negative integer"):
        prepare_goal_deactivation({}, "session", "session-1", None)
    with pytest.raises(ValueError, match="no single session event tip"):
        prepare_goal_deactivation({}, "workspace", "workspace-1", 8)
    # A stale or corrupt tip must not close a window before it started.
    with pytest.raises(ValueError, match="earlier point"):
        prepare_goal_deactivation(base, "session", "session-1", 3)
    with pytest.raises(ValueError, match="status"):
        prepare_goal_deactivation({**base, "status": "sleeping"}, "session", "session-1", 8)
    with pytest.raises(ValueError, match="unknown extra fields"):
        prepare_goal_deactivation({**base, "unknown_flag": True}, "session", "session-1", 8)
    incomplete = {key: value for key, value in base.items() if key != "window_allowance"}
    with pytest.raises(ValueError, match="window_allowance"):
        prepare_goal_deactivation(incomplete, "session", "session-1", 8)
    # A workspace-scope row carries no session tip, so an open window cannot be closed truthfully.
    with pytest.raises(ValueError, match="event tip"):
        prepare_goal_deactivation(base, "workspace", "workspace-1", None)


@pytest.mark.parametrize("budget", [0, 1000], ids=["invalid-budget", "unknown-prior-usage"])
def test_rejected_goal_edit_does_not_write_objective_attachment(tmp_path: Path, budget: int) -> None:
    services = FakeServices()
    goal = GoalState(
        goal_id="g1",
        status="active",
        objective="Original",
        created_at=1,
        updated_at=1,
        tokens_used=None,
        has_unknown_usage=True,
    )
    services.replace_state(goal.to_dict(), expected_version=0)
    before = services.get_state()
    result = GoalEditCommand().invoke(
        CommandInvocation(
            context=_make_context(tmp_path),
            name="goal.edit",
            arguments={"objective": "A" * 5000, "token_budget": budget},
        ),
        services,
    )
    assert result.status == "error"
    assert services.get_state() == before
    assert not (tmp_path / ".voidcode").exists()
    assert not services.staged_events
    assert services.budget is None


@pytest.mark.parametrize("closed", [False, True], ids=["open", "closed"])
@pytest.mark.parametrize(
    "usage",
    [None, ProviderTokenUsage(input_tokens=20), ProviderTokenUsage(input_tokens=20, output_tokens=5)],
    ids=["absent", "partial", "reported"],
)
def test_usage_window_updates_share_truthful_lifetime_metrics(
    tmp_path: Path,
    closed: bool,
    usage: ProviderTokenUsage | None,
) -> None:
    services = FakeServices()
    goal = GoalState(
        goal_id="g1",
        status="paused" if closed else "active",
        objective="Task",
        created_at=1,
        updated_at=1,
        token_budget=25,
        window_allowance=25,
        active_windows=(ActiveWindow(start_sequence=0, end_sequence=10 if closed else None),),
    )
    services.replace_state(goal.to_dict(), expected_version=0)
    fact = UsageFact(
        session_id="s1",
        run_id="r1",
        revision_id="rev1",
        logical_turn="turn1",
        event_sequence=12,
        dispatch_sequence=5,
        provider_name="test",
        model_name="m1",
        usage=usage,
        elapsed_seconds=1.5,
        cost_usd=0.1,
    )
    goal_observer(fact, _make_context(tmp_path), services)
    state, _ = services.get_state()
    assert state is not None
    known_total = usage is not None and usage.output_tokens is not None
    assert state["tokens_used"] == (25 if known_total else None)
    assert state["has_unknown_usage"] is (not known_total)
    assert state["time_used_seconds"] == 1.5
    assert state["cost_usd"] == 0.1
    assert state["window_tokens_used"] == (0 if closed else state["tokens_used"])
    if closed:
        assert state["status"] == "paused"
        assert not services.staged_events
        assert not services.cancelled_continuations
        assert services.budget is None
    else:
        assert state["status"] == ("budget-limited" if known_total else "usage-limited")
        assert len(services.staged_events) == 1
        assert len(services.cancelled_continuations) == 1


@pytest.mark.parametrize(
    "status, expected",
    [
        ("active", {"goal.edit", "goal.pause", "goal.clear"}),
        ("paused", {"goal.edit", "goal.resume", "goal.clear"}),
        ("blocked", {"goal.edit", "goal.pause", "goal.resume", "goal.clear"}),
        ("budget-limited", {"goal.edit", "goal.pause", "goal.resume", "goal.clear"}),
        ("complete", {"goal.edit", "goal.clear"}),
    ],
)
def test_goal_details_offer_applicable_actions_without_mutation(tmp_path: Path, status: GoalStatus, expected: set[str]):
    from voidcode.plugins.goal.goal_plugin import GoalCommand
    from voidcode.sdk.views import MarkdownViewData

    services = FakeServices()
    goal = GoalState(goal_id="g1", status=status, objective="🙂" * 7000, created_at=1, updated_at=1)
    services.replace_state(goal.to_dict(), expected_version=0)
    before = services.get_state("session")
    result = GoalCommand().invoke(CommandInvocation(context=_make_context(tmp_path), name="goal"), services)
    detail = MarkdownViewData.from_payload(result.presentation.data)
    assert {action.action for action in detail.actions} == expected
    assert next(action for action in detail.actions if action.action == "goal.edit").prepare_input
    assert "Unbounded" in detail.content
    assert "Objective preview" in detail.content
    assert services.get_state("session") == before


# --- 7. Accounting, State-Schema, and Lifecycle Regressions ---


@dataclass
class _ClosedSpendServices(FakeServices):
    """Fake core usage whose closed-window spend is fixed for budget-edit tests."""

    closed_spend: int = 0

    def get_usage(self, *, since_sequence: int = 0, until_sequence: int | None = None) -> SessionUsageReport:
        return SessionUsageReport(
            session_id=self.session_id,
            input_tokens=self.closed_spend,
            output_tokens=0,
            cache_read_tokens=0,
            uncached_input_tokens=0,
            has_unknown_usage=False,
            through_sequence=self.through_sequence,
            since_sequence=since_sequence,
            until_sequence=until_sequence,
        )


@dataclass
class _QueueServices(FakeServices):
    """Fake continuation queue that honours cancel-then-enqueue and keyed dedupe."""

    pending_pursuits: dict[str, tuple[dict[str, object], str]] = field(default_factory=dict)

    def stage_continuation(
        self,
        payload: Mapping[str, object],
        *,
        idempotency_key: str | None = None,
        role: str = "system",
    ) -> str:
        key = idempotency_key or f"anonymous-{len(self.staged_continuations)}"
        if key not in self.pending_pursuits:
            self.pending_pursuits[key] = (dict(payload), role)
            self.staged_continuations.append((dict(payload), idempotency_key, role))
        return key

    enqueue_continuation = stage_continuation

    def cancel_continuations(self, *, continuation_id: str | None = None) -> None:
        self.cancelled_continuations.append(continuation_id)
        if continuation_id is None:
            self.pending_pursuits.clear()
        else:
            self.pending_pursuits.pop(continuation_id, None)


class _StateCopySource:
    def __init__(self, row: PluginStateRow) -> None:
        self._row = row

    def plugin_state_rows(self, scope: str, scope_key: str) -> tuple[PluginStateRow, ...]:
        return (self._row,) if (scope, scope_key) == ("session", "session-1") else ()

    def session(self, session_id: str) -> SimpleNamespace:
        _ = session_id
        return SimpleNamespace(last_event_sequence=10)


def _deactivate_copy(owner: str, payload: Mapping[str, object], scope: str, scope_key: str, tip: int | None) -> Mapping[str, object]:
    _ = (owner, scope, scope_key, tip)
    return payload


@pytest.mark.parametrize("new_budget", [900, 1000], ids=["below-spend", "equal-to-spend"])
def test_goal_edit_refuses_finite_budget_at_or_below_closed_spend(tmp_path: Path, new_budget: int) -> None:
    services = _ClosedSpendServices(through_sequence=20, closed_spend=1000)
    goal = GoalState(
        goal_id="g1",
        status="active",
        objective="Task",
        created_at=1,
        updated_at=1,
        token_budget=2000,
        tokens_used=1000,
        window_tokens_used=0,
        window_allowance=1000,
        active_windows=(ActiveWindow(0, 10), ActiveWindow(10)),
    )
    services.replace_state(goal.to_dict(), expected_version=0)
    before = services.get_state()
    result = GoalEditCommand().invoke(
        CommandInvocation(context=_make_context(tmp_path), name="goal.edit", arguments={"token_budget": new_budget}),
        services,
    )
    assert result.status == "error"
    assert "at or below already spent tokens" in result.error
    assert services.get_state() == before
    assert services.budget is None
    assert not services.staged_events


def test_goal_edit_refuses_finite_budget_underfunding_the_open_window(tmp_path: Path) -> None:
    services = _ClosedSpendServices(through_sequence=20, closed_spend=500)
    goal = GoalState(
        goal_id="g1",
        status="active",
        objective="Task",
        created_at=1,
        updated_at=1,
        token_budget=1000,
        tokens_used=800,
        window_tokens_used=300,
        window_allowance=500,
        active_windows=(ActiveWindow(0, 10), ActiveWindow(10)),
    )
    services.replace_state(goal.to_dict(), expected_version=0)
    before = services.get_state()
    result = GoalEditCommand().invoke(
        CommandInvocation(context=_make_context(tmp_path), name="goal.edit", arguments={"token_budget": 800}),
        services,
    )
    assert result.status == "error"
    assert "no tokens for the current open window" in result.error
    assert services.get_state() == before
    assert services.budget is None

    accepted = GoalEditCommand().invoke(
        CommandInvocation(context=_make_context(tmp_path), name="goal.edit", arguments={"token_budget": 900}),
        services,
    )
    assert accepted.status == "ok"
    assert services.budget is not None
    assert services.budget.max_total_tokens == 400
    assert services.budget.since_sequence == 10
    state, _ = services.get_state("session")
    assert state is not None
    assert state["status"] == "active"
    assert state["window_allowance"] == 400


def test_goal_edit_non_active_budget_edit_matches_resume_refusal_and_allowance(tmp_path: Path) -> None:
    services = _ClosedSpendServices(through_sequence=20, closed_spend=800)
    goal = GoalState(
        goal_id="g1",
        status="paused",
        objective="Task",
        created_at=1,
        updated_at=1,
        token_budget=1000,
        tokens_used=800,
        window_tokens_used=0,
        window_allowance=200,
        active_windows=(ActiveWindow(0, 10),),
    )
    services.replace_state(goal.to_dict(), expected_version=0)
    refused = GoalEditCommand().invoke(
        CommandInvocation(context=_make_context(tmp_path), name="goal.edit", arguments={"token_budget": 800}),
        services,
    )
    assert refused.status == "error"
    assert "at or below already spent tokens" in refused.error

    accepted = GoalEditCommand().invoke(
        CommandInvocation(context=_make_context(tmp_path), name="goal.edit", arguments={"token_budget": 1000}),
        services,
    )
    assert accepted.status == "ok"
    state, _ = services.get_state("session")
    assert state is not None
    assert state["status"] == "paused"
    assert state["window_allowance"] == 200
    assert services.budget is None


def test_create_then_run_finished_leaves_one_pending_pursuit(tmp_path: Path) -> None:
    services = _QueueServices(through_sequence=3)
    ctx = _make_context(tmp_path)
    created = CreateGoalTool().invoke(
        ToolCall(tool_name="create_goal", arguments={"objective": "Task"}),
        context=ctx,
        services=services,
    )
    assert isinstance(created, ToolSuccess)
    assert len(services.pending_pursuits) == 1

    goal_observer(
        RunFinishedFact(
            session_id="session-1",
            run_id="run-1",
            revision_id="rev-1",
            logical_turn="turn-1",
            event_sequence=9,
            status="completed",
            stop_cause="completed",
        ),
        ctx,
        services,
    )
    assert len(services.pending_pursuits) == 1
    payload, _role = next(iter(services.pending_pursuits.values()))
    assert payload["action"] == "pursue_goal"


def test_busy_set_and_resume_leave_one_pending_pursuit(tmp_path: Path) -> None:
    services = _QueueServices(through_sequence=5)
    ctx_user = _make_context(tmp_path, actor="user")
    set_cmd = GoalSetCommand()
    assert set_cmd.invoke(CommandInvocation(context=ctx_user, name="goal.set", arguments={"objective": "Base"}), services).status == "ok"
    assert len(services.pending_pursuits) == 1

    replaced = set_cmd.invoke(CommandInvocation(context=ctx_user, name="goal.set", arguments={"objective": "Replacement"}), services)
    assert replaced.status == "ok"
    assert len(services.pending_pursuits) == 1

    goal_observer(
        RunFinishedFact(
            session_id="session-1",
            run_id="run-1",
            revision_id="rev-1",
            logical_turn="turn-busy",
            event_sequence=12,
            status="completed",
            stop_cause="completed",
        ),
        _make_context(tmp_path),
        services,
    )
    assert len(services.pending_pursuits) == 1

    assert GoalPauseCommand().invoke(CommandInvocation(context=ctx_user, name="goal.pause"), services).status == "ok"
    assert services.pending_pursuits == {}
    resumed = GoalResumeCommand().invoke(CommandInvocation(context=ctx_user, name="goal.resume"), services)
    assert resumed.status == "ok"
    assert len(services.pending_pursuits) == 1
    assert services.pending_pursuits[next(iter(services.pending_pursuits))][0]["action"] == "pursue_goal"

    goal_observer(
        RunFinishedFact(
            session_id="session-1",
            run_id="run-2",
            revision_id="rev-1",
            logical_turn="turn-resumed",
            event_sequence=15,
            status="completed",
            stop_cause="completed",
        ),
        _make_context(tmp_path),
        services,
    )
    assert len(services.pending_pursuits) == 1


def test_goal_state_schema_matches_version_and_allows_cleared_state() -> None:
    plugin = create_plugin({})
    assert plugin.state.version == SCHEMA_VERSION
    assert plugin.state.schema is not None
    goal = GoalState(
        goal_id="g1",
        status="active",
        objective="Task",
        created_at=1,
        updated_at=1,
        active_windows=(ActiveWindow(0),),
    )
    payload = goal.to_dict()
    assert validate_state_payload(payload, plugin.state) == payload
    assert validate_state_payload({}, plugin.state) == {}

    with pytest.raises(ValueError, match="declared state schema"):
        validate_state_payload({**payload, "extra_field": True}, plugin.state)
    missing = dict(payload)
    del missing["goal_id"]
    with pytest.raises(ValueError, match="declared state schema"):
        validate_state_payload(missing, plugin.state)


def test_hot_activation_validates_goal_state_and_cleared_state() -> None:
    plugin = create_plugin({})
    goal = GoalState(
        goal_id="g1",
        status="paused",
        objective="Task",
        created_at=1,
        updated_at=1,
        active_windows=(ActiveWindow(0, 5),),
    )
    for payload in (goal.to_dict(), {}):
        row = PluginStateRow(
            owner="goal",
            scope="session",
            scope_key="session-1",
            payload=payload,
            version=1,
            schema_version=SCHEMA_VERSION,
            revision_id="rev-1",
        )
        plan = plan_state_copies(
            store=_StateCopySource(row),
            workspace="/workspace",
            session_ids=("session-1",),
            old_states={"goal": plugin.state},
            candidate_states={"goal": plugin.state},
            deactivate=_deactivate_copy,
        )
        assert plan.updates == ()

    malformed = PluginStateRow(
        owner="goal",
        scope="session",
        scope_key="session-1",
        payload={"schema_version": SCHEMA_VERSION, "goal_id": "g1"},
        version=1,
        schema_version=SCHEMA_VERSION,
        revision_id="rev-1",
    )
    with pytest.raises(ValueError, match="declared state schema"):
        plan_state_copies(
            store=_StateCopySource(malformed),
            workspace="/workspace",
            session_ids=("session-1",),
            old_states={"goal": plugin.state},
            candidate_states={"goal": plugin.state},
            deactivate=_deactivate_copy,
        )


@pytest.mark.parametrize(
    "window, match",
    [
        ({"start_sequence": 1}, "end_sequence"),
        ({"end_sequence": 1}, "start_sequence"),
        ({"start_sequence": -1, "end_sequence": None}, "non-negative"),
        ({"start_sequence": 2, "end_sequence": -1}, "non-negative"),
        ({"start_sequence": 5, "end_sequence": 4}, "precedes"),
    ],
)
def test_active_window_requires_complete_non_negative_ordered_bounds(window: dict[str, object], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        ActiveWindow.from_dict(window)


def test_parse_goal_windows_enforces_order_single_open_and_no_overlap() -> None:
    with pytest.raises(ValueError, match="ascending start_sequence"):
        goal_plugin._parse_goal_windows([{"start_sequence": 10, "end_sequence": 20}, {"start_sequence": 5, "end_sequence": 6}])
    with pytest.raises(ValueError, match="overlap"):
        goal_plugin._parse_goal_windows([{"start_sequence": 0, "end_sequence": 10}, {"start_sequence": 5, "end_sequence": 20}])
    with pytest.raises(ValueError, match="at most one open window"):
        goal_plugin._parse_goal_windows([{"start_sequence": 0, "end_sequence": None}, {"start_sequence": 5, "end_sequence": None}])
    with pytest.raises(ValueError, match="at most one open window"):
        goal_plugin._parse_goal_windows([{"start_sequence": 0, "end_sequence": None}, {"start_sequence": 5, "end_sequence": 10}])
    assert goal_plugin._parse_goal_windows(
        [
            {"start_sequence": 0, "end_sequence": 5},
            {"start_sequence": 5, "end_sequence": 10},
            {"start_sequence": 10, "end_sequence": None},
        ]
    ) == (ActiveWindow(0, 5), ActiveWindow(5, 10), ActiveWindow(10))
    # A zero-length window and a re-open at the same tip are the pause/resume boundary.
    assert goal_plugin._parse_goal_windows([{"start_sequence": 5, "end_sequence": 5}, {"start_sequence": 5, "end_sequence": None}]) == (
        ActiveWindow(5, 5),
        ActiveWindow(5),
    )


@pytest.mark.parametrize("bad", [[], "", 0, False])
def test_goal_state_distinguishes_cleared_state_from_falsey_non_mappings(bad: object) -> None:
    assert GoalState.from_dict(None) is None
    assert GoalState.from_dict({}) is None
    with pytest.raises(ValueError, match="mapping"):
        GoalState.from_dict(bad)  # type: ignore[arg-type]


def test_state_parse_requires_window_end_key() -> None:
    now = int(time.time() * 1000)
    payload = GoalState(
        goal_id="g1",
        status="active",
        objective="Task",
        created_at=now,
        updated_at=now,
        active_windows=(ActiveWindow(0),),
    ).to_dict()
    windows = payload["active_windows"]
    assert isinstance(windows, list)
    window = dict(windows[0])
    del window["end_sequence"]
    with pytest.raises(ValueError, match="end_sequence"):
        GoalState.from_dict({**payload, "active_windows": [window]})


def test_update_goal_rejects_blocked_from_complete_and_paused(tmp_path: Path) -> None:
    tool = UpdateGoalTool()
    ctx = _make_context(tmp_path)
    for status in ("complete", "paused"):
        services = FakeServices(through_sequence=10)
        goal = GoalState(
            goal_id="g1",
            status=status,
            objective="Task",
            created_at=1,
            updated_at=1,
            active_windows=(ActiveWindow(0, 5),),
        )
        services.replace_state(goal.to_dict(), expected_version=0)
        before = services.get_state()
        result = tool.invoke(ToolCall(tool_name="update_goal", arguments={"status": "blocked"}), context=ctx, services=services)
        assert isinstance(result, ToolFailure)
        assert "cannot mark" in result.error
        assert services.get_state() == before
        assert not services.staged_events


def test_update_goal_repeated_same_status_is_idempotent(tmp_path: Path) -> None:
    tool = UpdateGoalTool()
    ctx = _make_context(tmp_path)

    complete_services = FakeServices(through_sequence=10)
    complete = GoalState(goal_id="g1", status="complete", objective="Done", created_at=1, updated_at=1)
    complete_services.replace_state(complete.to_dict(), expected_version=0)
    before = complete_services.get_state()
    result = tool.invoke(ToolCall(tool_name="update_goal", arguments={"status": "complete"}), context=ctx, services=complete_services)
    assert isinstance(result, ToolSuccess)
    assert result.body.as_payload()["mutated"] is False
    assert complete_services.get_state() == before
    assert not complete_services.staged_events

    blocked_services = FakeServices(through_sequence=10)
    blocked = GoalState(
        goal_id="g2",
        status="blocked",
        objective="Stuck",
        created_at=1,
        updated_at=1,
        blocked_turns_count=3,
        last_blocked_logical_turn="turn-3",
    )
    blocked_services.replace_state(blocked.to_dict(), expected_version=0)
    before_blocked = blocked_services.get_state()
    result_blocked = tool.invoke(ToolCall(tool_name="update_goal", arguments={"status": "blocked"}), context=ctx, services=blocked_services)
    assert isinstance(result_blocked, ToolSuccess)
    assert result_blocked.body.as_payload()["mutated"] is False
    assert blocked_services.get_state() == before_blocked
    assert not blocked_services.staged_events


@pytest.mark.parametrize("status", ["complete", "paused"])
def test_run_finished_noop_does_not_churn_state_or_release_budget(tmp_path: Path, status: GoalStatus) -> None:
    services = FakeServices(through_sequence=10)
    services.set_budget(since_sequence=0, max_total_tokens=1000)
    goal = GoalState(
        goal_id="g1",
        status=status,
        objective="Task",
        created_at=1,
        updated_at=123,
        token_budget=1000,
        window_allowance=900,
        active_windows=(ActiveWindow(0, 5),),
    )
    services.replace_state(goal.to_dict(), expected_version=0)
    before = services.get_state()
    goal_observer(
        RunFinishedFact(
            session_id="session-1",
            run_id="run-1",
            revision_id="rev-1",
            logical_turn="turn-noop",
            event_sequence=20,
            status="completed",
            stop_cause="completed",
        ),
        _make_context(tmp_path),
        services,
    )
    assert services.get_state() == before
    assert services.budget is not None
    assert services.budget.max_total_tokens == 1000
    assert not services.cancelled_continuations
    assert not services.staged_events


def test_budget_only_edit_preserves_blocked_audit_but_objective_edit_resets_it(tmp_path: Path) -> None:
    services = FakeServices(through_sequence=10)
    goal = GoalState(
        goal_id="g1",
        status="paused",
        objective="Task",
        created_at=1,
        updated_at=1,
        blocked_turns_count=2,
        last_blocked_logical_turn="turn-2",
        active_windows=(ActiveWindow(0, 5),),
    )
    services.replace_state(goal.to_dict(), expected_version=0)
    edit_cmd = GoalEditCommand()

    budget_edit = edit_cmd.invoke(
        CommandInvocation(context=_make_context(tmp_path), name="goal.edit", arguments={"token_budget": 5000}),
        services,
    )
    assert budget_edit.status == "ok"
    state, _ = services.get_state("session")
    assert state is not None
    assert state["blocked_turns_count"] == 2
    assert state["last_blocked_logical_turn"] == "turn-2"

    objective_edit = edit_cmd.invoke(
        CommandInvocation(context=_make_context(tmp_path), name="goal.edit", arguments={"objective": "Updated task"}),
        services,
    )
    assert objective_edit.status == "ok"
    state, _ = services.get_state("session")
    assert state is not None
    assert state["blocked_turns_count"] == 0
    assert state["last_blocked_logical_turn"] is None
    assert state["objective_edited"] is True


@pytest.mark.parametrize(
    "status, expected_status",
    [
        ("active", "paused"),
        ("paused", "paused"),
        ("complete", "complete"),
        ("usage-limited", "usage-limited"),
        ("budget-limited", "budget-limited"),
    ],
)
def test_user_stop_only_transitions_and_releases_an_active_goal(tmp_path: Path, status: GoalStatus, expected_status: GoalStatus) -> None:
    services = FakeServices(through_sequence=10)
    services.set_budget(since_sequence=0, max_total_tokens=1000)
    goal = GoalState(
        goal_id="g1",
        status=status,
        objective="Task",
        created_at=1,
        updated_at=123,
        token_budget=1000,
        tokens_used=100,
        window_allowance=900,
        active_windows=(ActiveWindow(0),),
    )
    services.replace_state(goal.to_dict(), expected_version=0)
    before = services.get_state()
    goal_observer(
        RunFinishedFact(
            session_id="session-1",
            run_id="run-1",
            revision_id="rev-1",
            logical_turn="turn-stop",
            event_sequence=30,
            status="interrupted",
            stop_cause="user_stop",
        ),
        _make_context(tmp_path),
        services,
    )
    state, _ = services.get_state("session")
    assert state is not None
    assert state["status"] == expected_status
    if status == "active":
        assert state["active_windows"] == [{"start_sequence": 0, "end_sequence": 30}]
        assert services.budget is None
        assert services.cancelled_continuations
        assert [event[0] for event in services.staged_events] == ["goal_paused"]
    else:
        assert services.get_state() == before
        assert services.budget is not None
        assert services.budget.max_total_tokens == 1000
        assert not services.cancelled_continuations
        assert not services.staged_events


def test_goal_clear_without_goal_clears_stale_budget_and_queue(tmp_path: Path) -> None:
    services = _QueueServices()
    services.set_budget(since_sequence=0, max_total_tokens=500)
    services.enqueue_continuation({"action": "pursue_goal", "goal_id": "stale"}, idempotency_key="stale")
    assert services.pending_pursuits
    result = GoalClearCommand().invoke(CommandInvocation(context=_make_context(tmp_path), name="goal.clear"), services)
    assert result.status == "ok"
    assert result.output == "No goal to clear"
    assert services.budget is None
    assert services.pending_pursuits == {}
    assert services.cancelled_continuations


def test_goal_clear_with_malformed_state_stays_fail_visible(tmp_path: Path) -> None:
    services = FakeServices()
    services.set_budget(since_sequence=0, max_total_tokens=500)
    services.seed_state({"not_a_goal": True})
    with pytest.raises(ValueError, match="unknown extra fields"):
        GoalClearCommand().invoke(CommandInvocation(context=_make_context(tmp_path), name="goal.clear"), services)
    assert services.budget is not None
    assert not services.cancelled_continuations


def test_goal_forms_agree_on_required_and_labels(tmp_path: Path) -> None:
    plugin = create_plugin({})
    form = next(view for view in plugin.views if view.name == "goal.form").render("session-1", FakeServices())
    set_form = GoalSetCommand().prepare_input(CommandInvocation(context=_make_context(tmp_path), name="goal.set"), FakeServices())
    assert form.data["fields"] == set_form.data["fields"]
    fields = {field["name"]: field for field in form.data["fields"]}  # type: ignore[index]
    assert fields["objective"]["label"] == "Objective"
    assert fields["objective"]["required"] is True
    assert fields["token_budget"]["label"] == "Token Budget"
    assert fields["token_budget"]["required"] is False


def test_goal_details_note_truncated_objective_and_pause_reports_remaining(tmp_path: Path) -> None:
    services = FakeServices(through_sequence=5)
    goal = GoalState(
        goal_id="g1",
        status="active",
        objective="X" * 2000,
        created_at=1,
        updated_at=1,
        token_budget=1000,
        tokens_used=200,
        window_tokens_used=200,
        window_allowance=1000,
        active_windows=(ActiveWindow(0),),
    )
    services.replace_state(goal.to_dict(), expected_version=0)
    details = GoalDetailsView().render("session-1", services)
    rows = {row["label"]: row["value"] for row in details.data["rows"]}  # type: ignore[index]
    assert "Objective preview" in rows["Objective"]
    pause = GoalPauseCommand().invoke(CommandInvocation(context=_make_context(tmp_path), name="goal.pause"), services)
    assert pause.status == "ok"
    assert pause.data["remaining_tokens"] == 800


def test_model_query_usage_fact_counts_open_window_once(tmp_path: Path) -> None:
    services = FakeServices(through_sequence=10)
    goal = GoalState(
        goal_id="g1",
        status="active",
        objective="Task",
        created_at=1,
        updated_at=1,
        token_budget=100,
        tokens_used=0,
        window_tokens_used=0,
        window_allowance=100,
        active_windows=(ActiveWindow(10),),
    )
    services.replace_state(goal.to_dict(), expected_version=0)
    goal_observer(
        UsageFact(
            session_id="session-1",
            run_id="run-1",
            revision_id="rev-1",
            logical_turn="turn-query",
            event_sequence=12,
            dispatch_sequence=12,
            provider_name="endpoint",
            model_name="find-judge",
            usage=ProviderTokenUsage(input_tokens=20, output_tokens=5),
        ),
        _make_context(tmp_path),
        services,
    )
    state, _ = services.get_state("session")
    assert state is not None
    assert state["tokens_used"] == 25
    assert state["window_tokens_used"] == 25
    assert state["status"] == "active"


def test_model_query_usage_fact_does_not_double_count_closed_window_spend(tmp_path: Path) -> None:
    services = _ClosedSpendServices(through_sequence=20, closed_spend=100)
    goal = GoalState(
        goal_id="g1",
        status="active",
        objective="Task",
        created_at=1,
        updated_at=1,
        token_budget=200,
        tokens_used=75,
        window_tokens_used=25,
        window_allowance=100,
        active_windows=(ActiveWindow(0, 10), ActiveWindow(10)),
    )
    services.replace_state(goal.to_dict(), expected_version=0)
    goal_observer(
        UsageFact(
            session_id="session-1",
            run_id="run-1",
            revision_id="rev-1",
            logical_turn="turn-query",
            event_sequence=5,
            dispatch_sequence=5,
            provider_name="endpoint",
            model_name="find-judge",
            usage=ProviderTokenUsage(input_tokens=20, output_tokens=5),
        ),
        _make_context(tmp_path),
        services,
    )
    state, _ = services.get_state("session")
    assert state is not None
    # 75 lifetime + 25 from this model query, once; the closed-window total (100)
    # already contains the query and must not be subtracted a second time.
    assert state["tokens_used"] == 100
    assert state["window_tokens_used"] == 25
    assert state["window_allowance"] == 100
    assert state["status"] == "active"


def test_goal_plugin_rejects_unknown_config_but_accepts_injected_providers() -> None:
    plugin = create_plugin({"providers": {"endpoint": {}}})
    assert plugin.state.version == SCHEMA_VERSION
    with pytest.raises(ValueError, match="does not support field"):
        create_plugin({"endpoint": {}})


@pytest.mark.parametrize(
    "exhaustion",
    [
        BudgetExhaustion(
            dimension="provider_turns",
            source="max_steps",
            limit=5,
            consumed=5,
            remaining=0,
            continue_with="resume_with_max_steps",
        ),
        BudgetExhaustion(
            dimension="root_actions",
            source="root_action_budget",
            limit=4,
            consumed=4,
            remaining=0,
            continue_with="extend_root_actions",
        ),
    ],
    ids=["provider-turns", "root-actions"],
)
def test_limit_reached_pauses_active_goal_with_typed_exhaustion(tmp_path: Path, exhaustion: BudgetExhaustion) -> None:
    services = _QueueServices(through_sequence=35)
    services.set_budget(since_sequence=0, max_total_tokens=1000)
    services.enqueue_continuation({"action": "pursue_goal", "goal_id": "g1"}, idempotency_key="stale-pursuit")
    goal = GoalState(
        goal_id="g1",
        status="active",
        objective="Long-running task",
        created_at=1,
        updated_at=1,
        token_budget=1000,
        tokens_used=100,
        window_tokens_used=100,
        window_allowance=900,
        active_windows=(ActiveWindow(20),),
        blocked_turns_count=2,
        last_blocked_logical_turn="turn-2",
    )
    services.replace_state(goal.to_dict(), expected_version=0)
    ctx = _make_context(tmp_path)

    goal_observer(
        RunFinishedFact(
            session_id="session-1",
            run_id="run-1",
            revision_id="rev-1",
            logical_turn="turn-limit",
            event_sequence=35,
            status="interrupted",
            stop_cause="limit_reached",
            budget_exhaustion=exhaustion,
        ),
        ctx,
        services,
    )

    state, _ = services.get_state("session")
    assert state is not None
    assert state["status"] == "paused"
    assert state["active_windows"] == [{"start_sequence": 20, "end_sequence": 35}]
    assert state["token_budget"] == 1000
    assert state["window_allowance"] == 900
    assert state["tokens_used"] == 100
    assert state["blocked_turns_count"] == 2
    # The goal's own budget and the Core constraint stay in place for edit/resume.
    assert services.budget is not None
    assert services.budget.max_total_tokens == 1000
    assert services.pending_pursuits == {}
    assert [event[0] for event in services.staged_events] == ["goal_paused"]
    payload = services.staged_events[0][1]
    assert payload["reason"] == "limit_reached"
    assert payload["stop_cause"] == "limit_reached"
    assert payload["budget_exhaustion"] == exhaustion.as_payload()

    resumed = GoalResumeCommand().invoke(CommandInvocation(context=ctx, name="goal.resume"), services)
    assert resumed.status == "ok"
    state, _ = services.get_state("session")
    assert state is not None
    assert state["status"] == "active"
    assert services.budget is not None
    assert len(services.pending_pursuits) == 1


def test_limit_reached_without_typed_source_still_pauses_active_goal(tmp_path: Path) -> None:
    """An owner-budget stop has no typed exhaustion yet; it pauses without guessing a source."""
    services = _QueueServices(through_sequence=35)
    services.set_budget(since_sequence=0, max_total_tokens=1000)
    services.enqueue_continuation({"action": "pursue_goal", "goal_id": "g1"}, idempotency_key="stale-pursuit")
    goal = GoalState(
        goal_id="g1",
        status="active",
        objective="Owner budget stop",
        created_at=1,
        updated_at=1,
        token_budget=1000,
        tokens_used=100,
        window_tokens_used=100,
        window_allowance=900,
        active_windows=(ActiveWindow(20),),
    )
    services.replace_state(goal.to_dict(), expected_version=0)
    ctx = _make_context(tmp_path)

    goal_observer(
        RunFinishedFact(
            session_id="session-1",
            run_id="run-1",
            revision_id="rev-1",
            logical_turn="turn-owner-limit",
            event_sequence=35,
            status="interrupted",
            stop_cause="limit_reached",
            budget_exhaustion=None,
        ),
        ctx,
        services,
    )

    state, _ = services.get_state("session")
    assert state is not None
    assert state["status"] == "paused"
    assert state["token_budget"] == 1000
    assert state["window_allowance"] == 900
    assert services.budget is not None
    assert services.pending_pursuits == {}
    assert [event[0] for event in services.staged_events] == ["goal_paused"]
    payload = services.staged_events[0][1]
    assert payload["reason"] == "limit_reached"
    assert payload["stop_cause"] == "limit_reached"
    assert "budget_exhaustion" not in payload

    resumed = GoalResumeCommand().invoke(CommandInvocation(context=ctx, name="goal.resume"), services)
    assert resumed.status == "ok"


@pytest.mark.parametrize(
    "status",
    ["complete", "paused", "usage-limited", "budget-limited"],
)
def test_limit_reached_does_not_rewrite_non_active_goal(tmp_path: Path, status: GoalStatus) -> None:
    services = FakeServices(through_sequence=35)
    services.set_budget(since_sequence=0, max_total_tokens=1000)
    windows = (ActiveWindow(0, 30),) if status in {"complete", "paused"} else (ActiveWindow(20),)
    goal = GoalState(
        goal_id="g1",
        status=status,
        objective="Already halted",
        created_at=1,
        updated_at=123,
        token_budget=1000,
        window_allowance=900,
        active_windows=windows,
    )
    services.replace_state(goal.to_dict(), expected_version=0)
    before = services.get_state()
    goal_observer(
        RunFinishedFact(
            session_id="session-1",
            run_id="run-1",
            revision_id="rev-1",
            logical_turn="turn-limit",
            event_sequence=35,
            status="interrupted",
            stop_cause="limit_reached",
            budget_exhaustion=BudgetExhaustion(
                dimension="provider_turns",
                source="max_steps",
                limit=5,
                consumed=5,
                remaining=0,
                continue_with="resume_with_max_steps",
            ),
        ),
        _make_context(tmp_path),
        services,
    )
    assert services.get_state() == before
    assert services.budget is not None
    assert services.budget.max_total_tokens == 1000
    assert not services.cancelled_continuations
    assert not services.staged_events
