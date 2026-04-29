from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from voidcode.runtime import config as runtime_config
from voidcode.runtime.config import RuntimeMcpConfig, RuntimeMcpServerConfig, load_runtime_config

_parse_mcp_config = runtime_config.__dict__["_parse_mcp_config"]


def test_runtime_config_parses_mcp_stdio_servers(tmp_path: Path) -> None:
    (tmp_path / ".voidcode.json").write_text(
        json.dumps(
            {
                "mcp": {
                    "enabled": True,
                    "servers": {
                        "echo": {
                            "transport": "stdio",
                            "command": ["python", "tests/fixtures/echo_mcp.py"],
                            "env": {"ECHO_MODE": "1"},
                            "scope": "session",
                        }
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    config = load_runtime_config(tmp_path, env={})

    assert config.mcp == RuntimeMcpConfig(
        enabled=True,
        servers={
            "echo": RuntimeMcpServerConfig(
                transport="stdio",
                command=("python", "tests/fixtures/echo_mcp.py"),
                env={"ECHO_MODE": "1"},
                scope="session",
            )
        },
    )


def test_runtime_config_rejects_unknown_mcp_transport(tmp_path: Path) -> None:
    (tmp_path / ".voidcode.json").write_text(
        json.dumps(
            {
                "mcp": {
                    "enabled": True,
                    "servers": {
                        "echo": {
                            "transport": "sse",
                            "command": ["python", "tests/fixtures/echo_mcp.py"],
                        }
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="mcp.servers.echo.transport"):
        load_runtime_config(tmp_path, env={})


def test_runtime_config_rejects_missing_mcp_command(tmp_path: Path) -> None:
    (tmp_path / ".voidcode.json").write_text(
        json.dumps(
            {
                "mcp": {
                    "enabled": True,
                    "servers": {
                        "echo": {
                            "transport": "stdio",
                        }
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="mcp.servers.echo.command"):
        load_runtime_config(tmp_path, env={})


def test_parse_mcp_config_defaults_transport_and_preserves_public_dataclasses() -> None:
    assert _parse_mcp_config(
        {
            "enabled": False,
            "request_timeout_seconds": 3.5,
            "servers": {
                "echo": {
                    "command": ["python", "tests/fixtures/echo_mcp.py"],
                    "env": {"ECHO_MODE": "1"},
                }
            },
        }
    ) == RuntimeMcpConfig(
        enabled=False,
        request_timeout_seconds=3.5,
        servers={
            "echo": RuntimeMcpServerConfig(
                transport="stdio",
                command=("python", "tests/fixtures/echo_mcp.py"),
                env={"ECHO_MODE": "1"},
            )
        },
    )


@pytest.mark.parametrize(
    ("raw_value", "match"),
    [
        pytest.param([], "runtime config field 'mcp'", id="mcp-shape"),
        pytest.param({"enabled": "yes"}, "runtime config field 'mcp.enabled'", id="enabled-type"),
        pytest.param({"servers": []}, "runtime config field 'mcp.servers'", id="servers-shape"),
        pytest.param(
            {"servers": {"echo": []}}, "runtime config field 'mcp.servers.echo'", id="server-shape"
        ),
        pytest.param(
            {"servers": {"echo": {"command": [False]}}},
            "runtime config field 'mcp.servers.echo.command\\[0\\]'",
            id="command-item-type",
        ),
        pytest.param(
            {"servers": {"echo": {"command": ["python"], "env": []}}},
            "runtime config field 'mcp.servers.echo.env'",
            id="env-shape",
        ),
        pytest.param(
            {"servers": {"echo": {"command": ["python"], "env": {"ECHO_MODE": False}}}},
            "runtime config field 'mcp.servers.echo.env.ECHO_MODE'",
            id="env-item-type",
        ),
        pytest.param(
            {"servers": {"echo": {"command": ["python"], "env": {1: "enabled"}}}},
            "runtime config field 'mcp.servers.echo.env' keys must be strings",
            id="env-key-type",
        ),
        pytest.param(
            {"servers": {"echo": {"command": ["python"], "scope": "workspace"}}},
            "runtime config field 'mcp.servers.echo.scope' must be one of: runtime, session",
            id="scope-value",
        ),
        pytest.param(
            {"request_timeout_seconds": 0},
            "runtime config field 'mcp.request_timeout_seconds' must be greater than 0",
            id="request-timeout-positive",
        ),
        pytest.param(
            {"request_timeout_seconds": math.nan},
            "runtime config field 'mcp.request_timeout_seconds' must be a finite number",
            id="request-timeout-nan",
        ),
        pytest.param(
            {"request_timeout_seconds": math.inf},
            "runtime config field 'mcp.request_timeout_seconds' must be a finite number",
            id="request-timeout-inf",
        ),
        pytest.param(
            {"request_timeout_seconds": -math.inf},
            "runtime config field 'mcp.request_timeout_seconds' must be a finite number",
            id="request-timeout-neg-inf",
        ),
    ],
)
def test_parse_mcp_config_rejects_invalid_shapes_and_values(raw_value: object, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        _ = _parse_mcp_config(raw_value)
