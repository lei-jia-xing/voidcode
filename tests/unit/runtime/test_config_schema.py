from __future__ import annotations

import json
from dataclasses import fields
from pathlib import Path
from typing import cast

import jsonschema
import pytest

from voidcode.hook.config import RuntimeHooksConfig
from voidcode.provider.config import parse_provider_configs_payload
from voidcode.runtime.config import (
    RuntimeAgentConfig,
    RuntimeBackgroundTaskConfig,
    RuntimeConfig,
    RuntimeContextWindowConfig,
    RuntimeFormatterConfig,
    RuntimeLspConfig,
    RuntimeMcpConfig,
    RuntimeSkillsConfig,
    RuntimeToolsConfig,
    RuntimeTuiConfig,
    load_runtime_config,
)
from voidcode.runtime.config_models import (
    SCHEMA_DEFINITION_NAMES,
    RuntimeConfigPayload,
    config_model_keys,
)
from voidcode.runtime.config_schema import (
    RUNTIME_CONFIG_SCHEMA_ID,
    format_runtime_config_schema_json,
    format_starter_runtime_config_json,
    generate_starter_runtime_config,
    runtime_config_json_schema,
)


def _referenced_definition(
    schema: dict[str, object],
    node: object,
) -> dict[str, object]:
    """Resolve a generated property to the object it references.

    The artifact is generated from the payload models, so an object-valued
    property is a ``$ref`` (optionally wrapped in the ``null`` branch of an
    optional field) rather than an inline object.
    """
    if isinstance(node, dict):
        reference = node.get("$ref")
        if isinstance(reference, str):
            name = reference.rsplit("/", 1)[-1]
            return cast(dict[str, object], cast(dict[str, object], schema["$defs"])[name])
        branches = node.get("anyOf")
        if isinstance(branches, list):
            for branch in branches:
                resolved = _referenced_definition(schema, branch) if isinstance(branch, dict) and "$ref" in branch else None
                if resolved is not None:
                    return resolved
    raise AssertionError(f"property does not reference a definition: {node!r}")


def test_runtime_config_json_schema_exposes_core_fields() -> None:
    schema = runtime_config_json_schema()

    assert schema["$id"] == RUNTIME_CONFIG_SCHEMA_ID
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    properties = cast(dict[str, object], schema["properties"])
    assert isinstance(properties, dict)
    assert schema["additionalProperties"] is False
    defs = cast(dict[str, object], schema["$defs"])
    providers = _referenced_definition(schema, properties["providers"])
    assert providers["additionalProperties"] is False
    provider_properties = cast(dict[str, object], providers["properties"])
    payload = {
        "providers": {
            "openai": {"timeout_seconds": 10.0},
            "opencode-go": {"ssl_verify": False, "transient_retry": {"max_retries": 2}},
            "custom": {"local": {"model_map": {"alias": "local/model"}}},
        }
    }
    assert parse_provider_configs_payload(payload["providers"], source="runtime config field 'providers'") is not None
    assert "openai" in provider_properties
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"providers": {"unknown": {}}}, schema)
    with pytest.raises(ValueError):
        parse_provider_configs_payload({"unknown": {}}, source="runtime config field 'providers'")
    with pytest.raises(ValueError):
        parse_provider_configs_payload({"openai": {"unknown": True}}, source="runtime config field 'providers'")
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"providers": {"openai": {"unknown": True}}}, schema)
    assert "plan" not in properties
    assert "workflow_mode" not in properties
    assert "agents" in properties
    assert "workflows" not in properties
    # An optional section publishes an explicit ``null`` branch: the loader treats
    # an explicit ``null`` as "unset", so the contract accepts it too.
    assert properties["approval_mode"] == {
        "anyOf": [{"type": "string", "enum": ["allow", "deny", "ask"]}, {"type": "null"}],
        "description": "Default approval policy for tool execution.",
    }
    assert properties["permission"] == {"anyOf": [{"$ref": "#/$defs/permissionConfig"}, {"type": "null"}]}
    assert properties["execution_engine"] == {
        "anyOf": [{"type": "string", "enum": ["deterministic", "provider"]}, {"type": "null"}],
        "description": "Execution engine used when no request or environment override is set.",
    }
    assert properties["agent"] == {
        "anyOf": [
            {
                "allOf": [
                    {"$ref": "#/$defs/agentConfig"},
                    {"required": ["preset"]},
                    {"properties": {"preset": {"type": "string", "pattern": "^[a-z][a-z0-9_-]*$"}}},
                ]
            },
            {"type": "null"},
        ]
    }
    agents = cast(dict[str, object], properties["agents"])
    assert agents["additionalProperties"] == {"$ref": "#/$defs/customAgentConfig"}
    assert agents["propertyNames"] == {"pattern": "^[a-z][a-z0-9_-]*$"}
    # Builtin map keys may omit ``preset``; only additional entries require it.
    assert set(cast(dict[str, object], agents["properties"])) == {
        "leader",
        "worker",
        "advisor",
        "explore",
        "researcher",
        "product",
    }
    assert "categories" not in properties
    assert isinstance(defs, dict)
    assert "categoryConfig" not in defs
    agent_config = cast(dict[str, object], defs["agentConfig"])
    assert agent_config["additionalProperties"] is False
    agent_properties = cast(dict[str, object], agent_config["properties"])
    assert "plan" not in agent_properties
    assert "leader_mode" not in agent_properties
    preset_property = cast(dict[str, object], agent_properties["preset"])
    assert preset_property["pattern"] == "^[a-z][a-z0-9_-]*$"
    assert "enum" not in preset_property
    assert agent_properties["fallback_models"] == {
        "type": ["array", "null"],
        "items": {"type": "string"},
        "description": "Agent-scoped fallback model chain; requires agent.model.",
    }
    assert agent_properties["mcp_binding"] == {"anyOf": [{"$ref": "#/$defs/agentMcpBindingConfig"}, {"type": "null"}]}
    agent_mcp_binding_config = cast(dict[str, object], defs["agentMcpBindingConfig"])
    assert agent_mcp_binding_config["additionalProperties"] is False
    agent_mcp_binding_properties = cast(
        dict[str, object],
        agent_mcp_binding_config["properties"],
    )
    assert agent_mcp_binding_properties["profile"] == {"type": ["string", "null"], "minLength": 1}
    # Duplicate detection is a loader rule (see the parity corpus), not a
    # published constraint.
    assert agent_mcp_binding_properties["servers"] == {
        "type": ["array", "null"],
        "items": {"type": "string", "minLength": 1},
    }
    custom_agent_config = cast(dict[str, object], defs["customAgentConfig"])
    assert custom_agent_config["required"] == ["preset"]
    mcp_schema = _referenced_definition(schema, properties["mcp"])
    mcp_properties = cast(dict[str, object], mcp_schema["properties"])
    mcp_servers = cast(dict[str, object], mcp_properties["servers"])
    mcp_server_schema = _referenced_definition(schema, mcp_servers["additionalProperties"])
    assert "required" not in mcp_server_schema
    mcp_server_properties = cast(dict[str, object], mcp_server_schema["properties"])
    # an explicit null is normalised to the default transport
    assert mcp_server_properties["transport"] == {"anyOf": [{"type": "string", "enum": ["stdio", "remote-http"]}, {"type": "null"}]}
    assert mcp_server_properties["url"] == {
        "type": ["string", "null"],
        "format": "uri",
        "minLength": 1,
        "description": ("Remote HTTP MCP endpoint URL. Required when transport is remote-http."),
    }
    # The transport/argv requirement is enforced by the loader, not published:
    # a builtin server shorthand may omit both keys (the descriptor fills them).
    assert "allOf" not in mcp_server_schema
    background_task_schema = _referenced_definition(schema, properties["background_task"])
    assert background_task_schema["additionalProperties"] is False
    background_task_properties = cast(dict[str, object], background_task_schema["properties"])
    # An explicit null is normalised to the default, so the artifact accepts it.
    assert background_task_properties["delegated_reminders_enabled"] == {
        "type": ["boolean", "null"],
    }
    assert background_task_properties["delegated_reminder_cooldown_seconds"] == {
        "type": "integer",
        "minimum": 1,
    }
    assert background_task_properties["default_concurrency"] == {
        "type": "integer",
        "minimum": 1,
    }
    provider_concurrency = cast(dict[str, object], background_task_properties["provider_concurrency"])
    assert provider_concurrency["additionalProperties"] == {
        "type": "integer",
        "minimum": 1,
    }
    hooks_schema = _referenced_definition(schema, properties["hooks"])
    hooks_properties = cast(dict[str, object], hooks_schema["properties"])
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
    for key in ("default_tool_result_chars",):
        numeric_property = cast(dict[str, object], context_window_properties[key])
        assert numeric_property["minimum"] == 1
    provider_context_diagnostics = cast(dict[str, object], context_window_properties["provider_context_diagnostics"])
    assert provider_context_diagnostics["anyOf"] == [
        {"type": "string", "enum": ["off", "warn", "block"]},
        {"type": "null"},
    ]
    transform_failure_policy = cast(dict[str, object], context_window_properties["context_transform_failure_policy"])
    assert transform_failure_policy["anyOf"] == [
        {"type": "string", "enum": ["ignore", "warn", "block"]},
        {"type": "null"},
    ]
    provider_context_threshold = cast(dict[str, object], context_window_properties["provider_context_oversized_feedback_chars"])
    assert provider_context_threshold["minimum"] == 1
    tools_config = cast(dict[str, object], defs["runtimeToolsConfig"])
    assert tools_config["additionalProperties"] is False
    tools_properties = cast(dict[str, object], tools_config["properties"])
    assert "paths" not in tools_properties
    assert tools_properties["local"] == {"anyOf": [{"$ref": "#/$defs/localToolsConfig"}, {"type": "null"}]}
    assert properties["tools"] == {"anyOf": [{"$ref": "#/$defs/runtimeToolsConfig"}, {"type": "null"}]}
    assert agent_properties["tools"] == {"anyOf": [{"$ref": "#/$defs/agentToolsConfig"}, {"type": "null"}]}
    agent_tools_config = cast(dict[str, object], defs["agentToolsConfig"])
    assert agent_tools_config["additionalProperties"] is False
    agent_tools_properties = cast(dict[str, object], agent_tools_config["properties"])
    # ``essential_only`` is accepted for agent tools by the loader, so the
    # published contract declares it instead of hiding an accepted key.
    assert set(agent_tools_properties) == {"builtin", "allowlist", "default", "essential_only"}
    assert "local" not in agent_tools_properties
    local_tools_config = cast(dict[str, object], defs["localToolsConfig"])
    assert local_tools_config["additionalProperties"] is False
    local_tools_properties = cast(dict[str, object], local_tools_config["properties"])
    assert local_tools_properties["enabled"] == {"type": ["boolean", "null"]}
    assert local_tools_properties["path"] == {
        "type": ["string", "null"],
        "minLength": 1,
        "description": "Workspace-relative directory containing *.json tool manifests.",
    }
    permission_config = cast(dict[str, object], defs["permissionConfig"])
    permission_properties = cast(dict[str, object], permission_config["properties"])
    # an explicit null is normalised to the default rule map
    assert permission_properties["external_directory_read"] == {"anyOf": [{"$ref": "#/$defs/permissionRules"}, {"type": "null"}]}
    assert permission_properties["external_directory_write"] == {"anyOf": [{"$ref": "#/$defs/permissionRules"}, {"type": "null"}]}
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
        include_examples=True,
    )

    assert payload == {
        "$schema": RUNTIME_CONFIG_SCHEMA_ID,
        "approval_mode": "deny",
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
        generate_starter_runtime_config(approval_mode="always")

    with pytest.raises(ValueError, match="provider/model"):
        generate_starter_runtime_config(model="gpt-5")
    with pytest.raises(ValueError, match="provider/model"):
        generate_starter_runtime_config(model="provider/")
    with pytest.raises(ValueError, match="provider/model"):
        generate_starter_runtime_config(model="/gpt-5")


def test_format_starter_runtime_config_json_preserves_order() -> None:
    payload = generate_starter_runtime_config(include_schema_reference=False)

    assert format_starter_runtime_config_json(payload) == '{\n  "approval_mode": "ask"\n}\n'


def test_runtime_config_rejects_removed_workflows_field(tmp_path: Path) -> None:
    config_path = tmp_path / ".voidcode.json"
    config_path.write_text(
        json.dumps(
            {
                "workflows": {
                    "custom": {
                        "id": "custom",
                        "default_agent": "leader",
                        "category": "implementation",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="runtime config field 'workflows' is not supported"):
        _ = load_runtime_config(tmp_path, env={})


def test_runtime_config_rejects_repo_local_workflow_mode_default(tmp_path: Path) -> None:
    config_path = tmp_path / ".voidcode.json"
    config_path.write_text(json.dumps({"workflow_mode": "review"}), encoding="utf-8")

    with pytest.raises(
        ValueError,
        match="runtime config field 'workflow_mode' is not supported",
    ):
        _ = load_runtime_config(tmp_path, env={})


def test_runtime_config_loads_repo_local_execution_engine(tmp_path: Path) -> None:
    (tmp_path / ".voidcode.json").write_text(
        json.dumps({"execution_engine": "provider"}),
        encoding="utf-8",
    )

    config = load_runtime_config(tmp_path, env={})

    assert config.execution_engine == "provider"


def test_runtime_config_json_schema_exposes_policy_config_contract() -> None:
    schema = runtime_config_json_schema()
    properties = cast(dict[str, object], schema["properties"])
    defs = cast(dict[str, object], schema["$defs"])

    assert properties["policy"] == {"anyOf": [{"$ref": "#/$defs/runtimePolicyConfig"}, {"type": "null"}]}
    policy_config = cast(dict[str, object], defs["runtimePolicyConfig"])
    assert policy_config["additionalProperties"] is False
    policy_properties = cast(dict[str, object], policy_config["properties"])
    assert set(policy_properties) == {
        "enabled",
        "version",
        "tool_policy",
        "delegation_policy",
        "hook_policy",
        "prompt_activation",
    }
    assert "metadata" not in policy_properties
    assert policy_properties["version"] == {"type": "string", "const": "v1"}
    assert policy_properties["tool_policy"] == {"$ref": "#/$defs/runtimePolicyToolPolicyConfig"}
    assert policy_properties["delegation_policy"] == {"$ref": "#/$defs/runtimePolicyDelegationPolicyConfig"}
    assert policy_properties["hook_policy"] == {"$ref": "#/$defs/runtimePolicyHookPolicyConfig"}
    assert policy_properties["prompt_activation"] == {"$ref": "#/$defs/runtimePolicyPromptActivationConfig"}
    assert policy_properties["enabled"] == {"type": "boolean"}
    assert policy_config["required"] == ["version"]
    # The policy list sections accept the ``default`` key the loader understands,
    # so the published contract lists it next to allow/deny.
    tool_policy_properties = cast(dict[str, object], cast(dict[str, object], defs["runtimePolicyToolPolicyConfig"])["properties"])
    assert set(tool_policy_properties) == {"allow", "deny", "default"}
    # an explicit null is rejected for ``default``, and the value must be non-empty
    assert tool_policy_properties["default"] == {"type": "string", "minLength": 1}

    hook_policy = cast(dict[str, object], defs["runtimePolicyHookPolicyConfig"])
    hook_properties = cast(dict[str, object], hook_policy["properties"])
    assert "enum" not in cast(dict[str, object], hook_properties["actions"])
    allowed_scopes = cast(dict[str, object], hook_properties["allowed_event_scopes"])
    allowed_scope_items = cast(dict[str, object], allowed_scopes["items"])
    # The enum is generated from ``policy.runtime_policy_allowed_hook_scopes()``,
    # the same table the loader validates against. It therefore covers all 20
    # executable hook surfaces.
    assert allowed_scope_items["enum"] == [
        "session_start",
        "session_end",
        "session_idle",
        "pre_tool",
        "post_tool",
        "background_task_registered",
        "background_task_started",
        "background_task_progress",
        "background_task_completed",
        "background_task_failed",
        "background_task_cancelled",
        "background_task_interrupted",
        "background_task_notification_enqueued",
        "background_task_result_read",
        "delegated_result_available",
        "turn_progress",
        "stuck_detected",
        "approval_requested",
        "question_asked",
        "before_compact",
    ]


def test_runtime_config_rejects_invalid_policy_shapes(tmp_path: Path) -> None:
    config_path = tmp_path / ".voidcode.json"
    config_path.write_text(
        json.dumps({"policy": {"metadata": {"unbounded": True}}}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="policy.metadata"):
        _ = load_runtime_config(tmp_path, env={})


def test_runtime_config_accepts_policy_product_delegation(tmp_path: Path) -> None:
    config_path = tmp_path / ".voidcode.json"
    config_path.write_text(
        json.dumps(
            {
                "policy": {
                    "version": "v1",
                    "delegation_policy": {"allow": ["product"]},
                }
            }
        ),
        encoding="utf-8",
    )

    config = load_runtime_config(tmp_path, env={})

    assert config.policy is not None
    assert config.policy.delegation_policy.allowed == ("product",)


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


def _schema_properties(schema: dict[str, object], ref: str) -> set[str]:
    node: object = schema
    if ref:
        for part in ref.split("."):
            node = cast(dict[str, object], node)[part]
    return set(cast(dict[str, object], cast(dict[str, object], node)["properties"]))


def _dataclass_field_names(cls: type) -> set[str]:
    return {f.name for f in fields(cls)}


def _assert_config_section_mapping(
    schema_props: set[str],
    dataclass_cls: type,
    *,
    rename: dict[str, str] | None = None,
    schema_only: set[str] | None = None,
    runtime_only: set[str] | None = None,
) -> None:
    """Assert a bidirectional 1:1 mapping between schema properties and dataclass fields.

    ``rename`` maps a schema key to a differently-named dataclass field (the config
    file key is the schema name; the dataclass keeps a distinct public name).
    ``schema_only`` are schema keys with no dataclass counterpart (schema markers,
    e.g. ``$schema`` or ``context_window.version``).
    ``runtime_only`` are dataclass fields derived at runtime rather than set via the
    config file (e.g. resolution of env/explicit values), so they are intentionally
    absent from the schema.
    """
    rename = rename or {}
    schema_only = schema_only or set()
    runtime_only = runtime_only or set()
    config_keys = schema_props - schema_only
    config_fields = _dataclass_field_names(dataclass_cls) - runtime_only
    assert {rename.get(k, k) for k in config_keys} == config_fields


SCHEMA_PATH = Path(__file__).resolve().parents[3] / "schema" / "voidcode.config.schema.json"


def test_runtime_config_schema_file_matches_generated_schema() -> None:
    """The checked-in schema.json must be a faithful export of the generated schema.

    ``runtime_config_json_schema()`` is generated from the payload models in
    ``runtime/config_models.py`` and ``voidcode config schema`` prints it, while
    ``schema/voidcode.config.schema.json`` is its static editor-support export.
    The comparison is byte-exact, so the artifact must be regenerated with
    ``uv run python scripts/generate_config_schema.py`` (``mise run schema:check``)
    whenever a model changes: a field added to the models cannot ship without it.
    """
    assert SCHEMA_PATH.read_text(encoding="utf-8") == format_runtime_config_schema_json()


def test_runtime_config_schema_covers_every_published_definition() -> None:
    """Every model definition publishes an explicit name and every name is used.

    ``SCHEMA_DEFINITION_NAMES`` is the artifact's ``#/$defs`` contract; a payload
    model added without a published name makes schema generation fail loudly
    instead of silently renaming the definition, and a stale entry means the
    models and the published names drifted apart.
    """
    schema = runtime_config_json_schema()
    published = set(cast(dict[str, object], schema["$defs"]))
    # ``commandList``/``permissionRules``/``customAgentConfig`` are folded or
    # synthesized by the generator rather than owned by a payload model.
    shared = {"commandList", "permissionRules", "customAgentConfig"}

    generated_names = set(SCHEMA_DEFINITION_NAMES.values())
    assert published - shared <= generated_names
    # Every published name other than the folded ones is reachable in the artifact.
    assert generated_names & published >= published - shared


def test_runtime_config_schema_top_level_keys_match_the_payload_model() -> None:
    """The shipped artifact's top-level keys are exactly the payload model's keys."""
    file_schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    properties = cast(dict[str, object], file_schema["properties"])

    assert set(properties) == set(config_model_keys(RuntimeConfigPayload))


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
        "execution_engine": None,
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
    assert config.approval_mode == "ask"
    assert config.execution_engine == "provider"


@pytest.mark.parametrize(
    ("ref", "dataclass_cls", "rename", "schema_only", "runtime_only"),
    [
        # Top-level keys map to RuntimeConfig fields. `fallback_models` is the config-file
        # key for the dataclass field `provider_fallback`; `$schema` is an editor reference
        # and `config_schema_version` is a schema-only version marker (const 1).
        (
            "",
            RuntimeConfig,
            {"fallback_models": "provider_fallback"},
            {"$schema", "config_schema_version"},
            {"acp"},
        ),
        (
            "$defs.runtimeToolsConfig",
            RuntimeToolsConfig,
            {},
            set(),
            set(),
        ),
        # context_window.version is a schema-only schema-version marker (const 1); every
        # other property maps 1:1 onto RuntimeContextWindowConfig fields.
        (
            "$defs.contextWindowConfig",
            RuntimeContextWindowConfig,
            {},
            {"version"},
            set(),
        ),
        (
            "$defs.mcpConfig",
            RuntimeMcpConfig,
            {},
            set(),
            set(),
        ),
        (
            "$defs.agentConfig",
            RuntimeAgentConfig,
            {"fallback_models": "provider_fallback"},
            set(),
            {"execution_engine", "runtime_internal"},
        ),
        (
            "$defs.skillsConfig",
            RuntimeSkillsConfig,
            {},
            set(),
            set(),
        ),
        (
            "$defs.lspConfig",
            RuntimeLspConfig,
            {},
            set(),
            set(),
        ),
        (
            "$defs.formatterConfig",
            RuntimeFormatterConfig,
            {},
            set(),
            set(),
        ),
        (
            "$defs.backgroundTaskConfig",
            RuntimeBackgroundTaskConfig,
            {},
            set(),
            set(),
        ),
        (
            "$defs.tuiConfig",
            RuntimeTuiConfig,
            {},
            set(),
            set(),
        ),
        # RuntimeHooksConfig.format_on_write is a derived alias populated from
        # formatter.format_on_write/enabled, so it is not a hooks-level config key.
        (
            "$defs.hooksConfig",
            RuntimeHooksConfig,
            {},
            set(),
            {"format_on_write"},
        ),
    ],
    ids=[
        "top_level",
        "tools",
        "context_window",
        "mcp",
        "agent",
        "skills",
        "lsp",
        "formatter",
        "background_task",
        "tui",
        "hooks",
    ],
)
def test_runtime_config_schema_section_maps_to_config_fields(
    ref: str,
    dataclass_cls: type,
    rename: dict[str, str],
    schema_only: set[str],
    runtime_only: set[str],
) -> None:
    schema = runtime_config_json_schema()
    props = _schema_properties(schema, ref)

    _assert_config_section_mapping(
        props,
        dataclass_cls,
        rename=rename,
        schema_only=schema_only,
        runtime_only=runtime_only,
    )


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
