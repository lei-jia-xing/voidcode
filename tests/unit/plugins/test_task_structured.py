"""Task structured delivery: declared schemas validate after join, never guessed."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from tests.fixtures.fake_services import FakeServices
from voidcode.plugins.subagents.subagents_plugin import DelegationState, TaskBatchTool, TaskTool
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.services import BudgetExhaustion, ChildOutcome
from voidcode.sdk.tools import TextOutput, ToolCall, ToolFailure, ToolResult, ToolSuccess

_SCHEMA = {
    "type": "object",
    "required": ["files", "ok"],
    "properties": {"files": {"type": "array", "items": {"type": "string"}}, "ok": {"type": "boolean"}},
    "additionalProperties": False,
}


def _context(workspace: Path) -> ToolContext:
    return ToolContext(
        workspace=workspace,
        session_id="session-1",
        run_id="run-1",
        invocation_id="inv-1",
        revision_id="rev-1",
        actor="model",
        logical_turn="turn-1",
    )


def _task(services: FakeServices, tmp_path: Path, arguments: Mapping[str, object]) -> ToolResult:
    call = ToolCall(tool_name="task", arguments=arguments, tool_call_id="call-1")
    return TaskTool().invoke(call, context=_context(tmp_path), services=services)


def _batch(services: FakeServices, tmp_path: Path, arguments: Mapping[str, object]) -> ToolResult:
    call = ToolCall(tool_name="task_batch", arguments=arguments, tool_call_id="call-2")
    return TaskBatchTool().invoke(call, context=_context(tmp_path), services=services)


def _payload(result: ToolResult) -> dict[str, object]:
    assert result.body is not None
    return dict(result.body.as_payload())


def _text(result: ToolResult) -> str:
    assert isinstance(result.output, TextOutput)
    return result.output.text


def test_sync_spawn_returns_validated_data(tmp_path: Path) -> None:
    services = FakeServices()
    services.join_output = '{"files": ["a"], "ok": true}'
    result = _task(services, tmp_path, {"prompt": "list", "result_schema": dict(_SCHEMA), "run_in_background": False})
    assert isinstance(result, ToolSuccess)
    payload = _payload(result)
    assert payload["status"] == "completed"
    assert payload["data"] == {"files": ["a"], "ok": True}
    assert "parse_error" not in payload
    assert "structured: ok" in _text(result)


def test_sync_spawn_reports_schema_violation(tmp_path: Path) -> None:
    services = FakeServices()
    services.join_output = '{"files": []}'
    result = _task(services, tmp_path, {"prompt": "list", "result_schema": dict(_SCHEMA), "run_in_background": False})
    assert isinstance(result, ToolSuccess)
    payload = _payload(result)
    assert payload["status"] == "completed"
    assert "data" not in payload
    error = payload["parse_error"]
    assert isinstance(error, Mapping) and error["kind"] == "schema_violation" and error["path"] is not None
    assert "structured: schema_violation" in _text(result)


def test_unparseable_output_is_invalid_json(tmp_path: Path) -> None:
    services = FakeServices()
    services.join_output = "I cannot answer that."
    result = _task(services, tmp_path, {"prompt": "list", "result_schema": dict(_SCHEMA), "run_in_background": False})
    payload = _payload(result)
    assert "data" not in payload
    error = payload["parse_error"]
    assert isinstance(error, Mapping) and error["kind"] == "invalid_json" and "path" not in error
    assert "structured: invalid_json" in _text(result)


def test_running_child_has_no_structured_keys(tmp_path: Path) -> None:
    services = FakeServices()
    services.join_status = "running"
    services.join_output = None
    result = _task(services, tmp_path, {"prompt": "slow", "result_schema": dict(_SCHEMA), "run_in_background": False})
    payload = _payload(result)
    assert payload["status"] == "running"
    assert "data" not in payload and "parse_error" not in payload
    assert "structured:" not in _text(result)


def test_budget_exhaustion_has_no_structured_keys(tmp_path: Path) -> None:
    services = FakeServices()
    _task(services, tmp_path, {"prompt": "budgeted", "result_schema": dict(_SCHEMA)})
    services.roster["child-1"] = ChildOutcome(
        child_session_id="child-1",
        status="interrupted",
        stop_cause="limit_reached",
        run_id="run-budget",
        budget_exhaustion=BudgetExhaustion(
            dimension="provider_turns",
            source="max_steps",
            limit=2,
            consumed=2,
            remaining=0,
            continue_with="resume_with_max_steps",
        ),
    )
    result = _task(services, tmp_path, {"operation": "output", "task_id": "child-1"})
    payload = _payload(result)
    assert payload["budget_exhaustion"] is not None
    assert "data" not in payload and "parse_error" not in payload
    assert "structured:" not in _text(result)


def test_cancelled_child_has_no_structured_keys(tmp_path: Path) -> None:
    services = FakeServices()
    _task(services, tmp_path, {"prompt": "doomed", "result_schema": dict(_SCHEMA)})
    services.roster["child-1"] = replace(services.roster["child-1"], status="cancelled", stop_cause="owner_cancel", output=None)
    result = _task(services, tmp_path, {"operation": "output", "task_id": "child-1"})
    payload = _payload(result)
    assert payload["status"] == "cancelled"
    assert "data" not in payload and "parse_error" not in payload


def test_preview_only_reports_unavailable_cause(tmp_path: Path) -> None:
    services = FakeServices()
    _task(services, tmp_path, {"prompt": "big", "result_schema": dict(_SCHEMA)})
    services.roster["child-1"] = ChildOutcome(
        child_session_id="child-1",
        status="completed",
        output=None,
        run_id="run-capped",
        output_preview="partial result…",
        output_bytes=40000,
        output_error="ArtifactStoreError: quota exceeded",
    )
    result = _task(services, tmp_path, {"operation": "output", "task_id": "child-1"})
    payload = _payload(result)
    assert payload["status"] == "completed"
    assert "data" not in payload and "parse_error" not in payload
    assert "structured: unavailable (ArtifactStoreError: quota exceeded)" in _text(result)


def test_undeclared_spawn_has_no_structured_keys(tmp_path: Path) -> None:
    services = FakeServices()
    services.join_output = '{"files": ["a"], "ok": true}'
    result = _task(services, tmp_path, {"prompt": "list", "run_in_background": False})
    payload = _payload(result)
    assert "data" not in payload and "parse_error" not in payload
    assert "structured:" not in _text(result)


def test_spawn_records_schema_for_later_output_reads(tmp_path: Path) -> None:
    services = FakeServices()
    spawned = _task(services, tmp_path, {"prompt": "list", "result_schema": dict(_SCHEMA)})
    assert isinstance(spawned, ToolSuccess)
    stored, _version = services.get_state("session")
    record = DelegationState.from_payload(stored).children["child-1"]
    assert record.result_schema is not None and dict(record.result_schema) == _SCHEMA
    assert services.spawn_calls[0]["result_schema"] is not None
    services.roster["child-1"] = replace(services.roster["child-1"], output='{"files": [], "ok": false}')
    result = _task(services, tmp_path, {"operation": "output", "task_id": "child-1"})
    assert _payload(result)["data"] == {"files": [], "ok": False}


def test_result_schema_applies_to_spawns_only(tmp_path: Path) -> None:
    services = FakeServices()
    _task(services, tmp_path, {"prompt": "list"})
    result = _task(services, tmp_path, {"operation": "output", "task_id": "child-1", "result_schema": dict(_SCHEMA)})
    assert isinstance(result, ToolFailure)


def test_invalid_schema_fails_before_spawning(tmp_path: Path) -> None:
    services = FakeServices()
    result = _task(services, tmp_path, {"prompt": "list", "result_schema": {"type": "nonsense"}})
    assert isinstance(result, ToolFailure)
    assert services.spawn_calls == []


def test_batch_item_schema_validates_on_group_output(tmp_path: Path) -> None:
    services = FakeServices()
    spawned = _batch(services, tmp_path, {"tasks": [{"prompt": "list", "result_schema": dict(_SCHEMA)}]})
    assert isinstance(spawned, ToolSuccess)
    group_id = _payload(spawned)["parallel_group_id"]
    assert isinstance(group_id, str)
    services.roster["child-1"] = replace(services.roster["child-1"], output='{"files": ["b"], "ok": true}')
    result = _batch(services, tmp_path, {"operation": "output", "parallel_group_id": group_id})
    assert isinstance(result, ToolSuccess)
    children = _payload(result)["children"]
    assert isinstance(children, list) and children[0]["data"] == {"files": ["b"], "ok": True}
    assert "parse_error" not in children[0]


def test_artifact_backed_output_validates_full_text(tmp_path: Path) -> None:
    services = FakeServices()
    _task(services, tmp_path, {"prompt": "list", "result_schema": dict(_SCHEMA)})
    reference = services.spill_artifact('{"files": ["z"], "ok": true}')
    services.roster["child-1"] = replace(services.roster["child-1"], output=None, output_sha256=reference.sha256, output_bytes=reference.bytes)
    result = _task(services, tmp_path, {"operation": "output", "task_id": "child-1"})
    assert isinstance(result, ToolSuccess)
    assert _payload(result)["data"] == {"files": ["z"], "ok": True}
