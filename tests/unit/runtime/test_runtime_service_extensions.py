from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

import pytest

from voidcode.provider.registry import ModelProviderRegistry
from voidcode.runtime.acp import DisabledAcpAdapter
from voidcode.runtime.config import (
    RuntimeAcpConfig,
    RuntimeConfig,
    RuntimeLspConfig,
    RuntimeLspServerConfig,
    RuntimeMcpServerConfig,
    RuntimeProviderFallbackConfig,
    RuntimeSkillsConfig,
)
from voidcode.runtime.events import (
    RUNTIME_MCP_SERVER_STARTED,
    RUNTIME_MCP_SERVER_STOPPED,
    EventEnvelope,
)
from voidcode.runtime.lsp import DisabledLspManager
from voidcode.runtime.mcp import McpConfigState, McpManagerState, McpRuntimeEvent
from voidcode.runtime.permission import PermissionPolicy
from voidcode.runtime.service import (
    GraphRunRequest,
    RuntimeRequest,
    SessionState,
    VoidCodeRuntime,
)
from voidcode.runtime.single_agent_provider import (
    ProviderExecutionError,
    SingleAgentTurnRequest,
    SingleAgentTurnResult,
)
from voidcode.runtime.skills import SkillRegistry
from voidcode.tools import ToolCall


def _private_attr(instance: object, name: str) -> Any:
    return getattr(instance, name)


@dataclass(slots=True)
class _StubStep:
    tool_call: ToolCall | None = None
    output: str | None = None
    events: tuple[EventEnvelope, ...] = ()
    is_finished: bool = False


class _StubGraph:
    def step(
        self,
        request: GraphRunRequest,
        tool_results: tuple[object, ...],
        *,
        session: SessionState,
    ) -> _StubStep:
        if not tool_results:
            return _StubStep(
                tool_call=ToolCall(tool_name="read_file", arguments={"path": "sample.txt"})
            )
        return _StubStep(output=request.prompt, is_finished=True)


class _SkillCapturingStubGraph:
    last_request: GraphRunRequest | None = None

    def step(
        self,
        request: GraphRunRequest,
        tool_results: tuple[object, ...],
        *,
        session: SessionState,
    ) -> _StubStep:
        _ = tool_results, session
        type(self).last_request = request
        return _StubStep(output=request.prompt, is_finished=True)


class _SkillAwareStubGraph:
    last_request: GraphRunRequest | None = None

    def step(
        self,
        request: GraphRunRequest,
        tool_results: tuple[object, ...],
        *,
        session: SessionState,
    ) -> _StubStep:
        _ = tool_results, session
        type(self).last_request = request
        skill_names = [skill["name"] for skill in request.applied_skills]
        skill_contents = [skill["content"] for skill in request.applied_skills]
        if skill_names:
            output = f"{request.prompt}\n[skills={','.join(skill_names)}]\n{skill_contents[0]}"
        else:
            output = request.prompt
        return _StubStep(output=output, is_finished=True)


class _ApprovalThenCaptureSkillGraph:
    last_request: GraphRunRequest | None = None

    def step(
        self,
        request: GraphRunRequest,
        tool_results: tuple[object, ...],
        *,
        session: SessionState,
    ) -> _StubStep:
        _ = session
        type(self).last_request = request
        if not tool_results:
            return _StubStep(
                tool_call=ToolCall(
                    tool_name="write_file", arguments={"path": "alpha.txt", "content": "1"}
                )
            )
        return _StubStep(output="done", is_finished=True)


class _FailingProviderGraph:
    def step(
        self,
        request: GraphRunRequest,
        tool_results: tuple[object, ...],
        *,
        session: SessionState,
    ) -> _StubStep:
        _ = request, tool_results, session
        raise ValueError("provider context window exceeded")


class _ScriptedSingleAgentProvider:
    def __init__(self, *, name: str, outcomes: tuple[object, ...]) -> None:
        self.name = name
        self._outcomes = list(outcomes)

    def propose_turn(self, request: object) -> SingleAgentTurnResult:
        _ = request
        if not self._outcomes:
            return SingleAgentTurnResult(output="done")
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return cast(SingleAgentTurnResult, outcome)


@dataclass(frozen=True, slots=True)
class _ScriptedModelProvider:
    name: str
    outcomes: tuple[object, ...]

    def single_agent_provider(self) -> _ScriptedSingleAgentProvider:
        return _ScriptedSingleAgentProvider(name=self.name, outcomes=self.outcomes)


class _AlwaysFailingModelProvider:
    _error_kind: Literal["rate_limit", "context_limit", "invalid_model", "transient_failure"]

    def __init__(
        self,
        *,
        name: str,
        error_kind: Literal["rate_limit", "context_limit", "invalid_model", "transient_failure"],
    ) -> None:
        self.name = name
        self._error_kind = error_kind

    def single_agent_provider(self) -> _AlwaysFailingSingleAgentProvider:
        return _AlwaysFailingSingleAgentProvider(name=self.name, error_kind=self._error_kind)


@dataclass(slots=True)
class _AlwaysFailingSingleAgentProvider:
    name: str
    error_kind: Literal["rate_limit", "context_limit", "invalid_model", "transient_failure"]

    def propose_turn(self, request: object) -> SingleAgentTurnResult:
        _ = request
        raise ProviderExecutionError(
            kind=self.error_kind,
            provider_name=self.name,
            model_name="gpt-5.4",
            message=f"{self.error_kind} failure",
        )


@dataclass(slots=True)
class _ApprovalResumeFallbackModelProvider:
    name: str
    attempts_seen: list[int]

    def single_agent_provider(self) -> _ApprovalResumeFallbackSingleAgentProvider:
        return _ApprovalResumeFallbackSingleAgentProvider(
            name=self.name,
            attempts_seen=self.attempts_seen,
        )


@dataclass(slots=True)
class _ApprovalResumeFallbackSingleAgentProvider:
    name: str
    attempts_seen: list[int]

    def propose_turn(self, request: object) -> SingleAgentTurnResult:
        turn_request = cast(SingleAgentTurnRequest, request)
        self.attempts_seen.append(turn_request.attempt)
        if not turn_request.tool_results:
            return SingleAgentTurnResult(
                tool_call=ToolCall(
                    tool_name="write_file",
                    arguments={"path": "alpha.txt", "content": "1"},
                )
            )
        return SingleAgentTurnResult(output="done")


def _write_demo_skill(skill_dir: Path, *, description: str = "Demo skill", content: str) -> None:
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: demo\ndescription: {description}\n---\n{content}\n",
        encoding="utf-8",
    )


def test_runtime_initializes_empty_extension_state_by_default(tmp_path: Path) -> None:
    runtime = VoidCodeRuntime(workspace=tmp_path, config=RuntimeConfig())
    provider_model = _private_attr(runtime, "_provider_model")
    skill_registry = _private_attr(runtime, "_skill_registry")
    lsp_manager = _private_attr(runtime, "_lsp_manager")
    acp_adapter = _private_attr(runtime, "_acp_adapter")

    assert provider_model.selection.raw_model is None
    assert provider_model.provider is None
    assert skill_registry.skills == {}
    assert lsp_manager.current_state().mode == "disabled"
    assert lsp_manager.configuration.configured_enabled is False
    assert acp_adapter.current_state().mode == "disabled"
    assert acp_adapter.configuration.configured_enabled is False
    assert runtime.current_acp_state().status == "disconnected"


def test_runtime_initializes_extension_state_from_config_when_enabled(tmp_path: Path) -> None:
    skill_dir = tmp_path / ".voidcode" / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Demo skill\n---\n",
        encoding="utf-8",
    )

    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        config=RuntimeConfig(
            model="opencode/gpt-5.4",
            skills=RuntimeSkillsConfig(enabled=True),
            lsp=RuntimeLspConfig(
                enabled=True,
                servers={
                    "pyright": RuntimeLspServerConfig(command=("pyright-langserver", "--stdio"))
                },
            ),
            acp=RuntimeAcpConfig(enabled=True),
        ),
    )

    provider_model = _private_attr(runtime, "_provider_model")
    skill_registry = _private_attr(runtime, "_skill_registry")
    lsp_manager = _private_attr(runtime, "_lsp_manager")
    acp_adapter = _private_attr(runtime, "_acp_adapter")
    skill = skill_registry.resolve("demo")
    lsp_state = lsp_manager.current_state()
    acp_state = acp_adapter.current_state()

    assert provider_model.selection.raw_model == "opencode/gpt-5.4"
    assert provider_model.selection.provider == "opencode"
    assert provider_model.selection.model == "gpt-5.4"
    assert provider_model.provider is not None
    assert provider_model.provider.name == "opencode"
    assert skill.description == "Demo skill"
    assert skill.directory == skill_dir.resolve()
    assert lsp_state.mode == "managed"
    assert lsp_state.configuration.configured_enabled is True
    assert tuple(lsp_state.servers) == ("pyright",)
    assert lsp_state.servers["pyright"].status == "stopped"
    assert lsp_state.servers["pyright"].available is False
    assert acp_state.mode == "managed"
    assert acp_state.configuration.configured_enabled is True
    assert acp_state.configured is True
    assert acp_state.status == "disconnected"
    assert acp_state.available is False


def test_runtime_keeps_skill_registry_empty_when_skills_not_explicitly_enabled(
    tmp_path: Path,
) -> None:
    skill_dir = tmp_path / "custom-skills" / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Demo skill\n---\n",
        encoding="utf-8",
    )

    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        config=RuntimeConfig(
            skills=RuntimeSkillsConfig(enabled=False, paths=("custom-skills",)),
        ),
    )

    assert _private_attr(runtime, "_skill_registry").skills == {}


def test_runtime_retains_explicit_injected_extension_instances(tmp_path: Path) -> None:
    injected_skill_registry = SkillRegistry()
    injected_lsp_manager = DisabledLspManager()
    injected_acp_adapter = DisabledAcpAdapter()

    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        config=RuntimeConfig(
            skills=RuntimeSkillsConfig(enabled=True),
            lsp=RuntimeLspConfig(
                enabled=True,
                servers={
                    "pyright": RuntimeLspServerConfig(command=("pyright-langserver", "--stdio"))
                },
            ),
            acp=RuntimeAcpConfig(enabled=True),
        ),
        skill_registry=injected_skill_registry,
        lsp_manager=injected_lsp_manager,
        acp_adapter=injected_acp_adapter,
    )

    assert _private_attr(runtime, "_skill_registry") is injected_skill_registry
    assert _private_attr(runtime, "_lsp_manager") is injected_lsp_manager
    assert _private_attr(runtime, "_acp_adapter") is injected_acp_adapter
    assert injected_skill_registry.skills == {}
    assert injected_lsp_manager.current_state().configuration.configured_enabled is False
    assert injected_acp_adapter.current_state().configuration.configured_enabled is False


def test_runtime_exposes_managed_acp_connect_disconnect_and_request(tmp_path: Path) -> None:
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        config=RuntimeConfig(acp=RuntimeAcpConfig(enabled=True)),
    )

    assert runtime.current_acp_state().mode == "managed"
    assert runtime.current_acp_state().status == "disconnected"

    connect_events = runtime.connect_acp()
    assert [event.event_type for event in connect_events] == ["runtime.acp_connected"]
    assert runtime.current_acp_state().status == "connected"
    assert runtime.current_acp_state().available is True

    response = runtime.request_acp(request_type="ping", payload={"demo": True})
    assert response.status == "ok"
    assert response.payload == {"request_type": "ping", "accepted": True, "demo": True}

    disconnect_events = runtime.disconnect_acp()
    assert [event.event_type for event in disconnect_events] == ["runtime.acp_disconnected"]
    assert runtime.current_acp_state().status == "disconnected"


def test_runtime_acp_request_before_connect_returns_error_without_failing_adapter(
    tmp_path: Path,
) -> None:
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        config=RuntimeConfig(acp=RuntimeAcpConfig(enabled=True)),
    )

    response = runtime.request_acp(request_type="ping", payload={})

    assert response.status == "error"
    assert response.error == "ACP adapter is not connected"
    assert runtime.current_acp_state().status == "disconnected"
    assert runtime.current_acp_state().last_error is None

    connect_events = runtime.connect_acp()
    assert [event.event_type for event in connect_events] == ["runtime.acp_connected"]


def test_runtime_fail_acp_surfaces_failure_events_and_metadata(tmp_path: Path) -> None:
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        config=RuntimeConfig(acp=RuntimeAcpConfig(enabled=True)),
    )

    fail_events = runtime.fail_acp("boom")

    assert [event.event_type for event in fail_events] == ["runtime.acp_failed"]
    assert runtime.current_acp_state().status == "failed"
    assert runtime.current_acp_state().last_error == "boom"


def test_runtime_connect_acp_rejects_disabled_adapter(tmp_path: Path) -> None:
    runtime = VoidCodeRuntime(workspace=tmp_path, config=RuntimeConfig())

    with pytest.raises(ValueError, match="disabled"):
        _ = runtime.connect_acp()


def test_runtime_exit_disconnects_managed_acp(tmp_path: Path) -> None:
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        config=RuntimeConfig(acp=RuntimeAcpConfig(enabled=True)),
    )
    _ = runtime.connect_acp()

    runtime.__exit__(None, None, None)

    assert runtime.current_acp_state().status == "disconnected"


def test_runtime_exit_shuts_down_managed_mcp(tmp_path: Path) -> None:
    class _StubMcpManager:
        def __init__(self) -> None:
            self.shutdown_calls = 0

        @property
        def configuration(self) -> McpConfigState:
            return McpConfigState(configured_enabled=True)

        def current_state(self) -> McpManagerState:
            return McpManagerState(mode="managed", configuration=self.configuration)

        def list_tools(self, *, workspace: Path):
            _ = workspace
            return ()

        def call_tool(
            self, *, server_name: str, tool_name: str, arguments: dict[str, object], workspace: Path
        ):
            _ = server_name, tool_name, arguments, workspace
            raise AssertionError("not used")

        def shutdown(self) -> tuple[McpRuntimeEvent, ...]:
            self.shutdown_calls += 1
            return (
                McpRuntimeEvent(
                    event_type=RUNTIME_MCP_SERVER_STOPPED,
                    payload={"server": "echo", "workspace_root": str(tmp_path)},
                ),
            )

        def drain_events(self) -> tuple[McpRuntimeEvent, ...]:
            return ()

    mcp_manager = _StubMcpManager()
    runtime = VoidCodeRuntime(workspace=tmp_path, mcp_manager=mcp_manager)

    runtime.__exit__(None, None, None)

    assert mcp_manager.shutdown_calls == 1


def test_runtime_surfaces_mcp_lifecycle_events_in_run_responses(tmp_path: Path) -> None:
    class _StubMcpManager:
        @property
        def configuration(self) -> McpConfigState:
            return McpConfigState(configured_enabled=True)

        def current_state(self) -> McpManagerState:
            return McpManagerState(mode="managed", configuration=self.configuration)

        def list_tools(self, *, workspace: Path):
            _ = workspace
            return ()

        def call_tool(
            self, *, server_name: str, tool_name: str, arguments: dict[str, object], workspace: Path
        ):
            _ = server_name, tool_name, arguments, workspace
            raise AssertionError("not used")

        def shutdown(self) -> tuple[McpRuntimeEvent, ...]:
            return ()

        def drain_events(self) -> tuple[McpRuntimeEvent, ...]:
            return (
                McpRuntimeEvent(
                    event_type=RUNTIME_MCP_SERVER_STARTED,
                    payload={"server": "echo", "workspace_root": str(tmp_path)},
                ),
            )

    sample_file = tmp_path / "sample.txt"
    sample_file.write_text("alpha beta\n", encoding="utf-8")
    runtime = VoidCodeRuntime(workspace=tmp_path, graph=_StubGraph(), mcp_manager=_StubMcpManager())

    response = runtime.run(RuntimeRequest(prompt="hello"))

    assert any(event.event_type == RUNTIME_MCP_SERVER_STARTED for event in response.events)


def test_runtime_metadata_includes_mcp_state_when_configured(tmp_path: Path) -> None:
    class _StubMcpManager:
        @property
        def configuration(self) -> McpConfigState:
            return McpConfigState(
                configured_enabled=True,
                servers={
                    "echo": RuntimeMcpServerConfig(
                        transport="stdio",
                        command=("python", "server.py"),
                    )
                },
            )

        def current_state(self) -> McpManagerState:
            return McpManagerState(mode="managed", configuration=self.configuration)

        def list_tools(self, *, workspace: Path):
            _ = workspace
            return ()

        def call_tool(
            self, *, server_name: str, tool_name: str, arguments: dict[str, object], workspace: Path
        ):
            _ = server_name, tool_name, arguments, workspace
            raise AssertionError("not used")

        def shutdown(self) -> tuple[McpRuntimeEvent, ...]:
            return ()

        def drain_events(self) -> tuple[McpRuntimeEvent, ...]:
            return ()

    runtime = VoidCodeRuntime(workspace=tmp_path, mcp_manager=_StubMcpManager())

    runtime_config_metadata = runtime._runtime_config_metadata()  # pyright: ignore[reportPrivateUsage]

    assert runtime_config_metadata["mcp"] == {
        "mode": "managed",
        "configured_enabled": True,
        "servers": ["echo"],
    }


def test_runtime_default_extension_construction_preserves_public_run_path(
    tmp_path: Path,
) -> None:
    sample_file = tmp_path / "sample.txt"
    sample_file.write_text("alpha beta\n", encoding="utf-8")
    alpha_skill_dir = tmp_path / ".voidcode" / "skills" / "alpha"
    zeta_skill_dir = tmp_path / ".voidcode" / "skills" / "zeta"
    alpha_skill_dir.mkdir(parents=True)
    zeta_skill_dir.mkdir(parents=True)
    (alpha_skill_dir / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: Alpha skill\n---\n",
        encoding="utf-8",
    )
    (zeta_skill_dir / "SKILL.md").write_text(
        "---\nname: zeta\ndescription: Zeta skill\n---\n",
        encoding="utf-8",
    )

    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        graph=_StubGraph(),
        config=RuntimeConfig(
            skills=RuntimeSkillsConfig(enabled=True),
            lsp=RuntimeLspConfig(enabled=True),
            acp=RuntimeAcpConfig(enabled=True),
        ),
    )

    response = runtime.run(RuntimeRequest(prompt="hello"))

    assert response.session.status == "completed"
    assert response.output == "hello"
    assert response.events[1].event_type == "runtime.skills_loaded"
    assert response.events[1].payload == {"skills": ["alpha", "zeta"]}
    assert response.events[2].event_type == "runtime.skills_applied"
    assert response.events[3].event_type == "graph.tool_request_created"
    assert response.events[4].event_type == "runtime.tool_lookup_succeeded"
    assert response.events[6].event_type == "runtime.tool_completed"
    runtime_config_metadata = cast(dict[str, object], response.session.metadata["runtime_config"])
    assert runtime_config_metadata["acp"] == {
        "mode": "managed",
        "configured_enabled": True,
        "status": "disconnected",
        "available": False,
        "last_error": None,
    }


def test_runtime_emits_skills_applied_and_persists_frozen_skill_payloads(tmp_path: Path) -> None:
    skill_dir = tmp_path / ".voidcode" / "skills" / "demo"
    _write_demo_skill(
        skill_dir,
        content="# Demo\nAlways explain your reasoning.",
    )

    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        graph=_SkillCapturingStubGraph(),
        config=RuntimeConfig(skills=RuntimeSkillsConfig(enabled=True)),
    )

    response = runtime.run(RuntimeRequest(prompt="hello"))

    assert [event.event_type for event in response.events[:3]] == [
        "runtime.request_received",
        "runtime.skills_loaded",
        "runtime.skills_applied",
    ]
    assert response.events[2].payload == {"skills": ["demo"], "count": 1}
    assert response.session.metadata["applied_skills"] == ["demo"]
    assert response.session.metadata["applied_skill_payloads"] == [
        {
            "name": "demo",
            "description": "Demo skill",
            "content": "# Demo\nAlways explain your reasoning.",
        }
    ]
    assert _SkillCapturingStubGraph.last_request is not None
    assert _SkillCapturingStubGraph.last_request.applied_skills == (
        {
            "name": "demo",
            "description": "Demo skill",
            "content": "# Demo\nAlways explain your reasoning.",
        },
    )


def test_runtime_persists_explicit_empty_applied_skill_snapshot(tmp_path: Path) -> None:
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        graph=_SkillCapturingStubGraph(),
        config=RuntimeConfig(skills=RuntimeSkillsConfig(enabled=True)),
    )

    response = runtime.run(RuntimeRequest(prompt="hello"))

    assert [event.event_type for event in response.events[:2]] == [
        "runtime.request_received",
        "runtime.skills_loaded",
    ]
    assert response.session.metadata["applied_skills"] == []
    assert response.session.metadata["applied_skill_payloads"] == []
    assert _SkillCapturingStubGraph.last_request is not None
    assert _SkillCapturingStubGraph.last_request.applied_skills == ()


def test_runtime_skill_payloads_affect_execution_output_when_graph_consumes_them(
    tmp_path: Path,
) -> None:
    skill_dir = tmp_path / ".voidcode" / "skills" / "demo"
    _write_demo_skill(
        skill_dir,
        content="# Demo\nUse concise bullet points.",
    )

    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        graph=_SkillAwareStubGraph(),
        config=RuntimeConfig(skills=RuntimeSkillsConfig(enabled=True)),
    )

    response = runtime.run(RuntimeRequest(prompt="summarize sample.txt"))

    assert response.output == (
        "summarize sample.txt\n[skills=demo]\n# Demo\nUse concise bullet points."
    )
    assert _SkillAwareStubGraph.last_request is not None
    assert _SkillAwareStubGraph.last_request.applied_skills == (
        {
            "name": "demo",
            "description": "Demo skill",
            "content": "# Demo\nUse concise bullet points.",
        },
    )


def test_runtime_resume_reuses_frozen_skill_payloads_for_execution_semantics(
    tmp_path: Path,
) -> None:
    skill_dir = tmp_path / ".voidcode" / "skills" / "demo"
    _write_demo_skill(
        skill_dir,
        content="# Demo\nUse concise bullet points.",
    )

    initial_runtime = VoidCodeRuntime(
        workspace=tmp_path,
        graph=_ApprovalThenCaptureSkillGraph(),
        config=RuntimeConfig(skills=RuntimeSkillsConfig(enabled=True), approval_mode="ask"),
    )

    waiting = initial_runtime.run(RuntimeRequest(prompt="go", session_id="skill-exec-resume"))
    approval_request_id = str(waiting.events[-1].payload["request_id"])

    _write_demo_skill(
        skill_dir,
        description="Changed skill",
        content="# Changed\nDo something else.",
    )

    resumed_runtime = VoidCodeRuntime(
        workspace=tmp_path,
        graph=_SkillAwareStubGraph(),
        config=RuntimeConfig(skills=RuntimeSkillsConfig(enabled=True), approval_mode="ask"),
    )

    resumed = resumed_runtime.resume(
        session_id="skill-exec-resume",
        approval_request_id=approval_request_id,
        approval_decision="allow",
    )

    assert resumed.output == "go\n[skills=demo]\n# Demo\nUse concise bullet points."
    assert _SkillAwareStubGraph.last_request is not None
    assert _SkillAwareStubGraph.last_request.applied_skills == (
        {
            "name": "demo",
            "description": "Demo skill",
            "content": "# Demo\nUse concise bullet points.",
        },
    )


class _MultiStepStubGraph:
    def step(
        self,
        request: GraphRunRequest,
        tool_results: tuple[object, ...],
        *,
        session: SessionState,
    ) -> _StubStep:
        if not tool_results:
            return _StubStep(
                tool_call=ToolCall(
                    tool_name="write_file", arguments={"path": "alpha.txt", "content": "1"}
                )
            )
        if len(tool_results) == 1:
            return _StubStep(
                tool_call=ToolCall(
                    tool_name="write_file", arguments={"path": "beta.txt", "content": "2"}
                )
            )
        return _StubStep(output="done", is_finished=True)


def test_runtime_resumes_with_subsequent_tool_calls_properly(tmp_path: Path) -> None:
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        graph=_MultiStepStubGraph(),
        permission_policy=PermissionPolicy(mode="ask"),
    )

    # Run first, expects pending approval for write_file alpha.txt
    response = runtime.run(RuntimeRequest(prompt="go", session_id="test-resume"))
    assert response.session.status == "waiting"

    # Resume the pending approval for alpha.txt.
    # The graph will then return a second tool call for beta.txt.
    # It should result in a SECOND pending approval, not an error.
    approval_request_id = str(response.events[-1].payload.get("request_id", ""))

    second_response = runtime.resume(
        session_id="test-resume", approval_request_id=approval_request_id, approval_decision="allow"
    )
    assert second_response.session.status == "waiting"

    # Resume the second pending approval for beta.txt
    second_approval_request_id = str(second_response.events[-1].payload.get("request_id", ""))

    final_response = runtime.resume(
        session_id="test-resume",
        approval_request_id=second_approval_request_id,
        approval_decision="allow",
    )

    assert final_response.session.status == "completed"
    assert final_response.output == "done"


def test_runtime_skill_enabled_resume_emits_single_approval_resolution(tmp_path: Path) -> None:
    skill_dir = tmp_path / ".voidcode" / "skills" / "demo"
    _write_demo_skill(skill_dir, content="# Demo\nUse the demo skill.")

    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        graph=_ApprovalThenCaptureSkillGraph(),
        config=RuntimeConfig(skills=RuntimeSkillsConfig(enabled=True), approval_mode="ask"),
        permission_policy=PermissionPolicy(mode="ask"),
    )

    waiting = runtime.run(RuntimeRequest(prompt="go", session_id="skill-resume-session"))

    assert waiting.session.status == "waiting"
    assert sum(event.event_type == "runtime.skills_applied" for event in waiting.events) == 1

    approval_request_id = str(waiting.events[-1].payload["request_id"])
    resumed = runtime.resume(
        session_id="skill-resume-session",
        approval_request_id=approval_request_id,
        approval_decision="allow",
    )

    assert resumed.session.status == "completed"
    assert resumed.output == "done"
    assert sum(event.event_type == "runtime.approval_resolved" for event in resumed.events) == 1
    assert sum(event.event_type == "runtime.skills_applied" for event in resumed.events) == 1
    assert [event.sequence for event in resumed.events] == sorted(
        event.sequence for event in resumed.events
    )


def test_runtime_resume_uses_frozen_applied_skill_payloads_when_live_skill_changes(
    tmp_path: Path,
) -> None:
    skill_dir = tmp_path / ".voidcode" / "skills" / "demo"
    _write_demo_skill(
        skill_dir,
        description="Demo skill",
        content="# Demo\nOriginal instructions.",
    )

    initial_runtime = VoidCodeRuntime(
        workspace=tmp_path,
        graph=_ApprovalThenCaptureSkillGraph(),
        config=RuntimeConfig(skills=RuntimeSkillsConfig(enabled=True), approval_mode="ask"),
        permission_policy=PermissionPolicy(mode="ask"),
    )

    waiting = initial_runtime.run(RuntimeRequest(prompt="go", session_id="frozen-skill-session"))

    assert waiting.session.status == "waiting"
    approval_request_id = str(waiting.events[-1].payload["request_id"])

    _write_demo_skill(
        skill_dir,
        description="Changed skill",
        content="# Demo\nChanged instructions.",
    )

    resumed_runtime = VoidCodeRuntime(
        workspace=tmp_path,
        graph=_ApprovalThenCaptureSkillGraph(),
        config=RuntimeConfig(skills=RuntimeSkillsConfig(enabled=True), approval_mode="ask"),
        permission_policy=PermissionPolicy(mode="ask"),
    )

    resumed = resumed_runtime.resume(
        session_id="frozen-skill-session",
        approval_request_id=approval_request_id,
        approval_decision="allow",
    )

    assert resumed.session.status == "completed"
    assert _ApprovalThenCaptureSkillGraph.last_request is not None
    assert _ApprovalThenCaptureSkillGraph.last_request.applied_skills == (
        {
            "name": "demo",
            "description": "Demo skill",
            "content": "# Demo\nOriginal instructions.",
        },
    )


def test_runtime_resume_preserves_explicit_empty_applied_skill_snapshot(
    tmp_path: Path,
) -> None:
    initial_runtime = VoidCodeRuntime(
        workspace=tmp_path,
        graph=_ApprovalThenCaptureSkillGraph(),
        config=RuntimeConfig(skills=RuntimeSkillsConfig(enabled=True), approval_mode="ask"),
        permission_policy=PermissionPolicy(mode="ask"),
    )

    waiting = initial_runtime.run(RuntimeRequest(prompt="go", session_id="empty-skill-session"))

    assert waiting.session.status == "waiting"
    assert waiting.session.metadata["applied_skills"] == []
    assert waiting.session.metadata["applied_skill_payloads"] == []
    approval_request_id = str(waiting.events[-1].payload["request_id"])

    skill_dir = tmp_path / ".voidcode" / "skills" / "demo"
    _write_demo_skill(
        skill_dir,
        description="New skill",
        content="# Demo\nAdded after waiting.",
    )

    resumed_runtime = VoidCodeRuntime(
        workspace=tmp_path,
        graph=_ApprovalThenCaptureSkillGraph(),
        config=RuntimeConfig(skills=RuntimeSkillsConfig(enabled=True), approval_mode="ask"),
        permission_policy=PermissionPolicy(mode="ask"),
    )

    resumed = resumed_runtime.resume(
        session_id="empty-skill-session",
        approval_request_id=approval_request_id,
        approval_decision="allow",
    )

    assert resumed.session.status == "completed"
    assert _ApprovalThenCaptureSkillGraph.last_request is not None
    assert _ApprovalThenCaptureSkillGraph.last_request.applied_skills == ()


def test_runtime_resume_preserves_legacy_name_only_empty_applied_skills_snapshot(
    tmp_path: Path,
) -> None:
    initial_runtime = VoidCodeRuntime(
        workspace=tmp_path,
        graph=_ApprovalThenCaptureSkillGraph(),
        config=RuntimeConfig(skills=RuntimeSkillsConfig(enabled=True), approval_mode="ask"),
        permission_policy=PermissionPolicy(mode="ask"),
    )

    waiting = initial_runtime.run(
        RuntimeRequest(prompt="go", session_id="legacy-empty-skill-session")
    )

    assert waiting.session.status == "waiting"
    approval_request_id = str(waiting.events[-1].payload["request_id"])

    database_path = tmp_path / ".voidcode" / "sessions.sqlite3"
    connection = sqlite3.connect(database_path)
    try:
        row = connection.execute(
            "SELECT metadata_json FROM sessions WHERE session_id = ?",
            ("legacy-empty-skill-session",),
        ).fetchone()
        assert row is not None
        metadata = json.loads(str(row[0]))
        assert isinstance(metadata, dict)
        metadata_dict = cast(dict[str, object], metadata)
        metadata_dict.pop("applied_skill_payloads", None)
        _ = connection.execute(
            "UPDATE sessions SET metadata_json = ? WHERE session_id = ?",
            (json.dumps(metadata_dict, sort_keys=True), "legacy-empty-skill-session"),
        )
        connection.commit()
    finally:
        connection.close()

    skill_dir = tmp_path / ".voidcode" / "skills" / "demo"
    _write_demo_skill(
        skill_dir,
        description="New skill",
        content="# Demo\nAdded after waiting.",
    )

    resumed_runtime = VoidCodeRuntime(
        workspace=tmp_path,
        graph=_ApprovalThenCaptureSkillGraph(),
        config=RuntimeConfig(skills=RuntimeSkillsConfig(enabled=True), approval_mode="ask"),
        permission_policy=PermissionPolicy(mode="ask"),
    )

    resumed = resumed_runtime.resume(
        session_id="legacy-empty-skill-session",
        approval_request_id=approval_request_id,
        approval_decision="allow",
    )

    assert resumed.session.status == "completed"
    assert _ApprovalThenCaptureSkillGraph.last_request is not None
    assert _ApprovalThenCaptureSkillGraph.last_request.applied_skills == ()


def test_runtime_resume_reconstructs_legacy_applied_skill_names_from_live_registry(
    tmp_path: Path,
) -> None:
    skill_dir = tmp_path / ".voidcode" / "skills" / "demo"
    _write_demo_skill(
        skill_dir,
        description="Demo skill",
        content="# Demo\nOriginal instructions.",
    )

    initial_runtime = VoidCodeRuntime(
        workspace=tmp_path,
        graph=_ApprovalThenCaptureSkillGraph(),
        config=RuntimeConfig(skills=RuntimeSkillsConfig(enabled=True), approval_mode="ask"),
        permission_policy=PermissionPolicy(mode="ask"),
    )

    waiting = initial_runtime.run(RuntimeRequest(prompt="go", session_id="legacy-skill-session"))

    assert waiting.session.status == "waiting"
    approval_request_id = str(waiting.events[-1].payload["request_id"])

    database_path = tmp_path / ".voidcode" / "sessions.sqlite3"
    connection = sqlite3.connect(database_path)
    try:
        row = connection.execute(
            "SELECT metadata_json FROM sessions WHERE session_id = ?",
            ("legacy-skill-session",),
        ).fetchone()
        assert row is not None
        metadata = json.loads(str(row[0]))
        assert isinstance(metadata, dict)
        metadata_dict = cast(dict[str, object], metadata)
        metadata_dict.pop("applied_skill_payloads", None)
        _ = connection.execute(
            "UPDATE sessions SET metadata_json = ? WHERE session_id = ?",
            (json.dumps(metadata_dict, sort_keys=True), "legacy-skill-session"),
        )
        connection.commit()
    finally:
        connection.close()

    _write_demo_skill(
        skill_dir,
        description="Changed skill",
        content="# Demo\nChanged instructions.",
    )

    resumed_runtime = VoidCodeRuntime(
        workspace=tmp_path,
        graph=_ApprovalThenCaptureSkillGraph(),
        config=RuntimeConfig(skills=RuntimeSkillsConfig(enabled=True), approval_mode="ask"),
        permission_policy=PermissionPolicy(mode="ask"),
    )

    resumed = resumed_runtime.resume(
        session_id="legacy-skill-session",
        approval_request_id=approval_request_id,
        approval_decision="allow",
    )

    assert resumed.session.status == "completed"
    assert _ApprovalThenCaptureSkillGraph.last_request is not None
    assert _ApprovalThenCaptureSkillGraph.last_request.applied_skills == (
        {
            "name": "demo",
            "description": "Changed skill",
            "content": "# Demo\nChanged instructions.",
        },
    )


def test_runtime_resume_uses_persisted_approval_mode_for_follow_up_gated_tools(
    tmp_path: Path,
) -> None:
    initial_runtime = VoidCodeRuntime(
        workspace=tmp_path,
        graph=_MultiStepStubGraph(),
        config=RuntimeConfig(approval_mode="ask"),
        permission_policy=PermissionPolicy(mode="ask"),
    )

    waiting = initial_runtime.run(RuntimeRequest(prompt="go", session_id="persisted-ask-session"))

    assert waiting.session.status == "waiting"
    first_request_id = str(waiting.events[-1].payload["request_id"])

    resumed_runtime = VoidCodeRuntime(
        workspace=tmp_path,
        graph=_MultiStepStubGraph(),
        config=RuntimeConfig(approval_mode="deny"),
        permission_policy=PermissionPolicy(mode="deny"),
    )

    resumed = resumed_runtime.resume(
        session_id="persisted-ask-session",
        approval_request_id=first_request_id,
        approval_decision="allow",
    )

    assert resumed.session.status == "waiting"
    assert resumed.events[-1].event_type == "runtime.approval_requested"
    assert resumed.events[-1].payload["tool"] == "write_file"
    assert resumed.events[-1].payload["arguments"] == {"path": "beta.txt", "content": "2"}
    assert resumed.events[-1].payload["policy"] == {"mode": "ask"}


def test_runtime_resume_falls_back_to_fresh_policy_for_legacy_sessions_without_runtime_config(
    tmp_path: Path,
) -> None:
    initial_runtime = VoidCodeRuntime(
        workspace=tmp_path,
        graph=_MultiStepStubGraph(),
        config=RuntimeConfig(approval_mode="ask"),
        permission_policy=PermissionPolicy(mode="ask"),
    )

    waiting = initial_runtime.run(RuntimeRequest(prompt="go", session_id="legacy-approval-session"))

    assert waiting.session.status == "waiting"
    first_request_id = str(waiting.events[-1].payload["request_id"])

    database_path = tmp_path / ".voidcode" / "sessions.sqlite3"
    connection = sqlite3.connect(database_path)
    try:
        row = connection.execute(
            "SELECT metadata_json FROM sessions WHERE session_id = ?",
            ("legacy-approval-session",),
        ).fetchone()
        assert row is not None
        metadata = json.loads(str(row[0]))
        assert isinstance(metadata, dict)
        metadata_dict = cast(dict[str, object], metadata)
        metadata_dict.pop("runtime_config", None)
        _ = connection.execute(
            "UPDATE sessions SET metadata_json = ? WHERE session_id = ?",
            (json.dumps(metadata_dict, sort_keys=True), "legacy-approval-session"),
        )
        connection.commit()
    finally:
        connection.close()

    resumed_runtime = VoidCodeRuntime(
        workspace=tmp_path,
        graph=_MultiStepStubGraph(),
        config=RuntimeConfig(approval_mode="deny"),
        permission_policy=PermissionPolicy(mode="deny"),
    )

    resumed = resumed_runtime.resume(
        session_id="legacy-approval-session",
        approval_request_id=first_request_id,
        approval_decision="allow",
    )

    assert resumed.session.status == "failed"
    assert resumed.events[-2].event_type == "runtime.approval_resolved"
    assert resumed.events[-2].payload["decision"] == "deny"
    assert resumed.events[-1].event_type == "runtime.failed"
    assert resumed.events[-1].payload == {"error": "permission denied for tool: write_file"}


def test_runtime_effective_runtime_config_prefers_persisted_session_values(tmp_path: Path) -> None:
    sample_file = tmp_path / "sample.txt"
    sample_file.write_text("config session\n", encoding="utf-8")

    initial_runtime = VoidCodeRuntime(
        workspace=tmp_path,
        config=RuntimeConfig(approval_mode="allow", model="session/model"),
    )
    _ = initial_runtime.run(RuntimeRequest(prompt="read sample.txt", session_id="config-session"))

    resumed_runtime = VoidCodeRuntime(
        workspace=tmp_path,
        config=RuntimeConfig(approval_mode="deny", model="fresh/model", max_steps=9),
    )
    effective = resumed_runtime.effective_runtime_config(session_id="config-session")

    assert effective.approval_mode == "allow"
    assert effective.model == "session/model"
    assert effective.execution_engine == "deterministic"


def test_runtime_effective_runtime_config_recovers_persisted_max_steps(tmp_path: Path) -> None:
    sample_file = tmp_path / "sample.txt"
    sample_file.write_text("config session\n", encoding="utf-8")

    initial_runtime = VoidCodeRuntime(
        workspace=tmp_path,
        config=RuntimeConfig(approval_mode="allow", model="session/model", max_steps=7),
    )
    response = initial_runtime.run(
        RuntimeRequest(prompt="read sample.txt", session_id="max-steps-session")
    )

    assert response.session.metadata["runtime_config"] == {
        "approval_mode": "allow",
        "execution_engine": "deterministic",
        "max_steps": 7,
        "model": "session/model",
        "provider_fallback": None,
        "resolved_provider": {
            "active_target": {
                "raw_model": "session/model",
                "provider": "session",
                "model": "model",
            },
            "targets": [
                {
                    "raw_model": "session/model",
                    "provider": "session",
                    "model": "model",
                }
            ],
        },
        "lsp": {"mode": "disabled", "configured_enabled": False, "servers": []},
        "acp": {
            "mode": "disabled",
            "configured_enabled": False,
            "status": "disconnected",
            "available": False,
            "last_error": None,
        },
        "mcp": {"mode": "disabled", "configured_enabled": False, "servers": []},
    }

    resumed_runtime = VoidCodeRuntime(
        workspace=tmp_path,
        config=RuntimeConfig(approval_mode="deny", model="fresh/model", max_steps=3),
    )
    effective = resumed_runtime.effective_runtime_config(session_id="max-steps-session")

    assert effective.approval_mode == "allow"
    assert effective.model == "session/model"
    assert effective.execution_engine == "deterministic"
    assert effective.max_steps == 7


def test_runtime_effective_runtime_config_rejects_invalid_persisted_max_steps(
    tmp_path: Path,
) -> None:
    sample_file = tmp_path / "sample.txt"
    sample_file.write_text("config session\n", encoding="utf-8")

    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        config=RuntimeConfig(approval_mode="allow", model="session/model", max_steps=7),
    )
    _ = runtime.run(RuntimeRequest(prompt="read sample.txt", session_id="invalid-max-steps"))

    database_path = tmp_path / ".voidcode" / "sessions.sqlite3"
    connection = sqlite3.connect(database_path)
    try:
        row = connection.execute(
            "SELECT metadata_json FROM sessions WHERE session_id = ?",
            ("invalid-max-steps",),
        ).fetchone()
        assert row is not None
        metadata = json.loads(str(row[0]))
        assert isinstance(metadata, dict)
        metadata_dict = cast(dict[str, object], metadata)
        runtime_config = cast(dict[str, object], metadata_dict["runtime_config"])
        runtime_config["max_steps"] = 0
        _ = connection.execute(
            "UPDATE sessions SET metadata_json = ? WHERE session_id = ?",
            (json.dumps(metadata_dict, sort_keys=True), "invalid-max-steps"),
        )
        connection.commit()
    finally:
        connection.close()

    resumed_runtime = VoidCodeRuntime(workspace=tmp_path, config=RuntimeConfig(max_steps=3))

    with pytest.raises(ValueError, match="persisted runtime_config max_steps must be at least 1"):
        _ = resumed_runtime.effective_runtime_config(session_id="invalid-max-steps")


def test_runtime_effective_runtime_config_falls_back_for_legacy_sessions(tmp_path: Path) -> None:
    sample_file = tmp_path / "sample.txt"
    sample_file.write_text("legacy config\n", encoding="utf-8")

    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        config=RuntimeConfig(approval_mode="allow", model="session/model"),
    )
    _ = runtime.run(RuntimeRequest(prompt="read sample.txt", session_id="legacy-config-session"))

    database_path = tmp_path / ".voidcode" / "sessions.sqlite3"
    connection = sqlite3.connect(database_path)
    try:
        row = connection.execute(
            "SELECT metadata_json FROM sessions WHERE session_id = ?",
            ("legacy-config-session",),
        ).fetchone()
        assert row is not None
        metadata = json.loads(str(row[0]))
        assert isinstance(metadata, dict)
        metadata_dict = cast(dict[str, object], metadata)
        metadata_dict.pop("runtime_config", None)
        _ = connection.execute(
            "UPDATE sessions SET metadata_json = ? WHERE session_id = ?",
            (json.dumps(metadata_dict, sort_keys=True), "legacy-config-session"),
        )
        connection.commit()
    finally:
        connection.close()

    resumed_runtime = VoidCodeRuntime(
        workspace=tmp_path,
        config=RuntimeConfig(approval_mode="deny", model="fresh/model", max_steps=9),
    )
    effective = resumed_runtime.effective_runtime_config(session_id="legacy-config-session")

    assert effective.approval_mode == "deny"
    assert effective.model == "fresh/model"
    assert effective.execution_engine == "deterministic"
    assert effective.max_steps == 9


def test_runtime_effective_runtime_config_uses_request_metadata_max_steps_for_new_runs(
    tmp_path: Path,
) -> None:
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        graph=_SkillCapturingStubGraph(),
        config=RuntimeConfig(max_steps=6),
    )

    response = runtime.run(RuntimeRequest(prompt="hello", metadata={"max_steps": 2}))

    assert response.session.status == "completed"
    assert response.session.metadata["runtime_config"] == {
        "approval_mode": "ask",
        "execution_engine": "deterministic",
        "max_steps": 2,
        "provider_fallback": None,
        "resolved_provider": None,
        "lsp": {"mode": "disabled", "configured_enabled": False, "servers": []},
        "acp": {
            "mode": "disabled",
            "configured_enabled": False,
            "status": "disconnected",
            "available": False,
            "last_error": None,
        },
        "mcp": {"mode": "disabled", "configured_enabled": False, "servers": []},
    }


@pytest.mark.parametrize(
    "invalid_max_steps",
    [0, -1, "4", 1.5, [], {}],
)
def test_runtime_run_rejects_invalid_request_metadata_max_steps(
    tmp_path: Path, invalid_max_steps: object
) -> None:
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        graph=_SkillCapturingStubGraph(),
        config=RuntimeConfig(max_steps=6),
    )

    with pytest.raises(ValueError, match="request metadata 'max_steps'"):
        _ = runtime.run(RuntimeRequest(prompt="hello", metadata={"max_steps": invalid_max_steps}))


def test_runtime_effective_runtime_config_falls_back_to_fresh_max_steps_for_legacy_sessions(
    tmp_path: Path,
) -> None:
    sample_file = tmp_path / "sample.txt"
    sample_file.write_text("legacy config\n", encoding="utf-8")

    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        config=RuntimeConfig(approval_mode="allow", model="session/model", max_steps=5),
    )
    _ = runtime.run(RuntimeRequest(prompt="read sample.txt", session_id="legacy-max-steps"))

    database_path = tmp_path / ".voidcode" / "sessions.sqlite3"
    connection = sqlite3.connect(database_path)
    try:
        row = connection.execute(
            "SELECT metadata_json FROM sessions WHERE session_id = ?",
            ("legacy-max-steps",),
        ).fetchone()
        assert row is not None
        metadata = json.loads(str(row[0]))
        assert isinstance(metadata, dict)
        metadata_dict = cast(dict[str, object], metadata)
        metadata_dict.pop("runtime_config", None)
        _ = connection.execute(
            "UPDATE sessions SET metadata_json = ? WHERE session_id = ?",
            (json.dumps(metadata_dict, sort_keys=True), "legacy-max-steps"),
        )
        connection.commit()
    finally:
        connection.close()

    resumed_runtime = VoidCodeRuntime(
        workspace=tmp_path,
        config=RuntimeConfig(approval_mode="deny", model="fresh/model", max_steps=9),
    )
    effective = resumed_runtime.effective_runtime_config(session_id="legacy-max-steps")

    assert effective.approval_mode == "deny"
    assert effective.model == "fresh/model"
    assert effective.execution_engine == "deterministic"
    assert effective.max_steps == 9


def test_runtime_prefers_explicit_graph_over_config_selected_execution_engine(
    tmp_path: Path,
) -> None:
    sample_file = tmp_path / "sample.txt"
    sample_file.write_text("alpha beta\n", encoding="utf-8")

    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        graph=_StubGraph(),
        config=RuntimeConfig(execution_engine="deterministic"),
    )

    response = runtime.run(RuntimeRequest(prompt="hello"))

    assert response.session.status == "completed"
    assert response.output == "hello"


def test_runtime_initializes_single_agent_graph_from_config(tmp_path: Path) -> None:
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        config=RuntimeConfig(
            execution_engine="single_agent",
            model="opencode/gpt-5.4",
        ),
    )

    graph = _private_attr(runtime, "_graph")

    assert graph.__class__.__name__ == "ProviderSingleAgentGraph"


def test_runtime_classifies_provider_context_limit_failures(tmp_path: Path) -> None:
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        graph=_FailingProviderGraph(),
        config=RuntimeConfig(execution_engine="single_agent", model="opencode/gpt-5.4"),
    )

    response = runtime.run(RuntimeRequest(prompt="read sample.txt", session_id="provider-limit"))

    assert response.session.status == "failed"
    assert response.events[-1].event_type == "runtime.failed"
    assert response.events[-1].payload == {
        "error": "provider context window exceeded",
        "kind": "provider_context_limit",
    }


def test_runtime_single_agent_compaction_emits_memory_refresh_and_persists_metadata(
    tmp_path: Path,
) -> None:
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        graph=_SkillCapturingStubGraph(),
        config=RuntimeConfig(execution_engine="single_agent", model="opencode/gpt-5.4"),
    )

    response = runtime.run(RuntimeRequest(prompt="hello", metadata={}))

    assert response.session.status == "completed"
    assert response.session.metadata["context_window"] == {
        "compacted": False,
        "compaction_reason": None,
        "original_tool_result_count": 0,
        "retained_tool_result_count": 0,
    }


def test_runtime_rejects_single_agent_engine_without_model(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="requires a configured model"):
        _ = VoidCodeRuntime(
            workspace=tmp_path,
            config=RuntimeConfig(execution_engine="single_agent"),
        )


def test_runtime_effective_runtime_config_recovers_single_agent_engine(tmp_path: Path) -> None:
    sample_file = tmp_path / "sample.txt"
    sample_file.write_text("agent config\n", encoding="utf-8")

    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        config=RuntimeConfig(
            approval_mode="allow",
            execution_engine="single_agent",
            model="opencode/gpt-5.4",
        ),
    )
    _ = runtime.run(RuntimeRequest(prompt="read sample.txt", session_id="single-agent-config"))

    resumed_runtime = VoidCodeRuntime(
        workspace=tmp_path,
        config=RuntimeConfig(approval_mode="deny", model="fresh/model"),
    )
    effective = resumed_runtime.effective_runtime_config(session_id="single-agent-config")

    assert effective.approval_mode == "allow"
    assert effective.execution_engine == "single_agent"
    assert effective.model == "opencode/gpt-5.4"


def test_runtime_effective_runtime_config_recovers_provider_fallback_chain(tmp_path: Path) -> None:
    sample_file = tmp_path / "sample.txt"
    sample_file.write_text("fallback chain\n", encoding="utf-8")
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        config=RuntimeConfig(
            approval_mode="allow",
            execution_engine="single_agent",
            model="opencode/gpt-5.4",
            provider_fallback=RuntimeProviderFallbackConfig(
                preferred_model="opencode/gpt-5.4",
                fallback_models=("opencode/gpt-5.3", "custom/demo"),
            ),
        ),
    )
    _ = runtime.run(RuntimeRequest(prompt="read sample.txt", session_id="fallback-config"))

    resumed_runtime = VoidCodeRuntime(workspace=tmp_path, config=RuntimeConfig())
    effective = resumed_runtime.effective_runtime_config(session_id="fallback-config")

    assert effective.provider_fallback == RuntimeProviderFallbackConfig(
        preferred_model="opencode/gpt-5.4",
        fallback_models=("opencode/gpt-5.3", "custom/demo"),
    )


def test_runtime_effective_runtime_config_treats_missing_persisted_provider_fallback_as_none(
    tmp_path: Path,
) -> None:
    sample_file = tmp_path / "sample.txt"
    sample_file.write_text("fallback chain\n", encoding="utf-8")
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        config=RuntimeConfig(
            approval_mode="allow",
            execution_engine="single_agent",
            model="opencode/gpt-5.4",
            provider_fallback=RuntimeProviderFallbackConfig(
                preferred_model="opencode/gpt-5.4",
                fallback_models=("opencode/gpt-5.3", "custom/demo"),
            ),
        ),
    )
    _ = runtime.run(
        RuntimeRequest(prompt="read sample.txt", session_id="fallback-config-missing-key")
    )

    database_path = tmp_path / ".voidcode" / "sessions.sqlite3"
    connection = sqlite3.connect(database_path)
    try:
        row = connection.execute(
            "SELECT metadata_json FROM sessions WHERE session_id = ?",
            ("fallback-config-missing-key",),
        ).fetchone()
        assert row is not None
        metadata = json.loads(str(row[0]))
        assert isinstance(metadata, dict)
        metadata_dict = cast(dict[str, object], metadata)
        runtime_config = cast(dict[str, object], metadata_dict["runtime_config"])
        runtime_config.pop("provider_fallback", None)
        _ = connection.execute(
            "UPDATE sessions SET metadata_json = ? WHERE session_id = ?",
            (
                json.dumps(metadata_dict, sort_keys=True),
                "fallback-config-missing-key",
            ),
        )
        connection.commit()
    finally:
        connection.close()

    resumed_runtime = VoidCodeRuntime(
        workspace=tmp_path,
        config=RuntimeConfig(
            execution_engine="single_agent",
            model="fresh/model",
            provider_fallback=RuntimeProviderFallbackConfig(
                preferred_model="fresh/model",
                fallback_models=("fresh/fallback",),
            ),
        ),
    )
    effective = resumed_runtime.effective_runtime_config(session_id="fallback-config-missing-key")

    assert effective.approval_mode == "allow"
    assert effective.execution_engine == "single_agent"
    assert effective.model == "opencode/gpt-5.4"
    assert effective.provider_fallback == RuntimeProviderFallbackConfig(
        preferred_model="opencode/gpt-5.4",
        fallback_models=("opencode/gpt-5.3", "custom/demo"),
    )


def test_runtime_persists_resolved_provider_snapshot_in_runtime_metadata(tmp_path: Path) -> None:
    sample_file = tmp_path / "sample.txt"
    sample_file.write_text("resolved provider config\n", encoding="utf-8")

    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        config=RuntimeConfig(
            approval_mode="allow",
            execution_engine="single_agent",
            model="opencode/gpt-5.4",
            provider_fallback=RuntimeProviderFallbackConfig(
                preferred_model="opencode/gpt-5.4",
                fallback_models=("custom/demo",),
            ),
        ),
    )

    response = runtime.run(RuntimeRequest(prompt="read sample.txt", session_id="resolved-provider"))

    runtime_config = cast(dict[str, object], response.session.metadata["runtime_config"])
    assert runtime_config["resolved_provider"] == {
        "active_target": {
            "raw_model": "opencode/gpt-5.4",
            "provider": "opencode",
            "model": "gpt-5.4",
        },
        "targets": [
            {
                "raw_model": "opencode/gpt-5.4",
                "provider": "opencode",
                "model": "gpt-5.4",
            },
            {
                "raw_model": "custom/demo",
                "provider": "custom",
                "model": "demo",
            },
        ],
    }


def test_runtime_effective_runtime_config_rejects_malformed_persisted_provider_fallback(
    tmp_path: Path,
) -> None:
    sample_file = tmp_path / "sample.txt"
    sample_file.write_text("bad fallback\n", encoding="utf-8")

    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        config=RuntimeConfig(
            approval_mode="allow",
            execution_engine="single_agent",
            model="opencode/gpt-5.4",
            provider_fallback=RuntimeProviderFallbackConfig(
                preferred_model="opencode/gpt-5.4",
                fallback_models=("custom/demo",),
            ),
        ),
    )
    _ = runtime.run(
        RuntimeRequest(prompt="read sample.txt", session_id="malformed-provider-fallback")
    )

    database_path = tmp_path / ".voidcode" / "sessions.sqlite3"
    connection = sqlite3.connect(database_path)
    try:
        row = connection.execute(
            "SELECT metadata_json FROM sessions WHERE session_id = ?",
            ("malformed-provider-fallback",),
        ).fetchone()
        assert row is not None
        metadata = json.loads(str(row[0]))
        assert isinstance(metadata, dict)
        metadata_dict = cast(dict[str, object], metadata)
        runtime_config = cast(dict[str, object], metadata_dict["runtime_config"])
        runtime_config["provider_fallback"] = {
            "preferred_model": "opencode/gpt-5.4",
            "fallback_models": ["custom/demo", 7],
        }
        _ = connection.execute(
            "UPDATE sessions SET metadata_json = ? WHERE session_id = ?",
            (json.dumps(metadata_dict, sort_keys=True), "malformed-provider-fallback"),
        )
        connection.commit()
    finally:
        connection.close()

    resumed_runtime = VoidCodeRuntime(workspace=tmp_path, config=RuntimeConfig())

    with pytest.raises(ValueError, match="invalid provider config"):
        _ = resumed_runtime.effective_runtime_config(session_id="malformed-provider-fallback")


def test_runtime_resume_preserves_provider_attempt_and_target_across_pending_approval(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    custom_attempts: list[int] = []
    registry = ModelProviderRegistry(
        providers={
            "opencode": _AlwaysFailingModelProvider(name="opencode", error_kind="rate_limit"),
            "custom": _ApprovalResumeFallbackModelProvider(
                name="custom",
                attempts_seen=custom_attempts,
            ),
        }
    )
    config = RuntimeConfig(
        approval_mode="ask",
        execution_engine="single_agent",
        model="opencode/gpt-5.4",
        provider_fallback=RuntimeProviderFallbackConfig(
            preferred_model="opencode/gpt-5.4",
            fallback_models=("custom/demo",),
        ),
    )
    initial_runtime = VoidCodeRuntime(
        workspace=tmp_path,
        config=config,
        permission_policy=PermissionPolicy(mode="ask"),
        model_provider_registry=registry,
    )

    with caplog.at_level(logging.INFO):
        waiting = initial_runtime.run(
            RuntimeRequest(prompt="write alpha.txt 1", session_id="resume-provider-attempt")
        )

    assert waiting.session.status == "waiting"
    assert custom_attempts == [1]
    approval_event = waiting.events[-1]
    assert approval_event.event_type == "runtime.approval_requested"
    request_id = str(approval_event.payload["request_id"])
    fallback_events = [
        event for event in waiting.events if event.event_type == "runtime.provider_fallback"
    ]
    assert len(fallback_events) == 1
    assert "provider fallback" in caplog.text

    resumed_runtime = VoidCodeRuntime(
        workspace=tmp_path,
        config=config,
        permission_policy=PermissionPolicy(mode="ask"),
        model_provider_registry=registry,
    )
    resumed = resumed_runtime.resume(
        session_id="resume-provider-attempt",
        approval_request_id=request_id,
        approval_decision="allow",
    )

    assert resumed.session.status == "completed"
    assert resumed.output == "done"
    assert custom_attempts == [1, 1, 1]
    assert all(attempt == 1 for attempt in custom_attempts)
    resumed_fallback_events = [
        event for event in resumed.events if event.event_type == "runtime.provider_fallback"
    ]
    assert len(resumed_fallback_events) == 1


def test_runtime_persists_resume_checkpoint_for_waiting_session(tmp_path: Path) -> None:
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        graph=_ApprovalThenCaptureSkillGraph(),
        config=RuntimeConfig(approval_mode="ask"),
        permission_policy=PermissionPolicy(mode="ask"),
    )

    waiting = runtime.run(RuntimeRequest(prompt="go", session_id="checkpoint-waiting-session"))

    assert waiting.session.status == "waiting"
    database_path = tmp_path / ".voidcode" / "sessions.sqlite3"
    connection = sqlite3.connect(database_path)
    try:
        row = connection.execute(
            "SELECT resume_checkpoint_json FROM sessions WHERE session_id = ?",
            ("checkpoint-waiting-session",),
        ).fetchone()
        assert row is not None
        checkpoint = json.loads(str(row[0]))
    finally:
        connection.close()

    assert checkpoint["version"] == 1
    assert checkpoint["kind"] == "approval_wait"
    assert checkpoint["pending_approval_request_id"] == str(
        waiting.events[-1].payload["request_id"]
    )
    assert checkpoint["prompt"] == "go"
    assert checkpoint["session_status"] == "waiting"
    assert checkpoint["session_metadata"] == waiting.session.metadata
    assert checkpoint["tool_results"] == []
    assert checkpoint["last_event_sequence"] == waiting.events[-1].sequence


def test_runtime_resume_approval_rebuilds_from_persisted_checkpoint_after_restart(
    tmp_path: Path,
) -> None:
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        graph=_ApprovalThenCaptureSkillGraph(),
        config=RuntimeConfig(approval_mode="ask"),
        permission_policy=PermissionPolicy(mode="ask"),
    )

    waiting = runtime.run(RuntimeRequest(prompt="go", session_id="checkpoint-resume-session"))
    approval_request_id = str(waiting.events[-1].payload["request_id"])

    database_path = tmp_path / ".voidcode" / "sessions.sqlite3"
    connection = sqlite3.connect(database_path)
    try:
        _ = connection.execute(
            "DELETE FROM session_events WHERE session_id = ?", ("checkpoint-resume-session",)
        )
        connection.commit()
    finally:
        connection.close()

    resumed_runtime = VoidCodeRuntime(
        workspace=tmp_path,
        graph=_ApprovalThenCaptureSkillGraph(),
        config=RuntimeConfig(approval_mode="ask"),
        permission_policy=PermissionPolicy(mode="ask"),
    )

    resumed = resumed_runtime.resume(
        session_id="checkpoint-resume-session",
        approval_request_id=approval_request_id,
        approval_decision="allow",
    )

    assert resumed.session.status == "completed"
    assert resumed.output == "done"


def test_runtime_resume_falls_back_when_persisted_checkpoint_is_missing(tmp_path: Path) -> None:
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        graph=_ApprovalThenCaptureSkillGraph(),
        config=RuntimeConfig(approval_mode="ask"),
        permission_policy=PermissionPolicy(mode="ask"),
    )

    waiting = runtime.run(RuntimeRequest(prompt="go", session_id="checkpoint-fallback-session"))
    approval_request_id = str(waiting.events[-1].payload["request_id"])

    database_path = tmp_path / ".voidcode" / "sessions.sqlite3"
    connection = sqlite3.connect(database_path)
    try:
        _ = connection.execute(
            "UPDATE sessions SET resume_checkpoint_json = NULL WHERE session_id = ?",
            ("checkpoint-fallback-session",),
        )
        connection.commit()
    finally:
        connection.close()

    resumed_runtime = VoidCodeRuntime(
        workspace=tmp_path,
        graph=_ApprovalThenCaptureSkillGraph(),
        config=RuntimeConfig(approval_mode="ask"),
        permission_policy=PermissionPolicy(mode="ask"),
    )

    resumed = resumed_runtime.resume(
        session_id="checkpoint-fallback-session",
        approval_request_id=approval_request_id,
        approval_decision="allow",
    )

    assert resumed.session.status == "completed"
    assert resumed.output == "done"


def test_runtime_resume_falls_back_when_persisted_checkpoint_json_is_corrupt(
    tmp_path: Path,
) -> None:
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        graph=_ApprovalThenCaptureSkillGraph(),
        config=RuntimeConfig(approval_mode="ask"),
        permission_policy=PermissionPolicy(mode="ask"),
    )

    waiting = runtime.run(RuntimeRequest(prompt="go", session_id="checkpoint-corrupt-json-session"))
    approval_request_id = str(waiting.events[-1].payload["request_id"])

    database_path = tmp_path / ".voidcode" / "sessions.sqlite3"
    connection = sqlite3.connect(database_path)
    try:
        _ = connection.execute(
            "UPDATE sessions SET resume_checkpoint_json = ? WHERE session_id = ?",
            ("{not valid json", "checkpoint-corrupt-json-session"),
        )
        connection.commit()
    finally:
        connection.close()

    resumed_runtime = VoidCodeRuntime(
        workspace=tmp_path,
        graph=_ApprovalThenCaptureSkillGraph(),
        config=RuntimeConfig(approval_mode="ask"),
        permission_policy=PermissionPolicy(mode="ask"),
    )

    resumed = resumed_runtime.resume(
        session_id="checkpoint-corrupt-json-session",
        approval_request_id=approval_request_id,
        approval_decision="allow",
    )

    assert resumed.session.status == "completed"
    assert resumed.output == "done"


def test_runtime_resume_falls_back_when_persisted_checkpoint_payload_is_not_object(
    tmp_path: Path,
) -> None:
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        graph=_ApprovalThenCaptureSkillGraph(),
        config=RuntimeConfig(approval_mode="ask"),
        permission_policy=PermissionPolicy(mode="ask"),
    )

    waiting = runtime.run(RuntimeRequest(prompt="go", session_id="checkpoint-non-object-session"))
    approval_request_id = str(waiting.events[-1].payload["request_id"])

    database_path = tmp_path / ".voidcode" / "sessions.sqlite3"
    connection = sqlite3.connect(database_path)
    try:
        _ = connection.execute(
            "UPDATE sessions SET resume_checkpoint_json = ? WHERE session_id = ?",
            (json.dumps(["not", "an", "object"]), "checkpoint-non-object-session"),
        )
        connection.commit()
    finally:
        connection.close()

    resumed_runtime = VoidCodeRuntime(
        workspace=tmp_path,
        graph=_ApprovalThenCaptureSkillGraph(),
        config=RuntimeConfig(approval_mode="ask"),
        permission_policy=PermissionPolicy(mode="ask"),
    )

    resumed = resumed_runtime.resume(
        session_id="checkpoint-non-object-session",
        approval_request_id=approval_request_id,
        approval_decision="allow",
    )

    assert resumed.session.status == "completed"
    assert resumed.output == "done"


@pytest.mark.parametrize("error_kind", ["rate_limit", "invalid_model", "transient_failure"])
def test_runtime_downgrades_to_next_provider_target_on_provider_failures(
    tmp_path: Path,
    error_kind: Literal["rate_limit", "invalid_model", "transient_failure"],
) -> None:
    registry = ModelProviderRegistry(
        providers={
            "opencode": _ScriptedModelProvider(
                name="opencode",
                outcomes=(
                    ProviderExecutionError(
                        kind=error_kind,
                        provider_name="opencode",
                        model_name="gpt-5.4",
                        message=f"{error_kind} failure",
                    ),
                ),
            ),
            "custom": _ScriptedModelProvider(
                name="custom",
                outcomes=(SingleAgentTurnResult(output="fallback complete"),),
            ),
        }
    )
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        config=RuntimeConfig(
            execution_engine="single_agent",
            model="opencode/gpt-5.4",
            provider_fallback=RuntimeProviderFallbackConfig(
                preferred_model="opencode/gpt-5.4",
                fallback_models=("custom/demo",),
            ),
        ),
        model_provider_registry=registry,
    )

    response = runtime.run(RuntimeRequest(prompt="read sample.txt"))

    assert response.session.status == "completed"
    assert response.output == "fallback complete"
    fallback_events = [
        event for event in response.events if event.event_type == "runtime.provider_fallback"
    ]
    assert len(fallback_events) == 1
    assert fallback_events[0].payload == {
        "reason": error_kind,
        "from_provider": "opencode",
        "from_model": "gpt-5.4",
        "to_provider": "custom",
        "to_model": "demo",
        "attempt": 1,
    }


def test_runtime_fails_without_downgrade_on_context_limit(tmp_path: Path) -> None:
    registry = ModelProviderRegistry(
        providers={
            "opencode": _ScriptedModelProvider(
                name="opencode",
                outcomes=(
                    ProviderExecutionError(
                        kind="context_limit",
                        provider_name="opencode",
                        model_name="gpt-5.4",
                        message="context exceeded",
                    ),
                ),
            )
        }
    )
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        config=RuntimeConfig(
            execution_engine="single_agent",
            model="opencode/gpt-5.4",
            provider_fallback=RuntimeProviderFallbackConfig(
                preferred_model="opencode/gpt-5.4",
                fallback_models=("custom/demo",),
            ),
        ),
        model_provider_registry=registry,
    )

    response = runtime.run(RuntimeRequest(prompt="read sample.txt"))

    assert response.session.status == "failed"
    assert response.events[-1].event_type == "runtime.failed"
    assert response.events[-1].payload == {
        "error": "context exceeded",
        "provider_error_kind": "context_limit",
        "provider": "opencode",
        "model": "gpt-5.4",
    }


def test_runtime_rejects_malformed_model_reference_during_initialization(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="provider/model"):
        _ = VoidCodeRuntime(
            workspace=tmp_path,
            config=RuntimeConfig(model="invalid-model"),
        )
