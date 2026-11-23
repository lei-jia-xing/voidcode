"""Unit tests for the Subagents plugin: state contract, task control, and bounded batches."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from tests.fixtures.fake_services import FakeServices
from voidcode.core.cancellation import Cancellation
from voidcode.plugins.subagents import subagents_plugin
from voidcode.plugins.subagents.agent_reader import read_agent_resource
from voidcode.plugins.subagents.profiles.registry import load_agent_registry
from voidcode.plugins.subagents.subagents_plugin import (
    CANCEL_REASON,
    MAX_BATCH_SIZE,
    SCHEMA_VERSION,
    ChildRecord,
    DelegationState,
    TaskBatchTool,
    TaskTool,
    _merge_batch_prompt,
)
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.resources import ResourceReadCaller, ResourceReadRequest
from voidcode.sdk.services import (
    BudgetExhaustion,
    ChildOutcome,
    ChildSendReceipt,
    ChildSessionSnapshot,
    DescendantNode,
    EventView,
)
from voidcode.sdk.tools import TextOutput, ToolCall, ToolFailure, ToolResult, ToolSuccess, UnconfirmedStop


def _context(
    workspace: Path,
    *,
    origin_readonly: bool | None = None,
    abort_signal: Cancellation | None = None,
) -> ToolContext:
    return ToolContext(
        workspace=workspace,
        session_id="session-1",
        run_id="run-1",
        invocation_id="inv-1",
        revision_id="rev-1",
        actor="model",
        logical_turn="turn-1",
        origin_readonly=origin_readonly,
        abort_signal=abort_signal,
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


def _loaded(payload: Mapping[str, object] | None) -> DelegationState:
    services = FakeServices()
    if payload is not None:
        services.seed_state(payload)
    return subagents_plugin._store(services).load()[0]


def _stored(services: FakeServices) -> DelegationState:
    return subagents_plugin._store(services).load()[0]


def _builtin_body(workspace: Path, profile_id: str) -> str:
    manifest = load_agent_registry(workspace).get(profile_id)
    assert manifest is not None
    return manifest.prompt


def _custom_workspace(tmp_path: Path, filename: str, markdown: str) -> Path:
    workspace = tmp_path / "workspace"
    project_dir = workspace / ".claude" / "agents"
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / filename).write_text(markdown, encoding="utf-8")
    return workspace


def _task_at(services: FakeServices, workspace: Path, arguments: Mapping[str, object]) -> ToolResult:
    call = ToolCall(tool_name="task", arguments=arguments, tool_call_id="call-1")
    return TaskTool().invoke(call, context=_context(workspace), services=services)


def _task_with_context(services: FakeServices, context: ToolContext, arguments: Mapping[str, object]) -> ToolResult:
    call = ToolCall(tool_name="task", arguments=arguments, tool_call_id="call-1")
    return TaskTool().invoke(call, context=context, services=services)


def _batch_at(services: FakeServices, workspace: Path, arguments: Mapping[str, object]) -> ToolResult:
    call = ToolCall(tool_name="task_batch", arguments=arguments, tool_call_id="call-2")
    return TaskBatchTool().invoke(call, context=_context(workspace), services=services)


def _batch_with_context(services: FakeServices, context: ToolContext, arguments: Mapping[str, object]) -> ToolResult:
    call = ToolCall(tool_name="task_batch", arguments=arguments, tool_call_id="call-2")
    return TaskBatchTool().invoke(call, context=context, services=services)


_RESULT_SCHEMA = {
    "type": "object",
    "required": ["ok"],
    "properties": {"ok": {"type": "boolean"}},
    "additionalProperties": False,
}


# --- State contract ---


def test_state_round_trip_keeps_children_and_groups() -> None:
    state = DelegationState(
        children={"child-1": ChildRecord(label="explore", group_id="g1", subagent_type="explore"), "child-2": ChildRecord()},
        groups={"g1": ("child-1",)},
    )
    payload = state.to_payload()
    assert payload == {
        "schema_version": SCHEMA_VERSION,
        "children": {
            "child-1": {"label": "explore", "group_id": "g1", "subagent_type": "explore", "result_schema": None},
            "child-2": {"label": "", "group_id": None, "subagent_type": None, "result_schema": None},
        },
        "groups": {"g1": ["child-1"]},
    }
    assert DelegationState.from_payload(payload) == state
    assert _loaded(None) == DelegationState()


def test_state_rejects_rows_written_before_profile_selection() -> None:
    """A child row without the current shape fails loudly instead of restoring with guesses."""
    payload = {
        "schema_version": SCHEMA_VERSION,
        "children": {"child-1": {"label": "a", "group_id": None}},
        "groups": {},
    }
    with pytest.raises(ValueError, match="missing field"):
        DelegationState.from_payload(payload)


def test_state_round_trip_accepts_handler_snapshots() -> None:
    """Plugin state reaches a handler as an immutable JSON snapshot: arrays are tuples."""
    payload = {
        "schema_version": SCHEMA_VERSION,
        "children": {"child-1": {"label": "a", "group_id": "g1", "subagent_type": None, "result_schema": None}},
        "groups": {"g1": ("child-1",)},
    }
    assert DelegationState.from_payload(payload).groups == {"g1": ("child-1",)}


@pytest.mark.parametrize(
    "payload",
    [
        {"schema_version": 2, "children": {}, "groups": {}},
        {"schema_version": SCHEMA_VERSION, "children": [], "groups": {}},
        {"schema_version": SCHEMA_VERSION, "children": {}, "groups": []},
        {"schema_version": SCHEMA_VERSION, "children": {"child-1": {"label": "a"}}, "groups": {}},
        {"schema_version": SCHEMA_VERSION, "children": {"child-1": {"label": 1, "group_id": None}}, "groups": {}},
        {"schema_version": SCHEMA_VERSION, "children": {"child-1": {"label": "a", "group_id": ""}}, "groups": {}},
        {"schema_version": SCHEMA_VERSION, "children": {"child-1": {"label": "a", "group_id": "g1", "extra": 1}}, "groups": {"g1": ["child-1"]}},
        {"schema_version": SCHEMA_VERSION, "children": {"child-1": {"label": "a", "group_id": None}}, "groups": {"g1": ["child-1"]}},
        {"schema_version": SCHEMA_VERSION, "children": {"child-1": {"label": "a", "group_id": "g1"}}, "groups": {"g1": ["child-2"]}},
        {"schema_version": SCHEMA_VERSION, "children": {"child-1": {"label": "a", "group_id": "g9"}}, "groups": {}},
        {"schema_version": SCHEMA_VERSION, "children": {"child-1": {"label": "a", "group_id": "g1"}}, "groups": {"g1": ["child-1", "child-1"]}},
        {"schema_version": SCHEMA_VERSION, "children": {"child-1": {"label": "a", "group_id": "g1"}}, "groups": {"g1": "child-1"}},
        {"schema_version": SCHEMA_VERSION, "children": {"child-1": {"label": "a", "group_id": None, "subagent_type": ""}}, "groups": {}},
        {"schema_version": SCHEMA_VERSION, "children": {"child-1": {"label": "a", "group_id": None, "subagent_type": 7}}, "groups": {}},
    ],
)
def test_state_rejects_malformed_documents(payload: Mapping[str, object]) -> None:
    with pytest.raises(ValueError):
        DelegationState.from_payload(payload)


def test_store_rejects_unknown_and_missing_top_level_keys() -> None:
    with pytest.raises(ValueError, match="not supported"):
        _loaded({"schema_version": SCHEMA_VERSION, "children": {}, "groups": {}, "unknown": 1})
    with pytest.raises(ValueError, match="missing field"):
        _loaded({"schema_version": SCHEMA_VERSION})


# --- task: spawning ---


def test_spawn_passes_arguments_through_and_records_roster_event(tmp_path: Path) -> None:
    services = FakeServices()
    res = _task(
        services,
        tmp_path,
        {"prompt": "explore the docs", "label": "explore", "tool_allowlist": ["read", "glob"], "max_steps": 5},
    )
    assert isinstance(res, ToolSuccess)
    assert services.spawn_calls == [
        {
            "prompt": "explore the docs",
            "label": "explore",
            "tool_allowlist": ("read", "glob"),
            "max_steps": 5,
            "readonly": False,
            "notify_on_completion": True,
            "idempotency_key": None,
            "result_schema": None,
        }
    ]
    assert services.join_calls == []
    assert _payload(res) == {
        "operation": "spawn",
        "task_id": "child-1",
        "label": "explore",
        "agent_uri": "agent://child-1",
        "history_uri": "transcript://child-1",
    }
    assert "child-1" in res.output.text
    assert "background" in res.output.text
    assert "Current status/latest published output: agent://child-1" in res.output.text

    state = _stored(services)
    assert state.children == {"child-1": ChildRecord(label="explore", group_id=None)}
    assert state.groups == {}
    assert services.staged_events == [("task_spawned", {"task_id": "child-1", "label": "explore", "group_id": None})]


def test_synchronous_spawn_joins_and_returns_the_child_outcome(tmp_path: Path) -> None:
    services = FakeServices()
    res = _task(services, tmp_path, {"prompt": "do the work", "run_in_background": False, "timeout": 2_500})
    assert isinstance(res, ToolSuccess)
    assert services.join_calls == [("child-1", 1.0)]
    payload = _payload(res)
    assert payload["status"] == "completed"
    assert payload["output"] == "result of do the work"
    assert "result of do the work" in res.output.text
    assert "status=completed" in res.output.text
    assert _stored(services).children == {"child-1": ChildRecord()}


def test_synchronous_join_slices_preserve_the_caller_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    services = FakeServices()
    calls: list[float] = []

    def join_slices(child_id: str, *, timeout_seconds: float) -> ChildOutcome:
        calls.append(timeout_seconds)
        status = "running" if len(calls) < 3 else "completed"
        return ChildOutcome(
            child_session_id=child_id,
            status=status,
            output="finished" if status == "completed" else None,
            run_id="run-child-1",
        )

    monkeypatch.setattr(services, "join_child", join_slices)
    res = _task(services, tmp_path, {"prompt": "work", "run_in_background": False, "timeout": 2_500})
    assert isinstance(res, ToolSuccess)
    assert calls == [1.0, 1.0, 0.5]
    assert _payload(res)["status"] == "completed"


def test_synchronous_spawn_without_timeout_uses_short_slices(tmp_path: Path) -> None:
    services = FakeServices()
    _task(services, tmp_path, {"prompt": "work", "run_in_background": False})
    assert services.join_calls == [("child-1", 1.0)]


# --- task: profile selection ---


def test_profile_spawn_materializes_the_prompt_and_applies_its_scope(tmp_path: Path) -> None:
    services = FakeServices()
    res = _task(services, tmp_path, {"prompt": "map the retry paths", "label": "explore", "subagent_type": "explore"})

    assert isinstance(res, ToolSuccess)
    assert services.spawn_calls == [
        {
            "prompt": f"{_builtin_body(tmp_path, 'explore')}\n\nmap the retry paths",
            "label": "explore",
            "tool_allowlist": None,
            "max_steps": None,
            "readonly": True,
            "notify_on_completion": True,
            "idempotency_key": None,
            "result_schema": None,
        }
    ]
    assert _payload(res) == {
        "operation": "spawn",
        "task_id": "child-1",
        "label": "explore",
        "subagent_type": "explore",
        "agent_uri": "agent://child-1",
        "history_uri": "transcript://child-1",
    }
    assert "profile=explore" in res.output.text
    assert _stored(services).children == {"child-1": ChildRecord(label="explore", subagent_type="explore")}


def test_profile_child_scope_is_reported_by_the_control_path(tmp_path: Path) -> None:
    services = FakeServices()
    _task(services, tmp_path, {"prompt": "plan it", "subagent_type": "plan"})

    res = _task(services, tmp_path, {"operation": "output", "task_id": "child-1"})

    assert _payload(res)["subagent_type"] == "plan"
    assert "profile=plan" in res.output.text

    cancelled = _task(services, tmp_path, {"operation": "cancel", "task_id": "child-1"})
    assert _payload(cancelled)["subagent_type"] == "plan"
    assert "profile=plan" in cancelled.output.text


def test_unknown_subagent_type_fails_without_spawning(tmp_path: Path) -> None:
    services = FakeServices()
    res = _task(services, tmp_path, {"prompt": "work", "subagent_type": "ghost"})

    assert isinstance(res, ToolFailure)
    assert res.error == "task unknown subagent_type: 'ghost'"
    assert services.spawn_calls == []
    assert services.get_state("session") == (None, 0)


def test_execute_agent_is_a_normal_delegatable_definition(tmp_path: Path) -> None:
    services = FakeServices()
    result = _task(services, tmp_path, {"prompt": "work", "subagent_type": "execute"})
    assert isinstance(result, ToolSuccess)
    assert services.spawn_calls[0]["prompt"] == f"{_builtin_body(tmp_path, 'execute')}\n\nwork"


def test_custom_profile_spawns_with_its_own_body_scope_and_writable_stance(tmp_path: Path) -> None:
    workspace = _custom_workspace(
        tmp_path,
        "helper.md",
        "---\nname: helper\ndescription: Local helper\n---\n\nHelp locally.\n",
    )
    services = FakeServices()
    res = _task_at(services, workspace, {"prompt": "count doors", "subagent_type": "helper"})

    assert isinstance(res, ToolSuccess)
    assert services.spawn_calls[0]["prompt"] == "Help locally.\n\ncount doors"
    assert services.spawn_calls[0]["tool_allowlist"] is None
    assert services.spawn_calls[0]["readonly"] is False
    assert _stored(services).children == {"child-1": ChildRecord(subagent_type="helper")}


def test_profile_and_explicit_allowlist_can_be_combined(tmp_path: Path) -> None:
    services = FakeServices()
    res = _task(services, tmp_path, {"prompt": "work", "subagent_type": "plan", "tool_allowlist": ["read"]})
    assert isinstance(res, ToolSuccess)
    assert services.spawn_calls[0]["tool_allowlist"] == ("read",)
    assert services.spawn_calls[0]["readonly"] is True


def test_subagent_type_applies_to_spawns_only(tmp_path: Path) -> None:
    services = FakeServices()
    res = _task(services, tmp_path, {"operation": "output", "task_id": "child-1", "subagent_type": "plan"})

    assert isinstance(res, ToolFailure)
    assert "applies to a spawn only" in res.error
    assert services.spawn_calls == []


def test_profile_inherits_scope_without_tool_discovery(tmp_path: Path) -> None:
    services = FakeServices()
    services.unavailable_tools = {"read", "glob", "grep", "ast_grep", "lsp"}
    res = _task(services, tmp_path, {"prompt": "map it", "subagent_type": "explore"})
    assert isinstance(res, ToolSuccess)
    assert services.spawn_calls[0]["tool_allowlist"] is None
    assert services.spawn_calls[0]["readonly"] is True
    assert "notes" not in _payload(res)


@pytest.mark.parametrize("failure", [ValueError("spawn refused"), RuntimeError("host refused")])
def test_profile_spawn_failure_is_not_retried_with_a_wider_scope(tmp_path: Path, failure: Exception) -> None:
    services = FakeServices()
    services.spawn_failures = {1: failure}
    res = _task(services, tmp_path, {"prompt": "map it", "subagent_type": "explore"})
    assert isinstance(res, ToolFailure)
    assert str(failure) in res.error
    assert len(services.spawn_calls) == 1
    assert services.get_state("session") == (None, 0)


def test_no_profile_keeps_the_calling_allowlist_and_writable_stance(tmp_path: Path) -> None:
    services = FakeServices()
    res = _task(services, tmp_path, {"prompt": "plain work", "tool_allowlist": ["read", "glob"]})

    assert isinstance(res, ToolSuccess)
    assert services.spawn_calls[0]["prompt"] == "plain work"
    assert services.spawn_calls[0]["tool_allowlist"] == ("read", "glob")
    assert services.spawn_calls[0]["readonly"] is False
    assert "subagent_type" not in _payload(res)
    assert _stored(services).children == {"child-1": ChildRecord()}


def test_unsupported_profile_model_is_refused_before_spawning(tmp_path: Path) -> None:
    workspace = _custom_workspace(tmp_path, "picky.md", "---\nname: picky\ndescription: Choosy helper\nmodel: endpoint/gpt-x\n---\nPick well.")
    services = FakeServices()
    result = _task_at(services, workspace, {"prompt": "work", "subagent_type": "picky"})
    assert isinstance(result, ToolFailure)
    assert "per-agent model selection" in result.error
    assert services.spawn_calls == []


@pytest.mark.parametrize("subagent_type", [None, "execute"])
def test_readonly_parent_spawns_readonly_child(tmp_path: Path, subagent_type: str | None) -> None:
    services = FakeServices()
    arguments: dict[str, object] = {"prompt": "work"}
    if subagent_type is not None:
        arguments["subagent_type"] = subagent_type
    result = _task_with_context(services, _context(tmp_path, origin_readonly=True), arguments)

    assert isinstance(result, ToolSuccess)
    assert services.spawn_calls[0]["readonly"] is True
    assert _stored(services).children == {"child-1": ChildRecord(subagent_type=subagent_type)}


def test_writable_parent_keeps_the_child_writable(tmp_path: Path) -> None:
    services = FakeServices()
    result = _task_with_context(services, _context(tmp_path, origin_readonly=False), {"prompt": "work"})

    assert isinstance(result, ToolSuccess)
    assert services.spawn_calls[0]["readonly"] is False


def test_spawn_cancelled_before_creating_does_not_spawn(tmp_path: Path) -> None:
    services = FakeServices()
    abort = Cancellation()
    abort.set_cancelled(True, reason="test stop")
    result = _task_with_context(services, _context(tmp_path, abort_signal=abort), {"prompt": "work"})

    assert isinstance(result, ToolFailure)
    assert result.execution == UnconfirmedStop(cancellation_signalled=True)
    assert services.spawn_calls == []
    assert "task_id" not in _payload(result)
    assert services.staged_events == []
    assert services.get_state("session") == (None, 0)


def test_spawn_cancelled_after_creation_records_the_mapping(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    services = FakeServices()
    abort = Cancellation()
    spawn = services.spawn_child

    def spawn_then_stop(*args: object, **kwargs: object) -> str:
        child_id = spawn(*args, **kwargs)  # type: ignore[arg-type]
        abort.set_cancelled(True, reason="test stop")
        return child_id

    monkeypatch.setattr(services, "spawn_child", spawn_then_stop)
    result = _task_with_context(services, _context(tmp_path, abort_signal=abort), {"prompt": "work"})

    assert isinstance(result, ToolFailure)
    assert result.execution == UnconfirmedStop(cancellation_signalled=True)
    assert _payload(result)["task_id"] == "child-1"
    assert _stored(services).children == {"child-1": ChildRecord()}
    assert services.staged_events == [("task_spawned", {"task_id": "child-1", "label": "", "group_id": None})]


def test_spawn_store_failure_reports_the_created_child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    services = FakeServices()

    def refuse_state(*args: object, **kwargs: object) -> int:
        raise RuntimeError("state store is unavailable")

    monkeypatch.setattr(services, "replace_state", refuse_state)
    result = _task(services, tmp_path, {"prompt": "work"})

    assert isinstance(result, ToolFailure)
    assert "child session was created" in result.error
    assert "could not be saved" in result.error
    assert _payload(result)["task_id"] == "child-1"
    assert len(services.spawn_calls) == 1
    assert services.staged_events == []


# --- task: control ---


def test_output_joins_with_the_requested_timeout_and_surfaces_running(tmp_path: Path) -> None:
    services = FakeServices()
    _task(services, tmp_path, {"prompt": "long work", "label": "long"})
    services.roster["child-1"] = ChildOutcome(child_session_id="child-1", status="running")
    res = _task(services, tmp_path, {"operation": "output", "task_id": "child-1", "timeout": 500})
    assert isinstance(res, ToolSuccess)
    assert services.join_calls == [("child-1", 0.5)]
    payload = _payload(res)
    assert payload == {
        "operation": "output",
        "task_id": "child-1",
        "label": "long",
        "status": "running",
        "agent_uri": "agent://child-1",
        "history_uri": "transcript://child-1",
    }
    assert "status=running" in res.output.text
    assert 'task(operation="output"' in res.output.text

    zero = _task(services, tmp_path, {"operation": "output", "task_id": "child-1", "timeout": 0})
    assert services.join_calls[-1] == ("child-1", 0.0)
    assert _payload(zero)["status"] == "running"


def test_output_join_observes_parent_stop_between_slices(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    services = FakeServices()
    _task(services, tmp_path, {"prompt": "long work"})
    abort = Cancellation()
    calls: list[float] = []

    def join_then_stop(child_id: str, *, timeout_seconds: float) -> ChildOutcome:
        calls.append(timeout_seconds)
        abort.set_cancelled(True, reason="test stop")
        return ChildOutcome(child_session_id=child_id, status="running", run_id="run-child-1")

    monkeypatch.setattr(services, "join_child", join_then_stop)
    result = _task_with_context(
        services,
        _context(tmp_path, abort_signal=abort),
        {"operation": "output", "task_id": "child-1", "timeout": 10_000},
    )

    assert isinstance(result, ToolFailure)
    assert result.execution == UnconfirmedStop(cancellation_signalled=True)
    assert calls == [1.0]
    assert _payload(result)["task_id"] == "child-1"
    assert "cancelled with the parent task" in result.error


def test_output_reports_the_terminal_child_outcome(tmp_path: Path) -> None:
    services = FakeServices()
    _task(services, tmp_path, {"prompt": "work"})
    services.roster["child-1"] = ChildOutcome(child_session_id="child-1", status="failed", output=None, error="child blew up", stop_cause="error")
    res = _task(services, tmp_path, {"operation": "output", "task_id": "child-1"})
    payload = _payload(res)
    assert payload["status"] == "failed"
    assert payload["error"] == "child blew up"
    assert payload["stop_cause"] == "error"
    assert "child blew up" in res.output.text
    assert "budget_exhaustion" not in payload


def test_output_preserves_typed_budget_exhaustion(tmp_path: Path) -> None:
    services = FakeServices()
    _task(services, tmp_path, {"prompt": "budgeted work"})
    outcome = ChildOutcome(
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
    services.roster["child-1"] = outcome

    result = _task(services, tmp_path, {"operation": "output", "task_id": "child-1"})

    payload = _payload(result)
    assert payload["status"] == "interrupted"
    assert payload["stop_cause"] == "limit_reached"
    assert payload["budget_exhaustion"] == outcome.as_payload()["budget_exhaustion"]
    assert "budget_exhaustion: dimension=provider_turns" in result.output.text
    assert services.acks == [], "recoverable budget stops are not completion notices"


def test_output_preserves_typed_capture_errors_without_relabeling(tmp_path: Path) -> None:
    services = FakeServices()
    _task(services, tmp_path, {"prompt": "work"})
    output_cause = "ArtifactStoreError: quota exceeded"
    history_cause = "HistoryStoreError: snapshot write failed"
    services.roster["child-1"] = ChildOutcome(
        child_session_id="child-1",
        status="completed",
        output=None,
        run_id="run-capped",
        output_preview="partial result…",
        output_bytes=40000,
        output_error=output_cause,
        history_error=history_cause,
    )

    res = _task(services, tmp_path, {"operation": "output", "task_id": "child-1"})

    payload = _payload(res)
    assert payload["status"] == "completed"
    assert payload["run_id"] == "run-capped"
    assert payload["output_preview"] == "partial result…"
    assert payload["output_bytes"] == 40000
    assert payload["output_error"] == output_cause
    assert payload["history_error"] == history_cause
    assert "output_uri" not in payload
    assert isinstance(res.output, TextOutput)
    assert output_cause in res.output.text
    assert history_cause in res.output.text
    assert "partial result…" in res.output.text


def test_foreground_spawn_validates_a_declared_result_schema(tmp_path: Path) -> None:
    services = FakeServices()
    services.join_output = '{"ok": true}'
    result = _task(
        services,
        tmp_path,
        {"prompt": "shape the answer", "run_in_background": False, "result_schema": _RESULT_SCHEMA},
    )

    assert isinstance(result, ToolSuccess)
    payload = _payload(result)
    assert payload["data"] == {"ok": True}
    assert "parse_error" not in payload
    assert "structured: ok" in result.output.text


def test_foreground_spawn_reports_a_structured_parse_error(tmp_path: Path) -> None:
    services = FakeServices()
    services.join_output = '{"ok": "yes"}'
    result = _task(
        services,
        tmp_path,
        {"prompt": "shape the answer", "run_in_background": False, "result_schema": _RESULT_SCHEMA},
    )

    assert isinstance(result, ToolSuccess)
    payload = _payload(result)
    assert "data" not in payload
    assert isinstance(payload["parse_error"], Mapping)
    assert payload["parse_error"]["kind"] == "schema_violation"
    assert "structured: schema_violation" in result.output.text


def test_output_surfaces_the_unavailable_structured_cause_in_the_body(tmp_path: Path) -> None:
    services = FakeServices()
    _task(services, tmp_path, {"prompt": "work", "result_schema": _RESULT_SCHEMA})
    cause = "ArtifactStoreError: quota exceeded"
    services.roster["child-1"] = ChildOutcome(
        child_session_id="child-1",
        status="completed",
        output=None,
        run_id="run-capped",
        output_error=cause,
    )

    result = _task(services, tmp_path, {"operation": "output", "task_id": "child-1"})

    payload = _payload(result)
    assert payload["structured_unavailable"] == cause
    assert "data" not in payload
    assert "parse_error" not in payload
    assert f"structured: unavailable ({cause})" in result.output.text


def test_structured_validation_refuses_full_output_above_the_byte_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    services = FakeServices()
    reference = services.spill_artifact('{"ok": true}')
    _task(services, tmp_path, {"prompt": "work", "result_schema": _RESULT_SCHEMA})
    services.roster["child-1"] = ChildOutcome(
        child_session_id="child-1",
        status="completed",
        output_sha256=reference.sha256,
        output_bytes=reference.bytes,
        run_id="run-capped",
    )
    monkeypatch.setattr(subagents_plugin, "MAX_STRUCTURED_OUTPUT_BYTES", 4)

    result = _task(services, tmp_path, {"operation": "output", "task_id": "child-1"})

    payload = _payload(result)
    cause = payload["structured_unavailable"]
    assert isinstance(cause, str)
    assert "above the 4-byte cap" in cause
    assert "data" not in payload
    assert "parse_error" not in payload
    assert "structured: unavailable" in result.output.text


def test_agent_reader_shows_only_the_publication_run_own_errors(tmp_path: Path) -> None:
    services = FakeServices()
    reference = services.spill_artifact("published findings", media_type="text/plain; charset=utf-8")
    history_cause = "HistoryStoreError: snapshot write failed"
    snapshot = ChildSessionSnapshot(
        descendant=DescendantNode(
            session_id="child-1",
            parent_session_id="session-1",
            depth=1,
            status="running",
            run_id="run-B",
        ),
        latest_outcome=ChildOutcome(
            child_session_id="child-1",
            status="completed",
            output="published findings",
            run_id="run-A",
            output_sha256=reference.sha256,
            output_bytes=reference.bytes,
            history_error=history_cause,
        ),
    )

    class _AgentServices:
        def read_child_snapshot(self, child_session_id: str) -> ChildSessionSnapshot | None:
            assert child_session_id == "child-1"
            return snapshot

        def read_artifact(self, sha256: str, *, offset: int = 0, limit: int = 65536):  # type: ignore[no-untyped-def]
            return services.read_artifact(sha256, offset=offset, limit=limit)

    page = read_agent_resource(
        ResourceReadRequest(uri="agent://child-1"),
        ResourceReadCaller(session_id="session-1", workspace=tmp_path, revision_id="rev-1", run_id="run-1"),
        _AgentServices(),  # type: ignore[arg-type]
    )

    assert page is not None
    assert isinstance(page.output, TextOutput)
    assert "run-A" in page.output.text
    assert "run-B" in page.output.text
    assert history_cause in page.output.text
    latest = page.body["latest_output"]
    assert isinstance(latest, Mapping)
    assert latest["run_id"] == "run-A"
    assert latest["history_error"] == history_cause
    assert latest["output_error"] is None


def test_send_maps_the_receipt_without_claiming_consumption(tmp_path: Path) -> None:
    services = FakeServices()
    _task(services, tmp_path, {"prompt": "work", "label": "sender"})
    services.send_result = ChildSendReceipt(
        child_session_id="child-1",
        delivery="started",
        child_status="running",
        run_id="run-2",
        message_id="msg-1",
    )
    res = _task(services, tmp_path, {"operation": "send", "task_id": "child-1", "prompt": "check the north entrance"})
    assert isinstance(res, ToolSuccess)
    assert services.send_calls == [
        {"child_session_id": "child-1", "prompt": "check the north entrance", "label": "sender", "notify_on_completion": True}
    ]
    payload = _payload(res)
    assert payload["operation"] == "send"
    assert payload["delivery"] == "started"
    assert payload["child_status"] == "running"
    assert payload["run_id"] == "run-2"
    assert payload["message_id"] == "msg-1"
    assert payload["agent_uri"] == "agent://child-1"
    assert payload["history_uri"] == "transcript://child-1"
    assert isinstance(res.output, TextOutput)
    assert "started in the same session" in res.output.text

    services.send_result = ChildSendReceipt(child_session_id="child-1", delivery="queued", child_status="running")
    queued = _task(services, tmp_path, {"operation": "send", "task_id": "child-1", "prompt": "later"})
    assert _payload(queued)["delivery"] == "queued"
    assert isinstance(queued.output, TextOutput)
    assert "queued behind the child's current work" in queued.output.text
    assert "does not mean the child read or finished it" in queued.output.text


def test_parked_send_receipt_requires_resume_and_never_promises_consumption(tmp_path: Path) -> None:
    services = FakeServices()
    _task(services, tmp_path, {"prompt": "work"})
    services.send_result = ChildSendReceipt(child_session_id="child-1", delivery="parked", child_status="interrupted")

    result = _task(services, tmp_path, {"operation": "send", "task_id": "child-1", "prompt": "resume this"})

    assert isinstance(result, ToolSuccess)
    assert _payload(result)["delivery"] == "parked"
    assert isinstance(result.output, TextOutput)
    assert "durably accepted but not claimable now" in result.output.text
    assert "explicit resume" in result.output.text
    assert "does not mean the child read or finished it" in result.output.text
    assert "next safe boundary" not in result.output.text


def test_history_pages_through_the_cursor_without_loss(tmp_path: Path) -> None:
    services = FakeServices()
    _task(services, tmp_path, {"prompt": "work", "label": "hist"})
    filler = "x" * (20 * 1024)
    services.child_events["child-1"] = tuple(
        EventView(
            sequence=index + 1,
            event_type="core.user_message",
            run_id="run-1",
            revision_id="rev-1",
            source="user",
            actor="user",
            payload={"content": f"E{index + 1}-START{filler}E{index + 1}-END"},
        )
        for index in range(3)
    ) + (
        EventView(
            sequence=4,
            event_type="core.user_message",
            run_id="run-1",
            revision_id="rev-1",
            source="user",
            actor="user",
            payload={"content": f"E4-START{'y' * (50 * 1024)}E4-END"},
        ),
    )

    pages: list[str] = []
    cursors: list[tuple[int, int]] = []
    split_offsets: list[int] = []
    after, offset = 0, 0
    for _ in range(8):
        res = _task(
            services,
            tmp_path,
            {"operation": "history", "task_id": "child-1", "after_sequence": after, "history_offset": offset},
        )
        assert isinstance(res, ToolSuccess)
        payload = _payload(res)
        cursors.append((after, offset))
        assert isinstance(res.output, TextOutput)
        pages.append(res.output.text)
        if not payload["truncated"]:
            break
        next_after = payload["next_after_sequence"]
        next_offset = payload["next_event_offset"]
        assert isinstance(next_after, int) and isinstance(next_offset, int)
        after, offset = next_after, next_offset
        split_offsets.append(offset)
    else:
        raise AssertionError("history did not reach its end within 8 pages")

    assert len(pages) >= 3, "the transcript must span multiple bounded pages"
    assert any(offset > 0 for offset in split_offsets), "a block larger than one page is split, not clipped"
    assert len(set(cursors)) == len(cursors)
    text = "\n\n".join(pages)
    for index in range(1, 5):
        assert text.count(f"E{index}-START") == 1
        assert text.count(f"E{index}-END") == 1
    assert text.count("y" * (20 * 1024)) >= 1, "the oversized block keeps its content across the split"


def test_unknown_task_ids_fail_without_touching_the_core(tmp_path: Path) -> None:
    services = FakeServices()
    _task(services, tmp_path, {"prompt": "work"})
    for operation in ("output", "cancel"):
        res = _task(services, tmp_path, {"operation": operation, "task_id": "child-404"})
        assert isinstance(res, ToolFailure)
        assert res.error == "unknown task: child-404"
    assert services.join_calls == []
    assert services.cancel_calls == []


def test_cancel_reports_acceptance(tmp_path: Path) -> None:
    services = FakeServices()
    _task(services, tmp_path, {"prompt": "work", "label": "cancel me"})
    res = _task(services, tmp_path, {"operation": "cancel", "task_id": "child-1"})
    assert isinstance(res, ToolSuccess)
    assert services.cancel_calls == [("child-1", CANCEL_REASON)]
    assert _payload(res) == {
        "operation": "cancel",
        "task_id": "child-1",
        "label": "cancel me",
        "cancelled": True,
        "agent_uri": "agent://child-1",
    }
    assert "cancellation accepted" in res.output.text

    services.cancel_result = False
    rejected = _task(services, tmp_path, {"operation": "cancel", "task_id": "child-1"})
    assert _payload(rejected)["cancelled"] is False
    assert "not accepted" in rejected.output.text


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"prompt": "x", "operation": "output", "task_id": "child-1"}, "one mode per call"),
        ({"prompt": "x", "task_id": "child-1"}, "one mode per call"),
        ({"prompt": "x", "operation": "cancel"}, "one mode per call"),
        ({"operation": "output"}, "requires a task_id"),
        ({"operation": "cancel"}, "requires a task_id"),
        ({"task_id": "child-1"}, "one mode per call"),
        ({}, "requires a prompt"),
        ({"prompt": "   "}, "must be non-empty"),
        ({"operation": "ps"}, "operation must be one of output, cancel, send, history"),
        ({"prompt": "x", "max_steps": 0}, "max_steps must be a positive integer"),
        ({"prompt": "x", "timeout": -1}, "timeout must be a non-negative integer"),
        ({"prompt": "x", "tool_allowlist": []}, "tool_allowlist must be a non-empty array"),
        ({"prompt": "x", "run_in_background": "yes"}, "run_in_background must be a boolean"),
    ],
)
def test_task_argument_conflicts_fail_without_spawning(tmp_path: Path, arguments: Mapping[str, object], message: str) -> None:
    services = FakeServices()
    res = _task(services, tmp_path, arguments)
    assert isinstance(res, ToolFailure)
    assert message in res.error
    assert res.output.text == res.error
    assert services.spawn_calls == []
    assert services.staged_events == []
    assert services.get_state("session") == (None, 0)


def test_core_spawn_refusal_is_reported_as_a_failure(tmp_path: Path) -> None:
    services = FakeServices()
    services.spawn_failures = {1: "tool_allowlist names an unknown tool: nope"}
    res = _task(services, tmp_path, {"prompt": "work", "tool_allowlist": ["nope"]})
    assert isinstance(res, ToolFailure)
    assert res.error == "tool_allowlist names an unknown tool: nope"
    assert services.get_state("session") == (None, 0)


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"prompt": "x", "bogus": 1}, "task field 'bogus' is not supported"),
        ({"prompt": "x", "timeout": 1_000}, "does not apply to a background spawn"),
        ({"prompt": "x", "output_offset": 1}, "does not apply to a background spawn"),
        ({"prompt": "x", "output_limit": 10}, "does not apply to a background spawn"),
        ({"prompt": "x", "after_sequence": 1}, "does not apply to a background spawn"),
        ({"prompt": "x", "run_in_background": False, "after_sequence": 1}, "does not apply to a foreground spawn"),
        ({"operation": "output", "task_id": "child-1", "label": "x"}, 'does not apply to operation="output"'),
        ({"operation": "cancel", "task_id": "child-1", "max_steps": 2}, 'does not apply to operation="cancel"'),
        ({"operation": "cancel", "task_id": "child-1", "run_in_background": True}, 'does not apply to operation="cancel"'),
        ({"operation": "send", "task_id": "child-1", "prompt": "p", "timeout": 10}, 'does not apply to operation="send"'),
        ({"operation": "history", "task_id": "child-1", "output_limit": 10}, 'does not apply to operation="history"'),
    ],
)
def test_task_rejects_unknown_or_mode_irrelevant_fields(
    tmp_path: Path,
    arguments: Mapping[str, object],
    message: str,
) -> None:
    services = FakeServices()
    result = _task(services, tmp_path, arguments)
    assert isinstance(result, ToolFailure)
    assert message in result.error
    assert services.spawn_calls == []
    assert services.staged_events == []
    assert services.get_state("session") == (None, 0)


# --- task_batch ---


def test_batch_preflights_every_item_before_any_spawn(tmp_path: Path) -> None:
    services = FakeServices()
    too_many = _batch(services, tmp_path, {"tasks": [{"prompt": "ok"} for _ in range(MAX_BATCH_SIZE + 1)]})
    assert isinstance(too_many, ToolFailure)
    assert f"at most {MAX_BATCH_SIZE} tasks, got {MAX_BATCH_SIZE + 1}" in too_many.error

    empty_prompt = _batch(services, tmp_path, {"tasks": [{"prompt": "ok"}, {"prompt": "  "}, {"prompt": "ok"}]})
    assert isinstance(empty_prompt, ToolFailure)
    assert "task_batch item 1 requires a non-empty prompt" in empty_prompt.error

    bad_allowlist = _batch(services, tmp_path, {"tasks": [{"prompt": "ok"}, {"prompt": "x", "tool_allowlist": []}]})
    assert isinstance(bad_allowlist, ToolFailure)
    assert "task_batch item 1 tool_allowlist must be a non-empty array" in bad_allowlist.error

    unknown_field = _batch(services, tmp_path, {"tasks": [{"prompt": "ok", "load_skills": []}]})
    assert isinstance(unknown_field, ToolFailure)
    assert "task_batch item 0 field 'load_skills' is not supported" in unknown_field.error

    assert services.spawn_calls == []
    assert services.staged_events == []
    assert services.get_state("session") == (None, 0)


def test_batch_argument_conflicts_fail(tmp_path: Path) -> None:
    services = FakeServices()
    both_modes = _batch(services, tmp_path, {"tasks": [{"prompt": "x"}], "operation": "output", "parallel_group_id": "g1"})
    assert isinstance(both_modes, ToolFailure)
    assert "one mode per call" in both_modes.error

    no_tasks = _batch(services, tmp_path, {})
    assert isinstance(no_tasks, ToolFailure)
    assert "requires tasks to spawn a group" in no_tasks.error

    no_group = _batch(services, tmp_path, {"operation": "output"})
    assert isinstance(no_group, ToolFailure)
    assert "requires a parallel_group_id" in no_group.error

    no_operation = _batch(services, tmp_path, {"parallel_group_id": "g1"})
    assert isinstance(no_operation, ToolFailure)
    assert "requires tasks to spawn a group" in no_operation.error
    assert services.spawn_calls == []


def test_batch_spawns_the_whole_group_and_records_it(tmp_path: Path) -> None:
    services = FakeServices()
    res = _batch(services, tmp_path, {"tasks": [{"prompt": "one", "label": "a"}, {"prompt": "two", "max_steps": 7}]})
    assert isinstance(res, ToolSuccess)
    payload = _payload(res)
    group_id = str(payload["parallel_group_id"])
    assert len(group_id) == 32
    assert payload == {
        "operation": "spawn",
        "parallel_group_id": group_id,
        "task_ids": ["child-1", "child-2"],
        "partial": False,
        "created": [
            {"index": 0, "task_id": "child-1", "agent_uri": "agent://child-1"},
            {"index": 1, "task_id": "child-2", "agent_uri": "agent://child-2"},
        ],
    }
    assert services.spawn_calls == [
        {
            "prompt": "one",
            "label": "a",
            "tool_allowlist": None,
            "max_steps": None,
            "readonly": False,
            "notify_on_completion": True,
            "idempotency_key": None,
            "result_schema": None,
        },
        {
            "prompt": "two",
            "label": "",
            "tool_allowlist": None,
            "max_steps": 7,
            "readonly": False,
            "notify_on_completion": True,
            "idempotency_key": None,
            "result_schema": None,
        },
    ]
    assert services.join_calls == []
    assert services.staged_events == [("task_batch_spawned", {"parallel_group_id": group_id, "task_ids": ["child-1", "child-2"]})]
    state = _stored(services)
    assert state.groups == {group_id: ("child-1", "child-2")}
    assert state.children == {
        "child-1": ChildRecord(label="a", group_id=group_id),
        "child-2": ChildRecord(label="", group_id=group_id),
    }
    assert "Started 2 child task(s)" in res.output.text


def test_batch_partial_spawn_keeps_created_children(tmp_path: Path) -> None:
    services = FakeServices()
    services.spawn_failures = {3: "delegation spawn budget exhausted for this chain"}
    res = _batch(
        services,
        tmp_path,
        {"tasks": [{"prompt": "one"}, {"prompt": "two"}, {"prompt": "three"}, {"prompt": "four"}]},
    )
    assert isinstance(res, ToolSuccess)
    payload = _payload(res)
    group_id = str(payload["parallel_group_id"])
    assert payload["partial"] is True
    assert payload["task_ids"] == ["child-1", "child-2", "child-3"]
    assert payload["created"] == [
        {"index": 0, "task_id": "child-1", "agent_uri": "agent://child-1"},
        {"index": 1, "task_id": "child-2", "agent_uri": "agent://child-2"},
        {"index": 3, "task_id": "child-3", "agent_uri": "agent://child-3"},
    ]
    assert payload["failed"] == [{"index": 2, "error": "delegation spawn budget exhausted for this chain"}]
    assert "Started 3 of 4 child task(s)" in res.output.text
    assert "[2] failed: delegation spawn budget exhausted for this chain" in res.output.text

    state = _stored(services)
    assert set(state.children) == {"child-1", "child-2", "child-3"}
    assert state.groups == {group_id: ("child-1", "child-2", "child-3")}
    assert len(services.spawn_calls) == 4


def test_batch_with_no_created_child_fails(tmp_path: Path) -> None:
    services = FakeServices()
    services.spawn_failures = {
        1: "delegation depth limit exceeded: depth 4 exceeds max 3",
        2: "delegation depth limit exceeded: depth 4 exceeds max 3",
    }
    res = _batch(services, tmp_path, {"tasks": [{"prompt": "one"}, {"prompt": "two"}]})
    assert isinstance(res, ToolFailure)
    assert "delegation depth limit exceeded" in res.error
    assert len(services.spawn_calls) == 2
    assert services.roster == {}
    assert services.staged_events == []
    assert services.get_state("session") == (None, 0)


def test_unprofiled_batch_does_not_load_the_agent_registry(tmp_path: Path) -> None:
    workspace = _custom_workspace(
        tmp_path,
        "broken.md",
        "---\nname: broken\ndescription: Broken helper\nmodel: endpoint/other\n---\nBroken.\n",
    )
    services = FakeServices()
    res = _batch_at(services, workspace, {"tasks": [{"prompt": "one"}, {"prompt": "two"}]})
    assert isinstance(res, ToolSuccess)
    assert len(services.spawn_calls) == 2


def test_profiled_batch_still_reports_a_malformed_agent_registry(tmp_path: Path) -> None:
    workspace = _custom_workspace(
        tmp_path,
        "broken.md",
        "---\nname: broken\ndescription: Broken helper\nmodel: endpoint/other\n---\nBroken.\n",
    )
    services = FakeServices()
    res = _batch_at(services, workspace, {"tasks": [{"prompt": "one", "subagent_type": "execute"}]})
    assert isinstance(res, ToolFailure)
    assert "agent profile registry is unavailable" in res.error
    assert services.spawn_calls == []


def test_batch_parent_stop_preserves_created_group(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    services = FakeServices()
    abort = Cancellation()
    spawn = services.spawn_child

    def spawn_then_stop(*args: object, **kwargs: object) -> str:
        child_id = spawn(*args, **kwargs)  # type: ignore[arg-type]
        abort.set_cancelled(True, reason="test stop")
        return child_id

    monkeypatch.setattr(services, "spawn_child", spawn_then_stop)
    result = _batch_with_context(
        services,
        _context(tmp_path, abort_signal=abort),
        {"tasks": [{"prompt": "one"}, {"prompt": "two"}, {"prompt": "three"}]},
    )

    assert isinstance(result, ToolFailure)
    assert result.execution == UnconfirmedStop(cancellation_signalled=True)
    payload = _payload(result)
    group_id = str(payload["parallel_group_id"])
    assert payload["partial"] is True
    assert payload["task_ids"] == ["child-1"]
    assert payload["created"] == [{"index": 0, "task_id": "child-1", "agent_uri": "agent://child-1"}]
    assert payload["failed"] == [
        {"index": 1, "error": "not started: the parent task was cancelled"},
        {"index": 2, "error": "not started: the parent task was cancelled"},
    ]
    assert len(services.spawn_calls) == 1
    state = _stored(services)
    assert state.groups == {group_id: ("child-1",)}
    assert set(state.children) == {"child-1"}
    assert services.staged_events == [("task_batch_spawned", {"parallel_group_id": group_id, "task_ids": ["child-1"]})]


def test_batch_store_failure_reports_created_children(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    services = FakeServices()

    def refuse_state(*args: object, **kwargs: object) -> int:
        raise RuntimeError("state store is unavailable")

    monkeypatch.setattr(services, "replace_state", refuse_state)
    result = _batch(services, tmp_path, {"tasks": [{"prompt": "one"}, {"prompt": "two"}]})

    assert isinstance(result, ToolFailure)
    assert "2 child session(s)" in result.error
    assert "group mapping could not be saved" in result.error
    payload = _payload(result)
    assert payload["partial"] is True
    assert payload["task_ids"] == ["child-1", "child-2"]
    assert services.staged_events == []


def test_batch_group_read_separates_ready_results_and_pending_runs(tmp_path: Path) -> None:
    services = FakeServices()
    spawned = _batch(services, tmp_path, {"tasks": [{"prompt": "secret prompt one"}, {"prompt": "secret prompt two"}]})
    group_id = str(_payload(spawned)["parallel_group_id"])
    services.roster["child-2"] = ChildOutcome(
        child_session_id="child-2", status="failed", error="child blew up", stop_cause="error", run_id="run-child-2"
    )
    services.roster["child-1"] = ChildOutcome(child_session_id="child-1", status="running", run_id="run-child-1")

    res = _batch(services, tmp_path, {"operation": "output", "parallel_group_id": group_id})
    assert isinstance(res, ToolSuccess)
    payload = _payload(res)
    assert payload == {
        "operation": "output",
        "parallel_group_id": group_id,
        "children": [
            {
                "task_id": "child-2",
                "status": "failed",
                "stop_cause": "error",
                "error": "child blew up",
                "run_id": "run-child-2",
                "agent_uri": "agent://child-2",
                "history_uri": "transcript://child-2",
            },
        ],
        "pending": [
            {
                "task_id": "child-1",
                "label": "",
                "status": "running",
                "agent_uri": "agent://child-1",
                "run_id": "run-child-1",
            },
        ],
    }
    rendered = f"{res.output.text}\n{json.dumps(payload)}"
    assert "secret prompt" not in rendered
    assert "result of secret prompt" not in rendered
    assert "child-1 status=running" in res.output.text


def test_batch_group_read_keeps_profile_on_pending_run(tmp_path: Path) -> None:
    services = FakeServices()
    spawned = _batch(services, tmp_path, {"tasks": [{"prompt": "one", "subagent_type": "explore"}, {"prompt": "two"}]})
    group_id = str(_payload(spawned)["parallel_group_id"])
    services.roster["child-1"] = ChildOutcome(child_session_id="child-1", status="running", run_id="run-child-1")
    services.roster["child-2"] = ChildOutcome(child_session_id="child-2", status="completed", run_id="run-child-2")

    res = _batch(services, tmp_path, {"operation": "output", "parallel_group_id": group_id})
    payload = _payload(res)

    assert payload["children"] == [
        {
            "task_id": "child-2",
            "status": "completed",
            "run_id": "run-child-2",
            "agent_uri": "agent://child-2",
            "history_uri": "transcript://child-2",
        }
    ]
    assert payload["pending"] == [
        {
            "task_id": "child-1",
            "label": "",
            "status": "running",
            "agent_uri": "agent://child-1",
            "subagent_type": "explore",
            "run_id": "run-child-1",
        }
    ]


def test_batch_group_read_rejects_unknown_groups(tmp_path: Path) -> None:
    services = FakeServices()
    res = _batch(services, tmp_path, {"operation": "output", "parallel_group_id": "deadbeef"})
    assert isinstance(res, ToolFailure)
    assert res.error == "unknown task group: deadbeef"
    assert services.join_calls == []


# --- task_batch: profile selection ---


def test_batch_items_select_their_own_profiles(tmp_path: Path) -> None:
    services = FakeServices()
    res = _batch(services, tmp_path, {"tasks": [{"prompt": "read the docs", "subagent_type": "research"}, {"prompt": "plain"}]})

    assert isinstance(res, ToolSuccess)
    payload = _payload(res)
    group_id = str(payload["parallel_group_id"])
    assert payload == {
        "operation": "spawn",
        "parallel_group_id": group_id,
        "task_ids": ["child-1", "child-2"],
        "partial": False,
        "created": [
            {"index": 0, "task_id": "child-1", "agent_uri": "agent://child-1", "subagent_type": "research"},
            {"index": 1, "task_id": "child-2", "agent_uri": "agent://child-2"},
        ],
    }
    assert services.spawn_calls == [
        {
            "prompt": f"{_builtin_body(tmp_path, 'research')}\n\nread the docs",
            "label": "",
            "tool_allowlist": None,
            "max_steps": None,
            "readonly": True,
            "notify_on_completion": True,
            "idempotency_key": None,
            "result_schema": None,
        },
        {
            "prompt": "plain",
            "label": "",
            "tool_allowlist": None,
            "max_steps": None,
            "readonly": False,
            "notify_on_completion": True,
            "idempotency_key": None,
            "result_schema": None,
        },
    ]
    assert _stored(services).children == {
        "child-1": ChildRecord(group_id=group_id, subagent_type="research"),
        "child-2": ChildRecord(group_id=group_id),
    }
    assert "profile=research" in res.output.text


def test_batch_combines_profile_and_caller_scope_and_preserves_spawn_failures(tmp_path: Path) -> None:
    services = FakeServices()
    services.spawn_failures = {2: "delegation spawn budget exhausted for this chain"}
    res = _batch(services, tmp_path, {"tasks": [{"prompt": "plan it", "subagent_type": "plan", "tool_allowlist": ["read"]}, {"prompt": "doomed"}]})
    payload = _payload(res)
    assert payload["partial"] is True
    assert payload["created"] == [{"index": 0, "task_id": "child-1", "agent_uri": "agent://child-1", "subagent_type": "plan"}]
    assert payload["failed"] == [{"index": 1, "error": "delegation spawn budget exhausted for this chain"}]
    assert [call["tool_allowlist"] for call in services.spawn_calls] == [("read",), None]


def test_batch_group_read_preserves_typed_budget_exhaustion(tmp_path: Path) -> None:
    services = FakeServices()
    spawned = _batch(services, tmp_path, {"tasks": [{"prompt": "budgeted"}, {"prompt": "pending"}]})
    group_id = str(_payload(spawned)["parallel_group_id"])
    outcome = ChildOutcome(
        child_session_id="child-1",
        status="interrupted",
        stop_cause="limit_reached",
        run_id="run-budget",
        budget_exhaustion=BudgetExhaustion(
            dimension="root_actions",
            source="root_action_budget",
            limit=4,
            consumed=4,
            remaining=0,
            continue_with="extend_root_actions",
        ),
    )
    services.roster["child-1"] = outcome
    services.roster["child-2"] = ChildOutcome(child_session_id="child-2", status="running", run_id="run-pending")

    result = _batch(services, tmp_path, {"operation": "output", "parallel_group_id": group_id})

    payload = _payload(result)
    children = payload["children"]
    pending = payload["pending"]
    assert children[0]["status"] == "interrupted"
    assert children[0]["run_id"] == "run-budget"
    assert children[0]["budget_exhaustion"] == outcome.as_payload()["budget_exhaustion"]
    assert pending[0]["task_id"] == "child-1"
    assert pending[0]["status"] == "interrupted"
    assert pending[0]["run_id"] == "run-budget"
    assert "budget_exhaustion" not in pending[0]
    assert pending[1]["task_id"] == "child-2"
    assert pending[1]["status"] == "running"
    assert pending[1]["run_id"] == "run-pending"
    assert services.acks == [], "recoverable budget stops are not completion notices"


def test_batch_group_read_preserves_typed_capture_errors(tmp_path: Path) -> None:
    services = FakeServices()
    spawned = _batch(services, tmp_path, {"tasks": [{"prompt": "capped"}]})
    group_id = str(_payload(spawned)["parallel_group_id"])
    output_cause = "ArtifactStoreError: quota exceeded"
    services.roster["child-1"] = ChildOutcome(
        child_session_id="child-1",
        status="completed",
        output=None,
        run_id="run-capped",
        output_preview="partial result…",
        output_error=output_cause,
    )

    result = _batch(services, tmp_path, {"operation": "output", "parallel_group_id": group_id})

    payload = _payload(result)
    children = payload["children"]
    assert children[0]["status"] == "completed"
    assert children[0]["run_id"] == "run-capped"
    assert children[0]["output_preview"] == "partial result…"
    assert children[0]["output_error"] == output_cause
    assert "output_uri" not in children[0]
    assert isinstance(result.output, TextOutput)
    assert output_cause in result.output.text


def test_batch_item_with_unknown_profile_rejects_the_whole_batch(tmp_path: Path) -> None:
    services = FakeServices()
    res = _batch(services, tmp_path, {"tasks": [{"prompt": "ok"}, {"prompt": "x", "subagent_type": "ghost"}]})

    assert isinstance(res, ToolFailure)
    assert res.error == "task_batch item 1 unknown subagent_type: 'ghost'"
    assert services.spawn_calls == []
    assert services.get_state("session") == (None, 0)


# --- Profile-driven selection ---


def test_task_schemas_accept_workspace_dynamic_profile_ids() -> None:
    """Custom ids cannot be a static enum: workspace profiles resolve at ingress instead."""
    task_properties = TaskTool.definition.input_schema["properties"]
    assert "enum" not in task_properties["subagent_type"]
    assert task_properties["subagent_type"]["minLength"] == 1
    item_properties = TaskBatchTool.definition.input_schema["properties"]["tasks"]["items"]["properties"]
    assert "enum" not in item_properties["subagent_type"]
    assert item_properties["subagent_type"]["minLength"] == 1


def test_standard_agent_max_turns_apply_to_task_and_batch(tmp_path: Path):
    workspace = _custom_workspace(tmp_path, "helper.md", "---\nname: helper\ndescription: d\nmaxTurns: 3\nmodel: inherit\n---\nHelp.")
    services = FakeServices()
    result = _task_at(services, workspace, {"prompt": "work", "subagent_type": "helper", "tool_allowlist": ["read", "glob"], "max_steps": 7})
    assert isinstance(result, ToolSuccess)
    assert services.spawn_calls[0]["tool_allowlist"] == ("read", "glob")
    assert services.spawn_calls[0]["max_steps"] == 3
    tool = TaskBatchTool()
    result = tool.invoke(
        ToolCall("task_batch", {"tasks": [{"prompt": "work", "subagent_type": "helper", "max_steps": 2}]}),
        context=_context(workspace),
        services=services,
    )
    assert isinstance(result, ToolSuccess)
    assert services.spawn_calls[1]["tool_allowlist"] is None
    assert services.spawn_calls[1]["max_steps"] == 2


# --- task_batch: shared context ---


def test_batch_shared_context_merges_into_every_child_prompt(tmp_path: Path) -> None:
    services = FakeServices()
    res = _batch(
        services,
        tmp_path,
        {"tasks": [{"prompt": "one", "label": "a"}, {"prompt": "two"}], "context": "The guide is on the north wall."},
    )
    assert isinstance(res, ToolSuccess)
    payload = _payload(res)
    group_id = str(payload["parallel_group_id"])
    assert payload["operation"] == "spawn"
    assert payload["task_ids"] == ["child-1", "child-2"]
    assert payload["partial"] is False
    assert [call["prompt"] for call in services.spawn_calls] == [
        "Shared context:\nThe guide is on the north wall.\n\nTask:\none",
        "Shared context:\nThe guide is on the north wall.\n\nTask:\ntwo",
    ]
    assert [call["label"] for call in services.spawn_calls] == ["a", ""]
    # The context rides the spawn prompt only: stored records keep no copy.
    assert _stored(services).children == {
        "child-1": ChildRecord(label="a", group_id=group_id),
        "child-2": ChildRecord(label="", group_id=group_id),
    }
    assert services.staged_events == [("task_batch_spawned", {"parallel_group_id": group_id, "task_ids": ["child-1", "child-2"]})]
    assert "Started 2 child task(s)" in res.output.text


@pytest.mark.parametrize("extra", [{}, {"context": ""}, {"context": "   "}])
def test_batch_shared_context_empty_is_byte_identical_noop(tmp_path: Path, extra: dict[str, object]) -> None:
    services = FakeServices()
    res = _batch(services, tmp_path, {"tasks": [{"prompt": "one"}, {"prompt": "two"}], **extra})
    assert isinstance(res, ToolSuccess)
    assert [call["prompt"] for call in services.spawn_calls] == ["one", "two"]


def test_batch_shared_context_template_is_fixed_and_verbatim() -> None:
    prompt = "do the thing"
    assert _merge_batch_prompt(None, prompt) is prompt
    assert _merge_batch_prompt("", prompt) is prompt
    assert _merge_batch_prompt("   ", prompt) is prompt
    assert _merge_batch_prompt("  padded  ", prompt) == "Shared context:\n  padded  \n\nTask:\ndo the thing"


def test_batch_shared_context_validation_rejects_before_any_spawn(tmp_path: Path) -> None:
    services = FakeServices()
    non_string = _batch(services, tmp_path, {"tasks": [{"prompt": "ok"}], "context": 42})
    assert isinstance(non_string, ToolFailure)
    assert non_string.error == "task_batch context must be a string"

    unknown = _batch(services, tmp_path, {"tasks": [{"prompt": "ok"}], "bogus": "x"})
    assert isinstance(unknown, ToolFailure)
    assert unknown.error == "task_batch field 'bogus' is not supported"

    spawned = _batch(services, tmp_path, {"tasks": [{"prompt": "ok"}]})
    group_id = str(_payload(spawned)["parallel_group_id"])
    on_output = _batch(services, tmp_path, {"operation": "output", "parallel_group_id": group_id, "context": "late"})
    assert isinstance(on_output, ToolFailure)
    assert on_output.error == "task_batch context declares shared background and applies to a spawn only"

    assert len(services.spawn_calls) == 1, "only the legitimate spawn ran"


def test_batch_shared_context_separates_effective_prompts(tmp_path: Path) -> None:
    """A changed context never reuses another context's effective prompt (hence never its spec hash)."""
    north_services = FakeServices()
    assert isinstance(_batch(north_services, tmp_path, {"tasks": [{"prompt": "audit"}], "context": "north wing"}), ToolSuccess)
    south_services = FakeServices()
    assert isinstance(_batch(south_services, tmp_path, {"tasks": [{"prompt": "audit"}], "context": "south wing"}), ToolSuccess)
    north_again_services = FakeServices()
    assert isinstance(_batch(north_again_services, tmp_path, {"tasks": [{"prompt": "audit"}], "context": "north wing"}), ToolSuccess)
    north = str(north_services.spawn_calls[0]["prompt"])
    south = str(south_services.spawn_calls[0]["prompt"])
    north_again = str(north_again_services.spawn_calls[0]["prompt"])
    assert north != south
    assert north_again == north
    assert hashlib.sha256(north.encode()).hexdigest() != hashlib.sha256(south.encode()).hexdigest()
    assert "audit" in north and "north wing" in north


def test_batch_shared_context_applies_inside_profile_prefix(tmp_path: Path) -> None:
    services = FakeServices()
    res = _batch(
        services,
        tmp_path,
        {"tasks": [{"prompt": "read the docs", "subagent_type": "research"}], "context": "repo uses src layout"},
    )
    assert isinstance(res, ToolSuccess)
    assert services.spawn_calls[0]["prompt"] == (
        f"{_builtin_body(tmp_path, 'research')}\n\nShared context:\nrepo uses src layout\n\nTask:\nread the docs"
    )
    assert services.spawn_calls[0]["readonly"] is True


def test_batch_shared_context_is_never_inherited_by_later_calls(tmp_path: Path) -> None:
    services = FakeServices()
    first = _batch(services, tmp_path, {"tasks": [{"prompt": "one"}], "context": "background"})
    assert isinstance(first, ToolSuccess)
    second = _batch(services, tmp_path, {"tasks": [{"prompt": "two"}]})
    assert isinstance(second, ToolSuccess)
    assert [call["prompt"] for call in services.spawn_calls] == [
        "Shared context:\nbackground\n\nTask:\none",
        "two",
    ]
