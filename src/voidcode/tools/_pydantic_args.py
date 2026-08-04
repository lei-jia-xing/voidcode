from __future__ import annotations

from collections.abc import Mapping

from pydantic import ValidationError


def format_validation_error(tool_name: str, exc: ValidationError) -> str:
    details = "; ".join(_format_validation_error_item(error) for error in exc.errors())
    return (
        f"{tool_name} Validation error: {details}. "
        "Please retry with corrected arguments that satisfy the tool schema."
    )


def _format_validation_error_item(error: Mapping[str, object]) -> str:
    loc = error.get("loc", ())
    message = str(error.get("msg") or "invalid value")
    input_type = type(error.get("input")).__name__
    field_path = _format_error_location(loc)
    return f"{field_path}: {message} (received {input_type})"


def _format_error_location(loc: object) -> str:
    if isinstance(loc, str):
        return loc
    if isinstance(loc, (tuple, list)) and len(loc) > 0:
        parts = [str(part) for part in loc if isinstance(part, str)]
        return ".".join(parts) if parts else "arguments"
    return "arguments"
