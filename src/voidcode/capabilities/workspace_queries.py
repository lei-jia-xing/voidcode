"""Optional workspace query contracts, independent of their implementing plugins.

QueryUnavailable means no implementation; None means no matching resource.
Malformed answers are contract violations, never an unavailable resource.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from voidcode.sdk.plugin import PluginQuery, PluginQueryCaller
from voidcode.sdk.services import QueryContractError, ToolServices

DOCUMENT_VERSION_QUERY = "workspace.document_version"
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


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


class DocumentStateRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    path: str = Field(min_length=1)


class _DocumentStateQuery:
    name: str = DOCUMENT_VERSION_QUERY

    def ask(self, services: ToolServices, request: DocumentStateRequest) -> DocumentState | None:
        value = services.query(self.name, request.model_dump())
        if value is None:
            return None
        if not isinstance(value, Mapping):
            raise QueryContractError(f"query {self.name!r} must answer with a JSON object")
        state = DocumentState.from_query(value, path=request.path)
        if state is None:
            raise QueryContractError(f"query {self.name!r} answered outside its request/answer contract")
        return state

    def bind(self, handler: Callable[[DocumentStateRequest, PluginQueryCaller], DocumentState | None]) -> PluginQuery:
        def invoke(arguments: Mapping[str, object], caller: PluginQueryCaller) -> Mapping[str, object] | None:
            req = DocumentStateRequest.model_validate(arguments)
            ans = handler(req, caller)
            return None if ans is None else ans.as_payload()

        return PluginQuery(name=self.name, handler=invoke)


DOCUMENT_STATES = _DocumentStateQuery()
