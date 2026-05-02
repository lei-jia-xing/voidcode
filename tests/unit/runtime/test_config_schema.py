from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from voidcode.runtime.config import load_runtime_config
from voidcode.runtime.config_schema import (
    RUNTIME_CONFIG_SCHEMA_ID,
    format_starter_runtime_config_json,
    generate_starter_runtime_config,
    runtime_config_json_schema,
)
from voidcode.runtime.task import supported_subagent_categories


def test_runtime_config_json_schema_exposes_core_fields() -> None:
    schema = runtime_config_json_schema()

    assert schema["$id"] == RUNTIME_CONFIG_SCHEMA_ID
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    properties = cast(dict[str, object], schema["properties"])
    assert isinstance(properties, dict)
    assert schema["additionalProperties"] is False
    assert "plan" not in properties
    assert "agents" in properties
    assert "workflows" in properties
    assert properties["approval_mode"] == {
        "type": "string",
        "enum": ["allow", "deny", "ask"],
        "description": "Default approval policy for tool execution.",
    }
    assert properties["permission"] == {"$ref": "#/$defs/permissionConfig"}
    assert properties["agent"] == {"$ref": "#/$defs/agentConfig"}
    agents = cast(dict[str, object], properties["agents"])
    agent_map_properties = cast(dict[str, object], agents["properties"])
    assert agent_map_properties["worker"] == {"$ref": "#/$defs/agentConfig"}
    assert agents["additionalProperties"] == {"$ref": "#/$defs/customAgentConfig"}
    categories = cast(dict[str, object], properties["categories"])
    category_names = cast(dict[str, object], categories["propertyNames"])
    assert category_names["enum"] == list(supported_subagent_categories())
    defs = cast(dict[str, object], schema["$defs"])
    assert isinstance(defs, dict)
    agent_config = cast(dict[str, object], defs["agentConfig"])
    assert agent_config["additionalProperties"] is False
    agent_properties = cast(dict[str, object], agent_config["properties"])
    assert "plan" not in agent_properties
    assert "leader_mode" not in agent_properties
    preset_property = cast(dict[str, object], agent_properties["preset"])
    assert preset_property["pattern"] == "^[a-z][a-z0-9_-]*$"
    assert "enum" not in preset_property
    assert agent_properties["fallback_models"] == {
        "type": "array",
        "items": {"type": "string", "minLength": 1},
        "description": (
            "Agent-scoped shorthand for provider_fallback.fallback_models; "
            "requires agent.model as the preferred model."
        ),
    }
    assert agent_properties["mcp_binding"] == {"$ref": "#/$defs/agentMcpBindingConfig"}
    agent_mcp_binding_config = cast(dict[str, object], defs["agentMcpBindingConfig"])
    assert agent_mcp_binding_config["additionalProperties"] is False
    agent_mcp_binding_properties = cast(
        dict[str, object],
        agent_mcp_binding_config["properties"],
    )
    assert agent_mcp_binding_properties["profile"] == {"type": "string", "minLength": 1}
    assert agent_mcp_binding_properties["servers"] == {
        "type": "array",
        "items": {"type": "string", "minLength": 1},
        "uniqueItems": True,
    }
    custom_agent_config = cast(dict[str, object], defs["customAgentConfig"])
    assert custom_agent_config["required"] == ["preset"]
    workflows = cast(dict[str, object], properties["workflows"])
    workflow_map_properties = cast(dict[str, object], workflows["properties"])
    assert set(workflow_map_properties) == {
        "research",
        "implementation",
        "frontend",
        "review",
        "git",
    }
    assert workflows["additionalProperties"] == {"$ref": "#/$defs/workflowPresetConfig"}
    workflow_property_names = cast(dict[str, object], workflows["propertyNames"])
    assert workflow_property_names["pattern"] == "^[a-z][a-z0-9_-]*$"
    workflow_preset_config = cast(dict[str, object], defs["workflowPresetConfig"])
    assert workflow_preset_config["additionalProperties"] is False
    assert workflow_preset_config["required"] == ["id", "default_agent", "category"]
    workflow_preset_properties = cast(dict[str, object], workflow_preset_config["properties"])
    assert set(workflow_preset_properties) == {
        "id",
        "default_agent",
        "category",
        "prompt_append",
        "skill_refs",
        "force_load_skills",
        "hook_preset_refs",
        "mcp_binding_intents",
        "tool_policy_ref",
        "permission_policy_ref",
        "read_only_default",
        "verification_guidance",
    }
    assert workflow_preset_properties["id"] == {
        "type": "string",
        "pattern": "^[a-z][a-z0-9_-]*$",
    }
    assert workflow_preset_properties["force_load_skills"] == {
        "type": "array",
        "items": {"type": "string", "minLength": 1},
        "uniqueItems": True,
    }
    assert workflow_preset_properties["mcp_binding_intents"] == {
        "type": "array",
        "items": {"$ref": "#/$defs/workflowMcpBindingIntentConfig"},
    }
    workflow_mcp_binding = cast(dict[str, object], defs["workflowMcpBindingIntentConfig"])
    assert workflow_mcp_binding["additionalProperties"] is False
    workflow_mcp_properties = cast(dict[str, object], workflow_mcp_binding["properties"])
    assert workflow_mcp_properties["required"] == {"type": "boolean", "default": True}
    assert workflow_mcp_properties["servers"] == {
        "type": "array",
        "items": {"type": "string", "minLength": 1},
        "uniqueItems": True,
    }
    mcp_schema = cast(dict[str, object], properties["mcp"])
    mcp_properties = cast(dict[str, object], mcp_schema["properties"])
    mcp_servers = cast(dict[str, object], mcp_properties["servers"])
    mcp_server_schema = cast(dict[str, object], mcp_servers["additionalProperties"])
    assert mcp_server_schema["required"] == ["command"]
    mcp_server_properties = cast(dict[str, object], mcp_server_schema["properties"])
    assert mcp_server_properties["scope"] == {
        "type": "string",
        "enum": ["runtime", "session"],
        "description": (
            "Runtime-scoped servers are shared by the runtime; "
            "session-scoped servers are isolated per session."
        ),
    }
    background_task_schema = cast(dict[str, object], properties["background_task"])
    background_task_properties = cast(dict[str, object], background_task_schema["properties"])
    assert background_task_properties["default_concurrency"] == {
        "type": "integer",
        "minimum": 1,
    }
    provider_concurrency = cast(
        dict[str, object], background_task_properties["provider_concurrency"]
    )
    assert provider_concurrency["additionalProperties"] == {
        "type": "integer",
        "minimum": 1,
    }
    hooks_schema = cast(dict[str, object], properties["hooks"])
    hooks_properties = cast(dict[str, object], hooks_schema["properties"])
    assert hooks_properties["on_context_pressure"] == {"$ref": "#/$defs/commandList"}
    formatter_presets = cast(dict[str, object], hooks_properties["formatter_presets"])
    assert formatter_presets["additionalProperties"] == {"$ref": "#/$defs/formatterPresetConfig"}
    formatter_preset_config = cast(dict[str, object], defs["formatterPresetConfig"])
    assert formatter_preset_config["additionalProperties"] is False
    formatter_preset_properties = cast(dict[str, object], formatter_preset_config["properties"])
    assert set(formatter_preset_properties) == {
        "command",
        "extensions",
        "root_markers",
        "fallback_commands",
        "cwd_policy",
    }
    context_window_config = cast(dict[str, object], defs["contextWindowConfig"])
    context_window_properties = cast(dict[str, object], context_window_config["properties"])
    for key in (
        "max_tool_results",
        "minimum_retained_tool_results",
        "recent_tool_result_count",
        "reserved_output_tokens",
    ):
        numeric_property = cast(dict[str, object], context_window_properties[key])
        assert numeric_property["minimum"] == 1
    pressure_threshold = cast(
        dict[str, object], context_window_properties["context_pressure_threshold"]
    )
    assert pressure_threshold["exclusiveMinimum"] == 0
    assert pressure_threshold["maximum"] == 1
    pressure_cooldown = cast(
        dict[str, object], context_window_properties["context_pressure_cooldown_steps"]
    )
    assert pressure_cooldown["minimum"] == 1
    provider_context_diagnostics = cast(
        dict[str, object], context_window_properties["provider_context_diagnostics"]
    )
    assert provider_context_diagnostics["enum"] == ["off", "warn", "block"]
    provider_context_threshold = cast(
        dict[str, object], context_window_properties["provider_context_oversized_feedback_chars"]
    )
    assert provider_context_threshold["minimum"] == 1
    tools_config = cast(dict[str, object], defs["runtimeToolsConfig"])
    assert tools_config["additionalProperties"] is False
    tools_properties = cast(dict[str, object], tools_config["properties"])
    assert "paths" not in tools_properties
    assert tools_properties["local"] == {"$ref": "#/$defs/localToolsConfig"}
    assert properties["tools"] == {"$ref": "#/$defs/runtimeToolsConfig"}
    assert agent_properties["tools"] == {"$ref": "#/$defs/agentToolsConfig"}
    agent_tools_config = cast(dict[str, object], defs["agentToolsConfig"])
    assert agent_tools_config["additionalProperties"] is False
    agent_tools_properties = cast(dict[str, object], agent_tools_config["properties"])
    assert set(agent_tools_properties) == {"builtin", "allowlist", "default"}
    assert "local" not in agent_tools_properties
    local_tools_config = cast(dict[str, object], defs["localToolsConfig"])
    assert local_tools_config["additionalProperties"] is False
    local_tools_properties = cast(dict[str, object], local_tools_config["properties"])
    assert local_tools_properties["enabled"] == {"type": "boolean"}
    assert local_tools_properties["path"] == {
        "type": "string",
        "minLength": 1,
        "description": "Workspace-relative directory containing *.json tool manifests.",
    }
    permission_config = cast(dict[str, object], defs["permissionConfig"])
    permission_properties = cast(dict[str, object], permission_config["properties"])
    assert permission_properties["external_directory_read"] == {"$ref": "#/$defs/permissionRules"}
    assert permission_properties["external_directory_write"] == {"$ref": "#/$defs/permissionRules"}
    permission_rule_list = cast(dict[str, object], permission_properties["rules"])
    assert permission_rule_list["items"] == {"$ref": "#/$defs/patternPermissionRule"}
    pattern_permission_rule = cast(dict[str, object], defs["patternPermissionRule"])
    assert pattern_permission_rule["additionalProperties"] is False
    assert pattern_permission_rule["required"] == ["decision"]
    pattern_permission_properties = cast(dict[str, object], pattern_permission_rule["properties"])
    assert pattern_permission_properties["decision"] == {
        "type": "string",
        "enum": ["allow", "deny", "ask"],
    }
    assert set(pattern_permission_properties) == {"tool", "path", "command", "decision"}


def test_generate_starter_runtime_config_excludes_secrets() -> None:
    payload = generate_starter_runtime_config(
        approval_mode="deny",
        model="opencode-go/glm-5",
        execution_engine="provider",
        max_steps=7,
        include_examples=True,
    )

    assert payload == {
        "$schema": RUNTIME_CONFIG_SCHEMA_ID,
        "approval_mode": "deny",
        "model": "opencode-go/glm-5",
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

    with pytest.raises(ValueError, match="provider/model"):
        generate_starter_runtime_config(model="gpt-5")
    with pytest.raises(ValueError, match="provider/model"):
        generate_starter_runtime_config(model="provider/")
    with pytest.raises(ValueError, match="provider/model"):
        generate_starter_runtime_config(model="/gpt-5")
    with pytest.raises(ValueError, match="requires model"):
        generate_starter_runtime_config(execution_engine="provider")
    with pytest.raises(ValueError, match="execution_engine"):
        generate_starter_runtime_config(execution_engine="remote")
    with pytest.raises(ValueError, match="max_steps"):
        generate_starter_runtime_config(max_steps=0)


def test_format_starter_runtime_config_json_preserves_order() -> None:
    payload = generate_starter_runtime_config(include_schema_reference=False)

    assert format_starter_runtime_config_json(payload) == '{\n  "approval_mode": "ask"\n}\n'


def test_runtime_config_loads_workflow_preset_declarations(tmp_path: Path) -> None:
    skill_dir = tmp_path / ".voidcode" / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Demo skill.\n---\n# Demo\n",
        encoding="utf-8",
    )
    config_path = tmp_path / ".voidcode.json"
    config_path.write_text(
        json.dumps(
            {
                "mcp": {
                    "servers": {
                        "docs": {
                            "command": ["docs-mcp"],
                        }
                    }
                },
                "workflows": {
                    "custom": {
                        "id": "custom",
                        "default_agent": "leader",
                        "category": "implementation",
                        "force_load_skills": ["demo"],
                        "mcp_binding_intents": [{"servers": ["docs"], "required": True}],
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    config = load_runtime_config(tmp_path, env={})

    assert config.workflows is not None
    custom = config.workflows.get("custom")
    assert custom is not None
    assert custom.default_agent == "leader"
    assert custom.force_load_skills == ("demo",)
    assert custom.mcp_binding_intents[0].servers == ("docs",)


def test_runtime_config_without_workflows_skips_skill_discovery(tmp_path: Path) -> None:
    bad_skill_dir = tmp_path / ".voidcode" / "skills" / "bad"
    bad_skill_dir.mkdir(parents=True)
    (bad_skill_dir / "SKILL.md").write_text(
        "---\nname: bad\n---\n# Missing description\n",
        encoding="utf-8",
    )
    config_path = tmp_path / ".voidcode.json"
    config_path.write_text(json.dumps({"approval_mode": "ask"}), encoding="utf-8")

    config = load_runtime_config(tmp_path, env={})

    assert config.approval_mode == "ask"
    assert config.workflows is None


def test_runtime_config_rejects_workflow_missing_forced_skill(tmp_path: Path) -> None:
    config_path = tmp_path / ".voidcode.json"
    config_path.write_text(
        json.dumps(
            {
                "workflows": {
                    "custom": {
                        "id": "custom",
                        "default_agent": "leader",
                        "category": "implementation",
                        "force_load_skills": ["missing"],
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="force_load_skills references missing skill: missing"):
        _ = load_runtime_config(tmp_path, env={})


def test_runtime_config_accepts_builtin_workflow_skill_refs_without_workspace_skills(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / ".voidcode.json"
    config_path.write_text(
        json.dumps(
            {
                "workflows": {
                    "custom": {
                        "id": "custom",
                        "default_agent": "leader",
                        "category": "implementation",
                        "skill_refs": ["git-master"],
                        "force_load_skills": ["review-work"],
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    config = load_runtime_config(tmp_path, env={})

    assert config.workflows is not None
    custom = config.workflows.get("custom")
    assert custom is not None
    assert custom.skill_refs == ("git-master",)
    assert custom.force_load_skills == ("review-work",)


def test_runtime_config_rejects_workflow_missing_skill_ref(tmp_path: Path) -> None:
    config_path = tmp_path / ".voidcode.json"
    config_path.write_text(
        json.dumps(
            {
                "workflows": {
                    "custom": {
                        "id": "custom",
                        "default_agent": "leader",
                        "category": "implementation",
                        "skill_refs": ["missing"],
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="skill_refs references missing skill: missing"):
        _ = load_runtime_config(tmp_path, env={})
