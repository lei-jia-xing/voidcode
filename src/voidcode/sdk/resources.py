"""Resource readers: typed ``scheme://`` handlers for the ``read`` tool.

A reader is an ordinary plugin registration, not a core builtin: the runtime
owns the ``scheme -> (handler, owner)`` map, rejects duplicate schemes at
admission, and dispatches each ``scheme://`` URI to its owner's handler under
a provider-owner sub-lease. The caller session, workspace, revision, and
stance flow unchanged, so lineage and visibility gates apply as for a direct
backend call. Child and process projections remain bound to the caller's
authorized scope, not the provider owner's ambient visibility.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

from .json_values import own_json_object
from .tools import AttachmentOutput, CompositeOutput, TextOutput

if TYPE_CHECKING:
    from .provider import ProviderAbortSignal
    from .services import ArtifactPage, ChildSessionSnapshot, HistoryPageView, ProcessOutput
    from .tools import ToolDefinition


@dataclass(frozen=True, slots=True)
class ResourceProcessStatus:
    """Path-free status for one supervised process owned by this reader and caller session."""

    process_id: str
    running: bool
    exit_code: int | None

    def __post_init__(self) -> None:
        if not isinstance(self.process_id, str) or not self.process_id:
            raise ValueError("resource process status requires a non-empty process id")
        if type(self.running) is not bool:
            raise ValueError("resource process status running must be a boolean")
        if self.exit_code is not None and type(self.exit_code) is not int:
            raise ValueError("resource process status exit_code must be an integer or None")
        if self.running and self.exit_code is not None:
            raise ValueError("a running resource process cannot have an exit code")


_READER_SCHEME_PATTERN = re.compile(r"^[a-z][a-z0-9+.-]*$")
_URI_SCHEME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*$")


class ResourceContractError(ValueError):
    """A reader answered, but its page violated the resource contract.

    Raised by the runtime dispatcher when a handler returns the wrong shape
    (not a ``ResourcePage`` or ``None``, wrong output type, non-object body).
    Handler bugs and backend failures surface as their own exceptions, never
    as this error; ``None`` means "no such resource", never malformed.
    """


class ResourceUnavailable(ValueError):
    """No admitted plugin in this runtime generation reads the URI scheme."""


@dataclass(frozen=True, slots=True)
class ResourceReadRequest:
    """One typed resource read: the URI plus generic paging cursors."""

    uri: str
    offset: int = 1
    #: Absent when the caller sets no limit; each reader applies its own default.
    limit: int | None = None
    byte_offset: int | None = None
    byte_limit: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.uri, str) or not self.uri.strip():
            raise ValueError("resource URI must be a non-empty string")
        scheme, sep, _rest = self.uri.partition("://")
        if not sep or _URI_SCHEME_PATTERN.fullmatch(scheme) is None:
            raise ValueError(f"unsupported resource URI: {self.uri}")
        if type(self.offset) is not int or self.offset < 1:
            raise ValueError("resource read offset must be a positive integer")
        if self.limit is not None and (type(self.limit) is not int or self.limit < 1):
            raise ValueError("resource read limit must be a positive integer or None")
        if self.byte_offset is not None and (type(self.byte_offset) is not int or self.byte_offset < 0):
            raise ValueError("resource read byte_offset must be a non-negative integer or None")
        if self.byte_limit is not None and (type(self.byte_limit) is not int or self.byte_limit < 1):
            raise ValueError("resource read byte_limit must be a positive integer or None")
        if self.byte_limit is not None and self.byte_offset is None:
            raise ValueError("byte_limit requires byte_offset")

    @property
    def scheme(self) -> str:
        """The lowercased URI scheme used for runtime lookup."""
        scheme, _, _ = self.uri.partition("://")
        return scheme.lower()


@dataclass(frozen=True, slots=True)
class ResourceReadCaller:
    """The lease facts the runtime injects into a reader."""

    session_id: str
    workspace: Path
    revision_id: str
    run_id: str
    abort_signal: ProviderAbortSignal | None = None

    def __post_init__(self) -> None:
        if isinstance(self.workspace, str):
            object.__setattr__(self, "workspace", Path(self.workspace))
        if not isinstance(self.session_id, str) or not self.session_id:
            raise ValueError("resource caller requires a non-empty session id")
        if not isinstance(self.workspace, Path) or not str(self.workspace):
            raise ValueError("resource caller requires an explicit workspace")
        if not isinstance(self.revision_id, str) or not self.revision_id:
            raise ValueError("resource caller requires an explicit revision id")
        if not isinstance(self.run_id, str) or not self.run_id:
            raise ValueError("resource caller requires a non-empty run id")


@dataclass(frozen=True, slots=True)
class ResourcePage:
    """One bounded resource page reusing the tool output shapes."""

    output: TextOutput | AttachmentOutput | CompositeOutput
    body: Mapping[str, object]

    def __post_init__(self) -> None:
        if not isinstance(self.output, (TextOutput, AttachmentOutput, CompositeOutput)):
            raise ValueError("resource page output must be TextOutput, AttachmentOutput, or CompositeOutput")
        if not isinstance(self.body, Mapping):
            raise ValueError("resource page body must be a JSON object")
        object.__setattr__(self, "body", own_json_object(self.body))


@dataclass(frozen=True, slots=True)
class ResourceSchemeCaps:
    """Declared capabilities of one ``scheme://`` namespace: what readers may route to it."""

    scheme: str
    enumerable: bool
    searchable: bool
    line_paging: bool
    byte_paging: bool
    inventory_uri: str | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.scheme, str) or _READER_SCHEME_PATTERN.fullmatch(self.scheme) is None:
            raise ValueError(f"invalid resource scheme: {self.scheme!r}")
        for name in ("enumerable", "searchable", "line_paging", "byte_paging"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"resource scheme capabilities {name} must be a boolean")
        if self.inventory_uri is not None and (not isinstance(self.inventory_uri, str) or not self.inventory_uri.startswith(f"{self.scheme}://")):
            raise ValueError("resource capabilities inventory_uri must be None or a URI in this scheme")
        if not isinstance(self.detail, str) or not self.detail.strip():
            raise ValueError("resource scheme capabilities require a non-empty detail note")


@dataclass(frozen=True, slots=True)
class ResourceEnumRequest:
    """One enumeration of a scope URI plus a 1-based match cursor."""

    uri: str
    offset: int = 1
    limit: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.uri, str) or not self.uri.strip():
            raise ValueError("resource URI must be a non-empty string")
        scheme, sep, _rest = self.uri.partition("://")
        if not sep or _URI_SCHEME_PATTERN.fullmatch(scheme) is None:
            raise ValueError(f"unsupported resource URI: {self.uri}")
        if type(self.offset) is not int or self.offset < 1:
            raise ValueError("resource enumerate offset must be a positive integer")
        if self.limit is not None and (type(self.limit) is not int or self.limit < 1):
            raise ValueError("resource enumerate limit must be a positive integer or None")

    @property
    def scheme(self) -> str:
        """The lowercased URI scheme used for runtime lookup."""
        scheme, _, _ = self.uri.partition("://")
        return scheme.lower()


@dataclass(frozen=True, slots=True)
class ResourceEnumEntry:
    """One enumerated scope member: its URI plus an optional display label."""

    uri: str
    label: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.uri, str) or not self.uri.strip():
            raise ValueError("resource enum entry requires a non-empty URI")
        scheme, sep, _rest = self.uri.partition("://")
        if not sep or _URI_SCHEME_PATTERN.fullmatch(scheme) is None:
            raise ValueError(f"unsupported resource URI: {self.uri}")
        if self.label is not None and not isinstance(self.label, str):
            raise ValueError("resource enum entry label must be text or null")


@dataclass(frozen=True, slots=True)
class ResourceEnumPage:
    """One bounded enumeration page: entries plus the continuation cursor (None = end)."""

    entries: tuple[ResourceEnumEntry, ...]
    next_offset: int | None
    total_count: int | None = None

    def __post_init__(self) -> None:
        entries = tuple(self.entries)
        object.__setattr__(self, "entries", entries)
        if any(not isinstance(entry, ResourceEnumEntry) for entry in entries):
            raise ValueError("resource enum page entries must be ResourceEnumEntry values")
        if self.next_offset is not None and (type(self.next_offset) is not int or self.next_offset < 1):
            raise ValueError("resource enum page next_offset must be a positive integer or None")
        if self.total_count is not None and (type(self.total_count) is not int or self.total_count < 0):
            raise ValueError("resource enum page total_count must be a non-negative integer or None")


@dataclass(frozen=True, slots=True)
class ResourceSearchRequest:
    """One scoped pattern search plus a 1-based match cursor."""

    uri: str
    pattern: str
    regex: bool = False
    ignore_case: bool = False
    offset: int = 1
    limit: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.uri, str) or not self.uri.strip():
            raise ValueError("resource URI must be a non-empty string")
        scheme, sep, _rest = self.uri.partition("://")
        if not sep or _URI_SCHEME_PATTERN.fullmatch(scheme) is None:
            raise ValueError(f"unsupported resource URI: {self.uri}")
        if not isinstance(self.pattern, str) or not self.pattern:
            raise ValueError("resource search pattern must be a non-empty string")
        for name in ("regex", "ignore_case"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"resource search {name} must be a boolean")
        if type(self.offset) is not int or self.offset < 1:
            raise ValueError("resource search offset must be a positive integer")
        if self.limit is not None and (type(self.limit) is not int or self.limit < 1):
            raise ValueError("resource search limit must be a positive integer or None")

    @property
    def scheme(self) -> str:
        """The lowercased URI scheme used for runtime lookup."""
        scheme, _, _ = self.uri.partition("://")
        return scheme.lower()


@dataclass(frozen=True, slots=True)
class ResourceSearchMatch:
    """One bounded match line plus whichever address its scope uses."""

    uri: str
    text: str
    line: int | None = None
    byte_offset: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.uri, str) or not self.uri.strip():
            raise ValueError("resource search match requires a non-empty URI")
        scheme, sep, _rest = self.uri.partition("://")
        if not sep or _URI_SCHEME_PATTERN.fullmatch(scheme) is None:
            raise ValueError(f"unsupported resource URI: {self.uri}")
        if not isinstance(self.text, str):
            raise ValueError("resource search match text must be a string")
        if self.line is not None and (type(self.line) is not int or self.line < 1):
            raise ValueError("resource search match line must be a positive integer or None")
        if self.byte_offset is not None and (type(self.byte_offset) is not int or self.byte_offset < 0):
            raise ValueError("resource search match byte_offset must be a non-negative integer or None")


@dataclass(frozen=True, slots=True)
class ResourceSearchPage:
    """One bounded search page: matches plus the continuation cursor (None = end)."""

    matches: tuple[ResourceSearchMatch, ...]
    next_offset: int | None
    truncated: bool = False

    def __post_init__(self) -> None:
        matches = tuple(self.matches)
        object.__setattr__(self, "matches", matches)
        if any(not isinstance(match, ResourceSearchMatch) for match in matches):
            raise ValueError("resource search page matches must be ResourceSearchMatch values")
        if self.next_offset is not None and (type(self.next_offset) is not int or self.next_offset < 1):
            raise ValueError("resource search page next_offset must be a positive integer or None")
        if type(self.truncated) is not bool:
            raise ValueError("resource search page truncated must be a boolean")


class ResourceUnsupported(ValueError):
    """A known scheme does not offer the requested capability."""


@runtime_checkable
class ResourceReaderServices(Protocol):
    """The narrow read surface a resource handler may use.

    Owner-scoped (the registering provider), session-bound (the caller):
    ``get_state`` reads the provider's own state, while artifact, catalog,
    child-result, history, and process reads see only the caller-scoped data.
    There are no staging, spawn, process-control, or network methods to misuse.
    """

    @property
    def owner(self) -> str:
        """The registering provider bound to this reader sub-lease."""
        ...

    def get_state(self, scope: Literal["session", "workspace"] = "session") -> tuple[Mapping[str, object] | None, int]:
        """Fetch current state and version for the bound owner in the given scope."""
        ...

    def read_artifact(self, sha256: str, *, offset: int = 0, limit: int = 65536) -> ArtifactPage:
        """Read one byte page by SHA-256 digest; ``FileNotFoundError`` means not stored."""
        ...

    def tool_definition(self, name: str) -> ToolDefinition | None:
        """The definition of ``name`` with this session's visibility applied."""
        ...

    def read_session_history(self, session_id: str, *, after_sequence: int = 0, offset: int = 0, limit: int = 100) -> HistoryPageView:
        """Render one bounded Markdown transcript page for the bound session or an owned child."""
        ...

    def read_child_snapshot(self, child_session_id: str) -> ChildSessionSnapshot | None:
        """Read one visible descendant's current status and latest eligible output without joining or acknowledging.

        ``latest_outcome`` skips failed, interrupted, cancelled, empty, and preview-only
        publications; current status/run identity stays separate. ``None`` covers both
        nonexistent and out-of-scope ids, so this read is not an existence oracle.
        """
        ...

    def list_processes(self) -> tuple[ResourceProcessStatus, ...]:
        """List only this reader owner's supervised processes in the caller session; paths are omitted."""
        ...

    def read_process_output(
        self,
        process_id: str,
        *,
        stream: Literal["stdout", "stderr"] = "stdout",
        offset: int = 0,
        limit: int = 65536,
    ) -> ProcessOutput:
        """Read one owner- and caller-session-scoped page without binding service cancellation or staging effects."""
        ...


@dataclass(frozen=True, slots=True)
class ResourceReader:
    """One ``scheme://`` namespace an admitted plugin serves.

    The handler runs synchronously inside the caller's invocation under a
    provider-owner, read-only sub-lease. It returns a bounded page, or
    ``None`` for "no such resource" (the caller renders a model-visible
    failure). Unknown schemes never reach a handler; malformed pages raise
    ``ResourceContractError`` from the dispatcher.
    """

    scheme: str
    handler: Callable[[ResourceReadRequest, ResourceReadCaller, ResourceReaderServices], ResourcePage | None]
    capabilities: ResourceSchemeCaps | None = None
    enumerate: Callable[[ResourceEnumRequest, ResourceReadCaller, ResourceReaderServices], ResourceEnumPage | None] | None = None
    search: Callable[[ResourceSearchRequest, ResourceReadCaller, ResourceReaderServices], ResourceSearchPage | None] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.scheme, str) or _READER_SCHEME_PATTERN.fullmatch(self.scheme) is None:
            raise ValueError(f"invalid resource scheme: {self.scheme!r}")
        if not callable(self.handler):
            raise ValueError(f"resource reader {self.scheme!r} handler must be callable")
        if self.capabilities is not None and not isinstance(self.capabilities, ResourceSchemeCaps):
            raise ValueError(f"resource reader {self.scheme!r} capabilities must be ResourceSchemeCaps or None")
        for name in ("enumerate", "search"):
            if getattr(self, name) is not None and not callable(getattr(self, name)):
                raise ValueError(f"resource reader {self.scheme!r} {name} must be callable or None")
