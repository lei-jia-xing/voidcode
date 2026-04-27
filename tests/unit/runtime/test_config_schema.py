from __future__ import annotations

import json
from typing import cast

import pytest

from voidcode.runtime.config_schema import (
    RUNTIME_CONFIG_SCHEMA_ID,
    apply_config_migrations,
    detect_config_migrations,
    format_starter_runtime_config_json,
    generate_starter_runtime_config,
    runtime_config_json_schema,
)


def test_runtime_config_json_schema_exposes_core_fields() -> None:
    schema = runtime_config_json_schema()

    assert schema["$id"] == RUNTIME_CONFIG_SCHEMA_ID
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    properties = schema["properties"]
    assert isinstance(properties, dict)
    assert properties["approval_mode"] == {
        "type": "string",
        "enum": ["allow", "deny", "ask"],
        "description": "Default approval policy for tool execution.",
    }
    assert properties["agent"] == {"$ref": "#/$defs/agentConfig"}
    defs = schema["$defs"]
    assert isinstance(defs, dict)
    agent_config = cast(dict[str, object], defs["agentConfig"])
    agent_properties = cast(dict[str, object], agent_config["properties"])
    preset_property = cast(dict[str, object], agent_properties["preset"])
    assert preset_property["enum"] == [
        "leader",
        "worker",
        "advisor",
        "explore",
        "researcher",
        "product",
    ]
    mcp_schema = cast(dict[str, object], properties["mcp"])
    mcp_properties = cast(dict[str, object], mcp_schema["properties"])
    mcp_servers = cast(dict[str, object], mcp_properties["servers"])
    mcp_server_schema = cast(dict[str, object], mcp_servers["additionalProperties"])
    assert mcp_server_schema["required"] == ["command"]


def test_generate_starter_runtime_config_excludes_secrets() -> None:
    payload = generate_starter_runtime_config(
        approval_mode="deny",
        execution_engine="provider",
        max_steps=7,
        include_examples=True,
    )

    assert payload == {
        "$schema": RUNTIME_CONFIG_SCHEMA_ID,
        "approval_mode": "deny",
        "execution_engine": "provider",
        "max_steps": 7,
        "tools": {"builtin": {"enabled": True}},
        "skills": {"enabled": True},
    }
    assert "providers" not in payload
    assert "api_key" not in json.dumps(payload)


def test_generate_starter_runtime_config_validates_inputs() -> None:
    with pytest.raises(ValueError, match="approval_mode"):
        generate_starter_runtime_config(approval_mode="always")
    with pytest.raises(ValueError, match="execution_engine"):
        generate_starter_runtime_config(execution_engine="remote")
    with pytest.raises(ValueError, match="max_steps"):
        generate_starter_runtime_config(max_steps=0)


def test_format_starter_runtime_config_json_preserves_order() -> None:
    payload = generate_starter_runtime_config(include_schema_reference=False)

    assert format_starter_runtime_config_json(payload) == '{\n  "approval_mode": "ask"\n}\n'


def test_detect_config_migrations_reports_removed_agent_leader_mode() -> None:
    payload: dict[str, object] = {
        "approval_mode": "ask",
        "agent": {"preset": "leader", "leader_mode": "legacy"},
    }

    migrations = detect_config_migrations(payload)

    assert len(migrations) == 1
    assert migrations[0].to_dict() == {
        "field_path": "agent.leader_mode",
        "action": "remove",
        "reason": (
            "agent.leader_mode has been removed; use the default leader execution flow instead"
        ),
    }


def test_apply_config_migrations_removes_legacy_field_without_mutating_input() -> None:
    payload: dict[str, object] = {
        "approval_mode": "ask",
        "agent": {"preset": "leader", "leader_mode": "legacy"},
    }
    migrations = detect_config_migrations(payload)

    updated = apply_config_migrations(payload, migrations)

    assert updated == {"approval_mode": "ask", "agent": {"preset": "leader"}}
    assert payload == {
        "approval_mode": "ask",
        "agent": {"preset": "leader", "leader_mode": "legacy"},
    }


def test_detect_config_migrations_ignores_non_object_payload() -> None:
    assert detect_config_migrations([]) == ()
