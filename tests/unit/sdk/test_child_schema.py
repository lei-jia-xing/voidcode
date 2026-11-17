"""Consumer-side child delivery schemas: declaration, hint, and validation."""

from __future__ import annotations

from voidcode.sdk.child_schema import (
    DELIVERY_SENTENCE,
    canonical_result_schema,
    compose_child_prompt,
    validate_child_text,
)

_SCHEMA = {
    "type": "object",
    "required": ["files", "ok"],
    "properties": {"files": {"type": "array", "items": {"type": "string"}}, "ok": {"type": "boolean"}},
    "additionalProperties": False,
}


def test_canonical_schema_returns_owned_wire_dict() -> None:
    canonical = canonical_result_schema(dict(_SCHEMA))
    assert canonical == _SCHEMA
    assert type(canonical) is dict


def test_non_json_declaration_is_refused() -> None:
    try:
        canonical_result_schema({"files": object()})  # type: ignore[dict-item]
    except ValueError as exc:
        assert "result_schema must be a JSON object" in str(exc)
    else:
        raise AssertionError("non-JSON schema was accepted")


def test_invalid_json_schema_is_refused() -> None:
    try:
        canonical_result_schema({"type": "nonsense"})
    except ValueError as exc:
        assert "not a valid JSON schema" in str(exc)
    else:
        raise AssertionError("invalid schema was accepted")


def test_undeclared_hint_leaves_prompt_unchanged() -> None:
    assert compose_child_prompt("do the thing", None) == "do the thing"


def test_declared_hint_appends_fixed_sentence_and_schema() -> None:
    composed = compose_child_prompt("do the thing", dict(_SCHEMA))
    assert composed.startswith("do the thing\n\n" + DELIVERY_SENTENCE + "\n")
    assert '"files"' in composed and '"ok"' in composed


def test_valid_delivery_returns_owned_data_only() -> None:
    data, error = validate_child_text(dict(_SCHEMA), '{"files": ["a"], "ok": true}')
    assert error is None
    assert data == {"files": ["a"], "ok": True}


def test_empty_delivery_is_invalid_json() -> None:
    data, error = validate_child_text(dict(_SCHEMA), "   ")
    assert data is None
    assert error is not None and error.kind == "invalid_json" and error.path is None


def test_unparseable_delivery_is_invalid_json() -> None:
    data, error = validate_child_text(dict(_SCHEMA), "I cannot answer that.")
    assert data is None
    assert error is not None and error.kind == "invalid_json" and error.path is None


def test_fenced_delivery_is_invalid_json_without_repair() -> None:
    data, error = validate_child_text(dict(_SCHEMA), '```json\n{"files": [], "ok": true}\n```')
    assert data is None
    assert error is not None and error.kind == "invalid_json"


def test_non_object_top_level_is_schema_violation() -> None:
    # Mirrors governed_model_query_structured: a parsed non-object top level
    # violates the object contract instead of posing as data.
    data, error = validate_child_text(dict(_SCHEMA), '["files"]')
    assert data is None
    assert error is not None and error.kind == "schema_violation"


def test_wrong_shape_reports_location() -> None:
    data, error = validate_child_text(dict(_SCHEMA), '{"files": []}')
    assert data is None
    assert error is not None and error.kind == "schema_violation"
    assert error.path is not None and error.message
