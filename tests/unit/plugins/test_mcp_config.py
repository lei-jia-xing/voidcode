"""Configuration mistakes must fail before opening a server connection."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from voidcode.plugins.mcp import _mcp_config


@pytest.mark.parametrize(
    "config",
    [
        {"unknown": True},
        {"enabled": "false"},
        {"request_timeout_seconds": 0},
        {"request_timeout_seconds": float("inf")},
        {"servers": {"demo": {"transport": "other"}}},
        {"servers": {"demo": {"command": ["python", 7]}}},
        {"servers": {"demo": {"command": []}}},
        {"servers": {"demo": {"command": ["python"], "env": {"TOKEN": 7}}}},
        {"servers": {"demo": {"command": ["python"], "unknown": True}}},
        {"servers": {"demo": {"transport": "remote-http", "url": "relative"}}},
        {"servers": {"demo": {"transport": "remote-http", "url": "http://localhost", "command": ["python"]}}},
        {"servers": {" ": {"command": ["python"]}}},
    ],
)
def test_invalid_configuration_is_not_silently_discarded(config: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _mcp_config(config)


def test_reserved_provider_configuration_is_ignored() -> None:
    config = _mcp_config({"providers": {"endpoint": {"api_key": "private"}}, "servers": {"demo": {"command": ["python", "server.py"]}}})
    assert config.servers["demo"].command == ["python", "server.py"]
    assert "private" not in repr(config)


def test_remote_configuration_and_disabled_default() -> None:
    config = _mcp_config({"enabled": False, "servers": {"demo": {"transport": "remote-http", "url": "http://127.0.0.1:8000/mcp"}}})
    assert config.enabled is False
    assert config.servers["demo"].url == "http://127.0.0.1:8000/mcp"
    assert _mcp_config(None).servers == {}
