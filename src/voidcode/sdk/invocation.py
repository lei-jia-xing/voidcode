"""Invocation context shared by kernel, plugin host, and plugins.

Neutral execution identity, capabilities, and cancellation handle handed to
tools and commands.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .provider import ProviderAbortSignal


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Explicit invocation facts plus the capabilities the kernel granted."""

    workspace: Path
    session_id: str
    run_id: str
    invocation_id: str
    revision_id: str
    actor: str
    capabilities: frozenset[str] = frozenset()
    logical_turn: str = ""
    abort_signal: ProviderAbortSignal | None = None

    def __post_init__(self) -> None:
        if isinstance(self.workspace, str):
            object.__setattr__(self, "workspace", Path(self.workspace))
        if not isinstance(self.capabilities, frozenset):
            object.__setattr__(self, "capabilities", frozenset(self.capabilities))

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
