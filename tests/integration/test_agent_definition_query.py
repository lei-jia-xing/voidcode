"""Named-agent query contract over a real core: shared provider, substitutes, absence."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from voidcode.bootstrap import open_core
from voidcode.core.engine import Core

_DELEGATION_PLUGIN_ROOT = Path(__file__).resolve().parents[2] / "src" / "voidcode" / "plugins" / "delegation"

_PROBE = """
from voidcode.capabilities.agent_queries import AGENT_DEFINITIONS, AgentDefinitionRequest
from voidcode.sdk.commands import CommandDefinition, CommandResult
from voidcode.sdk.plugin import Plugin
from voidcode.sdk.services import QueryUnavailable


class Probe:
    definition = CommandDefinition(
        name="agent.probe",
        description="probe",
        argument_schema={"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"], "additionalProperties": False},
    )

    def invoke(self, invocation, services):
        try:
            agent = AGENT_DEFINITIONS.ask(services, AgentDefinitionRequest(name=str(invocation.arguments["name"])))
        except QueryUnavailable as exc:
            return CommandResult(status="error", error=f"agent_unavailable: {exc}")
        if agent is None:
            return CommandResult(status="ok", output="unknown", data={"name": None})
        return CommandResult(status="ok", output=agent.name, data=agent.as_payload())


def create_plugin(config=None):
    return Plugin(commands=(Probe(),))
"""

_SUBSTITUTE = """
from voidcode.capabilities.agent_queries import AGENT_DEFINITIONS, AgentDefinition
from voidcode.sdk.plugin import Plugin


def create_plugin(config=None):
    def resolve(request, caller):
        if request.name != "helper":
            return None
        return AgentDefinition(name="helper", description="Substitute", prompt="Substitute.", readonly=True)

    return Plugin(queries=(AGENT_DEFINITIONS.bind(resolve),))
"""

_CREATED_CORES: list[Core] = []


@pytest.fixture(autouse=True)
def _close_created_cores() -> Iterator[None]:
    yield
    while _CREATED_CORES:
        _CREATED_CORES.pop().close()


def _core(tmp_path: Path, *, delegation: bool, substitute: bool) -> Core:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    probe = tmp_path / "probe"
    probe.mkdir(parents=True, exist_ok=True)
    (probe / "__init__.py").write_text(_PROBE, encoding="utf-8")
    plugins: dict[str, object] = {"files": {"enabled": False}, "probe": {"path": str(probe)}}
    if delegation:
        plugins["delegation"] = {"path": str(_DELEGATION_PLUGIN_ROOT)}
    else:
        plugins["delegation"] = {"enabled": False}
    if substitute:
        replacement = tmp_path / "replacement"
        replacement.mkdir(parents=True, exist_ok=True)
        (replacement / "__init__.py").write_text(_SUBSTITUTE, encoding="utf-8")
        plugins["replacement"] = {"path": str(replacement)}
    (workspace / "voidcode.json").write_text(
        json.dumps({"schema_version": 1, "policy": {"approval_mode": "ask"}, "plugins": plugins}), encoding="utf-8"
    )
    core = open_core(workspace, state_dir=tmp_path / "state")
    _CREATED_CORES.append(core)
    return core


def _probe(core: Core, session_id: str, name: str):
    return core.invoke_command(session_id, "agent.probe", {"name": name}, expected_revision=core.command_catalog().revision_id)


def test_delegation_answers_effective_named_definition(tmp_path: Path) -> None:
    project_dir = tmp_path / "workspace" / ".claude" / "agents"
    project_dir.mkdir(parents=True)
    (project_dir / "helper.md").write_text(
        "---\nname: helper\ndescription: Local helper\ntools: read, grep\nmaxTurns: 3\n---\n\nHelp locally.\n",
        encoding="utf-8",
    )
    core = _core(tmp_path, delegation=True, substitute=False)
    session_id = core.create_session().session_id
    helper = _probe(core, session_id, "helper")
    assert helper.status == "ok" and helper.data is not None
    assert helper.data["tools"] == ("read", "grep")
    assert helper.data["max_turns"] == 3
    assert helper.data["source_scope"] == "project"
    unknown = _probe(core, session_id, "missing")
    assert unknown.status == "ok" and unknown.output == "unknown"


def test_any_admitted_provider_can_answer_and_absence_is_visible(tmp_path: Path) -> None:
    core = _core(tmp_path, delegation=False, substitute=True)
    session_id = core.create_session().session_id
    answered = _probe(core, session_id, "helper")
    assert answered.status == "ok" and answered.data is not None
    assert answered.data["readonly"] is True and answered.data["description"] == "Substitute"

    core = _core(tmp_path / "absent", delegation=False, substitute=False)
    session_id = core.create_session().session_id
    absent = _probe(core, session_id, "helper")
    assert absent.status == "error" and absent.error is not None
    assert absent.error.startswith("agent_unavailable:")
