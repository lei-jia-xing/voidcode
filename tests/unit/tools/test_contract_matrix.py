"""Live provider-schema contract for builtin runtime tool definitions.

The check inspects the schema payload a real provider transport receives, so
invalid JSON Schema envelopes or dropped declared properties fail.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

import httpx2
import jsonschema
import pytest

from voidcode.core.transcript import AssembledContext, ContextSegment, ContextWindow
from voidcode.provider.config import OpenAIProviderConfig
from voidcode.provider.openai import OpenAIModelProvider
from voidcode.provider.openai_native import OpenAIChatCompletionsTransport
from voidcode.provider.protocol import ProviderTurnRequest
from voidcode.runtime.tool_provider import builtin_tool_definitions
from voidcode.tools.contracts import ToolDefinition


def _static_definitions() -> tuple[ToolDefinition, ...]:
    """Return the supported builtin tool definitions."""
    return builtin_tool_definitions()


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
    segments: tuple[ContextSegment, ...]
    metadata: dict[str, object]
    tool_results: tuple[object, ...] = ()
    continuity_state: object | None = None


def _provider_request(definitions: tuple[ToolDefinition, ...]) -> ProviderTurnRequest:
    context = _Context(prompt="hello", segments=(ContextSegment(role="user", content="hello"),), metadata={})
    return ProviderTurnRequest(
        assembled_context=cast(AssembledContext, context),
        bounded_context_window=cast(ContextWindow, _ContextWindow(prompt="hello")),
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
    if isinstance(properties, Mapping):
        return set(cast(Mapping[str, object], properties))
    return {key for key in schema if key != "required"}


def test_live_builtin_provider_schemas_validate_as_object_envelopes() -> None:
    definitions = _static_definitions()
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


def test_tool_definition_owns_immutable_input_schema() -> None:
    source = {"type": "object", "properties": {"path": {"type": "string"}}}
    definition = ToolDefinition(name="read", description="Read", input_schema=source)

    source["properties"]["path"]["type"] = "integer"
    properties = cast(dict[str, object], definition.input_schema["properties"])
    path = cast(dict[str, object], properties["path"])
    assert path["type"] == "string"
    with pytest.raises(TypeError):
        definition.input_schema["type"] = "array"  # type: ignore[index]
    with pytest.raises(TypeError):
        path["type"] = "integer"
