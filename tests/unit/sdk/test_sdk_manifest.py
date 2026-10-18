"""Manifest admission behavior: parse valid, reject malformed and mutating inputs."""

from __future__ import annotations

import pytest

from voidcode.sdk.manifest import parse_plugin_manifest

_VALID_SHA256 = "0123456789abcdef" * 4


def _minimal() -> dict[str, object]:
    return {"plugin_id": "hello", "version": "1.0.0", "entrypoint": "hello:serve"}


def test_manifest_rejects_malformed_version_types() -> None:
    with pytest.raises(ValueError):
        parse_plugin_manifest({**_minimal(), "schema_version": True})
    with pytest.raises(ValueError):
        parse_plugin_manifest({**_minimal(), "schema_version": 1.0})
    with pytest.raises(ValueError):
        parse_plugin_manifest({**_minimal(), "schema_version": 2})

    with pytest.raises(ValueError):
        parse_plugin_manifest({**_minimal(), "sdk_protocol": True})
    with pytest.raises(ValueError):
        parse_plugin_manifest({**_minimal(), "sdk_protocol": 1.0})
    with pytest.raises(ValueError):
        parse_plugin_manifest({**_minimal(), "sdk_protocol": 2})

    with pytest.raises(ValueError):
        parse_plugin_manifest({**_minimal(), "state_schema_version": True})
    with pytest.raises(ValueError):
        parse_plugin_manifest({**_minimal(), "state_schema_version": 0})
    with pytest.raises(ValueError):
        parse_plugin_manifest({**_minimal(), "state_schema_version": -1})


def test_manifest_rejects_malformed_identities() -> None:
    for invalid in ("", " ", "Hello", "1plugin", "-plugin", "_plugin", "plugin id", "plugin!"):
        with pytest.raises(ValueError):
            parse_plugin_manifest({**_minimal(), "plugin_id": invalid})

    for invalid_ver in ("", "   "):
        with pytest.raises(ValueError):
            parse_plugin_manifest({**_minimal(), "version": invalid_ver})


def test_manifest_rejects_coercive_entrypoints() -> None:
    for invalid in (
        "",
        "bare_identifier",
        ":callable",
        "module:",
        "1module:callable",
        "module:1callable",
        "mod.1sub:call",
        "mod:call:extra",
        "mod :call",
        "mod:call()",
    ):
        with pytest.raises(ValueError):
            parse_plugin_manifest({**_minimal(), "entrypoint": invalid})


def test_manifest_rejects_malformed_effects_and_replay_policy() -> None:
    with pytest.raises(ValueError):
        parse_plugin_manifest({**_minimal(), "tools": [{"name": "t", "description": "d", "effects": []}]})
    with pytest.raises(ValueError):
        parse_plugin_manifest({**_minimal(), "tools": [{"name": "t", "description": "d", "effects": ["unknown_effect"]}]})

    for mutating_effect in ("write", "execute", "spawn"):
        with pytest.raises(ValueError):
            parse_plugin_manifest(
                {
                    **_minimal(),
                    "tools": [
                        {
                            "name": "t",
                            "description": "d",
                            "effects": [mutating_effect],
                            "replay_policy": "safe",
                        }
                    ],
                }
            )

    with pytest.raises(ValueError):
        parse_plugin_manifest(
            {
                **_minimal(),
                "tools": [
                    {
                        "name": "t",
                        "description": "d",
                        "replay_policy": "invalid_policy",
                    }
                ],
            }
        )


def test_manifest_rejects_tool_level_entrypoint() -> None:
    with pytest.raises(ValueError):
        parse_plugin_manifest(
            {
                **_minimal(),
                "tools": [{"name": "t", "description": "d", "entrypoint": "module:tool_factory"}],
            }
        )


def test_manifest_rejects_undeclared_tool_capabilities() -> None:
    with pytest.raises(ValueError):
        parse_plugin_manifest(
            {
                **_minimal(),
                "tools": [
                    {
                        "name": "t",
                        "description": "d",
                        "capabilities": ["workspace.write"],
                    }
                ],
                "capabilities": [{"name": "workspace.read", "reason": "read only"}],
            }
        )


def test_manifest_rejects_malformed_files() -> None:
    for bad_path in ("/abs/path", "../escape", "a/../b", "a/./b", "a//b", "a\\b", "", "dir/"):
        with pytest.raises(ValueError):
            parse_plugin_manifest({**_minimal(), "files": [{"path": bad_path, "sha256": _VALID_SHA256}]})

    for bad_sha in ("short", "A" * 64, "g" * 64, _VALID_SHA256 + "0", _VALID_SHA256[:-1]):
        with pytest.raises(ValueError):
            parse_plugin_manifest({**_minimal(), "files": [{"path": "file.py", "sha256": bad_sha}]})

    with pytest.raises(ValueError):
        parse_plugin_manifest(
            {
                **_minimal(),
                "files": [
                    {"path": "file.py", "sha256": _VALID_SHA256},
                    {"path": "file.py", "sha256": _VALID_SHA256},
                ],
            }
        )


def test_manifest_rejects_duplicates_and_self_dependency() -> None:
    with pytest.raises(ValueError):
        parse_plugin_manifest(
            {
                **_minimal(),
                "tools": [{"name": "dup", "description": "1"}, {"name": "dup", "description": "2"}],
            }
        )
    with pytest.raises(ValueError):
        parse_plugin_manifest(
            {
                **_minimal(),
                "commands": [{"name": "dup", "description": "1"}, {"name": "dup", "description": "2"}],
            }
        )
    with pytest.raises(ValueError):
        parse_plugin_manifest(
            {
                **_minimal(),
                "capabilities": [{"name": "dup", "reason": "1"}, {"name": "dup", "reason": "2"}],
            }
        )
    with pytest.raises(ValueError):
        parse_plugin_manifest(
            {
                **_minimal(),
                "providers": [{"name": "dup", "description": "1"}, {"name": "dup", "description": "2"}],
            }
        )
    with pytest.raises(ValueError):
        parse_plugin_manifest({**_minimal(), "depends": ["dup", "dup"]})
    with pytest.raises(ValueError):
        parse_plugin_manifest({**_minimal(), "plugin_id": "loop", "depends": ["loop"]})


def test_manifest_rejects_unknown_fields_at_all_nesting_levels() -> None:
    with pytest.raises(ValueError):
        parse_plugin_manifest({**_minimal(), "unsupported_root": 1})
    with pytest.raises(ValueError):
        parse_plugin_manifest({**_minimal(), "files": [{"path": "f", "sha256": _VALID_SHA256, "extra": 1}]})
    with pytest.raises(ValueError):
        parse_plugin_manifest({**_minimal(), "tools": [{"name": "t", "description": "d", "extra": 1}]})
    with pytest.raises(ValueError):
        parse_plugin_manifest({**_minimal(), "commands": [{"name": "c", "description": "d", "extra": 1}]})
    with pytest.raises(ValueError):
        parse_plugin_manifest({**_minimal(), "capabilities": [{"name": "c", "reason": "r", "extra": 1}]})
    with pytest.raises(ValueError):
        parse_plugin_manifest({**_minimal(), "providers": [{"name": "p", "description": "d", "extra": 1}]})


def test_manifest_schema_validation_and_caller_mutation_isolation() -> None:
    with pytest.raises(ValueError):
        parse_plugin_manifest({**_minimal(), "config_schema": {"type": "nonexistent_type"}})

    with pytest.raises(ValueError):
        parse_plugin_manifest({**_minimal(), "config_schema": {"default": float("nan")}})

    input_schema = {"type": "object", "properties": {"setting": {"type": "string"}}}
    config_schema = {"type": "object", "properties": {"token": {"type": "string"}}}
    manifest = parse_plugin_manifest(
        {
            **_minimal(),
            "config_schema": config_schema,
            "tools": [{"name": "t", "description": "d", "input_schema": input_schema}],
        }
    )

    input_schema["properties"]["setting"]["type"] = "integer"  # type: ignore[index]
    input_schema["extra"] = "injected"
    config_schema["properties"]["token"]["type"] = "integer"  # type: ignore[index]
    config_schema["extra"] = "injected"

    assert manifest.tools[0].input_schema["properties"]["setting"]["type"] == "string"  # type: ignore[index]
    assert "extra" not in manifest.tools[0].input_schema
    assert manifest.config_schema["properties"]["token"]["type"] == "string"  # type: ignore[index]
    assert "extra" not in manifest.config_schema

    with pytest.raises(TypeError):
        manifest.tools[0].input_schema["new_key"] = 1  # type: ignore[index]
    with pytest.raises(TypeError):
        manifest.config_schema["new_key"] = 1  # type: ignore[index]
