"""Tool-name glob filters for pre/post hooks: match helper + config parsing."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from voidcode.hook.config import hook_tool_matches
from voidcode.runtime.config import load_runtime_config


def test_hook_tool_matches_empty_matches_all() -> None:
    assert hook_tool_matches((), "write") is True
    assert hook_tool_matches([], "anything") is True


def test_hook_tool_matches_glob_hit_and_miss() -> None:
    assert hook_tool_matches(("write*",), "write") is True
    assert hook_tool_matches(("write*",), "write_file") is True
    assert hook_tool_matches(("write*",), "read") is False
    assert hook_tool_matches(("read", "write"), "write") is True
    assert hook_tool_matches(("read",), "write") is False


def test_hook_tool_matches_is_case_sensitive() -> None:
    """``fnmatch.fnmatchcase`` is used, so the filter never case-folds."""
    assert hook_tool_matches(("write*",), "Write") is False
    assert hook_tool_matches(("WRITE*",), "write") is False


def test_hooks_match_config_parsing(tmp_path: Path) -> None:
    (tmp_path / ".voidcode.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "pre_tool": [["echo", "pre"]],
                    "pre_tool_match": ["write*"],
                    "post_tool_match": ["read"],
                }
            }
        ),
        encoding="utf-8",
    )

    config = load_runtime_config(tmp_path, env={})

    assert config.hooks is not None
    assert config.hooks.pre_tool_match == ("write*",)
    assert config.hooks.post_tool_match == ("read",)
    # Empty default still matches all (backcompat).
    assert config.hooks.pre_tool == (("echo", "pre"),)
    assert hook_tool_matches(config.hooks.pre_tool_match, "write_file") is True
    assert hook_tool_matches(config.hooks.pre_tool_match, "read") is False


def test_hooks_match_defaults_empty(tmp_path: Path) -> None:
    (tmp_path / ".voidcode.json").write_text(
        json.dumps({"hooks": {"pre_tool": [["echo", "pre"]]}}),
        encoding="utf-8",
    )

    config = load_runtime_config(tmp_path, env={})

    assert config.hooks is not None
    assert config.hooks.pre_tool_match == ()
    assert config.hooks.post_tool_match == ()
    assert hook_tool_matches(config.hooks.pre_tool_match, "anything") is True


def test_hooks_match_rejects_empty_and_non_string_patterns(tmp_path: Path) -> None:
    """An empty glob matches nothing, so it is rejected like other list configs."""
    config_path = tmp_path / ".voidcode.json"
    for bad_value in ([""], ["  "], [1]):
        config_path.write_text(json.dumps({"hooks": {"pre_tool_match": bad_value}}), encoding="utf-8")
        with pytest.raises(ValueError, match="hooks.pre_tool_match"):
            _ = load_runtime_config(tmp_path, env={})


def test_hooks_match_rejects_scalar_shape(tmp_path: Path) -> None:
    (tmp_path / ".voidcode.json").write_text(
        json.dumps({"hooks": {"pre_tool_match": "write*"}}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="hooks.pre_tool_match"):
        _ = load_runtime_config(tmp_path, env={})


def test_user_global_match_filters_merge_before_repo_local(tmp_path: Path) -> None:
    """Match filters concatenate user-first; neither side is dropped."""
    user_config_path = tmp_path / "user-config" / "voidcode" / "config.json"
    user_config_path.parent.mkdir(parents=True, exist_ok=True)
    user_config_path.write_text(json.dumps({"hooks": {"pre_tool_match": ["write*"]}}), encoding="utf-8")
    (tmp_path / ".voidcode.json").write_text(
        json.dumps({"hooks": {"pre_tool": [["echo", "repo"]], "pre_tool_match": ["read"]}}),
        encoding="utf-8",
    )

    config = load_runtime_config(tmp_path, env={"XDG_CONFIG_HOME": str(tmp_path / "user-config")})

    assert config.hooks is not None
    assert config.hooks.pre_tool_match == ("write*", "read")
