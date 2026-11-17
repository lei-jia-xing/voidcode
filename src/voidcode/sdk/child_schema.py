"""Consumer-side structured delivery for delegated child output.

A parent declares the expected shape once, at spawn, as ``result_schema`` (a
JSON Schema describing one JSON object). The core carries the declaration
durably and appends a fixed delivery instruction to the child's prompt text;
it never validates output against the schema. Each parent consumer validates
the child's full terminal text after join — mirroring
``ToolDefinition.output_schema`` + ``result_schema.py`` (consumer-side only:
a schema bug can never lose committed work) and
``model_query_structured``'s never-pose semantics (exactly one of
``data``|``parse_error``; no fence-stripping or reply repair).

Degraded paths are explicit in the types: an undeclared schema is ``None``
(unknown — never \"valid\"), and ``validate_child_text`` returns exactly one
of owned data or a ``StructuredQueryParseError``. Preview bytes are never
validated; callers validate full artifact-joined text only.
"""

from __future__ import annotations

import json
from collections.abc import Mapping

import jsonschema

from .json_values import json_wire_object, own_json_object
from .services import StructuredQueryParseError

#: Fixed delivery instruction appended to a child prompt when the parent
#: declares ``result_schema``. The single wording lives here so every spawn
#: path (task, batch, workflow) is identical; the core applies it at spawn.
DELIVERY_SENTENCE = "Deliver your final result as exactly one JSON object matching this schema and nothing else:"


def canonical_result_schema(schema: Mapping[str, object]) -> dict[str, object]:
    """Validate a declared child result schema; return its owned wire dict.

    Raises ``ValueError`` when the declaration is not a JSON object or is
    not a valid JSON Schema (Draft 2020-12). No coercion, no default filling.
    """
    try:
        owned = own_json_object(schema)
    except ValueError as exc:
        raise ValueError(f"result_schema must be a JSON object: {exc}") from exc
    wire = json_wire_object(owned)
    try:
        jsonschema.Draft202012Validator.check_schema(wire)
    except jsonschema.exceptions.SchemaError as err:
        raise ValueError(f"result_schema is not a valid JSON schema: {err.message}") from err
    return wire


def compose_child_prompt(base_prompt: str, result_schema: Mapping[str, object] | None) -> str:
    """The effective child prompt: base text plus the fixed delivery hint.

    ``None`` (undeclared) returns the base prompt unchanged. A declared
    schema appends the fixed sentence plus the canonical schema JSON as the
    authoritative field list — plain prompt text, not a builtin, tool, or
    privilege. The caller must have validated the schema already; this
    re-owns it for deterministic serialization only.
    """
    if result_schema is None:
        return base_prompt
    wire = json_wire_object(own_json_object(result_schema))
    schema_json = json.dumps(wire, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return f"{base_prompt}\n\n{DELIVERY_SENTENCE}\n{schema_json}"


def validate_child_text(
    schema: Mapping[str, object],
    full_text: str,
) -> tuple[dict[str, object] | None, StructuredQueryParseError | None]:
    """Validate one child's full terminal text against a declared schema.

    Returns exactly one of owned wire ``data`` or ``parse_error`` — never
    both, never neither. ``invalid_json`` covers empty and unparseable text;
    ``schema_violation`` covers well-formed JSON the schema rejects (including
    a non-object top level). No fence-stripping, no reply repair: fenced or
    redundant text is ``invalid_json``. This mirrors
    ``Core.governed_model_query_structured``'s parse/validate branch exactly.
    """
    stripped = full_text.strip()
    if not stripped:
        return None, StructuredQueryParseError(kind="invalid_json", message="child output is empty")
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError as exc:
        return None, StructuredQueryParseError(kind="invalid_json", message=f"child output is not JSON: {exc.msg}")
    if not isinstance(parsed, dict):
        return None, StructuredQueryParseError(kind="schema_violation", message="top-level JSON value must be an object")
    try:
        owned = own_json_object(parsed)
    except ValueError as exc:
        return None, StructuredQueryParseError(kind="invalid_json", message=f"child output is not JSON: {exc}")
    try:
        jsonschema.Draft202012Validator(json_wire_object(schema)).validate(json_wire_object(owned))
    except jsonschema.ValidationError as exc:
        return None, StructuredQueryParseError(kind="schema_violation", message=exc.message, path=exc.json_path)
    return json_wire_object(owned), None
