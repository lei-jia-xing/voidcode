"""Contract tests for ``voidcode config`` (show / schema / init) and secret non-disclosure.

``config show`` is the only machine-readable view of the config the runtime would use, so it pins
the resolved workspace/session model, provider readiness, context budget and MCP state.  The
``config schema`` / ``config init`` pair is the machine-readable onboarding surface.  Because the
same payload is built from live provider config, these tests also pin that a provider credential
taken from the environment is consumed but never printed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ._cli_harness import CliRun, run_cli

# Process exit codes owned by ``voidcode.cli_support``, pinned here as the user-visible contract.
EXIT_USAGE_ERROR = 2
EXIT_CONFIG_ERROR = 10
EXIT_RUNTIME_ERROR = 12
EXIT_INVALID_RESOURCE = 16

SESSION_ID = "config-session"
SENTINEL_CREDENTIAL = "sk-sentinel-value"
SENTINEL_ENV = {"OPENAI_API_KEY": SENTINEL_CREDENTIAL}

pytestmark = pytest.mark.usefixtures("_force_deterministic_engine_default")


@pytest.fixture
def _force_deterministic_engine_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOIDCODE_EXECUTION_ENGINE", "deterministic")


def write_config(workspace: Path, payload: dict[str, object]) -> Path:
    path = workspace / ".voidcode.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def seed_session(workspace: Path, *, session_id: str = SESSION_ID) -> None:
    """Run one deterministic turn so ``config show --session`` has a snapshot to resolve."""
    (workspace / "sample.txt").write_text("session config\n", encoding="utf-8")
    result = run_cli(
        "run",
        "read sample.txt",
        "--workspace",
        str(workspace),
        "--session-id",
        session_id,
        "--approval-mode",
        "yolo",
        cwd=workspace,
    )
    assert result.returncode == 0, result.stderr


def assert_clean_error(result: CliRun, expected_exit: int) -> str:
    """A CLI error owns stderr as a single ``error:`` line, leaves stdout empty and never traces back."""
    assert result.returncode == expected_exit
    assert result.stdout == ""
    lines = result.stderr.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("error: ")
    assert "Traceback" not in result.stderr
    return lines[0]


@pytest.fixture(scope="module")
def sentinel_workspace(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A workspace whose provider credential is read from ``OPENAI_API_KEY``."""
    workspace = tmp_path_factory.mktemp("config-sentinel")
    write_config(
        workspace,
        {
            "model": "opencode-zen/sentinel-model",
            "providers": {
                "opencode-zen": {
                    "api_key_env_var": "OPENAI_API_KEY",
                    "base_url": "https://sentinel.invalid/v1",
                }
            },
        },
    )
    seed_session(workspace)
    return workspace


def test_config_show_reports_effective_workspace_config(tmp_path: Path) -> None:
    write_config(
        tmp_path,
        {
            "approval_mode": "yolo",
            "model": "deepseek/deepseek-v4-flash",
            "reasoning_effort": "high",
            "agent": {"preset": "leader"},
        },
    )

    result = run_cli("config", "show", "--workspace", str(tmp_path), cwd=tmp_path)

    assert result.returncode == 0
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert {
        "workspace",
        "session_id",
        "approval_mode",
        "execution_engine",
        "model",
        "agent",
        "agents",
        "provider_readiness",
        "context_budget",
        "mcp",
    } <= payload.keys()
    assert payload["workspace"] == str(tmp_path)
    assert payload["session_id"] is None
    assert payload["approval_mode"] == "yolo"
    assert payload["execution_engine"] == "deterministic"
    assert payload["model"] == "deepseek/deepseek-v4-flash"
    assert payload["reasoning_effort"] == "high"
    assert payload["agent"] == {"preset": "leader", "prompt_profile": "leader"}
    assert payload["provider_readiness"]["provider"] == "deepseek"
    assert payload["provider_readiness"]["model"] == "deepseek-v4-flash"
    assert payload["agents"]["leader"]["effective_model"] == "deepseek/deepseek-v4-flash"
    assert payload["context_budget"]["context_window"] == payload["provider_readiness"]["context_window"]
    assert payload["context_budget"]["max_output_tokens"] == payload["provider_readiness"]["max_output_tokens"]
    assert payload["mcp"]["state"] == "stopped"
    assert payload["mcp"]["details"]["running_server_count"] == 0


def test_config_show_reports_defaults_for_unconfigured_workspace(tmp_path: Path) -> None:
    result = run_cli("config", "show", "--workspace", str(tmp_path), cwd=tmp_path)

    assert result.returncode == 0
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["workspace"] == str(tmp_path)
    assert payload["approval_mode"] == "yolo"
    assert payload["model"] is None
    assert payload["agent"] is None
    assert payload["provider_readiness"]["provider"] is None
    assert payload["provider_readiness"]["status"] == "missing_model"
    assert payload["provider_readiness"]["ok"] is False
    assert payload["context_budget"] == {"context_window": None, "max_output_tokens": None}


def test_config_show_rejects_missing_workspace(tmp_path: Path) -> None:
    missing = tmp_path / "missing-workspace"

    result = run_cli("config", "show", "--workspace", str(missing), cwd=tmp_path)

    message = assert_clean_error(result, EXIT_INVALID_RESOURCE)
    assert str(missing) in message


def test_config_schema_emits_json_schema(tmp_path: Path) -> None:
    result = run_cli("config", "schema", cwd=tmp_path)

    assert result.returncode == 0
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["type"] == "object"
    assert isinstance(payload["$id"], str)
    assert payload["$id"].endswith(".json")
    assert {"model", "approval_mode", "agents", "mcp", "providers"} <= payload["properties"].keys()
    # The shipped schema is generated from the payload models: an optional value
    # publishes its enum inside the non-null branch of the null union.
    assert payload["properties"]["approval_mode"]["anyOf"] == [
        {"type": "string", "enum": ["ask", "write", "yolo"]},
        {"type": "null"},
    ]


def test_config_init_writes_starter_config_and_refuses_overwrite(tmp_path: Path) -> None:
    config_path = tmp_path / ".voidcode.json"

    first = run_cli("config", "init", "--workspace", str(tmp_path), cwd=tmp_path)
    written = config_path.read_text(encoding="utf-8")
    second = run_cli("config", "init", "--workspace", str(tmp_path), cwd=tmp_path)

    assert first.returncode == 0
    assert first.stderr == ""
    payload = json.loads(first.stdout)
    assert payload["workspace"] == str(tmp_path)
    assert payload["config_path"] == str(config_path)
    assert payload["next_command"].startswith("voidcode doctor --workspace")
    assert payload["first_task_command"].startswith("voidcode run")
    assert str(tmp_path) in payload["next_command"]
    assert json.loads(written)["$schema"]
    message = assert_clean_error(second, EXIT_CONFIG_ERROR)
    assert "already exists" in message


def test_config_init_rejects_malformed_model_without_writing(tmp_path: Path) -> None:
    result = run_cli("config", "init", "--workspace", str(tmp_path), "--model", "not-a-model", cwd=tmp_path)

    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr.startswith("error: ")
    assert "model" in result.stderr
    assert "Traceback" not in result.stderr
    assert not (tmp_path / ".voidcode.json").exists()


def test_config_show_marks_env_credential_provider_ready_without_printing_it(sentinel_workspace: Path) -> None:
    result = run_cli("config", "show", "--workspace", str(sentinel_workspace), cwd=sentinel_workspace, env=dict(SENTINEL_ENV))

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    # The sentinel is a real credential for the configured provider, so readiness must see it.
    assert payload["provider_readiness"]["provider"] == "opencode-zen"
    assert payload["provider_readiness"]["auth_present"] is True
    assert SENTINEL_CREDENTIAL not in result.stdout
    assert SENTINEL_CREDENTIAL not in result.stderr


@pytest.mark.parametrize(
    ("args", "required_keys"),
    [
        pytest.param(("config", "show"), ("workspace", "provider_readiness"), id="config-show"),
        pytest.param(("config", "show", "--session", SESSION_ID), ("workspace", "session_id"), id="config-show-session"),
        pytest.param(("agents", "list", "--json"), ("workspace", "agents"), id="agents-list"),
        pytest.param(("mcp", "list", "--json"), ("workspace", "mcp"), id="mcp-list"),
        pytest.param(("doctor", "--json"), ("workspace", "results", "summary"), id="doctor"),
    ],
)
def test_commands_never_print_env_credential(sentinel_workspace: Path, args: tuple[str, ...], required_keys: tuple[str, ...]) -> None:
    result = run_cli(*args, "--workspace", str(sentinel_workspace), cwd=sentinel_workspace, env=dict(SENTINEL_ENV))

    assert SENTINEL_CREDENTIAL not in result.stdout
    assert SENTINEL_CREDENTIAL not in result.stderr
    assert "Traceback" not in result.stderr
    assert set(required_keys) <= json.loads(result.stdout).keys()
