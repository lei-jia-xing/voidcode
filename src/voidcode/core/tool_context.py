from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Protocol

from .todos import TodoPhase

if TYPE_CHECKING:
    from ..provider.protocol import ProviderAbortSignal
    from ..skills.models import SkillMetadata
    from ..tools.contracts import ToolCall, ToolDefinition, ToolResult

RULE_URI_PREFIX = "voidcode://rule/"


@dataclass(frozen=True, slots=True)
class PageEnd:
    """The requested page has no following line page."""


@dataclass(frozen=True, slots=True)
class NextPage:
    offset: int


type PagePosition = PageEnd | NextPage


@dataclass(frozen=True, slots=True)
class ArtifactMissing:
    """No artifact is visible to the caller."""


@dataclass(frozen=True, slots=True)
class ArtifactInvalid:
    reason: str


@dataclass(frozen=True, slots=True)
class ArtifactUnavailable:
    reason: str


@dataclass(frozen=True, slots=True)
class ArtifactPage:
    artifact_id: str
    text: str
    line_count: int
    offset: int
    limit: int
    page: PagePosition
    byte_offset: int | None = None
    byte_limit: int | None = None
    next_byte_offset: int | None = None
    sha256: str = ""
    byte_count: int = 0
    mime: str = "text/plain"
    source: str | None = None
    artifact: Mapping[str, object] | None = None

    @property
    def truncated(self) -> bool:
        return self.next_byte_offset is not None or isinstance(self.page, NextPage)

    def as_payload(self) -> dict[str, object]:
        next_offset = self.page.offset if isinstance(self.page, NextPage) else None
        payload = {key: value for key, value in (self.artifact or {}).items() if key != "data_uri"}
        payload.update(
            {
                "artifact_id": self.artifact_id,
                "status": "available",
                "offset": self.offset,
                "limit": self.limit,
                "offset_unit": "bytes" if self.byte_offset is not None else "lines",
                "line_count": self.line_count,
                "next_offset": next_offset,
                "byte_offset": self.byte_offset,
                "byte_limit": self.byte_limit,
                "next_byte_offset": self.next_byte_offset,
                "byte_count": self.byte_count,
                "sha256": self.sha256,
                "content_type": self.mime,
                "source": self.source,
                "truncated": self.truncated,
            }
        )
        return payload


@dataclass(frozen=True, slots=True)
class ArtifactBinary:
    artifact_id: str
    mime: str
    byte_count: int
    sha256: str
    data: bytes | None = None
    data_uri: str | None = None
    source: str | None = None
    artifact: Mapping[str, object] | None = None

    def as_payload(self) -> dict[str, object]:
        payload = {key: value for key, value in (self.artifact or {}).items() if key != "data_uri"}
        payload.update(
            {
                "artifact_id": self.artifact_id,
                "status": "available",
                "content_type": self.mime,
                "byte_count": self.byte_count,
                "sha256": self.sha256,
                "source": self.source,
            }
        )
        return payload


type ArtifactRead = ArtifactMissing | ArtifactInvalid | ArtifactUnavailable | ArtifactPage | ArtifactBinary


@dataclass(frozen=True, slots=True)
class TranscriptInaccessible:
    """The target does not exist or is outside the caller's admitted lineage."""


@dataclass(frozen=True, slots=True)
class TranscriptEntry:
    sequence: int
    event_type: str
    source: str

    def as_payload(self) -> dict[str, object]:
        return {"sequence": self.sequence, "event_type": self.event_type, "source": self.source}


@dataclass(frozen=True, slots=True)
class TranscriptPage:
    session_id: str
    status: str
    summary: str | None
    last_event_sequence: int
    message_limit: int
    entries: tuple[TranscriptEntry, ...]
    truncated: bool

    def as_payload(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "status": self.status,
            "summary": self.summary,
            "last_event_sequence": self.last_event_sequence,
            "message_limit": self.message_limit,
            "transcript_count": len(self.entries),
            "transcript_truncated": self.truncated,
            "transcript": [entry.as_payload() for entry in self.entries],
        }


type TranscriptRead = TranscriptInaccessible | TranscriptPage


@dataclass(frozen=True, slots=True)
class RulePage:
    rule: str
    path: str
    scope: str
    precedence: int
    application: str
    content_hash: str
    text: str
    offset: int
    limit: int
    page: PagePosition
    byte_truncated: bool

    def as_payload(self) -> dict[str, object]:
        next_offset = self.page.offset if isinstance(self.page, NextPage) else None
        return {
            "rule": self.rule,
            "path": self.path,
            "scope": self.scope,
            "precedence": self.precedence,
            "application": self.application,
            "content_hash": self.content_hash,
            "offset": self.offset,
            "limit": self.limit,
            "next_offset": next_offset,
        }


class RuleReader(Protocol):
    def __call__(self, path: str, *, workspace: Path, offset: int, limit: int) -> RulePage: ...


class ToolCommandHandler(Protocol):
    def __call__(self, call: ToolCall, *, context: ToolContext) -> ToolResult: ...


class ToolCatalog(Protocol):
    def lookup(self, tool_name: str) -> ToolDefinition | None: ...


class ArtifactReader(Protocol):
    def read_artifact(
        self,
        *,
        caller_session_id: str,
        artifact_id: str,
        offset: int | None = None,
        limit: int | None = None,
        byte_offset: int | None = None,
        byte_limit: int | None = None,
    ) -> ArtifactRead: ...


class TranscriptReader(Protocol):
    def read_transcript(
        self,
        *,
        caller_session_id: str,
        session_id: str,
        limit: int | None = None,
    ) -> TranscriptRead: ...


@dataclass(frozen=True, slots=True)
class LspDocumentState:
    version: int
    content_hash: str
    position_encoding: str


class LspDocumentStateReader(Protocol):
    def __call__(self, *, file_path: Path, workspace: Path) -> LspDocumentState | None: ...


class LspDiagnostics(Protocol):
    def request_diagnostics(self, *, file_path: str, workspace: str) -> dict[str, object]: ...


class LspRequestError(ValueError):
    """A failure reported by a host-owned LSP request capability."""


class LspResponse(Protocol):
    @property
    def response(self) -> dict[str, object]: ...
    @property
    def document_state(self) -> LspDocumentState | None: ...

    @property
    def position_encoding(self) -> str: ...


class LspRequester(Protocol):
    def __call__(
        self,
        *,
        server_name: str | None,
        method: str,
        params: dict[str, object],
        workspace: Path,
    ) -> LspResponse: ...


class McpCallResult(Protocol):
    @property
    def content(self) -> list[dict[str, object]]: ...

    @property
    def structured_content(self) -> Mapping[str, object] | None: ...

    @property
    def is_error(self) -> bool: ...


class McpRequester(Protocol):
    def __call__(
        self,
        *,
        server_name: str,
        tool_name: str,
        arguments: dict[str, object],
        workspace: Path,
    ) -> McpCallResult: ...


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Explicit invocation facts and the host-owned resources used by a tool."""

    workspace: Path | None = None
    session_id: str | None = None
    run_id: str | None = None
    invocation_id: str | None = None
    parent_session_id: str | None = None
    delegation_depth: int = 0
    remaining_spawn_budget: int | None = None
    read_paths: frozenset[str] = frozenset()
    read_lines: Mapping[tuple[str, str], frozenset[int]] = MappingProxyType({})
    read_whole_files: frozenset[tuple[str, str]] = frozenset()
    read_hash: str | None = None
    model: str | None = None
    todo_phases: tuple[TodoPhase, ...] = ()
    goal: Mapping[str, object] | None = None
    goal_consecutive_blocked_turns: int = 0
    tool_timeout_seconds: int | None = None
    abort_signal: ProviderAbortSignal | None = None
    emit_tool_progress: Callable[[Mapping[str, object]], None] | None = None
    lsp: LspDiagnostics | None = None
    lsp_request: LspRequester | None = None
    lsp_document_state: LspDocumentStateReader | None = None
    mcp_request: McpRequester | None = None
    read_rule: RuleReader | None = None
    resolve_skill: Callable[[str], SkillMetadata] | None = None
    tool_catalog: ToolCatalog | None = None
    artifact: ArtifactReader | None = None
    transcript: TranscriptReader | None = None
    task_runtime: ToolCommandHandler | None = None
    task_batch_runtime: ToolCommandHandler | None = None
    process_runtime: ToolCommandHandler | None = None
    lsp_diagnostics_on_write: bool = False

    def require_workspace(self) -> Path:
        if self.session_id is not None:
            self.require_session_id()
        if self.workspace is None:
            raise RuntimeError("tool requires an explicit workspace")
        return self.workspace

    def require_session_id(self) -> str:
        if not self.session_id:
            raise RuntimeError("tool requires an explicit session identity")
        return self.session_id
