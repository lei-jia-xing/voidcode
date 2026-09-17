"""Contract tests for the CLI discovery surfaces: ``agents list``, ``mcp list`` and ``commands``.

These commands are the machine-readable inventory a user (or editor integration) reads to learn
which agents, MCP servers and slash commands a workspace exposes.  The tests pin the JSON shape,
builtin-vs-project provenance, and the redaction of MCP command credentials.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ._cli_harness import run_cli

# Exit code owned by ``voidcode.cli_support``, pinned here as the user-visible contract.
EXIT_INVALID_COMMAND = 15

pytestmark = pytest.mark.usefixtures("_force_deterministic_engine_default")


@pytest.fixture
def _force_deterministic_engine_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOIDCODE_EXECUTION_ENGINE", "deterministic")


def write_config(workspace: Path, payload: dict[str, object]) -> Path:
    path = workspace / ".voidcode.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def write_project_agent(workspace: Path) -> Path:
    manifest = workspace / ".voidcode" / "agents" / "local-planner.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "---\nid: local-planner\nname: Local Planner\ndescription: Plans locally\nmode: primary\n---\nPlan from a local prompt.\n",
        encoding="utf-8",
    )
    return manifest


def write_project_command(workspace: Path, name: str = "review") -> Path:
    command_file = workspace / ".voidcode" / "commands" / f"{name}.md"
    command_file.parent.mkdir(parents=True, exist_ok=True)
    command_file.write_text(
        "---\ndescription: Project review override\nagent: reviewer\n---\nReview locally: $ARGUMENTS\n",
        encoding="utf-8",
    )
    return command_file


def test_agents_list_reports_builtin_and_project_sources(tmp_path: Path) -> None:
    manifest = write_project_agent(tmp_path)

    result = run_cli("agents", "list", "--workspace", str(tmp_path), "--json", cwd=tmp_path)

    assert result.returncode == 0
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["workspace"] == str(tmp_path)
    agents = {agent["id"]: agent for agent in payload["agents"]}
    assert {"leader", "local-planner"} <= agents.keys()
    for agent in payload["agents"]:
        assert {"id", "label", "mode", "selectable", "configured"} <= agent.keys()
    assert agents["leader"]["source_scope"] == "builtin"
    assert agents["leader"]["configured"] is False
    assert agents["leader"]["mode"] == "primary"
    assert agents["local-planner"]["source_scope"] == "project"
    assert agents["local-planner"]["source_path"] == str(manifest)
    assert agents["local-planner"]["label"] == "Local Planner"
    assert agents["local-planner"]["mode"] == "primary"


def test_agents_list_reports_configured_agent_model(tmp_path: Path) -> None:
    write_config(
        tmp_path,
        {
            "model": "deepseek/deepseek-v4-flash",
            "agents": {"leader": {"model": "deepseek/deepseek-v4-flash"}},
        },
    )

    result = run_cli("agents", "list", "--workspace", str(tmp_path), "--json", cwd=tmp_path)

    assert result.returncode == 0
    leader = next(agent for agent in json.loads(result.stdout)["agents"] if agent["id"] == "leader")
    assert leader["configured"] is True
    assert leader["model"] == "deepseek/deepseek-v4-flash"
    assert leader["model_source"] == "configured"
    assert leader["provider"] == "deepseek"


def test_mcp_list_reports_passive_status_and_redacts_command_credentials(tmp_path: Path) -> None:
    write_config(
        tmp_path,
        {
            "mcp": {
                "enabled": False,
                "servers": {
                    "context7": {
                        "command": ["context7", "--api-key", "sk-mcp-secret"],
                        "scope": "runtime",
                    }
                },
            }
        },
    )

    result = run_cli("mcp", "list", "--workspace", str(tmp_path), "--json", cwd=tmp_path)

    assert result.returncode == 0
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["workspace"] == str(tmp_path)
    assert {"state", "details"} <= payload["mcp"].keys()
    assert payload["mcp"]["state"] == "unconfigured"
    assert payload["mcp"]["details"]["configured"] is True
    assert payload["mcp"]["details"]["configured_enabled"] is False
    server = payload["mcp"]["details"]["servers"][0]
    assert {"server", "scope", "status", "transport", "command"} <= server.keys()
    assert server["server"] == "context7"
    assert server["scope"] == "runtime"
    assert server["status"] == "disabled"
    assert server["command"] == ["context7", "--api-key", "<redacted>"]
    assert "sk-mcp-secret" not in result.stdout


def test_mcp_list_reports_default_server_passive_status(tmp_path: Path) -> None:
    result = run_cli("mcp", "list", "--workspace", str(tmp_path), "--json", cwd=tmp_path)

    assert result.returncode == 0
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["mcp"]["state"] == "stopped"
    details = payload["mcp"]["details"]
    assert details["configured"] is True
    assert details["running_server_count"] == 0
    assert details["failed_server_count"] == 0
    assert details["servers"]
    for server in details["servers"]:
        assert {"server", "status", "transport", "command"} <= server.keys()
        assert server["status"] == "stopped"


def test_commands_list_reports_builtin_and_project_commands(tmp_path: Path) -> None:
    command_file = write_project_command(tmp_path)

    result = run_cli("commands", "list", "--workspace", str(tmp_path), "--json", cwd=tmp_path)

    assert result.returncode == 0
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["workspace"] == str(tmp_path)
    commands = {command["name"]: command for command in payload["commands"]}
    assert {"init", "plan"} <= commands.keys()
    assert commands["init"]["source"] == "builtin"
    assert commands["plan"]["source"] == "builtin"
    assert commands["plan"]["path"] is None
    assert commands["review"]["source"] == "project"
    assert commands["review"]["path"] == str(command_file)
    assert commands["review"]["description"] == "Project review override"
    assert commands["review"]["agent"] == "reviewer"


def test_commands_show_reports_command_definition(tmp_path: Path) -> None:
    write_project_command(tmp_path)

    result = run_cli("commands", "show", "review", "--workspace", str(tmp_path), "--json", cwd=tmp_path)

    assert result.returncode == 0
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["name"] == "review"
    assert payload["source"] == "project"
    assert payload["description"] == "Project review override"
    assert payload["agent"] == "reviewer"
    assert payload["template"] == "Review locally: $ARGUMENTS\n"
    assert payload["enabled"] is True


def test_commands_show_unknown_command_is_a_clean_invalid_command_error(tmp_path: Path) -> None:
    result = run_cli("commands", "show", "/missing", "--workspace", str(tmp_path), cwd=tmp_path)

    assert result.returncode == EXIT_INVALID_COMMAND
    assert result.stdout == ""
    assert result.stderr == "error: unknown command: /missing\n"
