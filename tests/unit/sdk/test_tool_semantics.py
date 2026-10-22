"""Argument predicates are immutable JSON Schema data, independent of tool authority."""

from dataclasses import replace

import pytest
from jsonschema import SchemaError

from voidcode.sdk.codec import decode_tool_definition, encode_tool_definition
from voidcode.sdk.tools import ToolCall, ToolDefinition, ToolEffect, ToolSemantic

EDIT = ToolSemantic.RESOURCE_EDIT


def test_conditional_semantics_roundtrip_and_do_not_widen_authority():
    schema = {"properties": {"operation": {"const": "replace"}}, "required": ["operation"]}
    rules = {EDIT: schema}
    definition = ToolDefinition("custom", "mixed operations", effects=frozenset({ToolEffect.WRITE}), semantic_rules=rules)
    restored = decode_tool_definition(encode_tool_definition(definition))
    assert restored == definition
    for mode in ("search", "preview", "replace"):
        arguments = ToolCall("custom", {"operation": mode}).arguments
        expected = frozenset({EDIT}) if mode == "replace" else frozenset()
        assert restored.semantics_for(arguments) == expected
        assert restored.effects == frozenset({ToolEffect.WRITE})
        assert restored.replay_policy_for(arguments) == "never"
    assert restored.semantics_for({}) == frozenset()
    schema["properties"]["operation"]["const"] = "search"
    rules.clear()
    assert definition.semantics_for({"operation": "replace"}) == frozenset({EDIT})
    assert replace(definition, name="renamed").semantics_for({"operation": "replace"}) == frozenset({EDIT})
    with pytest.raises(TypeError):
        definition.semantic_rules[EDIT] = {}


def test_static_semantics_are_additive_and_nested_arguments_use_json_schema():
    rule = {"properties": {"batch": {"contains": {"properties": {"write": {"const": True}}, "required": ["write"]}}}, "required": ["batch"]}
    conditional = ToolDefinition("batch", "batch", semantic_rules={EDIT: rule})
    arguments = ToolCall("batch", {"batch": [{"write": False}, {"write": True}]}).arguments
    assert conditional.semantics_for(arguments) == frozenset({EDIT})
    assert conditional.semantics_for({"batch": [{"write": False}]}) == frozenset()
    static = replace(conditional, semantics=frozenset({EDIT}))
    assert static.semantics_for({}) == frozenset({EDIT})


def test_invalid_rule_schema_and_unknown_semantic_fail_at_ingress():
    with pytest.raises(SchemaError):
        ToolDefinition("bad", "bad", semantic_rules={EDIT: {"type": "unsupported-type"}})
    payload = encode_tool_definition(ToolDefinition("bad", "bad"))
    payload["semantic_rules"] = {"invented.semantic": {}}
    with pytest.raises(ValueError):
        decode_tool_definition(payload)
