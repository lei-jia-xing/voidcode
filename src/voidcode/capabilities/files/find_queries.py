"""Typed files queries used by ``find`` for workspace grep and text reads.

The files plugin answers these queries by invoking the same ``GrepTool`` and
``ReadTool`` implementations exposed to the model; find never imports or
instantiates those tools. A query answer is a bounded JSON object, so the
contract carries only the body fields find needs.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Mapping
from typing import NoReturn, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from voidcode.capabilities.files.grep_tool import GrepResultBody, GrepTool
from voidcode.capabilities.files.read_tool import ReadResultBody, ReadTool
from voidcode.capabilities.tool_errors import ToolDiagnosticError
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.json_values import json_wire_object
from voidcode.sdk.plugin import PluginQuery, PluginQueryCaller
from voidcode.sdk.services import ArtifactReference, PluginServices, QueryContractError, QueryUnavailable, ToolServices
from voidcode.sdk.tools import ToolCall, ToolFailure, is_tool_diagnostic_kind

FIND_GREP_QUERY = "workspace.find_grep"
FIND_READ_QUERY = "workspace.find_read"


class FindGrepRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    pattern: str = Field(min_length=1)
    path: str = "."
    ignore_case: bool = True
    context: int = Field(default=0, ge=0, le=100)
    include: list[str] | None = None
    exclude: list[str] | None = None
    respect_gitignore: bool = True
    limit: int = Field(ge=1)


class FindGrepMatch(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    file: str
    line: int = Field(ge=1)
    text: str


class FindGrepResult(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    matches: list[FindGrepMatch]
    truncated: bool
    skipped_undecodable: int = Field(default=0, ge=0)


class FindReadRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    path: str = Field(min_length=1)
    offset: int = Field(ge=1)
    limit: int = Field(ge=1, le=2000)


class FindReadLine(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    line: int = Field(ge=1)
    text: str
    truncated: bool


class FindReadResult(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    line_count: int | None
    offset: int = Field(ge=1)
    limit: int = Field(ge=1)
    next_offset: int | None
    truncated: bool
    partial: bool
    byte_count: int = Field(ge=0)
    content_hash: str | None
    lines: list[FindReadLine]


def _answer[R: BaseModel](name: str, value: Mapping[str, object] | None, result_type: type[R]) -> R:
    if value is None:
        raise QueryUnavailable(f"no admitted plugin answers the query {name!r}")
    if not isinstance(value, Mapping):
        raise QueryContractError(f"query {name!r} must answer with a JSON object")
    try:
        return result_type.model_validate(json_wire_object(value))
    except ValidationError as exc:
        raise QueryContractError(f"query {name!r} answered outside its contract: {exc}") from exc


class _FindGrepQuery:
    name = FIND_GREP_QUERY

    def ask(self, services: ToolServices, request: FindGrepRequest) -> FindGrepResult:
        return _answer(self.name, services.query(self.name, request.model_dump(mode="json")), FindGrepResult)

    def bind(self, handler: Callable[[FindGrepRequest, PluginQueryCaller], FindGrepResult]) -> PluginQuery:
        def invoke(arguments: Mapping[str, object], caller: PluginQueryCaller) -> Mapping[str, object] | None:
            request = FindGrepRequest.model_validate(arguments)
            return handler(request, caller).model_dump(mode="json")

        return PluginQuery(name=self.name, handler=invoke)


class _FindReadQuery:
    name = FIND_READ_QUERY

    def ask(self, services: ToolServices, request: FindReadRequest) -> FindReadResult:
        return _answer(self.name, services.query(self.name, request.model_dump(mode="json")), FindReadResult)

    def bind(self, handler: Callable[[FindReadRequest, PluginQueryCaller], FindReadResult]) -> PluginQuery:
        def invoke(arguments: Mapping[str, object], caller: PluginQueryCaller) -> Mapping[str, object] | None:
            request = FindReadRequest.model_validate(arguments)
            return handler(request, caller).model_dump(mode="json")

        return PluginQuery(name=self.name, handler=invoke)


FIND_GREP = _FindGrepQuery()
FIND_READ = _FindReadQuery()


class _QueryArtifactSink:
    """Consume read-tool artifact bytes without publishing them.

    The query result carries only the bounded text page; find never consumes the
    artifact reference ReadTool builds for clipped/binary pages. The sink hashes
    the bytes so ReadTool's normal page assembly still runs unchanged.
    """

    def spill_artifact_stream(self, chunks: Iterable[bytes], *, media_type: str = "application/octet-stream") -> ArtifactReference:
        digest = hashlib.sha256()
        size = 0
        for chunk in chunks:
            digest.update(chunk)
            size += len(chunk)
        return ArtifactReference(digest.hexdigest(), size, media_type)


_QUERY_ARTIFACTS = cast(PluginServices, _QueryArtifactSink())


def _query_context(caller: PluginQueryCaller) -> ToolContext:
    return ToolContext(
        workspace=caller.workspace,
        session_id=caller.session_id,
        run_id="",
        invocation_id="files-query",
        revision_id="",
        actor="plugin",
    )


def _raise_tool_failure(result: ToolFailure) -> NoReturn:
    diagnostics = result.diagnostics
    kind = diagnostics.kind if diagnostics is not None and is_tool_diagnostic_kind(diagnostics.kind) else "tool_execution_error"
    raise ToolDiagnosticError(
        message=result.error or "files query failed",
        error_kind=kind,
        error_details=dict(diagnostics.details) if diagnostics is not None else {"reason": "files_query_failure"},
        retry_guidance=diagnostics.guidance if diagnostics is not None else None,
    )


def _grep_handler(request: FindGrepRequest, caller: PluginQueryCaller) -> FindGrepResult:
    arguments: dict[str, object] = {
        "pattern": request.pattern,
        "path": request.path,
        "ignore_case": request.ignore_case,
        "context": request.context,
        "respect_gitignore": request.respect_gitignore,
        "limit": request.limit,
    }
    if request.include is not None:
        arguments["include"] = list(request.include)
    if request.exclude is not None:
        arguments["exclude"] = list(request.exclude)
    result = GrepTool().invoke(ToolCall(tool_name="grep", arguments=arguments), context=_query_context(caller), services=_QUERY_ARTIFACTS)
    if isinstance(result, ToolFailure):
        _raise_tool_failure(result)
    body = result.body
    if not isinstance(body, GrepResultBody):
        raise QueryContractError("grep query returned no typed result body")
    return FindGrepResult(
        matches=[FindGrepMatch(file=match.file, line=match.line, text=match.text) for match in body.matches],
        truncated=body.truncated,
        skipped_undecodable=body.skipped_undecodable,
    )


def _read_handler(request: FindReadRequest, caller: PluginQueryCaller) -> FindReadResult:
    result = ReadTool().invoke(
        ToolCall(tool_name="read", arguments={"path": request.path, "offset": request.offset, "limit": request.limit}),
        context=_query_context(caller),
        services=_QUERY_ARTIFACTS,
    )
    if isinstance(result, ToolFailure):
        _raise_tool_failure(result)
    body = result.body
    if not isinstance(body, ReadResultBody):
        raise ToolDiagnosticError(
            message=f"read returned no text page for {request.path}",
            error_kind="tool_execution_error",
            error_details={"reason": "not_text", "path": request.path},
            retry_guidance="Use a text file path; binary and hash-only pages are not judge evidence.",
        )
    return FindReadResult(
        line_count=body.line_count,
        offset=body.offset,
        limit=body.limit,
        next_offset=body.next_offset,
        truncated=body.truncated,
        partial=body.partial,
        byte_count=body.byte_count,
        content_hash=body.content_hash,
        lines=[FindReadLine(line=line.line, text=line.text, truncated=line.truncated) for line in body.lines],
    )


def create_find_queries() -> tuple[PluginQuery, ...]:
    return (FIND_GREP.bind(_grep_handler), FIND_READ.bind(_read_handler))
