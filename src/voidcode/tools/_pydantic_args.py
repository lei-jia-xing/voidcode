from __future__ import annotations

from collections.abc import Iterable, Mapping

from pydantic import BaseModel, ValidationError, field_validator


def format_validation_error(tool_name: str, exc: ValidationError) -> str:
    details = "; ".join(_format_validation_error_item(error) for error in exc.errors())
    return (
        f"{tool_name} Validation error: {details}. "
        "Please retry with corrected arguments that satisfy the tool schema."
    )


def _format_validation_error_item(error: Mapping[str, object]) -> str:
    loc = error.get("loc", ())
    field_path = _format_error_location(loc)
    message = str(error.get("msg") or "invalid value")
    input_value = error.get("input")
    input_type = type(input_value).__name__
    return f"{field_path}: {message} (received {input_type})"


def _format_error_location(loc: object) -> str:
    if isinstance(loc, str):
        return loc
    if isinstance(loc, Iterable):
        parts = [str(part) for part in loc]
        if parts:
            return ".".join(parts)
    return "arguments"


class ReadFileArgs(BaseModel):
    filePath: str
    offset: int | None = None
    limit: int | None = None

    @field_validator("filePath", mode="after")
    @classmethod
    def _validate_file_path(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("filePath must not be empty")
        return value

    @field_validator("offset", mode="after")
    @classmethod
    def _validate_offset(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if value < 1:
            raise ValueError("offset must be greater than or equal to 1")
        return value

    @field_validator("limit", mode="after")
    @classmethod
    def _validate_limit(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if value < 1:
            raise ValueError("limit must be greater than or equal to 1")
        return value


class WriteFileArgs(BaseModel):
    path: str
    content: str


class GrepArgs(BaseModel):
    pattern: str
    path: str
    regex: bool = False
    context: int = 0
    include: list[str] | None = None
    exclude: list[str] | None = None

    @field_validator("pattern", mode="after")
    @classmethod
    def _validate_pattern(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("pattern must not be empty")
        return value

    @field_validator("path", mode="after")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("path must not be empty")
        return value

    @field_validator("context", mode="after")
    @classmethod
    def _validate_context(cls, value: int) -> int:
        if value < 0:
            raise ValueError("context must be greater than or equal to 0")
        return value

    @field_validator("include", "exclude", mode="after")
    @classmethod
    def _validate_glob_patterns(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        if not all(item.strip() for item in value):
            raise ValueError("glob patterns must be non-empty strings")
        return value


class WebSearchArgs(BaseModel):
    query: str
    numResults: int = 8

    @field_validator("query", mode="after")
    @classmethod
    def _validate_query(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query must not be empty")
        return value

    @field_validator("numResults", mode="after")
    @classmethod
    def _validate_num_results(cls, value: int) -> int:
        if value < 1:
            raise ValueError("numResults must be greater than or equal to 1")
        return value


class ShellExecArgs(BaseModel):
    command: str
    description: str | None = None

    @field_validator("command", mode="after")
    @classmethod
    def _validate_command(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("command must not be empty")
        return value

    @field_validator("description", mode="after")
    @classmethod
    def _validate_description(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("description must not be empty when provided")
        return value


class MultiEditItemArgs(BaseModel):
    oldString: str
    newString: str
    replaceAll: bool = False


class MultiEditArgs(BaseModel):
    path: str
    edits: list[MultiEditItemArgs]

    @field_validator("path", mode="after")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("path must be a non-empty string")
        return value

    @field_validator("edits", mode="after")
    @classmethod
    def _validate_edits(cls, value: list[MultiEditItemArgs]) -> list[MultiEditItemArgs]:
        if not value:
            raise ValueError("edits must not be empty")
        return value


class AstGrepSearchArgs(BaseModel):
    pattern: str
    path: str
    lang: str | None = None

    @field_validator("pattern", mode="after")
    @classmethod
    def _validate_pattern(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("pattern must not be empty")
        return value

    @field_validator("path", mode="after")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("path must be a non-empty string")
        return value

    @field_validator("lang", mode="after")
    @classmethod
    def _validate_lang(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.strip():
            raise ValueError("lang must not be empty")
        return value


class AstGrepReplaceArgs(BaseModel):
    pattern: str
    rewrite: str
    path: str
    lang: str | None = None
    apply: bool = False

    @field_validator("pattern", mode="after")
    @classmethod
    def _validate_pattern(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("pattern must not be empty")
        return value

    @field_validator("path", mode="after")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("path must be a non-empty string")
        return value

    @field_validator("lang", mode="after")
    @classmethod
    def _validate_lang(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.strip():
            raise ValueError("lang must not be empty")
        return value
