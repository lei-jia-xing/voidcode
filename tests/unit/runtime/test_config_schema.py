from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from voidcode.provider.config import parse_provider_configs_payload
from voidcode.runtime.config.loading import load_runtime_config
from voidcode.runtime.config.schema import (
    RUNTIME_CONFIG_SCHEMA_ID,
    generate_starter_runtime_config,
    runtime_config_json_schema,
)


def test_generate_starter_runtime_config_excludes_secrets() -> None:
    payload = generate_starter_runtime_config(
        approval_mode="yolo",
        model="opencode-go/glm-5",
        include_examples=True,
    )

    assert payload == {
        "$schema": RUNTIME_CONFIG_SCHEMA_ID,
        "approval_mode": "yolo",
        "model": "opencode-go/glm-5",
        "formatter": {"enabled": True},
        "lsp": {"enabled": True},
        "mcp": {"enabled": True},
        "tools": {"builtin": {"enabled": True}},
        "skills": {"enabled": True},
    }
    assert "providers" not in payload
    assert "api_key" not in json.dumps(payload)


def test_generate_starter_runtime_config_validates_inputs() -> None:
    with pytest.raises(ValueError, match="approval_mode"):
        generate_starter_runtime_config(approval_mode="allow")

    with pytest.raises(ValueError, match="provider/model"):
        generate_starter_runtime_config(model="gpt-5")
    with pytest.raises(ValueError, match="provider/model"):
        generate_starter_runtime_config(model="provider/")
    with pytest.raises(ValueError, match="provider/model"):
        generate_starter_runtime_config(model="/gpt-5")


def test_runtime_config_rejects_invalid_policy_shapes(tmp_path: Path) -> None:
    config_path = tmp_path / ".voidcode.json"
    config_path.write_text(
        json.dumps({"policy": {"metadata": {"unbounded": True}}}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="policy.metadata"):
        _ = load_runtime_config(tmp_path, env={})


def test_runtime_config_accepts_policy_plan_delegation(tmp_path: Path) -> None:
    config_path = tmp_path / ".voidcode.json"
    config_path.write_text(
        json.dumps(
            {
                "policy": {
                    "version": "v1",
                    "delegation_policy": {"allow": ["plan"]},
                }
            }
        ),
        encoding="utf-8",
    )

    config = load_runtime_config(tmp_path, env={})

    assert config.policy is not None
    assert config.policy.delegation_policy.allowed == ("plan",)


def test_runtime_config_rejects_invalid_policy_hook_scope(tmp_path: Path) -> None:
    config_path = tmp_path / ".voidcode.json"
    config_path.write_text(
        json.dumps(
            {
                "policy": {
                    "version": "v1",
                    "hook_policy": {"allowed_event_scopes": ["chat_message_transform"]},
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="policy.hook_policy.allowed_event_scopes"):
        _ = load_runtime_config(tmp_path, env={})


def test_runtime_config_schema_accepts_null_wherever_the_loader_treats_null_as_unset(tmp_path: Path) -> None:
    """Optional sections accept an explicit ``null`` in the published contract."""
    schema = runtime_config_json_schema()
    null_payload = {
        "$schema": None,
        "config_schema_version": None,
        "approval_mode": None,
        "permission": None,
        "policy": None,
        "model": None,
        "fallback_models": None,
        "tool_timeout_seconds": None,
        "reasoning_effort": None,
        "hooks": None,
        "formatter": None,
        "tools": None,
        "skills": None,
        "context_window": None,
        "lsp": None,
        "mcp": None,
        "tui": None,
        "providers": None,
        "background_task": None,
        "agent": None,
        "agents": None,
    }
    jsonschema.validate(null_payload, schema)
    # The loader must agree: a config file of explicit nulls resolves to defaults.
    (tmp_path / ".voidcode.json").write_text(json.dumps(null_payload), encoding="utf-8")
    config = load_runtime_config(tmp_path, env={})
    default_workspace = tmp_path / "default"
    default_workspace.mkdir()
    default_config = load_runtime_config(default_workspace, env={})
    assert config == default_config


def test_provider_schema_and_parser_reject_same_invalid_values() -> None:
    schema = runtime_config_json_schema()
    invalid_payloads = (
        {"providers": {"custom": {"openai": {}}}},
        {"providers": {"openai": {"timeout_seconds": 0}}},
        {"providers": {"endpoint": {"auth_scheme": "invalid"}}},
        {"providers": {"google": {"auth": {"method": "invalid"}}}},
        {"providers": {"github-copilot": {"auth": {"method": "invalid"}}}},
        {"providers": {"endpoint": {"model_map": {"alias": ""}}}},
    )
    for payload in invalid_payloads:
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(payload, schema)
        with pytest.raises(ValueError):
            parse_provider_configs_payload(payload["providers"], source="runtime config field 'providers'")


def test_provider_schema_and_parser_accept_valid_dynamic_provider_shapes() -> None:
    schema = runtime_config_json_schema()
    providers = {
        "providers": {
            "openrouter": {"auth_scheme": "bearer", "model_map": {"alias": "provider/model"}},
            "custom": {"team-gateway": {"base_url": "https://gateway.example"}, " padded-gateway ": {}},
        }
    }
    jsonschema.validate(providers, schema)
    parsed = parse_provider_configs_payload(providers["providers"], source="runtime config field 'providers'")
    assert parsed is not None
    # A custom provider id is trimmed and lowercased the same way a built-in id is.
    assert set(parsed.custom) == {"team-gateway", "padded-gateway"}


def test_provider_schema_and_parser_accept_explicit_null_as_unset() -> None:
    schema = runtime_config_json_schema()
    providers = {
        "providers": {
            "openai": {
                "api_key": None,
                "base_url": None,
                "timeout_seconds": None,
            },
            "endpoint": {"model_map": None, "timeout_seconds": None},
        }
    }
    jsonschema.validate(providers, schema)
    assert parse_provider_configs_payload(providers["providers"], source="runtime config field 'providers'") is not None
