from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ValidationError


def validate_command(value: str) -> str:
    if not value.strip():
        raise ValueError("command must not be empty")
    return value


def validate_description(value: str | None) -> str | None:
    if value is not None and not value.strip():
        raise ValueError("description must not be empty when provided")
    return value


def validate_process_id(value: str) -> str:
    if not value.strip():
        raise ValueError("process_id must be a non-empty string")
    return value


def validate_prompt(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError("prompt must be a non-empty string")
    return stripped


type NonEmptyCommand = Annotated[str, AfterValidator(validate_command)]
type OptionalDescription = Annotated[str | None, AfterValidator(validate_description)]
type NonEmptyProcessId = Annotated[str, AfterValidator(validate_process_id)]
type NonEmptyPrompt = Annotated[str, AfterValidator(validate_prompt)]


def parse_tool_args[M: BaseModel](model: type[M], arguments: Mapping[str, object] | dict[str, object], *, tool_name: str) -> M:
    try:
        return model.model_validate(dict(arguments))
    except ValidationError as exc:
        raise ValueError(format_validation_error(tool_name, exc)) from exc


def format_validation_error(tool_name: str, exc: ValidationError) -> str:
    details = "; ".join(_format_validation_error_item(error) for error in exc.errors())
    return f"{tool_name} Validation error: {details}. Please retry with corrected arguments that satisfy the tool schema."


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
