"""Use complete JSON Schema validation instead of checking a few top-level types."""

from __future__ import annotations

import pytest

from voidcode.plugins.mcp.mcp_manager import _validate_call_arguments_against_schema, _validate_input_schema

_SCHEMA = {
    "type": "object",
    "properties": {"request": {"$ref": "#/$defs/request"}},
    "required": ["request"],
    "additionalProperties": False,
    "$defs": {
        "request": {
            "type": "object",
            "properties": {"count": {"type": "integer", "minimum": 1}, "kind": {"enum": ["code", "text"]}},
            "required": ["count", "kind"],
            "additionalProperties": False,
        },
    },
}


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"request": {"count": 0, "kind": "code"}},
        {"request": {"count": True, "kind": "code"}},
        {"request": {"count": 1, "kind": "other"}},
        {"request": {"count": 1, "kind": "code", "extra": 1}},
    ],
)
def test_invalid_nested_arguments_are_rejected(arguments: dict[str, object]) -> None:
    with pytest.raises(ValueError, match=r"MCP\[demo/search\]"):
        _validate_call_arguments_against_schema(tool_name="demo/search", arguments=arguments, input_schema=_SCHEMA)


def test_local_references_are_resolved() -> None:
    schema = _validate_input_schema(_SCHEMA)
    _validate_call_arguments_against_schema(tool_name="demo/search", arguments={"request": {"count": 1, "kind": "code"}}, input_schema=schema)


def test_malformed_nested_schema_is_not_advertised_as_usable() -> None:
    with pytest.raises(ValueError, match="invalid MCP inputSchema"):
        _validate_input_schema({"type": "object", "properties": {"value": {"type": "bogus"}}})


def test_remote_schema_references_are_not_fetched() -> None:
    with pytest.raises(ValueError, match="unresolved schema reference"):
        _validate_call_arguments_against_schema(tool_name="demo/search", arguments={}, input_schema={"$ref": "https://example.invalid/schema"})
