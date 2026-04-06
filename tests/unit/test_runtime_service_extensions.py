from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from voidcode.runtime.acp import DisabledAcpAdapter
from voidcode.runtime.config import (
    RuntimeAcpConfig,
    RuntimeConfig,
    RuntimeLspConfig,
    RuntimeSkillsConfig,
)
from voidcode.runtime.lsp import DisabledLspManager
from voidcode.runtime.service import (
    GraphRunRequest,
    GraphRunResult,
    RuntimeRequest,
    SessionState,
    VoidCodeRuntime,
)
from voidcode.runtime.skills import SkillRegistry
from voidcode.tools import ToolCall


def _private_attr(instance: object, name: str) -> Any:
    return getattr(instance, name)


@dataclass(slots=True)
class _StubPlan:
    tool_call: ToolCall


class _StubGraph:
    def plan(self, request: GraphRunRequest) -> _StubPlan:
        _ = request
        return _StubPlan(ToolCall(tool_name="read_file", arguments={"path": "sample.txt"}))

    def finalize(
        self,
        request: GraphRunRequest,
        tool_result: object,
        *,
        session: SessionState,
    ) -> GraphRunResult:
        _ = tool_result
        return GraphRunResult(session=session, output=request.prompt)


def test_runtime_initializes_empty_extension_state_by_default(tmp_path: Path) -> None:
    runtime = VoidCodeRuntime(workspace=tmp_path, config=RuntimeConfig())
    skill_registry = _private_attr(runtime, "_skill_registry")
    lsp_manager = _private_attr(runtime, "_lsp_manager")
    acp_adapter = _private_attr(runtime, "_acp_adapter")

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
            skills=RuntimeSkillsConfig(enabled=True),
            lsp=RuntimeLspConfig(
                enabled=True,
                servers={"pyright": {"command": ["pyright-langserver", "--stdio"]}},
            ),
            acp=RuntimeAcpConfig(enabled=True),
        ),
    )

    skill_registry = _private_attr(runtime, "_skill_registry")
    lsp_manager = _private_attr(runtime, "_lsp_manager")
    acp_adapter = _private_attr(runtime, "_acp_adapter")
    skill = skill_registry.resolve("demo")
    lsp_state = lsp_manager.current_state()
    acp_state = acp_adapter.current_state()

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
    assert response.events[3].event_type == "runtime.tool_lookup_succeeded"
    assert response.events[5].event_type == "runtime.tool_completed"
