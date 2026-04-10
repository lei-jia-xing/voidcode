from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest

from voidcode.runtime.acp import DisabledAcpAdapter
from voidcode.runtime.config import (
    RuntimeAcpConfig,
    RuntimeConfig,
    RuntimeLspConfig,
    RuntimeSkillsConfig,
)
from voidcode.runtime.events import EventEnvelope
from voidcode.runtime.lsp import DisabledLspManager
from voidcode.runtime.permission import PermissionPolicy
from voidcode.runtime.service import (
    GraphRunRequest,
    RuntimeRequest,
    SessionState,
    VoidCodeRuntime,
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
                servers={"pyright": {"command": ["pyright-langserver", "--stdio"]}},
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
    assert lsp_state.mode == "disabled"
    assert lsp_state.configuration.configured_enabled is True
    assert tuple(lsp_state.servers) == ("pyright",)
    assert lsp_state.servers["pyright"].available is False
    assert acp_state.mode == "disabled"
    assert acp_state.configuration.configured_enabled is True
    assert acp_state.configured is True


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
                servers={"pyright": {"command": ["pyright-langserver", "--stdio"]}},
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
        config=RuntimeConfig(approval_mode="deny", model="fresh/model"),
    )
    effective = resumed_runtime.effective_runtime_config(session_id="config-session")

    assert effective.approval_mode == "allow"
    assert effective.model == "session/model"
    assert effective.execution_engine == "deterministic"


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
        config=RuntimeConfig(approval_mode="deny", model="fresh/model"),
    )
    effective = resumed_runtime.effective_runtime_config(session_id="legacy-config-session")

    assert effective.approval_mode == "deny"
    assert effective.model == "fresh/model"
    assert effective.execution_engine == "deterministic"


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


def test_runtime_rejects_malformed_model_reference_during_initialization(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="provider/model"):
        _ = VoidCodeRuntime(
            workspace=tmp_path,
            config=RuntimeConfig(model="invalid-model"),
        )
