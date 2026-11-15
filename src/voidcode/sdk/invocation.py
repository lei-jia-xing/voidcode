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
    root_action_id: str | None = None
    initial_root_budget: int | None = None
    #: Exact tool visibility captured when this invocation was admitted; None means unavailable,
    #: never an unrestricted grant.
    origin_visible_tools: tuple[str, ...] | None = None
    #: Effective restrictive stance captured at admission; current restrictions may only narrow it.
    origin_readonly: bool | None = None
    #: Explicit background driver identity for host-retained scripted deferred requests.
    background_action_id: str | None = None
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
        if self.root_action_id is not None and (not isinstance(self.root_action_id, str) or not self.root_action_id):
            raise ValueError("root action identity must be nonempty text or null")
        if self.initial_root_budget is not None and (
            type(self.initial_root_budget) is not int or not 0 <= self.initial_root_budget <= 9_223_372_036_854_775_807
        ):
            raise ValueError("initial root action budget must fit a non-negative SQLite INTEGER or be null")
        if self.origin_visible_tools is not None and (
            not isinstance(self.origin_visible_tools, tuple)
            or not all(isinstance(name, str) and name for name in self.origin_visible_tools)
            or len(set(self.origin_visible_tools)) != len(self.origin_visible_tools)
        ):
            raise ValueError("captured origin tool visibility must be unique tool names or null")
        if self.origin_readonly is not None and type(self.origin_readonly) is not bool:
            raise ValueError("captured origin stance must be boolean or null")
        if self.background_action_id is not None and (not isinstance(self.background_action_id, str) or not self.background_action_id):
            raise ValueError("background action identity must be nonempty text or null")

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
