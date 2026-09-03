"""Live contract matrix for builtin runtime tool definitions."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import jsonschema
import pytest

from voidcode.provider.litellm_backend import LiteLLMBackendSingleAgentProvider
from voidcode.runtime.service import VoidCodeRuntime
from voidcode.runtime.tool_registry import ToolRegistry
from voidcode.tools.contracts import ToolDefinition
from voidcode.tools.guidance import guidance_filename_for_tool, guidance_for_tool


def _static_definitions(registry: ToolRegistry) -> tuple[ToolDefinition, ...]:
    """Return the live builtin definitions, excluding runtime-discovered MCP tools."""
    return tuple(definition for definition in registry.definitions() if not definition.name.startswith("mcp/"))


def _provider_function_schema(definition: ToolDefinition) -> dict[str, object]:
    payload = LiteLLMBackendSingleAgentProvider._to_tool_schema(
        definition,
        original_to_provider={definition.name: definition.name},
    )
    assert payload["type"] == "function"
    function = payload["function"]
    assert isinstance(function, dict)
    assert function["name"] == definition.name
    assert function["description"] == definition.description
    parameters = function["parameters"]
    assert isinstance(parameters, dict)
    return cast(dict[str, object], parameters)


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[VoidCodeRuntime]:
    value = VoidCodeRuntime(workspace=tmp_path)
    try:
        yield value
    finally:
        value.__exit__(None, None, None)


def test_live_builtin_registry_has_unique_metadata_and_guidance(runtime: VoidCodeRuntime) -> None:
    definitions = _static_definitions(runtime._base_tool_registry)
    assert definitions
    assert len({definition.name for definition in definitions}) == len(definitions)
    names = {definition.name for definition in definitions}
    assert "background_process" in names
    assert not names.intersection({"background_process_start", "background_process_logs", "background_process_send", "background_process_stop"})

    for definition in definitions:
        assert definition.name.strip() == definition.name
        assert definition.description.strip()
        assert isinstance(definition.read_only, bool)
        assert definition.effective_replay_policy in {"safe", "never"}
        assert all(isinstance(key, str) and key.strip() for key in definition.path_argument_keys)

        filename = guidance_filename_for_tool(definition.name)
        assert filename is not None, f"missing guidance mapping for {definition.name}"
        assert filename != "mcp.txt"
        assert guidance_for_tool(definition.name), f"missing guidance sidecar for {definition.name}"

    # Dynamic MCP tools intentionally use the shared sidecar and are not part
    # of the static matrix above.
    assert guidance_filename_for_tool("mcp/example/tool") == "mcp.txt"
    assert guidance_for_tool("mcp/example/tool")


def test_live_builtin_provider_schemas_normalize_to_valid_object_envelopes(
    runtime: VoidCodeRuntime,
) -> None:
    definitions = _static_definitions(runtime._base_tool_registry)
    provider_names = {
        definition.name
        for definition in runtime.provider_tool_definitions(
            runtime._base_tool_registry,
            runtime._initial_effective_config,
        )
        if not definition.name.startswith("mcp/")
    }
    assert provider_names == {definition.name for definition in definitions}

    for definition in definitions:
        parameters = _provider_function_schema(definition)
        jsonschema.Draft202012Validator.check_schema(parameters)
        json.dumps(parameters, ensure_ascii=True, sort_keys=True)

        assert parameters["type"] == "object"
        properties = parameters.get("properties")
        assert isinstance(properties, dict)
        assert all(isinstance(name, str) and isinstance(schema, dict) for name, schema in properties.items())

        additional_properties = parameters.get("additionalProperties")
        assert isinstance(additional_properties, bool)
        required = parameters.get("required", [])
        assert isinstance(required, list)
        assert len(required) == len(set(required))
        assert all(isinstance(name, str) and name in properties for name in required)


def test_live_builtin_catalog_rows_match_registry_metadata(runtime: VoidCodeRuntime) -> None:
    definitions = _static_definitions(runtime._base_tool_registry)
    by_name = {definition.name: definition for definition in definitions}
    entries = {entry.name: entry for entry in runtime._base_tool_registry.capability_catalog() if not entry.name.startswith("mcp/")}
    assert set(entries) == set(by_name)

    for name, definition in by_name.items():
        entry = entries[name]
        assert entry.documentation_uri == f"voidcode://tool/{name}"
        assert entry.read_only is definition.read_only
        assert entry.replay_policy == definition.effective_replay_policy
        assert entry.visibility in {"essential", "discoverable"}
