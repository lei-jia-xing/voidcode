"""Optional workspace query contracts, independent of their implementing plugins.

QueryUnavailable means no implementation; None means no matching resource.
Malformed answers are contract violations, never an unavailable resource.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from .queries import QueryContract

RULE_URI_PREFIX = "voidcode://rule/"
RULE_PAGE_QUERY = "workspace.rule_page"
DOCUMENT_VERSION_QUERY = "workspace.document_version"
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class RulePage:
    rule: str
    content_hash: str
    text: str
    next_offset: int | None

    @classmethod
    def from_query(cls, value: Mapping[str, object]) -> RulePage | None:
        rule = value.get("rule")
        content_hash = value.get("content_hash")
        text = value.get("text")
        next_offset = value.get("next_offset")
        if not isinstance(rule, str) or not rule:
            return None
        if not isinstance(content_hash, str) or not content_hash:
            return None
        if not isinstance(text, str):
            return None
        if next_offset is not None and (not isinstance(next_offset, int) or isinstance(next_offset, bool) or next_offset < 1):
            return None
        return cls(rule=rule, content_hash=content_hash, text=text, next_offset=next_offset)

    def as_payload(self) -> dict[str, object]:
        return {
            "rule": self.rule,
            "content_hash": self.content_hash,
            "text": self.text,
            "next_offset": self.next_offset,
        }


@dataclass(frozen=True, slots=True)
class DocumentState:
    path: str
    version: int
    content_hash: str
    position_encoding: str

    @classmethod
    def from_query(cls, value: Mapping[str, object], *, path: str) -> DocumentState | None:
        answered_path = value.get("path")
        version = value.get("version")
        content_hash = value.get("content_hash")
        encoding = value.get("position_encoding")
        if answered_path != path:
            return None
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            return None
        if not isinstance(content_hash, str) or _SHA256_PATTERN.fullmatch(content_hash) is None:
            return None
        if not isinstance(encoding, str) or encoding not in ("utf-8", "utf-16", "utf-32"):
            return None
        return cls(path=path, version=version, content_hash=content_hash, position_encoding=encoding)

    def as_payload(self) -> dict[str, object]:
        return {"path": self.path, "version": self.version, "content_hash": self.content_hash, "position_encoding": self.position_encoding}


class RulePageRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, str_strip_whitespace=True)

    uri: str = Field(min_length=1)
    offset: int = Field(default=1, ge=1)
    limit: int = Field(default=2000, ge=1, le=2000)


class DocumentStateRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    path: str = Field(min_length=1)


RULE_PAGES = QueryContract(
    name=RULE_PAGE_QUERY,
    request_type=RulePageRequest,
    decode_answer=lambda value, _request: RulePage.from_query(value),
)
DOCUMENT_STATES = QueryContract(
    name=DOCUMENT_VERSION_QUERY,
    request_type=DocumentStateRequest,
    decode_answer=lambda value, request: DocumentState.from_query(value, path=request.path),
)
