"""Resource readers: typed ``scheme://`` handlers for the ``read`` tool.

A reader is an ordinary plugin registration, not a core builtin: the runtime
owns the ``scheme -> (handler, owner)`` map, rejects duplicate schemes at
admission, and dispatches each ``scheme://`` URI to its owner's handler under
a provider-owner sub-lease. The caller session, workspace, revision, and
stance flow unchanged, so lineage and visibility gates apply as for a direct
backend call.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

from .json_values import own_json_object
from .tools import AttachmentOutput, TextOutput

if TYPE_CHECKING:
    from .history import HistoryPage
    from .provider import ProviderAbortSignal
    from .services import ArtifactPage
    from .tools import ToolDefinition

#: Registered schemes are lowercase URI schemes; incoming URIs may use any case.
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

    output: TextOutput | AttachmentOutput
    body: Mapping[str, object]

    def __post_init__(self) -> None:
        if not isinstance(self.output, (TextOutput, AttachmentOutput)):
            raise ValueError("resource page output must be TextOutput or AttachmentOutput")
        if not isinstance(self.body, Mapping):
            raise ValueError("resource page body must be a JSON object")
        object.__setattr__(self, "body", own_json_object(self.body))


@runtime_checkable
class ResourceReaderServices(Protocol):
    """The narrow read surface a resource handler may use.

    Owner-scoped (the registering provider), session-bound (the caller):
    ``get_state`` reads the provider's own state, while artifact, catalog,
    and history reads see exactly what the caller session may see. There are
    no staging, spawn, process, or network methods to misuse.
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

    def read_session_history(self, session_id: str, *, after_sequence: int = 0, offset: int = 0, limit: int = 100) -> HistoryPage:
        """Render one bounded Markdown transcript page for the bound session or an owned child."""
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

    def __post_init__(self) -> None:
        if not isinstance(self.scheme, str) or _READER_SCHEME_PATTERN.fullmatch(self.scheme) is None:
            raise ValueError(f"invalid resource scheme: {self.scheme!r}")
        if not callable(self.handler):
            raise ValueError(f"resource reader {self.scheme!r} handler must be callable")
