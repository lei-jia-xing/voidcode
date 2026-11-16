"""Workflows ctx.agent behavior over an in-memory services double."""

import asyncio
from pathlib import Path
from typing import Any

import pytest

from tests.fixtures.fake_services import FakeServices
from voidcode.capabilities.agent_queries import AgentDefinition
from voidcode.plugins.workflows.workflows_plugin import (
    WorkflowContext,
    WorkflowNode,
    WorkflowRun,
    WorkflowState,
    _CancelledNode,
    _FinishedNode,
    _FinishNode,
    _ParkedNode,
    _ParkNode,
)
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.services import BudgetExhaustion, ChildOutcome


def _context(services: FakeServices) -> ToolContext:
    _ = services
    return ToolContext(workspace=Path("/workspace"), session_id="s1", run_id="r1", invocation_id="i1", revision_id="rev", actor="model")


def _seed(services: FakeServices, **overrides: Any) -> str:
    base = {
        "run_id": "run-1",
        "session_id": "s1",
        "workflow": "review",
        "source_path": "flows/review.py",
        "source_sha256": "a" * 64,
        "source_artifact": "b" * 64,
        "task": "review this",
        "input_sha256": "c" * 64,
    }
    base.update(overrides)
    run = WorkflowRun(**base)  # type: ignore[arg-type]
    services.seed_state(WorkflowState.from_payload(None).with_run(run).to_payload(), version=1)
    return run.run_id


def _agent_services() -> FakeServices:
    services = FakeServices(session_id="s1")
    services.spawn_child = _spawn_with_key(services)  # type: ignore[method-assign]
    profile = AgentDefinition(name="review", description="Review", prompt="Review.", readonly=True, max_turns=4)
    services.queries_dict["workspace.agent_definition"] = profile.as_payload()
    return services


def _spawn_with_key(services: FakeServices):  # type: ignore[no-untyped-def]
    original = FakeServices.spawn_child.__get__(services, FakeServices)
    keys: dict[str, str] = {}

    def spawn(prompt: str, **kwargs: Any) -> str:
        key = kwargs.pop("idempotency_key", None)
        if key is not None and key in keys:
            return keys[key]
        child_id = original(prompt, **kwargs)
        if key is not None:
            keys[key] = child_id
        return child_id

    return spawn


def test_agent_node_spawns_once_and_reuses_outcome() -> None:
    services = _agent_services()
    run_id = _seed(services)
    context = _context(services)
    driver = WorkflowContext(invoke=lambda callback: callback(context, services), run_id=run_id, task="review this")
    first = asyncio.run(driver.agent("review", "look", key="a"))
    second = asyncio.run(driver.agent("review", "look", key="a"))
    assert second == first
    assert len(services.spawn_calls) == 1


def test_same_key_with_different_effective_prompt_is_refused() -> None:
    services = _agent_services()
    run_id = _seed(services)
    context = _context(services)
    driver = WorkflowContext(invoke=lambda callback: callback(context, services), run_id=run_id, task="t")
    asyncio.run(driver.agent("review", "look", key="a"))
    with pytest.raises(ValueError, match="changed its effective input"):
        asyncio.run(driver.agent("review", "look harder", key="a"))


def test_unknown_dependency_is_refused_before_any_spawn() -> None:
    services = _agent_services()
    run_id = _seed(services)
    context = _context(services)
    driver = WorkflowContext(invoke=lambda callback: callback(context, services), run_id=run_id, task="t")
    with pytest.raises(ValueError, match="depends on unknown key"):
        asyncio.run(driver.agent("review", "look", key="b", depends_on=("missing",)))
    assert services.spawn_calls == []


def test_parallel_nodes_both_commit_without_losing_a_node() -> None:
    services = _agent_services()
    run_id = _seed(services)
    context = _context(services)
    driver = WorkflowContext(invoke=lambda callback: callback(context, services), run_id=run_id, task="t")

    async def both() -> tuple[str, str]:
        async with asyncio.TaskGroup() as group:
            first = group.create_task(driver.agent("review", "one", key="a"))
            second = group.create_task(driver.agent("review", "two", key="b"))
        return first.result(), second.result()

    asyncio.run(both())
    state = WorkflowState.from_payload(services.get_state("session")[0])
    assert sorted(node.key for node in state.runs[run_id].nodes) == ["a", "b"]
    assert all(node.status == "completed" for node in state.runs[run_id].nodes)


def test_waiting_child_parks_and_cancelled_child_raises() -> None:
    services = _agent_services()
    services.join_status = "waiting"
    run_id = _seed(services)
    context = _context(services)
    driver = WorkflowContext(invoke=lambda callback: callback(context, services), run_id=run_id, task="t")
    with pytest.raises(_ParkedNode):
        asyncio.run(driver.agent("review", "look", key="a"))
    state = WorkflowState.from_payload(services.get_state("session")[0])
    assert state.runs[run_id].status == "parked"
    assert state.runs[run_id].nodes[0].status == "waiting"

    services.join_status = "cancelled"
    run_id = _seed(services, run_id="run-2")
    driver = WorkflowContext(invoke=lambda callback: callback(context, services), run_id=run_id, task="t")
    with pytest.raises(_CancelledNode):
        asyncio.run(driver.agent("review", "look", key="a"))
    state = WorkflowState.from_payload(services.get_state("session")[0])
    assert state.runs[run_id].nodes[0].status == "cancelled"


def test_budget_interruption_parks_with_typed_stop_without_acknowledging() -> None:
    services = _agent_services()
    budget = BudgetExhaustion(
        dimension="provider_turns",
        source="max_steps",
        limit=1,
        consumed=1,
        remaining=0,
        continue_with="resume_with_max_steps",
    )
    run_id = _seed(services)
    context = _context(services)
    driver = WorkflowContext(invoke=lambda callback: callback(context, services), run_id=run_id, task="t")

    def interrupted(child_session_id: str, *, timeout_seconds: float = 300.0) -> ChildOutcome:
        _ = timeout_seconds
        return ChildOutcome(
            child_session_id=child_session_id,
            status="interrupted",
            error="provider turn cap exhausted",
            stop_cause="limit_reached",
            run_id=f"run-{child_session_id}",
            budget_exhaustion=budget,
        )

    services.join_child = interrupted  # type: ignore[method-assign]
    with pytest.raises(_ParkedNode, match="interrupted"):
        asyncio.run(driver.agent("review", "look", key="a"))

    state = WorkflowState.from_payload(services.get_state("session")[0])
    run = state.runs[run_id]
    node = run.node("a")
    assert node is not None and node.status == "interrupted"
    assert node.error == "provider turn cap exhausted"
    assert node.budget_exhaustion == budget
    assert services.acks == [], "a recoverable pause is not a terminal completion"
    assert node.child_session_id is not None and node.run_id is not None
    cancelled = ChildOutcome(
        child_session_id=node.child_session_id,
        status="cancelled",
        error="owner cancellation",
        run_id=node.run_id,
    )
    finished = driver.invoke(_FinishNode(run_id, "a", cancelled))
    assert isinstance(finished, _FinishedNode)
    assert finished.cancelled and finished.error is None and finished.text is None
    state = WorkflowState.from_payload(services.get_state("session")[0])
    cancelled_node = state.runs[run_id].node("a")
    assert cancelled_node is not None and cancelled_node.status == "cancelled"
    assert cancelled_node.budget_exhaustion is None
    assert services.acks == [(node.child_session_id, node.run_id)]


def test_large_outcome_spills_to_artifact_and_rereads() -> None:
    services = _agent_services()
    services.join_output = "x" * (9 * 1024)
    run_id = _seed(services)
    context = _context(services)
    driver = WorkflowContext(invoke=lambda callback: callback(context, services), run_id=run_id, task="t")
    assert asyncio.run(driver.agent("review", "look", key="a")) == "x" * (9 * 1024)
    state = WorkflowState.from_payload(services.get_state("session")[0])
    node = state.runs[run_id].nodes[0]
    assert node.output is None and node.output_sha256 is not None
    assert asyncio.run(driver.agent("review", "look", key="a")) == "x" * (9 * 1024)


def test_outcome_from_another_child_is_rejected() -> None:
    services = _agent_services()
    run_id = _seed(services)
    context = _context(services)
    driver = WorkflowContext(invoke=lambda callback: callback(context, services), run_id=run_id, task="t")
    asyncio.run(driver.agent("review", "look", key="a"))
    foreign = ChildOutcome(child_session_id="child-foreign", status="completed", output="forged", run_id="run-x")
    with pytest.raises(ValueError, match="no child to finish"):
        driver.invoke(_FinishNode(run_id, "a", foreign))


def test_large_multibyte_output_reads_whole_and_matches_cache() -> None:
    import hashlib

    # 70 KiB of 3-byte characters: crosses the 64 KiB page cut mid-character on purpose.
    full = "汉" * ((70 * 1024) // 3 + 1)
    expected_size = len(full.encode("utf-8"))
    expected_hash = hashlib.sha256(full.encode("utf-8")).hexdigest()
    services = _agent_services()
    reference = services.spill_artifact(full)
    assert reference.sha256 == expected_hash
    services.join_output = None
    services.roster.clear()
    run_id = _seed(services)
    context = _context(services)
    driver = WorkflowContext(invoke=lambda callback: callback(context, services), run_id=run_id, task="t")
    original_spawn = services.spawn_child

    def _spawn_and_stage(prompt: str, **kwargs: Any) -> str:
        child_id = original_spawn(prompt, **kwargs)
        services.roster[child_id] = ChildOutcome(
            child_session_id=child_id,
            status="completed",
            output="preview…",
            run_id=f"run-{child_id}",
            output_sha256=reference.sha256,
            output_bytes=reference.bytes,
        )
        return child_id

    services.spawn_child = _spawn_and_stage  # type: ignore[method-assign]
    first = asyncio.run(driver.agent("review", "look", key="big"))
    assert len(first.encode("utf-8")) == expected_size
    assert hashlib.sha256(first.encode("utf-8")).hexdigest() == expected_hash
    assert "preview" not in first
    state = WorkflowState.from_payload(services.get_state("session")[0])
    node = state.runs[run_id].nodes[0]
    assert node.output is None and node.output_sha256 == expected_hash
    # A fresh driver rereading the cached row must get the identical whole text.
    rereader = WorkflowContext(invoke=lambda callback: callback(context, services), run_id=run_id, task="t")
    second = asyncio.run(rereader.agent("review", "look", key="big"))
    assert second == first
    assert hashlib.sha256(second.encode("utf-8")).hexdigest() == expected_hash


def test_attach_always_spawns_so_core_repairs_pristine_reservations() -> None:
    from voidcode.plugins.workflows.workflows_plugin import _attach_node_child, _stage_pending_node

    services = _agent_services()
    run_id = _seed(services)
    context = _context(services)
    find_calls: list[str] = []

    def _find_child(*, idempotency_key: str) -> str | None:
        find_calls.append(idempotency_key)
        return "child-reserved"

    services.find_child = _find_child  # type: ignore[method-assign]
    staged = _stage_pending_node(context, services, run_id, "a", (), "review", "look", None, 32)
    assert staged.settled is None and staged.plan is not None
    assert staged.node is not None and staged.node.child_session_id is None
    planned = _attach_node_child(context, services, run_id, "a", staged.plan, "")
    # Attach must call keyed spawn (the core reuses or repairs the reservation);
    # a pure find would leave a reserved-but-unstarted child idle forever.
    assert find_calls == []
    assert len(services.spawn_calls) == 1
    assert planned.node.child_session_id is not None


def _stage_roster(services: FakeServices, outcome: ChildOutcome) -> None:
    original_spawn = services.spawn_child

    def _spawn_and_stage(prompt: str, **kwargs: Any) -> str:
        child_id = original_spawn(prompt, **kwargs)
        services.roster[child_id] = ChildOutcome(
            child_session_id=child_id,
            status=outcome.status,
            output=outcome.output,
            error=outcome.error,
            stop_cause=outcome.stop_cause,
            run_id=outcome.run_id or f"run-{child_id}",
            history_sha256=outcome.history_sha256,
            history_bytes=outcome.history_bytes,
            output_preview=outcome.output_preview,
            output_sha256=outcome.output_sha256,
            output_bytes=outcome.output_bytes,
            output_error=outcome.output_error,
            history_error=outcome.history_error,
            budget_exhaustion=outcome.budget_exhaustion,
        )
        return child_id

    services.spawn_child = _spawn_and_stage  # type: ignore[method-assign]


def test_preview_only_completed_fails_consumption_but_persists_capture_cause() -> None:
    services = _agent_services()
    run_id = _seed(services)
    context = _context(services)
    driver = WorkflowContext(invoke=lambda callback: callback(context, services), run_id=run_id, task="t")
    _stage_roster(
        services,
        ChildOutcome(
            child_session_id="child-x",
            status="completed",
            run_id="run-cap-1",
            output_preview="partial…",
            output_error="RuntimeError: boom",
        ),
    )
    with pytest.raises(ValueError, match="full output is unavailable.*run-cap-1.*RuntimeError: boom"):
        asyncio.run(driver.agent("review", "look", key="a"))
    state = WorkflowState.from_payload(services.get_state("session")[0])
    node = state.runs[run_id].node("a")
    assert node is not None and node.status == "completed"
    assert node.output is None and node.output_sha256 is None and node.output_bytes is None
    assert node.output_preview == "partial…" and node.output_error == "RuntimeError: boom"
    assert node.history_error is None and node.run_id == "run-cap-1"
    assert services.acks == [(node.child_session_id, "run-cap-1")]


def test_redriving_preview_only_node_fails_without_respawn() -> None:
    services = _agent_services()
    run_id = _seed(services)
    context = _context(services)
    driver = WorkflowContext(invoke=lambda callback: callback(context, services), run_id=run_id, task="t")
    _stage_roster(
        services,
        ChildOutcome(
            child_session_id="child-x",
            status="completed",
            run_id="run-cap-1",
            output_preview="partial…",
            output_error="RuntimeError: boom",
        ),
    )
    with pytest.raises(ValueError, match="full output is unavailable"):
        asyncio.run(driver.agent("review", "look", key="a"))
    spawns = len(services.spawn_calls)
    with pytest.raises(ValueError, match="full output is unavailable.*run-cap-1"):
        asyncio.run(driver.agent("review", "look", key="a"))
    assert len(services.spawn_calls) == spawns


def test_failed_child_returns_result_for_outside_lease_raise() -> None:
    services = _agent_services()
    services.join_status = "failed"
    services.join_error = "child blew up"
    run_id = _seed(services)
    context = _context(services)
    driver = WorkflowContext(invoke=lambda callback: callback(context, services), run_id=run_id, task="t")
    with pytest.raises(ValueError, match="node 'a' failed: child blew up"):
        asyncio.run(driver.agent("review", "look", key="a"))
    state = WorkflowState.from_payload(services.get_state("session")[0])
    node = state.runs[run_id].node("a")
    assert node is not None and node.status == "failed" and node.error == "child blew up"
    assert node.output == "" and node.output_error is None and node.history_error is None
    assert services.acks == [(node.child_session_id, node.run_id)]


def test_finish_callback_never_raises_after_staging_terminal_truth() -> None:
    services = _agent_services()
    services.join_status = "waiting"
    run_id = _seed(services)
    context = _context(services)
    driver = WorkflowContext(invoke=lambda callback: callback(context, services), run_id=run_id, task="t")
    with pytest.raises(Exception, match="parked"):
        asyncio.run(driver.agent("review", "look", key="a"))
    node = WorkflowState.from_payload(services.get_state("session")[0]).runs[run_id].node("a")
    assert node is not None and node.child_session_id is not None and node.run_id is not None
    failed = ChildOutcome(child_session_id=node.child_session_id, status="failed", error="boom", run_id=node.run_id)
    finished = driver.invoke(_FinishNode(run_id, "a", failed))
    assert isinstance(finished, _FinishedNode)
    assert finished.error == "node 'a' failed: boom" and finished.text is None and not finished.cancelled
    persisted = WorkflowState.from_payload(services.get_state("session")[0]).runs[run_id].node("a")
    assert persisted is not None and persisted.status == "failed" and persisted.error == "boom"


def test_completed_without_any_output_persists_explicit_empty_string() -> None:
    services = _agent_services()
    run_id = _seed(services)
    context = _context(services)
    driver = WorkflowContext(invoke=lambda callback: callback(context, services), run_id=run_id, task="t")
    _stage_roster(services, ChildOutcome(child_session_id="child-x", status="completed", run_id="run-empty-1"))
    assert asyncio.run(driver.agent("review", "look", key="a")) == ""
    node = WorkflowState.from_payload(services.get_state("session")[0]).runs[run_id].node("a")
    assert node is not None and node.status == "completed" and node.output == ""
    assert node.output_error is None and node.history_error is None


def test_history_capture_error_does_not_fail_output_consumption() -> None:
    services = _agent_services()
    run_id = _seed(services)
    context = _context(services)
    driver = WorkflowContext(invoke=lambda callback: callback(context, services), run_id=run_id, task="t")
    _stage_roster(
        services,
        ChildOutcome(
            child_session_id="child-x",
            status="completed",
            output="findings",
            run_id="run-hist-1",
            history_error="RuntimeError: history boom",
        ),
    )
    assert asyncio.run(driver.agent("review", "look", key="a")) == "findings"
    node = WorkflowState.from_payload(services.get_state("session")[0]).runs[run_id].node("a")
    assert node is not None and node.output == "findings"
    assert node.history_error == "RuntimeError: history boom" and node.output_error is None


def test_node_capture_fields_are_optional_and_backward_readable() -> None:
    legacy = WorkflowNode(key="a", status="completed", output="x").to_payload()
    assert "output_preview" not in legacy and "output_error" not in legacy and "history_error" not in legacy
    read = WorkflowNode.from_payload(legacy, run_id="run-1")
    assert read.output_preview is None and read.output_error is None and read.history_error is None
    full = WorkflowNode(
        key="a",
        status="completed",
        run_id="run-1",
        output_preview="p",
        output_error="oe",
        history_error="he",
    )
    assert WorkflowNode.from_payload(full.to_payload(), run_id="run-1") == full


def test_park_drops_prior_run_output_and_capture_data() -> None:
    services = _agent_services()
    stale = WorkflowNode(
        key="a",
        status="running",
        child_session_id="child-1",
        run_id="run-old",
        output="stale",
        output_preview="stale-prev",
        output_error="stale-err",
        history_error="stale-hist",
        error="stale-e",
    )
    run = WorkflowRun(
        run_id="run-1",
        session_id="s1",
        workflow="review",
        source_path="flows/review.py",
        source_sha256="a" * 64,
        source_artifact="b" * 64,
        task="t",
        input_sha256="c" * 64,
        nodes=(stale,),
    )
    services.seed_state(WorkflowState.from_payload(None).with_run(run).to_payload(), version=1)
    context = _context(services)
    driver = WorkflowContext(invoke=lambda callback: callback(context, services), run_id="run-1", task="t")
    outcome = ChildOutcome(child_session_id="child-1", status="waiting", error="waiting on approval", run_id="run-new")
    driver.invoke(_ParkNode("run-1", "a", outcome))
    node = WorkflowState.from_payload(services.get_state("session")[0]).runs["run-1"].node("a")
    assert node is not None and node.status == "waiting" and node.run_id == "run-new"
    assert node.output is None and node.output_sha256 is None and node.output_bytes is None
    assert node.output_preview is None and node.output_error is None and node.history_error is None
    assert node.error == "waiting on approval"
