"""Unit tests for the Delegation plugin: state contract, task control, and bounded batches."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

import pytest

from voidcode.plugins.delegation.delegation_plugin import (
    CANCEL_REASON,
    DEFAULT_JOIN_TIMEOUT_SECONDS,
    MAX_BATCH_SIZE,
    SCHEMA_VERSION,
    ChildRecord,
    DelegationState,
    TaskBatchTool,
    TaskTool,
    create_plugin,
)
from voidcode.plugins.delegation.profiles.prompts import render_agent_prompt
from voidcode.plugins.delegation.profiles.registry import load_agent_manifest_registry
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.services import (
    ArtifactPage,
    ChildOutcome,
    ChildSendReceipt,
    EventView,
    PluginBudget,
    PluginServices,
    SessionUsageReport,
)
from voidcode.sdk.tools import TextOutput, ToolCall, ToolFailure, ToolResult, ToolSuccess


class MockServices(PluginServices):
    """In-memory PluginServices: session state plus a roster of children this session spawned."""

    def __init__(self) -> None:
        self.state: dict[str, tuple[Mapping[str, object] | None, int]] = {}
        self.staged_events: list[tuple[str, Mapping[str, object]]] = []
        self.spawn_calls: list[dict[str, object]] = []
        self.join_calls: list[tuple[str, float]] = []
        self.cancel_calls: list[tuple[str, str]] = []
        self.send_calls: list[dict[str, object]] = []
        self.acks: list[tuple[str, str]] = []
        self.roster: dict[str, ChildOutcome] = {}
        #: 1-based spawn call numbers the core refuses, with its refusal message or exception.
        self.spawn_failures: dict[int, str | Exception] = {}
        self.cancel_result = True
        self.send_result = ChildSendReceipt(child_session_id="child-1", delivery="queued", child_status="running")
        self.child_events: dict[str, tuple[EventView, ...]] = {}
        self.artifacts: dict[str, bytes] = {}

    def get_state(self, scope: Literal["session", "workspace"] = "session") -> tuple[Mapping[str, object] | None, int]:
        return self.state.get(scope, (None, 0))

    def get_state_schema_version(self, scope: Literal["session", "workspace"] = "session") -> int:
        _ = scope
        return 0

    def replace_state(
        self,
        state: Mapping[str, object],
        expected_version: int,
        scope: Literal["session", "workspace"] = "session",
    ) -> int:
        _cur, ver = self.get_state(scope)
        if ver != expected_version:
            raise RuntimeError(f"CAS mismatch: expected {expected_version}, was {ver}")
        new_ver = expected_version + 1
        self.state[scope] = (dict(state), new_ver)
        return new_ver

    def stage_event(self, event_type: str, payload: Mapping[str, object]) -> None:
        self.staged_events.append((event_type, dict(payload)))

    def enqueue_continuation(self, payload: Mapping[str, object], *, idempotency_key: str | None = None) -> str:
        raise AssertionError("delegation enqueues no continuations")

    def spawn_child(
        self,
        prompt: str,
        *,
        label: str = "",
        tool_allowlist: tuple[str, ...] | None = None,
        max_steps: int = 32,
        readonly: bool = False,
        notify_on_completion: bool = False,
    ) -> str:
        call_number = len(self.spawn_calls) + 1
        self.spawn_calls.append(
            {
                "prompt": prompt,
                "label": label,
                "tool_allowlist": tool_allowlist,
                "max_steps": max_steps,
                "readonly": readonly,
                "notify_on_completion": notify_on_completion,
            }
        )
        refusal = self.spawn_failures.get(call_number)
        if refusal is not None:
            raise refusal if isinstance(refusal, Exception) else ValueError(refusal)
        child_id = f"child-{len(self.roster) + 1}"
        self.roster[child_id] = ChildOutcome(child_session_id=child_id, status="completed", output=f"result of {prompt}", run_id=f"run-{child_id}")
        return child_id

    def send_child(
        self,
        child_session_id: str,
        prompt: str,
        *,
        label: str = "",
        notify_on_completion: bool = True,
    ) -> ChildSendReceipt:
        self.send_calls.append(
            {
                "child_session_id": child_session_id,
                "prompt": prompt,
                "label": label,
                "notify_on_completion": notify_on_completion,
            }
        )
        return ChildSendReceipt(
            child_session_id=child_session_id,
            delivery=self.send_result.delivery,
            child_status=self.send_result.child_status,
            run_id=self.send_result.run_id,
            message_id=self.send_result.message_id,
        )

    def acknowledge_child_completion(self, child_session_id: str, run_id: str) -> None:
        self.acks.append((child_session_id, run_id))

    def read_child_events(
        self,
        child_session_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 100,
        include_payload: bool = True,
    ) -> tuple[EventView, ...]:
        return tuple(event for event in self.child_events.get(child_session_id, ()) if event.sequence > after_sequence)[:limit]

    def read_artifact(self, reference: object, *, offset: int = 0, limit: int = 65536) -> ArtifactPage:
        sha = str(getattr(reference, "sha256", reference))
        data = self.artifacts[sha]
        page = data[offset : offset + limit]
        return ArtifactPage(
            sha256=sha,
            offset=offset,
            total_bytes=len(data),
            data=page,
            eof=offset + len(page) >= len(data),
            media_type="text/plain; charset=utf-8",
        )

    def join_child(self, child_session_id: str, *, timeout_seconds: float = 300.0) -> ChildOutcome:
        self.join_calls.append((child_session_id, timeout_seconds))
        return self.roster[child_session_id]

    def cancel_child(self, child_session_id: str, reason: str = "owner cancellation") -> bool:
        self.cancel_calls.append((child_session_id, reason))
        return self.cancel_result

    def cancel_continuations(self, *, continuation_id: str | None = None) -> None:
        raise AssertionError("delegation cancels no continuations")

    def get_usage(self, *, since_sequence: int = 0, until_sequence: int | None = None) -> SessionUsageReport:
        raise AssertionError("delegation reads no usage")

    def get_budget(self) -> PluginBudget | None:
        raise AssertionError("delegation has no budget")

    def set_budget(self, **_kwargs: object) -> None:
        raise AssertionError("delegation sets no budget")

    def clear_budget(self) -> None:
        raise AssertionError("delegation clears no budget")


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


def _task(services: MockServices, tmp_path: Path, arguments: Mapping[str, object]) -> ToolResult:
    call = ToolCall(tool_name="task", arguments=arguments, tool_call_id="call-1")
    return TaskTool().invoke(call, context=_context(tmp_path), services=services)


def _batch(services: MockServices, tmp_path: Path, arguments: Mapping[str, object]) -> ToolResult:
    call = ToolCall(tool_name="task_batch", arguments=arguments, tool_call_id="call-2")
    return TaskBatchTool().invoke(call, context=_context(tmp_path), services=services)


def _payload(result: ToolResult) -> dict[str, object]:
    assert result.body is not None
    return dict(result.body.as_payload())


def _stored(services: MockServices) -> DelegationState:
    raw, _version = services.get_state("session")
    return DelegationState.from_payload(raw)


def _builtin_body(workspace: Path, profile_id: str) -> str:
    manifest = load_agent_manifest_registry(workspace).get(profile_id)
    assert manifest is not None
    rendered = render_agent_prompt(manifest.prompt_materialization)
    assert rendered is not None
    return rendered


def _custom_workspace(tmp_path: Path, filename: str, markdown: str) -> Path:
    workspace = tmp_path / "workspace"
    project_dir = workspace / ".voidcode" / "agents"
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / filename).write_text(markdown, encoding="utf-8")
    return workspace


def _context_for(workspace: Path) -> ToolContext:
    return ToolContext(
        workspace=workspace,
        session_id="session-1",
        run_id="run-1",
        invocation_id="inv-1",
        revision_id="rev-1",
        actor="model",
        logical_turn="turn-1",
    )


def _task_at(services: MockServices, workspace: Path, arguments: Mapping[str, object]) -> ToolResult:
    call = ToolCall(tool_name="task", arguments=arguments, tool_call_id="call-1")
    return TaskTool().invoke(call, context=_context_for(workspace), services=services)


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
            "child-1": {"label": "explore", "group_id": "g1", "subagent_type": "explore"},
            "child-2": {"label": "", "group_id": None, "subagent_type": None},
        },
        "groups": {"g1": ["child-1"]},
    }
    assert DelegationState.from_payload(payload) == state
    assert DelegationState.from_payload(None) == DelegationState()


def test_state_parses_rows_written_before_profile_selection() -> None:
    """A child row from before ``subagent_type`` existed still restores, as an unset profile."""
    payload = {
        "schema_version": SCHEMA_VERSION,
        "children": {"child-1": {"label": "a", "group_id": None}},
        "groups": {},
    }
    assert DelegationState.from_payload(payload).children == {"child-1": ChildRecord(label="a")}


def test_state_round_trip_accepts_handler_snapshots() -> None:
    """Plugin state reaches a handler as an immutable JSON snapshot: arrays are tuples."""
    payload = {
        "schema_version": SCHEMA_VERSION,
        "children": {"child-1": {"label": "a", "group_id": "g1"}},
        "groups": {"g1": ("child-1",)},
    }
    assert DelegationState.from_payload(payload).groups == {"g1": ("child-1",)}


@pytest.mark.parametrize(
    "payload",
    [
        {"schema_version": SCHEMA_VERSION, "children": {}, "groups": {}, "unknown": 1},
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


# --- task: spawning ---


def test_spawn_passes_arguments_through_and_records_roster_event(tmp_path: Path) -> None:
    services = MockServices()
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
        }
    ]
    assert services.join_calls == []
    assert _payload(res) == {
        "operation": "spawn",
        "task_id": "child-1",
        "label": "explore",
        "history_uri": "voidcode://transcript/child-1",
    }
    assert "child-1" in res.output.text
    assert "background" in res.output.text

    state = _stored(services)
    assert state.children == {"child-1": ChildRecord(label="explore", group_id=None)}
    assert state.groups == {}
    assert services.staged_events == [("task_spawned", {"task_id": "child-1", "label": "explore", "group_id": None})]


def test_synchronous_spawn_joins_and_returns_the_child_outcome(tmp_path: Path) -> None:
    services = MockServices()
    res = _task(services, tmp_path, {"prompt": "do the work", "run_in_background": False, "timeout": 2_500})
    assert isinstance(res, ToolSuccess)
    assert services.join_calls == [("child-1", 2.5)]
    payload = _payload(res)
    assert payload["status"] == "completed"
    assert payload["output"] == "result of do the work"
    assert "result of do the work" in res.output.text
    assert "status=completed" in res.output.text
    assert _stored(services).children == {"child-1": ChildRecord()}


def test_synchronous_spawn_without_timeout_uses_the_default(tmp_path: Path) -> None:
    services = MockServices()
    _task(services, tmp_path, {"prompt": "work", "run_in_background": False})
    assert services.join_calls == [("child-1", DEFAULT_JOIN_TIMEOUT_SECONDS)]


# --- task: profile selection ---


def test_profile_spawn_materializes_the_prompt_and_applies_its_scope(tmp_path: Path) -> None:
    services = MockServices()
    res = _task(services, tmp_path, {"prompt": "map the retry paths", "label": "explore", "subagent_type": "explore"})

    assert isinstance(res, ToolSuccess)
    assert services.spawn_calls == [
        {
            "prompt": f"{_builtin_body(tmp_path, 'explore')}\n\nmap the retry paths",
            "label": "explore",
            "tool_allowlist": ("read", "glob", "grep", "ast_grep", "lsp"),
            "max_steps": 32,
            "readonly": True,
            "notify_on_completion": True,
        }
    ]
    assert _payload(res) == {
        "operation": "spawn",
        "task_id": "child-1",
        "label": "explore",
        "subagent_type": "explore",
        "history_uri": "voidcode://transcript/child-1",
    }
    assert "profile=explore" in res.output.text
    assert _stored(services).children == {"child-1": ChildRecord(label="explore", subagent_type="explore")}


def test_profile_child_scope_is_reported_by_the_control_path(tmp_path: Path) -> None:
    services = MockServices()
    _task(services, tmp_path, {"prompt": "plan it", "subagent_type": "plan"})

    res = _task(services, tmp_path, {"operation": "output", "task_id": "child-1"})

    assert _payload(res)["subagent_type"] == "plan"
    assert "profile=plan" in res.output.text

    cancelled = _task(services, tmp_path, {"operation": "cancel", "task_id": "child-1"})
    assert _payload(cancelled)["subagent_type"] == "plan"
    assert "profile=plan" in cancelled.output.text


def test_unknown_subagent_type_fails_without_spawning(tmp_path: Path) -> None:
    services = MockServices()
    res = _task(services, tmp_path, {"prompt": "work", "subagent_type": "ghost"})

    assert isinstance(res, ToolFailure)
    assert res.error == "task unknown subagent_type: 'ghost'"
    assert services.spawn_calls == []
    assert services.get_state("session") == (None, 0)


def test_non_subagent_profile_is_not_executable_as_a_child(tmp_path: Path) -> None:
    services = MockServices()
    res = _task(services, tmp_path, {"prompt": "work", "subagent_type": "default"})

    assert isinstance(res, ToolFailure)
    assert res.error == "task subagent_type 'default' is not executable as a child"
    assert services.spawn_calls == []
    assert services.get_state("session") == (None, 0)


def test_custom_profile_spawns_with_its_own_body_scope_and_writable_stance(tmp_path: Path) -> None:
    workspace = _custom_workspace(
        tmp_path,
        "helper.md",
        "---\nname: Helper\ndescription: Local helper\nmode: subagent\ntool_allowlist:\n  - read\n---\n\nHelp locally.\n",
    )
    services = MockServices()
    res = _task_at(services, workspace, {"prompt": "count doors", "subagent_type": "helper"})

    assert isinstance(res, ToolSuccess)
    assert services.spawn_calls[0]["prompt"] == "Help locally.\n\ncount doors"
    assert services.spawn_calls[0]["tool_allowlist"] == ("read",)
    assert services.spawn_calls[0]["readonly"] is False
    assert _stored(services).children == {"child-1": ChildRecord(subagent_type="helper")}


def test_profile_and_explicit_allowlist_cannot_be_combined(tmp_path: Path) -> None:
    services = MockServices()
    res = _task(services, tmp_path, {"prompt": "work", "subagent_type": "plan", "tool_allowlist": ["read"]})

    assert isinstance(res, ToolFailure)
    assert "accepts either subagent_type or tool_allowlist" in res.error
    assert services.spawn_calls == []


def test_subagent_type_applies_to_spawns_only(tmp_path: Path) -> None:
    services = MockServices()
    res = _task(services, tmp_path, {"operation": "output", "task_id": "child-1", "subagent_type": "plan"})

    assert isinstance(res, ToolFailure)
    assert "applies to a spawn only" in res.error
    assert services.spawn_calls == []


def test_unavailable_profile_scope_falls_back_to_the_parent_scope(tmp_path: Path) -> None:
    services = MockServices()
    services.spawn_failures = {1: "tool_allowlist names an unknown tool: lsp"}
    res = _task(services, tmp_path, {"prompt": "map it", "subagent_type": "explore"})

    note = "profile 'explore' scope was unavailable (tool_allowlist names an unknown tool: lsp); the child inherited the parent tool scope instead"
    assert isinstance(res, ToolSuccess)
    assert [call["tool_allowlist"] for call in services.spawn_calls] == [("read", "glob", "grep", "ast_grep", "lsp"), None]
    assert [call["readonly"] for call in services.spawn_calls] == [True, True]
    assert [call["prompt"] for call in services.spawn_calls] == [f"{_builtin_body(tmp_path, 'explore')}\n\nmap it"] * 2
    assert _payload(res)["notes"] == [note]
    assert note in res.output.text
    assert _stored(services).children == {"child-1": ChildRecord(subagent_type="explore")}


def test_host_wrapped_scope_rejection_also_falls_back(tmp_path: Path) -> None:
    """A core refusal reaches the plugin as the host transport's wrapping ``RuntimeError``."""
    services = MockServices()
    services.spawn_failures = {
        1: RuntimeError("Service call rejected: ValueError: tool_allowlist names an unknown tool: lsp"),
    }
    res = _task(services, tmp_path, {"prompt": "map it", "subagent_type": "explore"})

    assert isinstance(res, ToolSuccess)
    assert [call["tool_allowlist"] for call in services.spawn_calls] == [("read", "glob", "grep", "ast_grep", "lsp"), None]
    assert [call["readonly"] for call in services.spawn_calls] == [True, True]
    assert _payload(res)["notes"] == [
        "profile 'explore' scope was unavailable (Service call rejected: ValueError: tool_allowlist names an unknown tool: lsp); "
        "the child inherited the parent tool scope instead"
    ]
    assert _stored(services).children == {"child-1": ChildRecord(subagent_type="explore")}


def test_unavailable_profile_scope_reports_a_failed_retry(tmp_path: Path) -> None:
    services = MockServices()
    services.spawn_failures = {
        1: "tool_allowlist names an unknown tool: lsp",
        2: "delegation depth limit exceeded: depth 4 exceeds max 3",
    }
    res = _task(services, tmp_path, {"prompt": "map it", "subagent_type": "explore"})

    assert isinstance(res, ToolFailure)
    assert res.error == "delegation depth limit exceeded: depth 4 exceeds max 3"
    assert len(services.spawn_calls) == 2
    assert services.get_state("session") == (None, 0)


def test_no_profile_keeps_the_calling_allowlist_and_writable_stance(tmp_path: Path) -> None:
    services = MockServices()
    res = _task(services, tmp_path, {"prompt": "plain work", "tool_allowlist": ["read", "glob"]})

    assert isinstance(res, ToolSuccess)
    assert services.spawn_calls[0]["prompt"] == "plain work"
    assert services.spawn_calls[0]["tool_allowlist"] == ("read", "glob")
    assert services.spawn_calls[0]["readonly"] is False
    assert "subagent_type" not in _payload(res)
    assert _stored(services).children == {"child-1": ChildRecord()}


def test_a_profile_model_preference_is_reported_instead_of_faked(tmp_path: Path) -> None:
    """No builtin profile declares one today: a custom with ``model:`` hits the note path."""
    workspace = _custom_workspace(
        tmp_path,
        "picky.md",
        "---\nname: Picky\ndescription: Choosy helper\nmode: subagent\nmodel: endpoint/gpt-x\ntool_allowlist:\n  - read\n---\n\nPick well.\n",
    )
    services = MockServices()

    res = _task_at(services, workspace, {"prompt": "work", "subagent_type": "picky"})

    note = "profile 'picky' prefers model 'endpoint/gpt-x', but every child runs the workspace model; the preference was not applied"
    assert isinstance(res, ToolSuccess)
    assert services.spawn_calls[0]["prompt"] == "Pick well.\n\nwork"
    assert _payload(res)["notes"] == [note]
    assert note in res.output.text


# --- task: control ---


def test_output_joins_with_the_requested_timeout_and_surfaces_running(tmp_path: Path) -> None:
    services = MockServices()
    _task(services, tmp_path, {"prompt": "long work", "label": "long"})
    services.roster["child-1"] = ChildOutcome(child_session_id="child-1", status="running")
    res = _task(services, tmp_path, {"operation": "output", "task_id": "child-1", "timeout": 5_000})
    assert isinstance(res, ToolSuccess)
    assert services.join_calls == [("child-1", 5.0)]
    payload = _payload(res)
    assert payload == {
        "operation": "output",
        "task_id": "child-1",
        "label": "long",
        "status": "running",
        "history_uri": "voidcode://transcript/child-1",
    }
    assert "status=running" in res.output.text
    assert 'task(operation="output"' in res.output.text

    zero = _task(services, tmp_path, {"operation": "output", "task_id": "child-1", "timeout": 0})
    assert services.join_calls[-1] == ("child-1", 0.0)
    assert _payload(zero)["status"] == "running"


def test_output_reports_the_terminal_child_outcome(tmp_path: Path) -> None:
    services = MockServices()
    _task(services, tmp_path, {"prompt": "work"})
    services.roster["child-1"] = ChildOutcome(child_session_id="child-1", status="failed", output=None, error="child blew up", stop_cause="error")
    res = _task(services, tmp_path, {"operation": "output", "task_id": "child-1"})
    payload = _payload(res)
    assert payload["status"] == "failed"
    assert payload["error"] == "child blew up"
    assert payload["stop_cause"] == "error"
    assert "child blew up" in res.output.text


def test_send_maps_the_receipt_without_claiming_consumption(tmp_path: Path) -> None:
    services = MockServices()
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
    assert payload["history_uri"] == "voidcode://transcript/child-1"
    assert isinstance(res.output, TextOutput)
    assert "started in the same session" in res.output.text

    services.send_result = ChildSendReceipt(child_session_id="child-1", delivery="queued", child_status="running")
    queued = _task(services, tmp_path, {"operation": "send", "task_id": "child-1", "prompt": "later"})
    assert _payload(queued)["delivery"] == "queued"
    assert isinstance(queued.output, TextOutput)
    assert "queued behind the child's current work" in queued.output.text
    assert "does not mean the child read or finished it" in queued.output.text


def test_history_pages_through_the_cursor_without_loss(tmp_path: Path) -> None:
    services = MockServices()
    _task(services, tmp_path, {"prompt": "work", "label": "hist"})
    filler = "x" * (20 * 1024)
    services.child_events["child-1"] = tuple(
        EventView(
            sequence=index + 1,
            event_type="kernel.user_message",
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
            event_type="kernel.user_message",
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
    services = MockServices()
    _task(services, tmp_path, {"prompt": "work"})
    for operation in ("output", "cancel"):
        res = _task(services, tmp_path, {"operation": operation, "task_id": "child-404"})
        assert isinstance(res, ToolFailure)
        assert res.error == "unknown task: child-404"
    assert services.join_calls == []
    assert services.cancel_calls == []


def test_cancel_reports_acceptance(tmp_path: Path) -> None:
    services = MockServices()
    _task(services, tmp_path, {"prompt": "work", "label": "cancel me"})
    res = _task(services, tmp_path, {"operation": "cancel", "task_id": "child-1"})
    assert isinstance(res, ToolSuccess)
    assert services.cancel_calls == [("child-1", CANCEL_REASON)]
    assert _payload(res) == {"operation": "cancel", "task_id": "child-1", "label": "cancel me", "cancelled": True}
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
    services = MockServices()
    res = _task(services, tmp_path, arguments)
    assert isinstance(res, ToolFailure)
    assert message in res.error
    assert res.output.text == res.error
    assert services.spawn_calls == []
    assert services.staged_events == []
    assert services.get_state("session") == (None, 0)


def test_core_spawn_refusal_is_reported_as_a_failure(tmp_path: Path) -> None:
    services = MockServices()
    services.spawn_failures = {1: "tool_allowlist names an unknown tool: nope"}
    res = _task(services, tmp_path, {"prompt": "work", "tool_allowlist": ["nope"]})
    assert isinstance(res, ToolFailure)
    assert res.error == "tool_allowlist names an unknown tool: nope"
    assert services.get_state("session") == (None, 0)


# --- task_batch ---


def test_batch_preflights_every_item_before_any_spawn(tmp_path: Path) -> None:
    services = MockServices()
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
    services = MockServices()
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
    services = MockServices()
    res = _batch(services, tmp_path, {"tasks": [{"prompt": "one", "label": "a"}, {"prompt": "two", "max_steps": 7}]})
    assert isinstance(res, ToolSuccess)
    payload = _payload(res)
    group_id = str(payload["parallel_group_id"])
    assert len(group_id) == 32
    assert payload == {"operation": "spawn", "parallel_group_id": group_id, "task_ids": ["child-1", "child-2"], "partial": False}
    assert services.spawn_calls == [
        {"prompt": "one", "label": "a", "tool_allowlist": None, "max_steps": 32, "readonly": False, "notify_on_completion": True},
        {"prompt": "two", "label": "", "tool_allowlist": None, "max_steps": 7, "readonly": False, "notify_on_completion": True},
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
    services = MockServices()
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
        {"index": 0, "task_id": "child-1"},
        {"index": 1, "task_id": "child-2"},
        {"index": 3, "task_id": "child-3"},
    ]
    assert payload["failed"] == [{"index": 2, "error": "delegation spawn budget exhausted for this chain"}]
    assert "Started 3 of 4 child task(s)" in res.output.text
    assert "[2] failed: delegation spawn budget exhausted for this chain" in res.output.text

    state = _stored(services)
    assert set(state.children) == {"child-1", "child-2", "child-3"}
    assert state.groups == {group_id: ("child-1", "child-2", "child-3")}
    assert len(services.spawn_calls) == 4


def test_batch_with_no_created_child_fails(tmp_path: Path) -> None:
    services = MockServices()
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


def test_batch_group_read_returns_bounded_per_child_statuses(tmp_path: Path) -> None:
    services = MockServices()
    spawned = _batch(services, tmp_path, {"tasks": [{"prompt": "secret prompt one"}, {"prompt": "secret prompt two"}]})
    group_id = str(_payload(spawned)["parallel_group_id"])
    services.roster["child-2"] = ChildOutcome(child_session_id="child-2", status="failed", error="child blew up", stop_cause="error")
    services.roster["child-1"] = ChildOutcome(child_session_id="child-1", status="running")

    res = _batch(services, tmp_path, {"operation": "output", "parallel_group_id": group_id})
    assert isinstance(res, ToolSuccess)
    payload = _payload(res)
    assert payload == {
        "operation": "output",
        "parallel_group_id": group_id,
        "children": [
            {"task_id": "child-1", "status": "running", "history_uri": "voidcode://transcript/child-1"},
            {
                "task_id": "child-2",
                "status": "failed",
                "stop_cause": "error",
                "error": "child blew up",
                "history_uri": "voidcode://transcript/child-2",
            },
        ],
    }
    assert services.join_calls == [("child-1", DEFAULT_JOIN_TIMEOUT_SECONDS), ("child-2", DEFAULT_JOIN_TIMEOUT_SECONDS)]
    rendered = f"{res.output.text}\n{json.dumps(payload)}"
    assert "secret prompt" not in rendered
    assert "result of secret prompt" not in rendered
    assert "child-1 status=running" in res.output.text


def test_batch_group_read_rejects_unknown_groups(tmp_path: Path) -> None:
    services = MockServices()
    res = _batch(services, tmp_path, {"operation": "output", "parallel_group_id": "deadbeef"})
    assert isinstance(res, ToolFailure)
    assert res.error == "unknown task group: deadbeef"
    assert services.join_calls == []


# --- task_batch: profile selection ---


def test_batch_items_select_their_own_profiles(tmp_path: Path) -> None:
    services = MockServices()
    res = _batch(services, tmp_path, {"tasks": [{"prompt": "read the docs", "subagent_type": "research"}, {"prompt": "plain"}]})

    assert isinstance(res, ToolSuccess)
    payload = _payload(res)
    group_id = str(payload["parallel_group_id"])
    assert payload == {"operation": "spawn", "parallel_group_id": group_id, "task_ids": ["child-1", "child-2"], "partial": False}
    assert services.spawn_calls == [
        {
            "prompt": f"{_builtin_body(tmp_path, 'research')}\n\nread the docs",
            "label": "",
            "tool_allowlist": ("web_search",),
            "max_steps": 32,
            "readonly": True,
            "notify_on_completion": True,
        },
        {"prompt": "plain", "label": "", "tool_allowlist": None, "max_steps": 32, "readonly": False, "notify_on_completion": True},
    ]
    assert _stored(services).children == {
        "child-1": ChildRecord(group_id=group_id, subagent_type="research"),
        "child-2": ChildRecord(group_id=group_id),
    }
    assert "profile=research" in res.output.text


def test_batch_item_profile_fallback_is_visible_in_the_result(tmp_path: Path) -> None:
    services = MockServices()
    services.spawn_failures = {
        1: "tool_allowlist names an unknown tool: skill",
        3: "delegation spawn budget exhausted for this chain",
    }
    res = _batch(services, tmp_path, {"tasks": [{"prompt": "plan it", "subagent_type": "plan"}, {"prompt": "doomed"}]})

    note = "profile 'plan' scope was unavailable (tool_allowlist names an unknown tool: skill); the child inherited the parent tool scope instead"
    payload = _payload(res)
    assert payload["partial"] is True
    assert payload["created"] == [{"index": 0, "task_id": "child-1", "subagent_type": "plan", "notes": [note]}]
    assert payload["failed"] == [{"index": 1, "error": "delegation spawn budget exhausted for this chain"}]
    assert "note: profile 'plan' scope was unavailable" in res.output.text
    assert [call["tool_allowlist"] for call in services.spawn_calls] == [
        ("read", "glob", "grep", "ast_grep", "lsp", "skill", "web_search"),
        None,
        None,
    ]


def test_batch_group_read_reports_each_child_profile(tmp_path: Path) -> None:
    services = MockServices()
    spawned = _batch(services, tmp_path, {"tasks": [{"prompt": "one", "subagent_type": "explore"}, {"prompt": "two"}]})
    group_id = str(_payload(spawned)["parallel_group_id"])
    services.roster["child-1"] = ChildOutcome(child_session_id="child-1", status="running")
    services.roster["child-2"] = ChildOutcome(child_session_id="child-2", status="completed")

    res = _batch(services, tmp_path, {"operation": "output", "parallel_group_id": group_id})

    assert _payload(res)["children"] == [
        {
            "task_id": "child-1",
            "status": "running",
            "subagent_type": "explore",
            "history_uri": "voidcode://transcript/child-1",
        },
        {"task_id": "child-2", "status": "completed", "history_uri": "voidcode://transcript/child-2"},
    ]
    assert "profile=explore" in res.output.text


def test_batch_item_with_unknown_profile_rejects_the_whole_batch(tmp_path: Path) -> None:
    services = MockServices()
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


# --- Plugin wiring ---


def test_plugin_registers_tools_and_profile_commands() -> None:
    plugin = create_plugin()
    names = [tool.definition.name for tool in plugin.tools]
    assert names == ["task", "task_batch"]
    for tool in plugin.tools:
        assert tool.definition.replay_policy == "never"
        assert {effect.value for effect in tool.definition.effects} == {"read", "spawn", "session"}
    assert [command.definition.name for command in plugin.commands] == ["agents", "agents.show"]
