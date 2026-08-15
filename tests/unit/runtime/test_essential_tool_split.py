"""Essential/discoverable tool split: provider filtering, doc reads, dispatch.

Covers:
(a) essential-only filtering of the provider tools array (with allowlist
    overrides and the all-top-level fallback),
(b) ``read(path="voidcode://tool/<name>")`` returning real guidance +
    input schema for a discoverable tool,
(c) ``invoke_tool`` dispatch executing a discoverable tool through the
    registry and enforcing permission (pending approval / read-only denial as
    tool-level feedback),
(d) the skill catalog living in system-prompt metadata instead of the skill
    tool description.
"""

from __future__ import annotations

import json
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest

from voidcode.graph.contracts import GraphEvent, GraphStep
from voidcode.runtime.config import (
    RuntimeAgentConfig,
    RuntimeConfig,
    RuntimeToolsConfig,
    RuntimeToolsLocalConfig,
)
from voidcode.runtime.permission import PermissionPolicy
from voidcode.runtime.service import (
    GraphRunRequest,
    RuntimeRequest,
    RuntimeRequestMetadataPayload,
    SessionState,
    VoidCodeRuntime,
)
from voidcode.runtime.tool_registry import ESSENTIAL_TOOL_NAMES
from voidcode.skills import LocalSkillMetadataLoader, SkillRegistry
from voidcode.tools import ToolCall
from voidcode.tools.contracts import ToolResult
from voidcode.tools.guidance import guidance_for_tool

pytestmark = pytest.mark.usefixtures("_force_deterministic_engine_default")


@pytest.fixture
def _force_deterministic_engine_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VOIDCODE_EXECUTION_ENGINE", "deterministic")


@dataclass(slots=True)
class _ScriptedStep:
    tool_call: ToolCall | None = None
    output: str | None = None
    events: tuple[GraphEvent, ...] = ()
    is_finished: bool = False


class _ScriptedGraph:
    def __init__(self, *steps: _ScriptedStep) -> None:
        self._steps = list(steps)
        self.requests: list[GraphRunRequest] = []

    def step(
        self,
        request: GraphRunRequest,
        tool_results: tuple[ToolResult, ...],
        *,
        session: SessionState,
    ) -> GraphStep:
        _ = session
        self.requests.append(request)
        if not self._steps:
            return _ScriptedStep(output="done", is_finished=True)
        return self._steps.pop(0)


def _run_events(runtime: VoidCodeRuntime, graph: _ScriptedGraph, *, metadata: dict[str, object] | None = None, session_id: str = "s1"):
    events = []
    outputs = []
    for chunk in runtime.run_stream(
        RuntimeRequest(
            prompt="go",
            session_id=session_id,
            metadata=cast(RuntimeRequestMetadataPayload, metadata or {}),
        )
    ):
        if chunk.event is not None:
            events.append(chunk.event)
        if chunk.kind == "output":
            outputs.append(chunk.output)
    return events, outputs, graph


# ---------------------------------------------------------------------------
# (a) provider filtering
# ---------------------------------------------------------------------------


def _runtime_available_tools(graph: _ScriptedGraph, config: RuntimeConfig, *, workspace: Path) -> tuple[str, ...]:
    runtime = VoidCodeRuntime(
        workspace=workspace,
        graph=graph,
        config=config,
        permission_policy=PermissionPolicy(mode="allow"),
    )
    events, _outputs, _ = _run_events(runtime, graph, session_id=f"essential-filter-{id(graph)}")
    assert events, "expected at least one event"
    recorded = [request for request in graph.requests if request.available_tools]
    assert recorded, "graph received no request with available_tools"
    return tuple(tool.name for tool in recorded[0].available_tools)


def test_provider_path_exposes_only_essential_tools_when_enabled(tmp_path: Path) -> None:
    graph = _ScriptedGraph(_ScriptedStep(output="done", is_finished=True))
    available = _runtime_available_tools(
        graph,
        RuntimeConfig(execution_engine="deterministic", tools=RuntimeToolsConfig(essential_only=True)),
        workspace=tmp_path,
    )
    names = set(available)
    assert names == ESSENTIAL_TOOL_NAMES, f"expected exactly the essential set, got {sorted(names)}"
    for discoverable in ("apply_patch", "multi_edit", "web_search", "web_fetch", "ast_grep", "lsp", "background_output"):
        assert discoverable not in names, f"{discoverable} should be discoverable, not top-level"


def test_provider_path_exposes_everything_by_default(tmp_path: Path) -> None:
    graph = _ScriptedGraph(_ScriptedStep(output="done", is_finished=True))
    available = _runtime_available_tools(
        graph,
        RuntimeConfig(execution_engine="deterministic"),
        workspace=tmp_path,
    )
    names = set(available)
    assert ESSENTIAL_TOOL_NAMES <= names
    assert "apply_patch" in names
    assert "multi_edit" in names
    assert "web_search" in names


def test_provider_path_keeps_allowlist_required_tools_top_level(tmp_path: Path) -> None:
    graph = _ScriptedGraph(_ScriptedStep(output="done", is_finished=True))
    available = _runtime_available_tools(
        graph,
        RuntimeConfig(
            execution_engine="deterministic",
            tools=RuntimeToolsConfig(essential_only=True),
            agent=RuntimeAgentConfig(
                preset="leader",
                tools=RuntimeToolsConfig(allowlist=("web_search", "multi_edit", "read", "grep")),
            ),
        ),
        workspace=tmp_path,
    )
    names = set(available)
    # Allowlist-selected tools stay top-level even when they are not part of
    # the essential set; scoping still narrows to the allowlist.
    assert names == {"read", "grep", "web_search", "multi_edit"}


# ---------------------------------------------------------------------------
# (b) internal-URL doc read
# ---------------------------------------------------------------------------


def test_read_serves_discoverable_tool_documentation(tmp_path: Path) -> None:
    graph = _ScriptedGraph(
        _ScriptedStep(
            tool_call=ToolCall(
                tool_name="read",
                arguments={"path": "voidcode://tool/apply_patch"},
            )
        ),
        _ScriptedStep(output="done", is_finished=True),
    )
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        graph=graph,
        config=RuntimeConfig(execution_engine="deterministic", tools=RuntimeToolsConfig(essential_only=True)),
        permission_policy=PermissionPolicy(mode="allow"),
    )
    events, outputs, _ = _run_events(runtime, graph, session_id="doc-read")
    completed = [event for event in events if event.event_type == "runtime.tool_completed" and event.payload.get("tool") == "read"]
    assert completed, "expected read completion"
    payload = completed[-1].payload
    assert payload["status"] == "ok"
    assert payload["type"] == "tool_documentation"
    assert payload["tool_name"] == "apply_patch"
    # Real content from the sidecar guidance file plus the registry schema.
    assert payload["input_schema"]["patch"]["type"] == "string"
    assert payload["guidance"] == guidance_for_tool("apply_patch")
    assert payload["read_only"] is False
    assert outputs == ["done"]


def test_read_rejects_unknown_tool_documentation(tmp_path: Path) -> None:
    graph = _ScriptedGraph(
        _ScriptedStep(
            tool_call=ToolCall(
                tool_name="read",
                arguments={"path": "voidcode://tool/does_not_exist"},
            )
        ),
        _ScriptedStep(output="done", is_finished=True),
    )
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        graph=graph,
        config=RuntimeConfig(execution_engine="deterministic"),
        permission_policy=PermissionPolicy(mode="allow"),
    )
    # read errors follow the existing tool error semantics: under the
    # deterministic engine an invalid target raises; under the provider engine
    # it surfaces as tool-level feedback.
    with pytest.raises(ValueError, match="unknown tool in runtime registry: does_not_exist"):
        for _chunk in runtime.run_stream(
            RuntimeRequest(
                prompt="go",
                session_id="doc-read-missing",
                metadata=cast(RuntimeRequestMetadataPayload, {}),
            )
        ):
            pass


# ---------------------------------------------------------------------------
# (c) dispatch through the registry + permission enforcement
# ---------------------------------------------------------------------------


def _write_local_tool_manifest(workspace: Path) -> None:
    tools_dir = workspace / ".voidcode" / "tools"
    tools_dir.mkdir(parents=True, exist_ok=True)
    (tools_dir / "echo.py").write_text(
        textwrap.dedent(
            """
            import json
            import sys
            args = json.loads(sys.stdin.read() or "{}")
            print(json.dumps({"args": args, "ok": True}, sort_keys=True))
            """
        ),
        encoding="utf-8",
    )
    (tools_dir / "local_echo.json").write_text(
        json.dumps(
            {
                "name": "local/echo",
                "description": "Echo arguments from a local manifest",
                "input_schema": {"type": "object", "properties": {"message": {"type": "string"}}},
                "command": [sys.executable, "${manifest_dir}/echo.py"],
                "read_only": True,
            }
        ),
        encoding="utf-8",
    )


def _local_tools_config() -> RuntimeConfig:
    return RuntimeConfig(
        execution_engine="deterministic",
        tools=RuntimeToolsConfig(
            essential_only=True,
            local=RuntimeToolsLocalConfig(enabled=True, path=".voidcode/tools"),
        ),
    )


def test_dispatch_executes_discoverable_tool_through_registry(tmp_path: Path) -> None:
    _write_local_tool_manifest(tmp_path)
    graph = _ScriptedGraph(
        _ScriptedStep(
            tool_call=ToolCall(
                tool_name="invoke_tool",
                arguments={"name": "local/echo", "arguments": {"message": "hello-from-dispatch"}},
            )
        ),
        _ScriptedStep(output="done", is_finished=True),
    )
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        graph=graph,
        config=_local_tools_config(),
        permission_policy=PermissionPolicy(mode="allow"),
    )
    events, outputs, _ = _run_events(runtime, graph, session_id="dispatch-ok")
    completed = [event for event in events if event.event_type == "runtime.tool_completed" and event.payload.get("tool") == "local/echo"]
    assert completed, "expected the dispatched local/echo tool to complete"
    payload = completed[-1].payload
    assert payload["status"] == "ok"
    assert payload["arguments"] == {"message": "hello-from-dispatch"}
    assert json.loads(payload["content"]) == {"args": {"message": "hello-from-dispatch"}, "ok": True}
    assert outputs == ["done"]
    # The dispatched result pairs with the provider-visible invoke_tool call id.
    invoke_completed = [event for event in events if event.event_type == "runtime.tool_completed" and event.payload.get("tool") == "invoke_tool"]
    assert not invoke_completed


def test_dispatch_of_unknown_tool_returns_tool_level_error(tmp_path: Path) -> None:
    graph = _ScriptedGraph(
        _ScriptedStep(
            tool_call=ToolCall(
                tool_name="invoke_tool",
                arguments={"name": "no/such/tool", "arguments": {}},
            )
        ),
        _ScriptedStep(output="done", is_finished=True),
    )
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        graph=graph,
        config=RuntimeConfig(execution_engine="deterministic", tools=RuntimeToolsConfig(essential_only=True)),
        permission_policy=PermissionPolicy(mode="allow"),
    )
    events, outputs, _ = _run_events(runtime, graph, session_id="dispatch-unknown")
    completed = [event for event in events if event.event_type == "runtime.tool_completed" and event.payload.get("tool") == "no/such/tool"]
    assert completed
    assert completed[-1].payload["status"] == "error"
    assert completed[-1].payload["error_kind"] == "unknown_tool"
    assert "unknown tool" in completed[-1].payload["error"]
    assert not any(event.event_type == "runtime.failed" for event in events)
    assert outputs == ["done"]


def test_dispatch_of_write_tool_creates_pending_approval(tmp_path: Path) -> None:
    graph = _ScriptedGraph(
        _ScriptedStep(
            tool_call=ToolCall(
                tool_name="invoke_tool",
                tool_call_id="provider-call-42",
                arguments={"name": "apply_patch", "arguments": {"patch": "+ new.txt\n+ hello\n"}},
            )
        ),
    )
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        graph=graph,
        config=RuntimeConfig(execution_engine="deterministic"),
        permission_policy=PermissionPolicy(mode="ask"),
    )
    events, _outputs, _ = _run_events(runtime, graph, session_id="dispatch-approval")
    approvals = [event for event in events if event.event_type == "runtime.approval_requested"]
    assert approvals, "expected a pending approval for the dispatched write tool"
    assert approvals[-1].payload["tool"] == "apply_patch"
    assert approvals[-1].payload["arguments"] == {"patch": "+ new.txt\n+ hello\n"}
    assert not any(event.event_type == "runtime.failed" for event in events)
    # The inner request record carries the provider-visible tool_call_id so the
    # approval-resume path can pair the resumed result with the invoke_tool call.
    inner_request = [event for event in events if event.event_type == "graph.tool_request_created" and event.payload.get("tool") == "apply_patch"]
    assert inner_request
    assert inner_request[-1].payload["tool_call_id"] == "provider-call-42"
    assert inner_request[-1].payload["arguments"] == {"patch": "+ new.txt\n+ hello\n"}


def test_dispatch_is_denied_under_read_only_with_tool_level_feedback(tmp_path: Path) -> None:
    graph = _ScriptedGraph(
        _ScriptedStep(
            tool_call=ToolCall(
                tool_name="invoke_tool",
                arguments={"name": "apply_patch", "arguments": {"patch": "+ new.txt\n+ hello\n"}},
            )
        ),
        _ScriptedStep(output="done", is_finished=True),
    )
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        graph=graph,
        config=RuntimeConfig(execution_engine="deterministic"),
        permission_policy=PermissionPolicy(mode="allow"),
    )
    events, outputs, _ = _run_events(runtime, graph, session_id="dispatch-read-only", metadata={"mode": "analyze"})
    completed = [event for event in events if event.event_type == "runtime.tool_completed" and event.payload.get("tool") == "apply_patch"]
    assert completed, "expected tool-level feedback for the denied dispatch"
    assert completed[-1].payload["status"] == "error"
    assert completed[-1].payload["error_kind"] == "runtime_tool_policy_denied"
    assert "read-only runtime policy denies mutating tools" in completed[-1].payload["error"]
    # Permission denial is tool-level feedback, not a terminal session failure.
    assert not any(event.event_type == "runtime.failed" for event in events)
    assert outputs == ["done"]


# ---------------------------------------------------------------------------
# (d) skill catalog in system-prompt metadata, not in the skill description
# ---------------------------------------------------------------------------


def _skill_registry_with(tmp_path: Path) -> SkillRegistry:
    skill_root = tmp_path / ".voidcode" / "skills"
    skill_dir = skill_root / "summarize"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: summarize\ndescription: Summarize selected files.\n---\n# Summarize\nUse concise bullet points.\n",
        encoding="utf-8",
    )
    discovered = LocalSkillMetadataLoader().discover(workspace=tmp_path)
    return SkillRegistry(skills={skill.name: skill for skill in discovered})


def test_skill_catalog_lives_in_system_prompt_metadata_not_skill_description(tmp_path: Path) -> None:
    graph = _ScriptedGraph(_ScriptedStep(output="done", is_finished=True))
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        graph=graph,
        config=RuntimeConfig(execution_engine="deterministic"),
        skill_registry=_skill_registry_with(tmp_path),
        permission_policy=PermissionPolicy(mode="allow"),
    )
    events, outputs, _ = _run_events(runtime, graph, session_id="skill-catalog")
    assert outputs == ["done"]
    recorded = [request for request in graph.requests if request.available_tools]
    assert recorded

    # System-prompt metadata carries the catalog (name + description).
    skill_meta = recorded[0].assembled_context.segments
    catalog_segments = [
        segment.content
        for segment in skill_meta
        if segment.role == "system" and isinstance(segment.content, str) and "<available_skills>" in segment.content
    ]
    assert catalog_segments, "expected the skill catalog in system-prompt metadata"
    assert "summarize" in catalog_segments[0]
    assert "Summarize selected files." in catalog_segments[0]

    # The skill tool description no longer embeds the catalog.
    skill_definition = next(tool for tool in recorded[0].available_tools if tool.name == "skill")
    assert "<available_skills>" not in skill_definition.description
    assert "catalog" in skill_definition.description
