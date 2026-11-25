"""Typed workspace path-completion query owned by the files capability.

The TUI asks this contract for ``@path`` candidates; it never reads the local
file system itself. The answer is workspace-relative because a remote client's
process has no access to the server workspace. A query names the exact request
it answers, so an out-of-order reply is recognisable even before the client's
own epoch check.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Literal, Protocol, Self

from voidcode.capabilities.files._common import DEFAULT_IGNORE_PATTERNS, is_default_ignored
from voidcode.capabilities.files.gitignore import GitIgnoreMatcher
from voidcode.sdk.plugin import PluginQuery, PluginQueryCaller
from voidcode.sdk.services import QueryContractError

PATH_COMPLETION_QUERY = "workspace.path_completion"

type PathKind = Literal["file", "directory"]
type PathMatchMode = Literal["prefix", "fuzzy"]

MAX_PATH_COMPLETION_QUERY_CHARS = 1024
MAX_PATH_COMPLETION_QUERY_BYTES = 4096
MAX_PATH_COMPLETION_LIMIT = 200
MAX_PATH_COMPLETION_DEPTH = 16
MAX_PATH_COMPLETION_ENTRY_BYTES = 4096
MAX_PATH_COMPLETION_RESULT_BYTES = 64 * 1024
_MAX_ENTRY_WIRE_OVERHEAD = 48


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _reject_reason(name: str) -> QueryContractError:
    return QueryContractError(f"query {name!r} answered outside its request/answer contract")


class PathCompletionTransport(Protocol):
    """The read-only transport a consumer uses to ask the named query."""

    def query(self, name: str, arguments: Mapping[str, object] | None = None) -> Mapping[str, object] | None: ...


@dataclass(frozen=True, slots=True)
class PathCompletionRequest:
    """One bounded, workspace-relative path-completion request.

    ``query`` is the draft prefix after ``@`` (or a bare path token), never a
    server path. ``depth=1`` lists only the requested directory; larger values
    search that many directory levels below it. ``mode="prefix"`` matches the
    entry name; ``mode="fuzzy"`` matches the ordered subsequence against the
    path below the requested directory, so recursive fuzzy queries can select
    nested files by their relative path.
    """

    query: str = ""
    mode: PathMatchMode = "prefix"
    depth: int = 1
    limit: int = 50
    include_hidden: bool = False
    respect_gitignore: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.query, str):
            raise ValueError("query must be a string")
        if "\x00" in self.query:
            raise ValueError("query must not contain NUL")
        if len(self.query) > MAX_PATH_COMPLETION_QUERY_CHARS or len(self.query.encode("utf-8")) > MAX_PATH_COMPLETION_QUERY_BYTES:
            raise ValueError(f"query must be within {MAX_PATH_COMPLETION_QUERY_CHARS} characters and {MAX_PATH_COMPLETION_QUERY_BYTES} bytes")
        if self.mode not in ("prefix", "fuzzy"):
            raise ValueError("mode must be 'prefix' or 'fuzzy'")
        if not _is_int(self.depth) or not 1 <= self.depth <= MAX_PATH_COMPLETION_DEPTH:
            raise ValueError(f"depth must be an integer from 1 to {MAX_PATH_COMPLETION_DEPTH}")
        if not _is_int(self.limit) or not 1 <= self.limit <= MAX_PATH_COMPLETION_LIMIT:
            raise ValueError(f"limit must be an integer from 1 to {MAX_PATH_COMPLETION_LIMIT}")
        if not isinstance(self.include_hidden, bool) or not isinstance(self.respect_gitignore, bool):
            raise ValueError("include_hidden and respect_gitignore must be booleans")

    @classmethod
    def from_payload(cls, value: Mapping[str, object]) -> Self:
        """Decode strict ingress: unknown keys, loose types, and out-of-range values fail."""
        allowed = {"query", "mode", "depth", "limit", "include_hidden", "respect_gitignore"}
        unknown = set(value) - allowed
        if unknown:
            raise ValueError(f"unknown path completion request fields: {', '.join(sorted(unknown))}")
        query = value.get("query", "")
        mode = value.get("mode", "prefix")
        depth = value.get("depth", 1)
        limit = value.get("limit", 50)
        include_hidden = value.get("include_hidden", False)
        respect_gitignore = value.get("respect_gitignore", True)
        if not isinstance(query, str):
            raise ValueError("query must be a string")
        if not isinstance(mode, str) or mode not in ("prefix", "fuzzy"):
            raise ValueError("mode must be 'prefix' or 'fuzzy'")
        if not isinstance(depth, int) or isinstance(depth, bool):
            raise ValueError("depth must be an integer")
        if not isinstance(limit, int) or isinstance(limit, bool):
            raise ValueError("limit must be an integer")
        if not isinstance(include_hidden, bool) or not isinstance(respect_gitignore, bool):
            raise ValueError("include_hidden and respect_gitignore must be booleans")
        return cls(
            query=query,
            mode=mode,
            depth=depth,
            limit=limit,
            include_hidden=include_hidden,
            respect_gitignore=respect_gitignore,
        )

    def as_payload(self) -> dict[str, object]:
        return {
            "query": self.query,
            "mode": self.mode,
            "depth": self.depth,
            "limit": self.limit,
            "include_hidden": self.include_hidden,
            "respect_gitignore": self.respect_gitignore,
        }


@dataclass(frozen=True, slots=True)
class PathCompletionEntry:
    """One workspace-relative candidate; directories keep the trailing ``/``."""

    path: str
    kind: PathKind

    def __post_init__(self) -> None:
        if not isinstance(self.path, str) or not self.path:
            raise ValueError("path completion entry path must be non-empty text")
        if "\x00" in self.path or self.path.startswith("/"):
            raise ValueError("path completion entry path must be workspace-relative without NUL")
        if len(self.path.encode("utf-8")) > MAX_PATH_COMPLETION_ENTRY_BYTES:
            raise ValueError("path completion entry path is too long")
        bare = self.path[:-1] if self.kind == "directory" else self.path
        if not bare or PurePath(bare).is_absolute() or ".." in PurePath(bare).parts:
            raise ValueError("path completion entry path must stay inside the workspace")
        if self.kind not in ("file", "directory"):
            raise ValueError("path completion entry kind must be 'file' or 'directory'")
        if (self.kind == "directory") != self.path.endswith("/"):
            raise ValueError("directory candidates must end with '/' and file candidates must not")

    def as_payload(self) -> dict[str, object]:
        return {"path": self.path, "kind": self.kind}


@dataclass(frozen=True, slots=True)
class PathCompletionResult:
    """One bounded answer correlated with its exact request."""

    query: str
    entries: tuple[PathCompletionEntry, ...]
    truncated: bool

    def __post_init__(self) -> None:
        if not isinstance(self.query, str):
            raise ValueError("query must be a string")
        if not isinstance(self.truncated, bool):
            raise ValueError("truncated must be a boolean")
        if any(not isinstance(entry, PathCompletionEntry) for entry in self.entries):
            raise ValueError("entries must be path completion entries")

    @classmethod
    def from_payload(cls, value: Mapping[str, object], request: PathCompletionRequest) -> Self:
        query = value.get("query")
        entries_value = value.get("entries")
        truncated = value.get("truncated")
        if query != request.query:
            raise _reject_reason(PATH_COMPLETION_QUERY)
        if not isinstance(entries_value, Sequence) or isinstance(entries_value, (str, bytes, bytearray)) or not isinstance(truncated, bool):
            raise _reject_reason(PATH_COMPLETION_QUERY)
        if len(entries_value) > request.limit:
            raise _reject_reason(PATH_COMPLETION_QUERY)
        entries: list[PathCompletionEntry] = []
        total = 64 + len(request.query.encode("utf-8"))
        for raw in entries_value:
            if not isinstance(raw, Mapping):
                raise _reject_reason(PATH_COMPLETION_QUERY)
            path = raw.get("path")
            kind = raw.get("kind")
            if kind not in ("file", "directory") or not isinstance(path, str):
                raise _reject_reason(PATH_COMPLETION_QUERY)
            try:
                entry = PathCompletionEntry(path=path, kind=kind)
            except ValueError as exc:
                raise _reject_reason(PATH_COMPLETION_QUERY) from exc
            entries.append(entry)
            total += len(entry.path.encode("utf-8")) + _MAX_ENTRY_WIRE_OVERHEAD
        if total > MAX_PATH_COMPLETION_RESULT_BYTES:
            raise _reject_reason(PATH_COMPLETION_QUERY)
        return cls(query=request.query, entries=tuple(entries), truncated=truncated)

    def as_payload(self) -> dict[str, object]:
        return {"query": self.query, "entries": [entry.as_payload() for entry in self.entries], "truncated": self.truncated}


class _PathCompletionQuery:
    name = PATH_COMPLETION_QUERY

    def ask(self, transport: PathCompletionTransport, request: PathCompletionRequest) -> PathCompletionResult:
        value = transport.query(self.name, request.as_payload())
        if value is None:
            # A list query answers with an empty list, never "no such resource".
            raise _reject_reason(self.name)
        if not isinstance(value, Mapping):
            raise _reject_reason(self.name)
        return PathCompletionResult.from_payload(value, request)

    def bind(self, handler: Callable[[PathCompletionRequest, PluginQueryCaller], PathCompletionResult]) -> PluginQuery:
        def invoke(arguments: Mapping[str, object], caller: PluginQueryCaller) -> Mapping[str, object] | None:
            request = PathCompletionRequest.from_payload(arguments)
            return handler(request, caller).as_payload()

        return PluginQuery(name=self.name, handler=invoke)


PATH_COMPLETION = _PathCompletionQuery()


def _normalized_parts(query: str) -> tuple[tuple[str, ...], str] | None:
    """Split a draft prefix into requested directory components and the last fragment.

    Leading ``/`` and repeated separators are harmless because the query is
    workspace-relative. ``..`` is refused rather than resolved: completion is
    never an escape hatch from the server workspace.
    """
    if "\x00" in query:
        return None
    raw_parts = query.split("/")
    if any(part == ".." for part in raw_parts):
        return None
    if query.endswith("/"):
        return tuple(part for part in raw_parts if part not in ("", ".")), ""
    fragment = raw_parts[-1]
    directory_parts = tuple(part for part in raw_parts[:-1] if part not in ("", "."))
    return directory_parts, fragment


def _hidden_requested(query: str, directory_parts: tuple[str, ...], fragment: str) -> bool:
    return fragment.startswith(".") or any(part.startswith(".") for part in directory_parts) or query.startswith(".")


def _fuzzy_rank(name: str, fragment: str) -> tuple[int, int, int] | None:
    """Ordered-subsequence match with a deterministic, preference-preserving rank."""
    haystack = name.casefold()
    needle = fragment.casefold()
    if not needle:
        return (0, 0, len(haystack))
    start: int | None = None
    gaps = 0
    cursor = 0
    previous = -1
    for character in needle:
        position = haystack.find(character, cursor)
        if position < 0:
            return None
        if start is None:
            start = position
        elif previous >= 0:
            gaps += position - previous - 1
        previous = position
        cursor = position + 1
    assert start is not None
    return (start, gaps, len(haystack))


def _match_rank(name: str, fragment: str, mode: PathMatchMode) -> tuple[int, int, int] | None:
    if mode == "fuzzy":
        return _fuzzy_rank(name, fragment)
    if not name.casefold().startswith(fragment.casefold()):
        return None
    return (0, 0, 0)


@dataclass(frozen=True, slots=True)
class _Matched:
    path: str
    kind: PathKind
    rank: tuple[int, int, int]


def _entry_target(entry: Path, workspace: Path) -> bool:
    """True when a listed entry resolves inside the workspace (symlinks included)."""
    try:
        return entry.resolve(strict=False).is_relative_to(workspace)
    except OSError:
        return False


def _directory_allowed(relative: str, *, gitignore: GitIgnoreMatcher | None) -> bool:
    if is_default_ignored(PurePath(relative), is_directory=True):
        return False
    if gitignore is not None and gitignore.is_ignored(relative, is_directory=True):
        return False
    return True


def _collect(
    *,
    workspace: Path,
    base: Path,
    base_parts: tuple[str, ...],
    fragment: str,
    request: PathCompletionRequest,
    gitignore: GitIgnoreMatcher | None,
    allow_hidden: bool,
    matched: list[_Matched],
) -> None:
    stack: list[tuple[Path, tuple[str, ...], int]] = [(base, base_parts, 0)]
    while stack:
        directory, parts, level = stack.pop()
        try:
            entries = list(directory.iterdir())
        except OSError:
            continue
        for entry in entries:
            name = entry.name
            if not name:
                continue
            if not allow_hidden and name.startswith("."):
                continue
            try:
                is_directory = entry.is_dir()
            except OSError:
                continue
            relative_parts = (*parts, name)
            relative = "/".join(relative_parts)
            if not _entry_target(entry, workspace):
                # A symlink that leaves the workspace is not a candidate at all:
                # showing it would invite a completion that can never resolve.
                continue
            if is_directory:
                if not _directory_allowed(relative, gitignore=gitignore):
                    continue
                path = f"{relative}/"
            else:
                if gitignore is not None and gitignore.is_ignored(relative, is_directory=False):
                    continue
                path = relative
            match_target = name if request.mode == "prefix" else "/".join(relative_parts[len(base_parts) :])
            rank = _match_rank(match_target, fragment, request.mode)
            if rank is not None:
                matched.append(_Matched(path=path, kind="directory" if is_directory else "file", rank=rank))
            if is_directory and level + 1 < request.depth and not entry.is_symlink():
                stack.append((entry, relative_parts, level + 1))


def complete_workspace_paths(request: PathCompletionRequest, caller: PluginQueryCaller) -> PathCompletionResult:
    """Answer one bounded workspace-relative completion request."""
    workspace = caller.workspace.resolve()
    normalized = _normalized_parts(request.query)
    if normalized is None:
        return PathCompletionResult(query=request.query, entries=(), truncated=False)
    directory_parts, fragment = normalized
    if directory_parts and any(part in DEFAULT_IGNORE_PATTERNS for part in directory_parts):
        return PathCompletionResult(query=request.query, entries=(), truncated=False)
    base = workspace.joinpath(*directory_parts)
    try:
        resolved = base.resolve(strict=False)
    except OSError:
        return PathCompletionResult(query=request.query, entries=(), truncated=False)
    if not resolved.is_relative_to(workspace) or not resolved.is_dir():
        return PathCompletionResult(query=request.query, entries=(), truncated=False)
    relative_base = "/".join(directory_parts)
    gitignore = GitIgnoreMatcher.load(workspace) if request.respect_gitignore else None
    if gitignore is not None and relative_base and gitignore.is_ignored(relative_base, is_directory=True):
        return PathCompletionResult(query=request.query, entries=(), truncated=False)
    allow_hidden = request.include_hidden or _hidden_requested(request.query, directory_parts, fragment)
    matched: list[_Matched] = []
    _collect(
        workspace=workspace,
        base=resolved,
        base_parts=directory_parts,
        fragment=fragment,
        request=request,
        gitignore=gitignore,
        allow_hidden=allow_hidden,
        matched=matched,
    )
    matched.sort(key=lambda item: (0 if item.kind == "directory" else 1, item.rank, item.path.casefold(), item.path))
    entries: list[PathCompletionEntry] = []
    used_bytes = 64 + len(request.query.encode("utf-8"))
    truncated = len(matched) > request.limit
    for item in matched[: request.limit]:
        cost = len(item.path.encode("utf-8")) + _MAX_ENTRY_WIRE_OVERHEAD
        if used_bytes + cost > MAX_PATH_COMPLETION_RESULT_BYTES:
            truncated = True
            break
        used_bytes += cost
        entries.append(PathCompletionEntry(path=item.path, kind=item.kind))
    return PathCompletionResult(query=request.query, entries=tuple(entries), truncated=truncated)


def create_path_queries() -> tuple[PluginQuery, ...]:
    return (PATH_COMPLETION.bind(complete_workspace_paths),)


__all__ = [
    "MAX_PATH_COMPLETION_DEPTH",
    "MAX_PATH_COMPLETION_ENTRY_BYTES",
    "MAX_PATH_COMPLETION_LIMIT",
    "MAX_PATH_COMPLETION_QUERY_BYTES",
    "MAX_PATH_COMPLETION_QUERY_CHARS",
    "MAX_PATH_COMPLETION_RESULT_BYTES",
    "PATH_COMPLETION",
    "PATH_COMPLETION_QUERY",
    "PathCompletionEntry",
    "PathCompletionRequest",
    "PathCompletionResult",
    "PathCompletionTransport",
    "complete_workspace_paths",
    "create_path_queries",
]
