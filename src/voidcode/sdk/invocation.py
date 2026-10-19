"""Invocation context shared by kernel, plugin host, and plugins.

Neutral execution identity, capabilities, and cancellation handle handed to
tools and commands.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .provider import ProviderAbortSignal


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Explicit invocation facts plus the capabilities the kernel granted.

    The read-coverage fields describe what this session's model has ALREADY been shown, derived
    by the runtime from committed, model-visible results: `read_paths` is every file some result
    made visible, `read_lines`/`read_whole_files` are keyed by `(path, content hash)` so lines of
    two different versions of a file are never merged, and a clipped observation or a partial
    result grants nothing. They are always present (empty when nothing was read); guards never
    need a fallback.
    """

    workspace: Path
    session_id: str
    run_id: str
    invocation_id: str
    revision_id: str
    actor: str
    capabilities: frozenset[str] = frozenset()
    logical_turn: str = ""
    #: How many nested invocations separate this call from the session's own tool call: a nested
    #: request inherits its caller's depth plus one, so a deferred descendant is always counted.
    invocation_depth: int = 0
    abort_signal: ProviderAbortSignal | None = None
    read_paths: frozenset[str] = frozenset()
    read_lines: Mapping[tuple[str, str], frozenset[int]] = field(default_factory=lambda: MappingProxyType({}))
    read_whole_files: frozenset[tuple[str, str]] = frozenset()
    #: The content version this call targets, when exactly one version of its declared target paths
    #: was observed; ``None`` when no target path was declared or the versions are ambiguous.
    read_hash: str | None = None
    #: Mid-invocation progress for the surface that called this tool; observational only, and
    #: absent for callbacks that have no calling surface (hooks, lifecycle, observers).
    emit_tool_progress: Callable[[Mapping[str, object]], None] | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        if isinstance(self.workspace, str):
            object.__setattr__(self, "workspace", Path(self.workspace))
        if not isinstance(self.capabilities, frozenset):
            object.__setattr__(self, "capabilities", frozenset(self.capabilities))
        if type(self.invocation_depth) is not int or self.invocation_depth < 0:
            raise ValueError("tool invocation depth must be a non-negative integer")
        if not isinstance(self.read_paths, frozenset):
            object.__setattr__(self, "read_paths", frozenset(self.read_paths))
        if not isinstance(self.read_whole_files, frozenset):
            object.__setattr__(self, "read_whole_files", frozenset(self.read_whole_files))
        if not isinstance(self.read_lines, MappingProxyType):
            object.__setattr__(
                self,
                "read_lines",
                MappingProxyType({(str(path), str(digest)): frozenset(lines) for (path, digest), lines in dict(self.read_lines).items()}),
            )

    def require_workspace(self) -> Path:
        if self.session_id:
            self.require_session_id()
        if self.workspace is None or not str(self.workspace):
            raise RuntimeError("tool requires an explicit workspace")
        return self.workspace

    def require_session_id(self) -> str:
        if not self.session_id:
            raise RuntimeError("tool requires an explicit session identity")
        return self.session_id

    def require_revision(self) -> str:
        if not self.revision_id:
            raise RuntimeError("tool requires an explicit capability revision")
        return self.revision_id

    def has_capability(self, name: str) -> bool:
        return name in self.capabilities
