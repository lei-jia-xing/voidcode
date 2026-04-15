from __future__ import annotations

from pydantic import BaseModel, field_validator


class ReadFileArgs(BaseModel):
    path: str


class WriteFileArgs(BaseModel):
    path: str
    content: str


class GrepArgs(BaseModel):
    pattern: str
    path: str

    @field_validator("pattern", mode="after")
    @classmethod
    def _validate_pattern(cls, value: str) -> str:
        if value == "":
            raise ValueError("pattern must not be empty")
        return value


class WebSearchArgs(BaseModel):
    query: str

    @field_validator("query", mode="after")
    @classmethod
    def _validate_query(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query must not be empty")
        return value


class ShellExecArgs(BaseModel):
    command: str

    @field_validator("command", mode="after")
    @classmethod
    def _validate_command(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("command must not be empty")
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

    @field_validator("rewrite", mode="after")
    @classmethod
    def _validate_rewrite(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("rewrite must not be empty")
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
