"""Native tool catalog and definition ownership contracts."""

from __future__ import annotations

from typing import cast

import pytest

from voidcode.runtime.tools.builtins import builtin_tool_definitions
from voidcode.tools.contracts import ToolDefinition


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


def test_builtin_catalog_exposes_only_the_supported_19_tool_names() -> None:
    names = tuple(definition.name for definition in builtin_tool_definitions())

    assert len(names) == 19
    assert len(names) == len(set(names))
    assert {"read", "edit", "write"} <= set(names)
    assert not {"multi_edit", "apply_patch"} & set(names)
