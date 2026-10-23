"""LSP configuration is strict before server resolution or process startup."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from voidcode.plugins.lsp.lsp_types import lsp_config_from_mapping
from voidcode.plugins.lsp.registry import resolve_lsp_server_configs


@pytest.mark.parametrize(
    "config",
    [
        {"enabled": "false"},
        {"unknown": True},
        {"servers": []},
        {"servers": {"demo": {"command": ["server", 3]}}},
        {"servers": {"demo": {"extensions": ".py"}}},
        {"servers": {"demo": {"extensions": [".py", False]}}},
        {"servers": {"demo": {"settings": []}}},
        {"servers": {"demo": {"unknown": True}}},
        {"servers": {"": {"command": ["server"]}}},
    ],
)
def test_invalid_configuration_is_not_silently_discarded(config: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        lsp_config_from_mapping(config)


def test_valid_config_is_resolved_without_losing_arguments_or_settings() -> None:
    config = lsp_config_from_mapping(
        {
            "providers": {"endpoint": {"api_key": "private"}},
            "servers": {"custom": {"command": ["server", "--stdio"], "extensions": ["py"], "settings": {"nested": {"enabled": False}}}},
        }
    )
    server = resolve_lsp_server_configs(config.servers)["custom"]
    assert server.command == ("server", "--stdio")
    assert server.extensions == (".py",)
    assert server.settings == {"nested": {"enabled": False}}
    assert "private" not in repr(config)


def test_default_and_explicit_disable_remain_distinct() -> None:
    assert lsp_config_from_mapping(None).enabled is True
    assert lsp_config_from_mapping({"enabled": False}).enabled is False
    assert lsp_config_from_mapping(None).servers == {}
