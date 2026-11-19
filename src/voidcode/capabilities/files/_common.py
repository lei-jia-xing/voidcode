"""Shared bounds and argument validators for the file-search tools."""

from __future__ import annotations

from pydantic import ValidationInfo

#: Upper bound for an explicit ``limit`` so one call cannot materialize an
#: unbounded match list.
MAX_RESULT_LIMIT = 5000


def validate_non_empty(value: str, info: ValidationInfo) -> str:
    if not value.strip():
        raise ValueError(f"{info.field_name} must not be empty")
    return value


def validate_glob_patterns(value: list[str] | None) -> list[str] | None:
    if value is None:
        return None
    if not all(item.strip() for item in value):
        raise ValueError("glob patterns must be non-empty strings")
    return value
