"""Field validators the exec tools actually use: non-empty command/process id/description."""

from __future__ import annotations

from typing import Annotated

from pydantic import AfterValidator


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


type NonEmptyCommand = Annotated[str, AfterValidator(validate_command)]
type OptionalDescription = Annotated[str | None, AfterValidator(validate_description)]
type NonEmptyProcessId = Annotated[str, AfterValidator(validate_process_id)]


__all__ = [
    "NonEmptyCommand",
    "NonEmptyProcessId",
    "OptionalDescription",
    "validate_command",
    "validate_description",
    "validate_process_id",
]
