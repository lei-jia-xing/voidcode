"""Schema-backed config UX for the VoidCode runtime.

``runtime_config_json_schema()`` is generated from the payload models in
``runtime/config_models.py`` (plus the provider boundary models in
``provider/config.py``), so ``schema/voidcode.config.schema.json`` cannot drift
from the boundary the runtime actually accepts. Regenerate the artifact with
``uv run python scripts/generate_config_schema.py`` and gate it with
``mise run schema:check`` or the test in
``tests/unit/runtime/test_config_schema.py``.

The models own shape; they do not own policy. Precedence, merge rules and
persistence decisions stay in ``runtime/config.py``; this module only publishes
the input contract.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from pydantic.json_schema import GenerateJsonSchema, JsonSchemaValue
from pydantic_core import core_schema

from ..agent import list_builtin_agent_manifests
from ..provider.naming import canonical_model_reference
from .config import RUNTIME_CONFIG_FILE_NAME, runtime_config_path
from .config_models import (
    AGENT_PRESET_ID_PATTERN,
    APPROVAL_MODE_ENV_VAR,
    MODEL_ENV_VAR,
    REASONING_EFFORT_ENV_VAR,
    SCHEMA_DEFINITION_NAMES,
    SHARED_SCHEMA_DEFINITIONS,
    TOOL_TIMEOUT_ENV_VAR,
    RuntimeConfigPayload,
)

RUNTIME_CONFIG_SCHEMA_ID = "https://raw.githubusercontent.com/lei-jia-xing/voidcode/master/schema/voidcode.config.schema.json"
RUNTIME_CONFIG_SCHEMA_URI = RUNTIME_CONFIG_SCHEMA_ID
RUNTIME_CONFIG_SCHEMA_TITLE = "VoidCode runtime config"
_JSON_SCHEMA_DRAFT = "https://json-schema.org/draft/2020-12/schema"

#: Name of the published definition for one agent-map entry that must carry an
#: explicit ``preset`` (a built-in key may omit it and inherits the key).
_CUSTOM_AGENT_DEFINITION_NAME = "customAgentConfig"
_AGENT_CONFIG_DEFINITION_NAME = "agentConfig"


class _RuntimeConfigSchemaGenerator(GenerateJsonSchema):
    """Pydantic's generator, tuned to the published artifact's conventions."""

    def field_title_should_be_set(self, schema: object) -> bool:  # noqa: ARG002 - pydantic hook signature
        """Field titles are generated names; the artifact never carried them."""
        return False

    def default_schema(self, schema: core_schema.WithDefaultSchema) -> JsonSchemaValue:
        """Emit the accepted shape only.

        Defaults belong to the resolution path in ``runtime/config.py`` (and to
        ``config_models``), not to the published input contract.
        """
        return self.generate_inner(schema["schema"])


def _strip_titles(value: object) -> object:
    if isinstance(value, dict):
        return {key: _strip_titles(item) for key, item in value.items() if key != "title"}
    if isinstance(value, list):
        return [_strip_titles(item) for item in value]
    return value


def _rewrite_refs(value: object, mapping: Mapping[str, str]) -> object:
    if isinstance(value, dict):
        rewritten: dict[str, object] = {}
        for key, item in value.items():
            if key == "$ref" and isinstance(item, str) and item in mapping:
                rewritten[key] = mapping[item]
            else:
                rewritten[key] = _rewrite_refs(item, mapping)
        return rewritten
    if isinstance(value, list):
        return [_rewrite_refs(item, mapping) for item in value]
    return value


def _is_scalar_definition(definition: Mapping[str, object]) -> bool:
    """A leaf definition (enum/value alias) rather than an object shape."""
    return "properties" not in definition and "additionalProperties" not in definition and "allOf" not in definition


def _inline_scalar_definitions(value: object, scalar_definitions: Mapping[str, Mapping[str, object]]) -> object:
    """Inline published value-type aliases; the artifact has never had enum defs."""
    if isinstance(value, dict):
        reference = value.get("$ref")
        if isinstance(reference, str):
            definition = scalar_definitions.get(reference)
            if definition is not None:
                siblings = {key: item for key, item in value.items() if key != "$ref"}
                return {**siblings, **definition}
        return {key: _inline_scalar_definitions(item, scalar_definitions) for key, item in value.items()}
    if isinstance(value, list):
        return [_inline_scalar_definitions(item, scalar_definitions) for item in value]
    return value


def _collapse_nullable_unions(value: object) -> object:
    """Express ``X | null`` the way the artifact always has: ``type`` gains null.

    A payload model types an optional value as ``X | None`` because the loader
    treats an explicit ``null`` as unset, so the published contract must accept
    ``null`` as well. Where the value has no ``type`` of its own (a ``$ref``),
    the union stays an ``anyOf``.
    """
    if isinstance(value, list):
        return [_collapse_nullable_unions(item) for item in value]
    if not isinstance(value, dict):
        return value

    collapsed = {key: _collapse_nullable_unions(item) for key, item in value.items() if key != "anyOf"}
    branches = value.get("anyOf")
    if isinstance(branches, list) and len(branches) == 2:
        nullable_branch = next((branch for branch in branches if branch == {"type": "null"}), None)
        non_nullable = [branch for branch in branches if branch != nullable_branch]
        other_branch = non_nullable[0] if len(non_nullable) == 1 and isinstance(non_nullable[0], dict) else None
        if nullable_branch is not None and other_branch is not None:
            branch_type = other_branch.get("type")
            # ``enum``/``const`` cannot absorb ``null``: the union must stay
            # explicit so that ``null`` remains valid alongside the fixed set.
            enumerated = "enum" in other_branch or "const" in other_branch
            if isinstance(branch_type, str) and not enumerated:
                return {**collapsed, **other_branch, "type": [branch_type, "null"]}
            return {**collapsed, "anyOf": [other_branch, {"type": "null"}]}
    if branches is not None:
        collapsed["anyOf"] = branches
    return collapsed


def _fold_shared_shapes(value: object) -> object:
    """Reuse one ``$defs`` entry for shapes that repeat verbatim.

    Every hook command slot accepts the same command list and both
    external-directory permission maps accept the same decision map, so the
    artifact publishes each once (``$defs.commandList`` / ``$defs.permissionRules``).
    """
    if isinstance(value, list):
        return [_fold_shared_shapes(item) for item in value]
    if not isinstance(value, dict):
        return value
    if "$ref" in value:
        return value
    for definition_name, (shared_shape, _description) in SHARED_SCHEMA_DEFINITIONS.items():
        if value == shared_shape:
            return {"$ref": f"#/$defs/{definition_name}"}
        # a field that also accepts an explicit null publishes the same shape with
        # null merged into its type; fold that back to the shared definition too
        shared_type = cast(dict[str, object], shared_shape).get("type")
        if isinstance(shared_type, str) and value == {**cast(dict[str, object], shared_shape), "type": [shared_type, "null"]}:
            return {"anyOf": [{"$ref": f"#/$defs/{definition_name}"}, {"type": "null"}]}
    return {key: _fold_shared_shapes(item) for key, item in value.items()}


def _shared_definition_entries() -> dict[str, dict[str, object]]:
    entries: dict[str, dict[str, object]] = {}
    for definition_name, (shared_shape, description) in SHARED_SCHEMA_DEFINITIONS.items():
        entry = dict(cast(dict[str, object], shared_shape))
        if description is not None:
            entry["description"] = description
        entries[definition_name] = entry
    return entries


#: Policy keys whose loader rejects an explicit ``null`` while the payload model
#: keeps it nullable for the other surfaces. The published contract drops the
#: ``null`` branch for exactly these keys.
_NON_NULLABLE_POLICY_KEYS: dict[str, tuple[str, ...]] = {
    "runtimePolicyConfig": ("enabled", "tool_policy", "delegation_policy", "hook_policy", "prompt_activation"),
    "runtimePolicyToolPolicyConfig": ("default",),
    "runtimePolicyDelegationPolicyConfig": ("default",),
    "runtimePolicyPromptActivationConfig": ("enabled",),
    # the loader rejects an explicit null for a formatter preset command
    # (an empty argv is not a formatter invocation)
    "formatterPresetConfig": ("command",),
}


def _enforce_policy_strictness(definitions: dict[str, object]) -> None:
    for definition_name, field_names in _NON_NULLABLE_POLICY_KEYS.items():
        definition = definitions.get(definition_name)
        if not isinstance(definition, dict):
            continue
        properties = definition.get("properties")
        if not isinstance(properties, dict):
            continue
        for field_name in field_names:
            field = properties.get(field_name)
            if not isinstance(field, dict):
                continue
            branches = field.get("anyOf")
            if isinstance(branches, list):
                non_null = [branch for branch in branches if branch != {"type": "null"}]
                if len(non_null) == 1 and isinstance(non_null[0], dict):
                    properties[field_name] = {**{k: v for k, v in field.items() if k != "anyOf"}, **non_null[0]}
            elif isinstance(field.get("type"), list):
                types = [item for item in field["type"] if item != "null"]
                if len(types) == 1:
                    properties[field_name] = {**field, "type": types[0]}


def _custom_agent_definition(properties: dict[str, object]) -> dict[str, object]:
    """The agent-map entry that must name its preset explicitly.

    A built-in map key may omit ``preset`` (the loader derives it from the key),
    so every built-in preset is published as a named property and only the
    *additional* entries require ``preset``. The preset list comes from the
    built-in manifest registry, the same source the loader resolves against.
    """
    agents = cast(dict[str, object], properties.get("agents"))
    if agents is not None:
        agents["properties"] = {manifest.id: {"$ref": f"#/$defs/{_AGENT_CONFIG_DEFINITION_NAME}"} for manifest in list_builtin_agent_manifests()}
        agents["additionalProperties"] = {"$ref": f"#/$defs/{_CUSTOM_AGENT_DEFINITION_NAME}"}
    return {
        "allOf": [{"$ref": f"#/$defs/{_AGENT_CONFIG_DEFINITION_NAME}"}],
        "required": ["preset"],
    }


def _publish_definitions(
    generated_definitions: dict[str, dict[str, object]],
) -> tuple[dict[str, dict[str, object]], dict[str, str]]:
    """Rename generated definitions to their published names.

    An unmapped generated definition is a hard error: a new payload model must
    be published under an explicit name so the artifact's external contract
    (``#/$defs/<name>``) stays stable.
    """
    unmapped = sorted(name for name in generated_definitions if name not in SCHEMA_DEFINITION_NAMES)
    if unmapped:
        raise ValueError(
            f"config schema definition(s) missing a published name in runtime/config_models.py SCHEMA_DEFINITION_NAMES: {', '.join(unmapped)}"
        )
    reference_map = {f"#/$defs/{name}": f"#/$defs/{SCHEMA_DEFINITION_NAMES[name]}" for name in generated_definitions}
    published = {SCHEMA_DEFINITION_NAMES[name]: definition for name, definition in generated_definitions.items()}
    return published, reference_map


def _normalize_schema(value: object, reference_map: Mapping[str, str], scalar_definitions: Mapping[str, Mapping[str, object]]) -> object:
    normalized = _rewrite_refs(value, reference_map)
    normalized = _strip_titles(normalized)
    normalized = _inline_scalar_definitions(normalized, scalar_definitions)
    normalized = _collapse_nullable_unions(normalized)
    return _fold_shared_shapes(normalized)


def _is_shared_definition_shape(definition: Mapping[str, object], name: str) -> bool:
    """A payload-model alias that is exactly the shared shape it publishes under."""
    shared = SHARED_SCHEMA_DEFINITIONS.get(name)
    return shared is not None and definition == shared[0]


def _fallback_chain_requires_model(target: dict[str, object]) -> None:
    """A fallback chain without a primary model is rejected by the loader.

    ``provider/config.py`` needs a non-empty ``model`` whenever a
    ``fallback_models`` array is present, which ``if``/``then`` can express.
    """
    target["allOf"] = [
        *cast(list[dict[str, object]], target.get("allOf", [])),
        {
            "if": {
                "properties": {"fallback_models": {"type": "array"}},
                "required": ["fallback_models"],
            },
            "then": {"required": ["model"]},
        },
    ]


def _require_agent_preset(properties: dict[str, object]) -> None:
    """The singular ``agent`` block must name a string preset.

    ``agents.<builtin>`` may omit it (the loader derives it from the key), so the
    requirement lives on the top-level property rather than on ``agentConfig``.
    """
    agent = properties.get("agent")
    if not isinstance(agent, dict):
        return
    branches = agent.get("anyOf")
    if not isinstance(branches, list):
        return
    for index, branch in enumerate(branches):
        if isinstance(branch, dict) and "$ref" in branch:
            branches[index] = {
                "allOf": [
                    {"$ref": branch["$ref"]},
                    {"required": ["preset"]},
                    {"properties": {"preset": {"type": "string", "pattern": AGENT_PRESET_ID_PATTERN}}},
                ]
            }


def _runtime_config_schema_body() -> dict[str, object]:
    generated = RuntimeConfigPayload.model_json_schema(schema_generator=_RuntimeConfigSchemaGenerator)
    published_defs, reference_map = _publish_definitions(cast(dict[str, dict[str, object]], generated.pop("$defs")))
    scalar_names = {
        name for name, definition in published_defs.items() if _is_scalar_definition(definition) and name not in SHARED_SCHEMA_DEFINITIONS
    }
    scalar_definitions = {f"#/$defs/{name}": published_defs[name] for name in scalar_names}
    definitions: dict[str, object] = {
        name: _normalize_schema(definition, reference_map, scalar_definitions)
        for name, definition in published_defs.items()
        if name not in scalar_names and not _is_shared_definition_shape(definition, name)
    }
    properties = cast(dict[str, object], _normalize_schema(generated.pop("properties"), reference_map, scalar_definitions))
    definitions.update(_shared_definition_entries())
    definitions[_CUSTOM_AGENT_DEFINITION_NAME] = _custom_agent_definition(properties)
    _require_agent_preset(properties)
    _enforce_policy_strictness(definitions)
    agent_config = definitions.get(_AGENT_CONFIG_DEFINITION_NAME)
    if isinstance(agent_config, dict):
        _fallback_chain_requires_model(agent_config)
    body: dict[str, object] = {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "$defs": definitions,
    }
    _fallback_chain_requires_model(body)
    return body


def runtime_config_json_schema() -> dict[str, object]:
    body = _runtime_config_schema_body()
    return {
        "$schema": _JSON_SCHEMA_DRAFT,
        "$id": RUNTIME_CONFIG_SCHEMA_ID,
        "title": RUNTIME_CONFIG_SCHEMA_TITLE,
        "description": (
            "Workspace-local VoidCode runtime configuration. Stored at "
            f"`{RUNTIME_CONFIG_FILE_NAME}` in the workspace root. "
            "Resolves alongside environment variables "
            f"({APPROVAL_MODE_ENV_VAR}, {MODEL_ENV_VAR}, "
            f"{TOOL_TIMEOUT_ENV_VAR}, {REASONING_EFFORT_ENV_VAR}) and the user-level "
            "`~/.config/voidcode/config.json`."
        ),
        **body,
    }


def format_runtime_config_schema_json(schema: Mapping[str, object] | None = None) -> str:
    """The exact text of the shipped ``schema/voidcode.config.schema.json``."""
    document = runtime_config_json_schema() if schema is None else schema
    return json.dumps(dict(document), indent=2, ensure_ascii=False) + "\n"


__all__ = [
    "RUNTIME_CONFIG_SCHEMA_ID",
    "RUNTIME_CONFIG_SCHEMA_TITLE",
    "RUNTIME_CONFIG_SCHEMA_URI",
    "format_runtime_config_schema_json",
    "format_starter_runtime_config_json",
    "generate_starter_runtime_config",
    "runtime_config_json_schema",
    "write_runtime_config_payload",
]


def generate_starter_runtime_config(
    *,
    approval_mode: str = "ask",
    model: str | None = None,
    include_examples: bool = False,
    include_schema_reference: bool = True,
) -> dict[str, object]:
    if approval_mode not in {"allow", "deny", "ask"}:
        raise ValueError(f"approval_mode must be one of: allow, deny, ask; received {approval_mode!r}")
    if model is not None:
        # The starter config stores the canonical provider id, so `config init
        # --model MiniMax/...` and `--model minimax/...` write the same file.
        model = canonical_model_reference(model)

    payload: dict[str, object] = {}
    if include_schema_reference:
        payload["$schema"] = RUNTIME_CONFIG_SCHEMA_ID
    payload["approval_mode"] = approval_mode
    if model is not None:
        payload["model"] = model
    if include_examples:
        payload["formatter"] = {"enabled": True}
        payload["lsp"] = {"enabled": True}
        payload["mcp"] = {"enabled": True}
        payload["tools"] = {"builtin": {"enabled": True}}
        payload["skills"] = {"enabled": True}
    return payload


def format_starter_runtime_config_json(payload: Mapping[str, object]) -> str:
    return json.dumps(dict(payload), indent=2, ensure_ascii=False) + "\n"


def write_runtime_config_payload(
    workspace: Path,
    payload: Mapping[str, object],
    *,
    create_parents: bool = True,
) -> Path:
    config_path = runtime_config_path(workspace.resolve())
    if create_parents:
        config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        format_starter_runtime_config_json(payload),
        encoding="utf-8",
    )
    return config_path
