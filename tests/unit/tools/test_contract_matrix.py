"""Live contract matrix for builtin runtime tool definitions.

Every check is evaluated against the live builtin registry — never against a
hand-maintained table of names — and the provider-schema check inspects the tool
payload the runtime actually hands to a provider transport. A builtin tool that loses
its guidance sidecar, emits a provider schema that is not a valid object envelope, or
drifts from its capability-catalog row therefore fails the suite.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import httpx2
import jsonschema
import pytest

from voidcode.provider.config import OpenAIProviderConfig
from voidcode.provider.openai import OpenAIModelProvider
from voidcode.provider.openai_native import OpenAIChatCompletionsTransport
from voidcode.provider.protocol import ProviderAssembledContext, ProviderContextSegment, ProviderContextWindow, ProviderTurnRequest
from voidcode.runtime.service import VoidCodeRuntime
from voidcode.runtime.tool_registry import ESSENTIAL_TOOL_NAMES, ToolRegistry
from voidcode.tools.contracts import ToolDefinition
from voidcode.tools.guidance import guidance_filename_for_tool, guidance_for_tool


def _static_definitions(registry: ToolRegistry) -> tuple[ToolDefinition, ...]:
    """Return the live builtin definitions, excluding runtime-discovered MCP tools."""
    return tuple(definition for definition in registry.definitions() if not definition.name.startswith("mcp/"))


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[VoidCodeRuntime]:
    value = VoidCodeRuntime(workspace=tmp_path)
    try:
        yield value
    finally:
        value.__exit__(None, None, None)


def test_live_builtin_registry_metadata_and_guidance_sidecars(runtime: VoidCodeRuntime) -> None:
    registry = runtime._base_tool_registry
    definitions = _static_definitions(registry)
    assert definitions

    mismatched = sorted(name for name, tool in registry.tools.items() if name != tool.definition.name)
    assert mismatched == [], f"registry keys must equal definition names: {mismatched}"

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

    # Dynamic MCP tools intentionally share one sidecar and stay outside the static matrix.
    assert guidance_filename_for_tool("mcp/example/tool") == "mcp.txt"
    assert guidance_for_tool("mcp/example/tool")


def test_live_builtin_capability_catalog_agrees_with_registry(runtime: VoidCodeRuntime) -> None:
    registry = runtime._base_tool_registry
    by_name = {definition.name: definition for definition in _static_definitions(registry)}
    entries = {entry.name: entry for entry in registry.capability_catalog() if not entry.name.startswith("mcp/")}
    assert set(entries) == set(by_name)
    assert len({entry.documentation_uri for entry in entries.values()}) == len(entries)

    for name, definition in by_name.items():
        entry = entries[name]
        assert entry.documentation_uri == f"voidcode://tool/{name}"
        assert entry.read_only is definition.read_only
        assert entry.replay_policy == definition.effective_replay_policy
        assert entry.visibility == ("essential" if name in ESSENTIAL_TOOL_NAMES else "discoverable")


@dataclass(frozen=True, slots=True)
class _ContextWindow:
    prompt: str
    tool_results: tuple[object, ...] = ()
    compacted: bool = False
    retained_tool_result_count: int = 0
    continuity_state: object | None = None


@dataclass(frozen=True, slots=True)
class _Context:
    prompt: str
    segments: tuple[ProviderContextSegment, ...]
    metadata: dict[str, object]
    tool_results: tuple[object, ...] = ()
    continuity_state: object | None = None


def _provider_request(definitions: tuple[ToolDefinition, ...]) -> ProviderTurnRequest:
    context = _Context(prompt="hello", segments=(ProviderContextSegment(role="user", content="hello"),), metadata={})
    return ProviderTurnRequest(
        assembled_context=cast(ProviderAssembledContext, context),
        bounded_context_window=cast(ProviderContextWindow, _ContextWindow(prompt="hello")),
        available_tools=definitions,
        provider_name="openai",
        model_name="gpt-4o",
        raw_model="openai/gpt-4o",
        abort_signal=None,
    )


def _provider_tool_parameters(definitions: tuple[ToolDefinition, ...]) -> dict[str, dict[str, object]]:
    """Return the ``parameters`` envelope a real provider turn emits per tool name."""
    seen: dict[str, object] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["payload"] = json.loads(request.content)
        return httpx2.Response(
            200,
            json={
                "id": "chatcmpl-contract",
                "model": "gpt-4o",
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    transport = OpenAIChatCompletionsTransport(api_key="sk-test", http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
    OpenAIModelProvider(config=OpenAIProviderConfig(api_key="sk-test"), transport=transport).turn_provider().propose_turn(
        _provider_request(definitions)
    )

    payload = cast(dict[str, object], seen["payload"])
    emitted: dict[str, dict[str, object]] = {}
    for raw_tool in cast(list[dict[str, object]], payload["tools"]):
        function = cast(dict[str, object], raw_tool["function"])
        emitted[cast(str, function["name"])] = cast(dict[str, object], function["parameters"])
    return emitted


def _declared_property_names(definition: ToolDefinition) -> set[str]:
    """Names the definition declares, whether it uses the envelope or flat map form."""
    schema = definition.input_schema
    properties = schema.get("properties")
    if isinstance(properties, dict):
        return set(cast(dict[str, object], properties))
    return {key for key in schema if key != "required"}


def test_live_builtin_provider_schemas_validate_as_object_envelopes(runtime: VoidCodeRuntime) -> None:
    definitions = _static_definitions(runtime._base_tool_registry)
    emitted = _provider_tool_parameters(definitions)

    assert set(emitted) == {definition.name for definition in definitions}

    for definition in definitions:
        parameters = emitted[definition.name]
        jsonschema.Draft202012Validator.check_schema(parameters)
        json.dumps(parameters, ensure_ascii=True, sort_keys=True)

        assert parameters.get("type") == "object", f"{definition.name} provider schema is not an object envelope"
        properties = parameters.get("properties")
        assert isinstance(properties, dict), f"{definition.name} provider schema has no properties object"
        assert all(isinstance(schema, dict) for schema in properties.values())
        assert isinstance(parameters.get("additionalProperties"), bool), f"{definition.name} provider schema is not strict about unknown arguments"

        required = parameters.get("required", [])
        assert isinstance(required, list)
        assert len(required) == len(set(required))
        assert all(isinstance(name, str) and name in properties for name in required)

        # A definition whose declared properties do not survive the provider projection
        # would silently hand the model a schema missing arguments.
        assert _declared_property_names(definition) <= set(properties), f"{definition.name} lost declared properties in the provider schema"


@pytest.mark.parametrize(
    ("tool_name", "expected_read_only"),
    (
        pytest.param("edit", False, id="edit"),
        pytest.param("glob", True, id="glob"),
        pytest.param("grep", True, id="grep"),
        pytest.param("read", True, id="read"),
        pytest.param("shell_exec", False, id="shell-exec"),
        pytest.param("web_fetch", True, id="web-fetch"),
        pytest.param("web_search", True, id="web-search"),
        pytest.param("write", False, id="write"),
    ),
)
def test_live_builtin_registry_read_only_metadata(tool_name: str, expected_read_only: bool) -> None:
    """The default registry's read-only classification feeds policy and replay decisions."""
    registry = ToolRegistry.with_defaults()

    assert registry.resolve(tool_name).definition.read_only is expected_read_only
