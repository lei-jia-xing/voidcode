"""Workflow structured delivery: declared node schemas validate at finish, never guessed."""

from __future__ import annotations

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
    _digest,
    _FinishNode,
    _ParkNode,
    _resolve_effective_plan,
    _stage_pending_node,
)
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.services import ChildOutcome, StructuredQueryParseError

_SCHEMA = {
    "type": "object",
    "required": ["files", "ok"],
    "properties": {"files": {"type": "array", "items": {"type": "string"}}, "ok": {"type": "boolean"}},
    "additionalProperties": False,
}


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


def _driver(services: FakeServices, run_id: str) -> tuple[WorkflowContext, ToolContext]:
    context = _context(services)
    return WorkflowContext(invoke=lambda callback: callback(context, services), run_id=run_id, task="t"), context


def _node_of(services: FakeServices, run_id: str, key: str) -> WorkflowNode:
    state = WorkflowState.from_payload(services.get_state("session")[0])
    node = state.runs[run_id].node(key)
    assert node is not None
    return node


def test_structured_node_returns_validated_data() -> None:
    services = _agent_services()
    services.join_output = '{"files": ["a"], "ok": true}'
    run_id = _seed(services)
    driver, _context = _driver(services, run_id)
    result = asyncio.run(driver.agent_structured("review", "look", key="a", result_schema=dict(_SCHEMA)))
    assert result.data is not None and result.as_payload()["data"] == {"files": ["a"], "ok": True}
    assert result.parse_error is None
    assert result.raw_text == '{"files": ["a"], "ok": true}'
    assert result.run_id is not None and result.child_session_id
    assert services.spawn_calls[0]["result_schema"] is not None
    node = _node_of(services, run_id, "a")
    assert node.result_schema is not None and dict(node.result_schema) == _SCHEMA
    assert node.data is not None and dict(node.data) == {"files": ["a"], "ok": True}
    assert node.parse_error is None


def test_structured_mismatch_returns_parse_error() -> None:
    services = _agent_services()
    services.join_output = '{"files": []}'
    run_id = _seed(services)
    driver, _context = _driver(services, run_id)
    result = asyncio.run(driver.agent_structured("review", "look", key="a", result_schema=dict(_SCHEMA)))
    assert result.data is None
    assert result.parse_error is not None and result.parse_error.kind == "schema_violation"
    assert result.parse_error.path is not None
    assert result.raw_text == '{"files": []}'
    assert _node_of(services, run_id, "a").data is None


def test_structured_reuse_returns_identical_reading_without_respawn() -> None:
    services = _agent_services()
    services.join_output = '{"files": ["a"], "ok": true}'
    run_id = _seed(services)
    driver, _context = _driver(services, run_id)
    first = asyncio.run(driver.agent_structured("review", "look", key="a", result_schema=dict(_SCHEMA)))
    second = asyncio.run(driver.agent_structured("review", "look", key="a", result_schema=dict(_SCHEMA)))
    assert second.data == first.data and second.raw_text == first.raw_text
    assert len(services.spawn_calls) == 1


def test_same_key_different_schema_is_refused() -> None:
    services = _agent_services()
    services.join_output = '{"files": ["a"], "ok": true}'
    run_id = _seed(services)
    driver, _context = _driver(services, run_id)
    asyncio.run(driver.agent_structured("review", "look", key="a", result_schema=dict(_SCHEMA)))
    with pytest.raises(ValueError, match="changed its effective input"):
        asyncio.run(driver.agent_structured("review", "look", key="a", result_schema={"type": "object"}))
    with pytest.raises(ValueError, match="changed its effective input"):
        asyncio.run(driver.agent("review", "look", key="a"))


def test_undeclared_fingerprint_omits_schema() -> None:
    services = _agent_services()
    run_id = _seed(services)
    legacy = _digest(
        {
            "body": "Review.\n\nlook",
            "readonly": True,
            "scope": None,
            "budget": None,
            "depends_on": [],
            "input_sha256": "c" * 64,
        }
    )
    plan = _resolve_effective_plan(services, run_id, "a", (), "review", "look", None, None)
    assert plan.effective_input == legacy
    assert plan.result_schema is None
    shaped = _resolve_effective_plan(services, run_id, "a", (), "review", "look", None, None, result_schema=dict(_SCHEMA))
    assert shaped.effective_input != legacy


def test_corrupt_row_refuses_to_guess() -> None:
    services = _agent_services()
    services.join_output = '{"files": ["a"], "ok": true}'
    run_id = _seed(services)
    driver, _context = _driver(services, run_id)
    asyncio.run(driver.agent_structured("review", "look", key="a", result_schema=dict(_SCHEMA)))
    payload, version = services.get_state("session")
    assert payload is not None
    editable = dict(payload)
    runs = dict(editable["runs"])  # type: ignore[arg-type]
    run_payload = dict(runs[run_id])  # type: ignore[arg-type]
    nodes = [dict(entry) for entry in run_payload["nodes"]]  # type: ignore[arg-type]
    nodes[0].pop("data")
    nodes[0].pop("parse_error", None)
    run_payload["nodes"] = nodes
    runs[run_id] = run_payload
    editable["runs"] = runs
    services.seed_state(editable, version=version)
    with pytest.raises(ValueError, match="refusing to guess"):
        asyncio.run(driver.agent_structured("review", "look", key="a", result_schema=dict(_SCHEMA)))


def test_preview_only_structured_fails_visibly() -> None:
    services = _agent_services()
    run_id = _seed(services)
    driver, context = _driver(services, run_id)
    staged = _stage_pending_node(context, services, run_id, "a", (), "review", "look", None, None, result_schema=dict(_SCHEMA))
    assert staged.plan is not None and staged.node is not None
    from voidcode.plugins.workflows.workflows_plugin import _attach_node_child

    planned = _attach_node_child(context, services, run_id, "a", staged.plan, "")
    assert planned.node.child_session_id is not None
    outcome = ChildOutcome(
        child_session_id=planned.node.child_session_id,
        status="completed",
        run_id="run-cap-9",
        output_preview="partial…",
        output_error="RuntimeError: boom",
    )
    finished = _FinishNode(run_id, "a", outcome)(context, services)
    assert finished.error is not None and "unavailable" in finished.error and "run-cap-9" in finished.error
    assert finished.structured is None
    node = _node_of(services, run_id, "a")
    assert node.data is None and node.parse_error is None and node.result_schema is not None


def test_failed_node_persists_no_reading() -> None:
    services = _agent_services()
    services.join_status = "failed"
    services.join_error = "child blew up"
    run_id = _seed(services)
    driver, _context = _driver(services, run_id)
    with pytest.raises(ValueError, match="failed"):
        asyncio.run(driver.agent_structured("review", "look", key="a", result_schema=dict(_SCHEMA)))
    node = _node_of(services, run_id, "a")
    assert node.status == "failed" and node.data is None and node.parse_error is None


def test_ingress_rejects_non_object_and_bad_schema() -> None:
    services = _agent_services()
    run_id = _seed(services)
    driver, _context = _driver(services, run_id)
    with pytest.raises(ValueError, match="result_schema must be an object"):
        asyncio.run(driver.agent_structured("review", "look", key="a", result_schema=["nope"]))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="not a valid JSON schema"):
        asyncio.run(driver.agent_structured("review", "look", key="a", result_schema={"type": "nonsense"}))
    assert services.spawn_calls == []


def test_node_payload_round_trip_with_structured() -> None:
    node = WorkflowNode(
        key="a",
        status="completed",
        child_session_id="child-1",
        run_id="run-1",
        output='{"files": [], "ok": true}',
        result_schema=dict(_SCHEMA),
        data={"files": [], "ok": True},
    )
    revived = WorkflowNode.from_payload(node.to_payload(), run_id="run-1")
    assert revived == node
    failure = WorkflowNode(
        key="b",
        status="completed",
        result_schema=dict(_SCHEMA),
        parse_error=StructuredQueryParseError(kind="invalid_json", message="child output is empty"),
    )
    assert WorkflowNode.from_payload(failure.to_payload(), run_id="run-1") == failure


def test_legacy_node_payload_parses_with_unknown_structured() -> None:
    revived = WorkflowNode.from_payload(
        {
            "key": "a",
            "status": "completed",
            "depends_on": [],
            "agent": "review",
            "child_session_id": "child-1",
            "run_id": "run-1",
            "effective_input_sha256": "d" * 64,
            "prompt_sha256": "e" * 64,
            "output": "plain",
            "output_sha256": None,
            "output_bytes": None,
            "error": None,
        },
        run_id="run-1",
    )
    assert revived.result_schema is None and revived.data is None and revived.parse_error is None


def test_park_clears_reading_but_keeps_schema() -> None:
    services = _agent_services()
    services.join_output = '{"files": ["a"], "ok": true}'
    run_id = _seed(services)
    driver, context = _driver(services, run_id)
    asyncio.run(driver.agent_structured("review", "look", key="a", result_schema=dict(_SCHEMA)))
    node = _node_of(services, run_id, "a")
    assert node.data is not None and node.child_session_id is not None
    _ParkNode(run_id, "a", ChildOutcome(child_session_id=node.child_session_id, status="waiting", run_id="run-next"))(context, services)
    parked = _node_of(services, run_id, "a")
    assert parked.status == "waiting"
    assert parked.data is None and parked.parse_error is None
    assert parked.result_schema is not None
