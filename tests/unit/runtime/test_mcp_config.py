from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from voidcode.mcp import get_builtin_mcp_descriptor, list_builtin_mcp_descriptors
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


def test_runtime_config_preserves_stdio_for_builtin_command_server(tmp_path: Path) -> None:
    (tmp_path / ".voidcode.json").write_text(
        json.dumps(
            {
                "mcp": {
                    "enabled": False,
                    "servers": {
                        "context7": {
                            "command": ["context7", "--api-key", "secret-token"],
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
        enabled=False,
        servers={
            "context7": RuntimeMcpServerConfig(
                transport="stdio",
                command=("context7", "--api-key", "secret-token"),
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


def test_builtin_mcp_descriptors_model_issue_405_capabilities() -> None:
    descriptors = {descriptor.name: descriptor for descriptor in list_builtin_mcp_descriptors()}

    assert set(descriptors) == {"context7", "websearch", "grep_app", "playwright"}
    assert descriptors["context7"].transport == "remote-http"
    assert descriptors["websearch"].transport == "remote-http"
    assert descriptors["grep_app"].transport == "remote-http"
    assert descriptors["grep_app"].url == "https://mcp.grep.app"
    assert descriptors["grep_app"].command == ()
    assert descriptors["grep_app"].lifecycle == "descriptor_only_config_gated"
    grep_app_payload = descriptors["grep_app"].to_payload()
    assert grep_app_payload["url"] == "https://mcp.grep.app"
    assert "command" not in grep_app_payload
    playwright = get_builtin_mcp_descriptor("playwright")
    assert playwright is not None
    assert playwright.transport == "stdio"
    assert playwright.command == ("npx", "@playwright/mcp@latest")
    assert playwright.skill_scoped is True
    assert playwright.skill_name == "playwright"


def test_builtin_grep_app_descriptor_has_remote_http_endpoint() -> None:
    descriptor = get_builtin_mcp_descriptor("grep_app")

    assert descriptor is not None
    assert descriptor.name == "grep_app"
    assert descriptor.transport == "remote-http"
    assert descriptor.url == "https://mcp.grep.app"

    config = RuntimeMcpConfig(enabled=True)

    assert config.servers is None


def test_runtime_config_parses_mcp_remote_http_servers(tmp_path: Path) -> None:
    (tmp_path / ".voidcode.json").write_text(
        json.dumps(
            {
                "mcp": {
                    "enabled": True,
                    "servers": {
                        "grep_app": {
                            "transport": "remote-http",
                            "url": "https://mcp.grep.app",
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
            "grep_app": RuntimeMcpServerConfig(
                transport="remote-http",
                url="https://mcp.grep.app",
            )
        },
    )


def test_runtime_config_expands_builtin_mcp_server_shorthand(tmp_path: Path) -> None:
    (tmp_path / ".voidcode.json").write_text(
        json.dumps(
            {
                "mcp": {
                    "enabled": True,
                    "servers": {
                        "grep_app": {},
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
            "grep_app": RuntimeMcpServerConfig(
                transport="remote-http",
                url="https://mcp.grep.app",
            )
        },
    )


def test_runtime_config_derives_default_grep_app_mcp_when_unconfigured(tmp_path: Path) -> None:
    config = load_runtime_config(tmp_path, env={})

    assert config.mcp == RuntimeMcpConfig(
        enabled=True,
        servers={
            "grep_app": RuntimeMcpServerConfig(
                transport="remote-http",
                url="https://mcp.grep.app",
            )
        },
    )


def test_runtime_config_rejects_missing_mcp_url_for_remote_http(tmp_path: Path) -> None:
    (tmp_path / ".voidcode.json").write_text(
        json.dumps(
            {
                "mcp": {
                    "enabled": True,
                    "servers": {
                        "custom_remote": {
                            "transport": "remote-http",
                        }
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="mcp.servers.custom_remote"):
        load_runtime_config(tmp_path, env={})
