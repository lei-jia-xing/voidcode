"""Argument models for the ast_grep search/preview and replace tools."""

from __future__ import annotations

import re
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from voidcode.capabilities.files._common import MAX_RESULT_LIMIT, validate_glob_patterns

_DEFAULT_LIMIT: Final[int] = 200
_SHA256_TEXT = re.compile(r"^[0-9a-f]{64}$")


class AstGrepArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    mode: Literal["search", "preview"]
    pattern: str
    path: str
    rewrite: str | None = None
    lang: str | None = None
    include: list[str] | None = None
    exclude: list[str] | None = None
    include_hidden: bool = True
    respect_gitignore: bool = False
    limit: int = Field(default=_DEFAULT_LIMIT, ge=1, le=MAX_RESULT_LIMIT)

    @field_validator("pattern", "path", "lang")
    @classmethod
    def _nonempty_text(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is not None and not value.strip():
            raise ValueError(f"{info.field_name} must not be empty")
        return value

    _glob_patterns = field_validator("include", "exclude", mode="after")(validate_glob_patterns)


class AstGrepReplaceArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    pattern: str
    rewrite: str
    path: str
    lang: str | None = None
    apply: Literal[True]
    expectedHash: str
    include_hidden: bool = True
    respect_gitignore: bool = False

    @field_validator("pattern", "path", "lang")
    @classmethod
    def _nonempty_text(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is not None and not value.strip():
            raise ValueError(f"{info.field_name} must not be empty")
        return value

    @field_validator("expectedHash")
    @classmethod
    def _sha256(cls, value: str) -> str:
        if _SHA256_TEXT.fullmatch(value) is None:
            raise ValueError("expectedHash must be a lowercase 64-character SHA-256 hex digest")
        return value


__all__ = ["AstGrepArgs", "AstGrepReplaceArgs"]
