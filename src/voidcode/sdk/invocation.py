"""Invocation context shared by core, runtime, and plugins.

Neutral execution identity and cancellation handle handed to tools and commands.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .provider import ProviderAbortSignal


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Explicit invocation identity, cancellation and progress callbacks."""

    workspace: Path
    session_id: str
    run_id: str
    invocation_id: str
    revision_id: str
    actor: str
    logical_turn: str = ""
    #: How many nested invocations separate this call from the session's own tool call: a nested
    #: request inherits its caller's depth plus one, so a deferred descendant is always counted.
    invocation_depth: int = 0
    abort_signal: ProviderAbortSignal | None = None
    #: Mid-invocation progress for the surface that called this tool; observational only, and
    #: absent for callbacks that have no calling surface (hooks, lifecycle, observers).
    emit_tool_progress: Callable[[Mapping[str, object]], None] | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        if isinstance(self.workspace, str):
            object.__setattr__(self, "workspace", Path(self.workspace))
        if type(self.invocation_depth) is not int or self.invocation_depth < 0:
            raise ValueError("tool invocation depth must be a non-negative integer")

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
