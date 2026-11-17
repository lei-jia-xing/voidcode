"""Consumer-side tool-body schema discovery and validation."""

from __future__ import annotations

import pytest

from voidcode.sdk.result_schema import (
    ToolBodyValidationError,
    resolve_output_schema,
    try_validate_tool_body,
    validate_tool_body,
)
from voidcode.sdk.tools import (
    EmptyOutput,
    OpaqueToolBody,
    ToolBody,
    ToolDefinition,
    ToolFailure,
    ToolSuccess,
)

_SCHEMA = {
    "type": "object",
    "required": ["path", "line"],
    "properties": {"path": {"type": "string"}, "line": {"type": "integer", "minimum": 1}},
    "additionalProperties": False,
}


def _definition(schema: dict[str, object] | None) -> ToolDefinition:
    return ToolDefinition(name="finder", description="find things", output_schema=schema)


def _success(body: OpaqueToolBody | None) -> ToolSuccess[OpaqueToolBody]:
    return ToolSuccess(tool_name="finder", output=EmptyOutput(), body=body)


def test_resolve_returns_none_when_undeclared() -> None:
    assert resolve_output_schema(_definition(None)) is None


def test_resolve_returns_declared_schema() -> None:
    assert dict(resolve_output_schema(_definition(dict(_SCHEMA))) or {})["type"] == "object"


def test_validate_accepts_good_body() -> None:
    validated = validate_tool_body(_SCHEMA, _success(OpaqueToolBody({"path": "a.py", "line": 3})))
    assert validated == {"path": "a.py", "line": 3}


def test_validate_rejects_bad_body_with_location() -> None:
    with pytest.raises(ToolBodyValidationError) as caught:
        validate_tool_body(_SCHEMA, _success(OpaqueToolBody({"path": "a.py", "line": 0})))
    assert caught.value.tool_name == "finder"
    assert caught.value.path == "$.line"
    assert caught.value.message
    assert "finder" in str(caught.value)


def test_validate_rejects_missing_body() -> None:
    with pytest.raises(ToolBodyValidationError, match="requires a body"):
        validate_tool_body(_SCHEMA, _success(None))


def test_validate_rejects_failure_results() -> None:
    failure = ToolFailure(tool_name="finder", error="boom", body=OpaqueToolBody({"path": "a.py", "line": 1}))
    with pytest.raises(ToolBodyValidationError, match="failure results"):
        validate_tool_body(_SCHEMA, failure)


def test_validate_rejects_non_json_body() -> None:
    class _Bad(ToolBody):
        def as_payload(self) -> dict[str, object]:
            return {"path": object()}  # type: ignore[dict-item]

    with pytest.raises(ToolBodyValidationError, match="not JSON"):
        validate_tool_body(_SCHEMA, ToolSuccess(tool_name="finder", output=EmptyOutput(), body=_Bad()))


def test_try_validate_none_only_when_undeclared() -> None:
    good = _success(OpaqueToolBody({"path": "a.py", "line": 3}))
    assert try_validate_tool_body(_definition(None), good) is None
    assert try_validate_tool_body(_definition(dict(_SCHEMA)), good) == {"path": "a.py", "line": 3}
    bad = _success(OpaqueToolBody({"path": "a.py"}))
    with pytest.raises(ToolBodyValidationError):
        try_validate_tool_body(_definition(dict(_SCHEMA)), bad)
