"""ToolDefinition wire compat: undeclared output schemas encode byte-identically."""

from __future__ import annotations

import json

from voidcode.sdk.codec import decode_tool_definition, encode_tool_definition
from voidcode.sdk.icons import Icon
from voidcode.sdk.json_values import json_wire_object
from voidcode.sdk.tools import ToolDefinition, ToolEffect


def _canonical(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def test_definition_without_output_schema_omits_the_key() -> None:
    definition = ToolDefinition(
        name="grep",
        description="Search text.",
        input_schema={"type": "object", "properties": {"pattern": {"type": "string"}}},
        effects=frozenset({ToolEffect.READ}),
        path_argument_keys=("path",),
        replay_policy="safe",
        icon=Icon(glyph="⌕"),
    )
    payload = encode_tool_definition(definition)
    assert "output_schema" not in payload
    assert payload == {
        "name": "grep",
        "description": "Search text.",
        "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}}},
        "effects": ["read"],
        "path_argument_keys": ["path"],
        "replay_policy": "safe",
        "icon": {"glyph": "⌕"},
    }
    assert _canonical(payload) == (
        '{"description":"Search text.",'
        '"effects":["read"],'
        '"icon":{"glyph":"⌕"},'
        '"input_schema":{"properties":{"pattern":{"type":"string"}},"type":"object"},'
        '"name":"grep",'
        '"path_argument_keys":["path"],'
        '"replay_policy":"safe"}'
    )


def test_minimal_definition_pins_nulls_and_absence() -> None:
    payload = encode_tool_definition(ToolDefinition(name="x", description="y"))
    assert payload == {
        "name": "x",
        "description": "y",
        "input_schema": {},
        "effects": ["read"],
        "path_argument_keys": [],
        "replay_policy": None,
        "icon": None,
    }


def test_legacy_payload_without_key_decodes_to_undeclared() -> None:
    legacy = {
        "name": "x",
        "description": "y",
        "input_schema": {},
        "effects": ["read"],
        "path_argument_keys": [],
        "replay_policy": None,
        "icon": None,
    }
    assert decode_tool_definition(legacy).output_schema is None


def test_declared_schema_round_trips_on_the_wire() -> None:
    schema = {"type": "object", "required": ["line"], "properties": {"line": {"type": "integer"}}}
    definition = ToolDefinition(name="x", description="y", output_schema=schema)
    payload = encode_tool_definition(definition)
    assert json_wire_object(payload["output_schema"]) == schema  # type: ignore[arg-type]
    assert json_wire_object(decode_tool_definition(payload).output_schema or {}) == schema
