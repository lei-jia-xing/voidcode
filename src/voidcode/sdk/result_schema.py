"""Consumer-side discovery and validation for declared tool-result body schemas.

A producer declares its success-body contract once, on
``ToolDefinition.output_schema``. Unrelated consumers (eval/tool bridges, typed
proxies, scripted waits) resolve that schema from the definition they already
hold and validate a peer's decoded body against it — no peer imports, no second
JSON body, no parallel registry.

Validation is consumer-side only: nothing on the commit path gates a producer's
result on this schema, so a schema bug can never lose committed work.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

import jsonschema

from .json_values import json_wire_object, own_json_object
from .tools import ToolFailure

if TYPE_CHECKING:
    from .tools import ToolDefinition, ToolResult


class ToolBodyValidationError(ValueError):
    """A peer's success body violated its tool's declared output schema."""

    def __init__(self, tool_name: str, path: str | None, message: str) -> None:
        self.tool_name = tool_name
        self.path = path
        self.message = message
        super().__init__(f"tool {tool_name} body failed validation at {path or '$'}: {message}")


def resolve_output_schema(definition: ToolDefinition) -> Mapping[str, object] | None:
    """The body contract a tool declares, or ``None`` when it declares none.

    ``None`` is unknown — the caller must treat the body as unvalidated, never
    as valid.
    """
    return definition.output_schema


def validate_tool_body(schema: Mapping[str, object], result: ToolResult) -> dict[str, object]:
    """Validate a success result's body against an explicit schema.

    Returns the owned wire dict on success. Raises
    ``ToolBodyValidationError`` when the result is a failure, the body is
    missing, or the body violates the schema. No coercion, no default filling,
    no partial success.
    """
    if isinstance(result, ToolFailure):
        raise ToolBodyValidationError(result.tool_name, None, "failure results carry no validatable success body")
    body = result.body
    if body is None:
        raise ToolBodyValidationError(result.tool_name, None, "declared output schema requires a body, but the result has none")
    try:
        owned = own_json_object(body.as_payload())
    except ValueError as exc:
        raise ToolBodyValidationError(result.tool_name, None, f"body is not JSON: {exc}") from exc
    try:
        jsonschema.Draft202012Validator(json_wire_object(schema)).validate(json_wire_object(owned))
    except jsonschema.ValidationError as exc:
        raise ToolBodyValidationError(result.tool_name, exc.json_path, exc.message) from exc
    return dict(owned)


def try_validate_tool_body(definition: ToolDefinition, result: ToolResult) -> dict[str, object] | None:
    """Validate against a definition's declared schema, or ``None`` if undeclared.

    ``None`` means only "this tool declares no contract". A declared contract
    that fails still raises ``ToolBodyValidationError`` — never ``None``.
    """
    schema = resolve_output_schema(definition)
    if schema is None:
        return None
    return validate_tool_body(schema, result)
