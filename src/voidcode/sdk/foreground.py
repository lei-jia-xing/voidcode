"""Foreground suspension: the driver-owned composition contract for eval cells.

A tool callback suspends by calling
``PluginServices.suspend_foreground_composition`` (which stages the
wait-intent and raises ``SuspendComposition``); a Core-supervised driver
thread claims the composition, runs each step under a fresh short lease via
``ForegroundContext.invoke``, waits unleased for inner deferred tools, and
stages exactly one terminal result for adoption without re-invoking the tool.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from .suspend import ParkError, SuspendPark

if TYPE_CHECKING:
    from .invocation import ToolContext
    from .services import PluginServices, ToolAwaitResult, ToolInvocationReceipt
    from .tools import ToolResult


@dataclass(frozen=True, slots=True)
class CompositionReceipt:
    """Stable identity of one parked composition for logs and cancel correlation."""

    composition_id: str
    tool_name: str
    depth: int

    def __post_init__(self) -> None:
        if not isinstance(self.composition_id, str) or not self.composition_id:
            raise ValueError("composition receipt requires a non-empty composition_id")
        if not isinstance(self.tool_name, str) or not self.tool_name:
            raise ValueError("composition receipt requires a non-empty tool_name")
        if type(self.depth) is not int or self.depth < 0:
            raise ValueError("composition receipt depth must be a non-negative integer")

    def as_payload(self) -> dict[str, object]:
        return {
            "composition_id": self.composition_id,
            "tool_name": self.tool_name,
            "depth": self.depth,
        }


@dataclass(frozen=True, slots=True)
class CompositionDraft:
    """What the suspending callback asks the driver to run. JSON-ownable, size-honest."""

    cell_source: str
    namespace_key: str
    tool_allowlist: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.cell_source, str) or not self.cell_source:
            raise ValueError("composition draft requires non-empty cell_source")
        if not isinstance(self.namespace_key, str) or not self.namespace_key:
            raise ValueError("composition draft requires a non-empty namespace_key")
        if self.tool_allowlist is not None:
            allowlist = tuple(self.tool_allowlist)
            if any(not isinstance(name, str) or not name for name in allowlist) or len(set(allowlist)) != len(allowlist):
                raise ValueError("composition draft tool_allowlist must hold unique non-empty tool names")
            object.__setattr__(self, "tool_allowlist", allowlist)

    def as_payload(self) -> dict[str, object]:
        return {
            "cell_source": self.cell_source,
            "namespace_key": self.namespace_key,
            "tool_allowlist": None if self.tool_allowlist is None else list(self.tool_allowlist),
        }

    @staticmethod
    def from_payload(payload: Mapping[str, object]) -> CompositionDraft:
        if not isinstance(payload, Mapping):
            raise ValueError("composition draft payload must be an object")
        allowed = {"cell_source", "namespace_key", "tool_allowlist"}
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise ValueError("composition draft contains unknown field(s): " + ", ".join(unknown))
        if "cell_source" not in payload or "namespace_key" not in payload:
            raise ValueError("composition draft is missing required field(s)")
        cell_source = payload["cell_source"]
        namespace_key = payload["namespace_key"]
        if not isinstance(cell_source, str) or not cell_source:
            raise ValueError("composition draft requires non-empty cell_source")
        if not isinstance(namespace_key, str) or not namespace_key:
            raise ValueError("composition draft requires a non-empty namespace_key")
        allowlist = payload.get("tool_allowlist")
        names: tuple[str, ...] | None
        if allowlist is None:
            names = None
        elif isinstance(allowlist, (list, tuple)) and all(isinstance(name, str) for name in allowlist):
            names = tuple(allowlist)
        else:
            raise ValueError("composition draft tool_allowlist must be a list of tool names or null")
        return CompositionDraft(cell_source=cell_source, namespace_key=namespace_key, tool_allowlist=names)


@runtime_checkable
class ForegroundContext(Protocol):
    """Driver handle for one claimed foreground composition. Mirrors BackgroundContext
    (sdk/services.py:773-808); the driver thread holds this, never a lease, across waits."""

    @property
    def composition_id(self) -> str: ...

    @property
    def session_id(self) -> str: ...

    @property
    def owner(self) -> str: ...

    @property
    def payload(self) -> Mapping[str, object]:
        """The immutable CompositionDraft.as_payload() this composition was claimed for."""
        ...

    @property
    def abort_signal(self) -> threading.Event:
        """Set on cancel/shutdown; the driver polls it (cf. sdk/services.py:787-790)."""
        ...

    def invoke[T](self, callback: Callable[[ToolContext, PluginServices], T]) -> T:
        """One atomic lease-bounded step: fresh owner/revision-bound lease, staged work commits
        before return; raises InterruptedError and commits nothing when aborted."""
        ...

    def wait_tool(self, receipt: ToolInvocationReceipt) -> ToolAwaitResult:
        """Wait unleased for this composition's exact deferred request. Reuses ToolAwaitResult
        (sdk/services.py:644-710) verbatim — no forked outcome type. Refused on any leased or
        dispatch thread (engine.py:972-979 discipline)."""
        ...

    def stage_terminal(self, result: ToolResult, *, for_tool: str) -> None:
        """Stage the parked call's terminal result for adoption. Single-shot (second call refused);
        data-only, no services cross. Core adopts it without re-invoking the tool."""
        ...


class SuspendComposition(SuspendPark):
    """Unwind a tool callback to park it for foreground composition."""

    def __init__(self, draft: CompositionDraft, *, idempotency_key: str | None = None) -> None:
        if not isinstance(draft, CompositionDraft):
            raise ValueError("suspend composition requires a CompositionDraft")
        if idempotency_key is not None and (not isinstance(idempotency_key, str) or not idempotency_key):
            raise ValueError("composition idempotency key must be non-empty text or null")
        self._draft = draft
        self._idempotency_key = idempotency_key
        intent: dict[str, object] = {"draft": draft.as_payload()}
        if idempotency_key is not None:
            intent["idempotency_key"] = idempotency_key
        super().__init__("composition", intent)

    @property
    def draft(self) -> CompositionDraft:
        return self._draft

    @property
    def idempotency_key(self) -> str | None:
        return self._idempotency_key


class CompositionNestedRefused(ParkError):
    """A composition-bound driver-step lease attempted a nested foreground suspension.

    The cursor keeps a single composition slot (no stack), so driver-step leases
    opened via ``ForegroundContext.invoke`` carry the composition binding and
    cannot suspend a second composition. ``suspend_for_ask`` from such a lease
    remains allowed (inner ask parks with both bindings).
    """


__all__ = [
    "CompositionDraft",
    "CompositionNestedRefused",
    "CompositionReceipt",
    "ForegroundContext",
    "SuspendComposition",
]
