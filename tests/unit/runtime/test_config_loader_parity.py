"""HEAD-parity regression tests for the loader's own acceptance rules.

These four cases were behaviour changes introduced while unifying the config
definition source. Each one is pinned here because the change was invisible in
the schema gates: they compare the *loader* against what HEAD accepted/rejected.
Every test fails if the corresponding regression comes back.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from voidcode.runtime.config import (
    _load_user_config,
    load_runtime_config,
)


def _load_workspace(workspace: Path, payload: dict[str, object]):
    (workspace / ".voidcode.json").write_text(json.dumps(payload), encoding="utf-8")
    return load_runtime_config(workspace, env={})


def test_mcp_stdio_server_with_empty_command_is_rejected(tmp_path: Path) -> None:
    """A stdio MCP server that cannot start is a config error, not a runtime one."""
    with pytest.raises(ValueError, match="using stdio transport requires a command"):
        _load_workspace(tmp_path, {"mcp": {"servers": {"s": {"transport": "stdio", "command": []}}}})


def test_mcp_remote_http_server_without_url_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="mcp.servers.s.url"):
        _load_workspace(tmp_path, {"mcp": {"servers": {"s": {"transport": "remote-http"}}}})


@pytest.mark.parametrize("schema_value", [5, [], {"x": 1}, True])
def test_non_string_schema_reference_is_ignored_in_the_workspace_config(tmp_path: Path, schema_value: object) -> None:
    """``$schema`` is an editor hint the loader never reads; HEAD ignored any value."""
    config = _load_workspace(tmp_path, {"$schema": schema_value, "approval_mode": "always-ask"})

    assert config.approval_mode == "always-ask"


def test_non_string_schema_reference_is_ignored_in_the_user_config(tmp_path: Path) -> None:
    config_home = tmp_path / "config"
    config_file = config_home / "voidcode" / "config.json"
    config_file.parent.mkdir(parents=True)
    config_file.write_text(json.dumps({"$schema": 5, "tui": {"keymap": {"n": "session_new"}}}), encoding="utf-8")

    user_config = _load_user_config({"XDG_CONFIG_HOME": str(config_home)})

    assert user_config.tui is not None
    assert user_config.tui.keymap == {"n": "session_new"}


@pytest.mark.parametrize("web_value", ["x", [], 5, True])
def test_non_object_web_block_is_ignored_in_the_user_config(tmp_path: Path, web_value: object) -> None:
    """The web-settings surface reads ``web`` opaquely; a bad value is not a config error."""
    config_home = tmp_path / "config"
    config_file = config_home / "voidcode" / "config.json"
    config_file.parent.mkdir(parents=True)
    config_file.write_text(json.dumps({"web": web_value}), encoding="utf-8")

    user_config = _load_user_config({"XDG_CONFIG_HOME": str(config_home)})

    assert user_config.tui is None


def test_formatter_block_without_languages_keeps_hooks_formatter_preset_overrides(tmp_path: Path) -> None:
    """``formatter: {}`` must not revert a ``hooks.formatter_presets`` override.

    An absent ``languages`` key means "no preset overrides"; only a present one
    merges over the built-in presets.
    """
    config = _load_workspace(
        tmp_path,
        {
            "hooks": {"formatter_presets": {"python": {"command": ["my-fmt"]}}},
            "formatter": {},
        },
    )

    assert config.hooks is not None
    assert config.hooks.formatter_presets["python"].command == ("my-fmt",)


def test_formatter_languages_merge_over_the_hooks_override(tmp_path: Path) -> None:
    """A present ``languages`` key still merges over the built-ins, as HEAD did."""
    config = _load_workspace(
        tmp_path,
        {
            "hooks": {"formatter_presets": {"python": {"command": ["my-fmt"]}}},
            "formatter": {"languages": {"python": {"command": ["other-fmt"]}}},
        },
    )

    assert config.hooks is not None
    assert config.hooks.formatter_presets["python"].command == ("other-fmt",)


def _load_workspace_with_user_config(
    workspace: Path,
    user_config_home: Path,
    repo_payload: dict[str, object],
    user_payload: dict[str, object],
):
    (workspace / ".voidcode.json").write_text(json.dumps(repo_payload), encoding="utf-8")
    user_config_path = user_config_home / "voidcode" / "config.json"
    user_config_path.parent.mkdir(parents=True, exist_ok=True)
    user_config_path.write_text(json.dumps(user_payload), encoding="utf-8")
    return load_runtime_config(workspace, env={"XDG_CONFIG_HOME": str(user_config_home)})


def test_user_global_hooks_concatenate_before_repo_local(tmp_path: Path) -> None:
    """User-global hook commands run before repo-local ones on the same surface."""
    config = _load_workspace_with_user_config(
        tmp_path,
        tmp_path / "user-config",
        {"hooks": {"pre_tool": [["echo", "repo-pre"]], "timeout_seconds": 12.5}},
        {"hooks": {"pre_tool": [["echo", "user-pre"]], "on_session_start": [["echo", "user-start"]]}},
    )

    assert config.hooks is not None
    assert config.hooks.pre_tool == (("echo", "user-pre"), ("echo", "repo-pre"))
    assert config.hooks.on_session_start == (("echo", "user-start"),)
    # Scalar governance stays repo-local.
    assert config.hooks.timeout_seconds == 12.5


def test_user_only_hooks_load_without_repo_local(tmp_path: Path) -> None:
    """User-global hooks alone produce an executable hooks config."""
    user_config_path = tmp_path / "user-config" / "voidcode" / "config.json"
    user_config_path.parent.mkdir(parents=True, exist_ok=True)
    user_config_path.write_text(json.dumps({"hooks": {"pre_tool": [["echo", "user-pre"]]}}), encoding="utf-8")

    config = load_runtime_config(tmp_path, env={"XDG_CONFIG_HOME": str(tmp_path / "user-config")})

    assert config.hooks is not None
    assert config.hooks.pre_tool == (("echo", "user-pre"),)
