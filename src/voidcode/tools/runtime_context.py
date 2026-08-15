from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol

from ..provider.protocol import ProviderAbortSignal
from ..runtime.memory import MemoryKind, MemoryRecord, MemorySearchResult
from .contracts import ToolDefinition


class RuntimeMemoryToolFacade(Protocol):
    def add_memory(
        self,
        *,
        content: str,
        kind: MemoryKind = "project",
        tags: tuple[str, ...] = (),
        source_session_id: str | None = None,
    ) -> MemoryRecord: ...

    def list_memories(self, *, include_deleted: bool = False) -> tuple[MemoryRecord, ...]: ...

    def search_memories(self, *, query: str) -> tuple[MemorySearchResult, ...]: ...

    def delete_memory(self, memory_id: str) -> MemoryRecord: ...


class RuntimeLspToolFacade(Protocol):
    def request_diagnostics(
        self,
        *,
        file_path: str,
        workspace: str,
    ) -> dict[str, object]: ...


class RuntimeToolCatalogFacade(Protocol):
    """Read-only view of the currently materialized tool registry."""

    def lookup(self, tool_name: str) -> ToolDefinition | None: ...


class RuntimeArtifactReadFacade(Protocol):
    """Session-validated reader for spilled tool-output artifacts.

    Resolves ``voidcode://artifact/<id>`` against the calling session's own
    transcript through the runtime's session/workspace/path guards, never
    through the external-directory permission path. Returns ``None`` when the
    artifact id does not exist in the caller session.
    """

    def read_artifact(
        self,
        *,
        artifact_id: str,
        offset: int | None = None,
        limit: int | None = None,
    ) -> dict[str, object] | None:
        """Return a bounded artifact slice or ``None`` when not found in the caller session."""
        ...


class RuntimeTranscriptFacade(Protocol):
    """Lineage-guarded reader for bounded, payload-stripped session transcripts.

    Resolves ``voidcode://transcript/<session_id>`` against the calling
    session's id from the runtime tool context. A session may read its own
    transcript or the transcript of a session it spawned (direct child, per
    the persisted session parent linkage or the background-task parent/child
    linkage). Returns ``None`` when the target is not accessible (unknown,
    unrelated, or wrong workspace).
    """

    def read_transcript(
        self,
        *,
        session_id: str,
        limit: int | None = None,
    ) -> dict[str, object] | None:
        """Return a bounded payload-stripped transcript or ``None`` when not accessible."""
        ...


@dataclass(frozen=True, slots=True)
class RuntimeToolInvocationContext:
    session_id: str
    parent_session_id: str | None = None
    delegation_depth: int = 0
    remaining_spawn_budget: int | None = None
    read_paths: frozenset[str] = frozenset()
    read_lines: Mapping[str, frozenset[int]] = MappingProxyType({})
    model: str | None = None
    abort_signal: ProviderAbortSignal | None = None
    emit_tool_progress: Callable[[Mapping[str, object]], None] | None = None
    memory: RuntimeMemoryToolFacade | None = None
    lsp: RuntimeLspToolFacade | None = None
    #: Read-only registry view for on-demand tool documentation (``voidcode://tool/<name>``).
    tool_catalog: RuntimeToolCatalogFacade | None = None
    #: Session-validated reader for spilled tool-output artifacts (``voidcode://artifact/<id>``).
    artifact: RuntimeArtifactReadFacade | None = None
    #: Lineage-guarded reader for bounded session transcripts (``voidcode://transcript/<session_id>``).
    transcript: RuntimeTranscriptFacade | None = None
    #: Opt-in gate for automatic post-write LSP diagnostics (default off).
    lsp_diagnostics_on_write: bool = False


_CURRENT_RUNTIME_TOOL_CONTEXT: ContextVar[RuntimeToolInvocationContext | None] = ContextVar(
    "voidcode_runtime_tool_context",
    default=None,
)


@contextmanager
def bind_runtime_tool_context(
    context: RuntimeToolInvocationContext,
) -> Iterator[None]:
    token: Token[RuntimeToolInvocationContext | None] = _CURRENT_RUNTIME_TOOL_CONTEXT.set(context)
    try:
        yield
    finally:
        _CURRENT_RUNTIME_TOOL_CONTEXT.reset(token)


def current_runtime_tool_context() -> RuntimeToolInvocationContext | None:
    return _CURRENT_RUNTIME_TOOL_CONTEXT.get()


def require_runtime_tool_context(tool_name: str) -> RuntimeToolInvocationContext:
    context = current_runtime_tool_context()
    if context is None:
        raise RuntimeError(f"{tool_name} requires an active runtime tool invocation context")
    return context
