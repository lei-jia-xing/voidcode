"""JSON Schema validation for delegated child structured output.

Phase 1 of the delegation-flexibility design: the parent declares an
invocation-level ``outputSchema`` on the ``task`` tool; the child's final
``submit_result`` ``data`` is validated against it at task finalize. The
verdict is persisted as runtime truth (``SchemaValidation``) and surfaced
through ``BackgroundTaskResult``.

Error formatting mirrors ``tools/_pydantic_args.format_validation_error``
(``location: message (received type)`` joined with ``; ``) so validation
failures read like the repo's other tool-validation errors.
"""

from __future__ import annotations

from typing import Literal

import jsonschema

from .task import SchemaValidation

_SCHEMA_SOURCE_INVOCATION = "invocation"


def validate_structured_output(
    *,
    data: dict[str, object],
    schema: dict[str, object],
    schema_mode: Literal["permissive", "strict"],
) -> SchemaValidation:
    """Validate ``data`` against the parent-declared ``schema``.

    All errors are collected (like ``format_validation_error``) and joined
    into a single message. ``schema_source`` is ``"invocation"`` in v1: the
    schema always comes from the ``task`` tool call, never from agent
    frontmatter.
    """
    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(data), key=lambda error: error.path)
    if not errors:
        return SchemaValidation(
            schema_source=_SCHEMA_SOURCE_INVOCATION,
            schema_mode=schema_mode,
            valid=True,
            error=None,
        )
    message = "; ".join(_format_schema_validation_error(error) for error in errors)
    return SchemaValidation(
        schema_source=_SCHEMA_SOURCE_INVOCATION,
        schema_mode=schema_mode,
        valid=False,
        error=message,
    )


def _format_schema_validation_error(error: jsonschema.ValidationError) -> str:
    location = error.json_path.removeprefix("$").lstrip(".") or "data"
    return f"{location}: {error.message} (received {type(error.instance).__name__})"
