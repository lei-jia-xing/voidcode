from __future__ import annotations

import json
from pathlib import Path

import pytest

from voidcode.runtime.config import RuntimeMcpConfig, RuntimeMcpServerConfig, load_runtime_config


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
