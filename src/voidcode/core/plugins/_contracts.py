"""Public invocation contracts shared by the plugin runtime and its callers."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from voidcode.sdk.commands import CommandResult
from voidcode.sdk.json_values import own_json_object
from voidcode.sdk.plugin import StateScope
from voidcode.sdk.provider import (
    ProviderAbortSignal,
)
from voidcode.sdk.services import (
    PluginBudget,
)
from voidcode.sdk.suspend import SuspendPark
from voidcode.sdk.tools import (
    Change,
    ToolResult,
)

from ..storage.store import (
    EventDraft,
    ObserverReceiptRecord,
    StagedCancellation,
    StagedContinuationDraft,
    StagedStateUpdate,
    StagedWakeup,
    StagedWakeupCancel,
)


@dataclass(frozen=True, slots=True)
class PluginSource:
    """One configured plugin a generation loads: its root and factory config."""

    root: Path
    plugin_id: str
    config: Mapping[str, object] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.root, Path):
            object.__setattr__(self, "root", Path(self.root))
        if not isinstance(self.plugin_id, str) or not self.plugin_id:
            raise ValueError("plugin source requires a non-empty plugin id")
        object.__setattr__(self, "config", own_json_object(self.config))


@dataclass
class LeaseStaging:
    """Per-invocation scratch work, frozen together and committed as one atomic unit."""

    state: dict[tuple[StateScope, str], tuple[Mapping[str, object], int, int]] = field(default_factory=dict)
    events: list[EventDraft] = field(default_factory=list)
    continuations: list[StagedContinuationDraft] = field(default_factory=list)
    cancellations: list[StagedCancellation] = field(default_factory=list)
    wakeups: list[StagedWakeup] = field(default_factory=list)
    wakeup_cancels: list[StagedWakeupCancel] = field(default_factory=list)
    observer_receipts: list[ObserverReceiptRecord] = field(default_factory=list)
    budget: PluginBudget | None | Literal["clear"] = None
    observations: list[Change] = field(default_factory=list)
    #: Dedicated suspend-intent slot, never an EventDraft, so it is uncommittable by
    #: construction. Set by the suspend services immediately before raising SuspendPark;
    #: a normal return with this set is a swallowed suspension and fails visibly.
    suspend: SuspendPark | None = None
    op_counter: int = 0


@dataclass(frozen=True, slots=True)
class LeaseIdentity:
    """The immutable authority one invocation runs under."""

    token: str
    owner: str
    session_id: str
    workspace: Path
    run_id: str
    revision_id: str
    invocation_id: str
    readonly: bool
    root_action_id: str | None = None
    initial_root_budget: int | None = None
    origin_visible_tools: tuple[str, ...] | None = None
    origin_readonly: bool | None = None
    background_action_id: str | None = None
    actor: str = "model"
    #: Hook leases cannot start nested work.
    hook: bool = False
    #: The lease may stage internal session work (state, events, budget, continuations).
    internal_work: bool = False
    invocation_depth: int = 0
    #: True only for tool-callback leases opened by PluginRuntime.invoke; suspension
    #: services refuse every other lease (hooks, commands, views, background drivers).
    tool_callback: bool = False

    @property
    def internal_restricted(self) -> bool:
        """Whether this lease refuses internal session work."""
        return self.readonly and not self.internal_work


@dataclass
class LeaseContext:
    identity: LeaseIdentity
    staging: LeaseStaging = field(default_factory=LeaseStaging)
    revoked: bool = False
    abort_signal: ProviderAbortSignal | None = None
    #: Unregister handle for the abort-signal cancel mirror, cleared at revoke.
    cancel_subscription: Callable[[], None] | None = None
    opened_thread_id: int = field(default_factory=threading.get_ident, init=False, repr=False)


@dataclass(frozen=True, slots=True)
class ToolInvocationOutcome:
    result: ToolResult
    staged_state: tuple[StagedStateUpdate, ...] = ()
    staged_events: tuple[EventDraft, ...] = ()
    staged_continuations: tuple[StagedContinuationDraft, ...] = ()
    staged_cancellations: tuple[StagedCancellation, ...] = ()
    staged_observer_receipts: tuple[ObserverReceiptRecord, ...] = ()
    staged_budget: PluginBudget | None | Literal["clear"] = None
    staged_wakeups: tuple[StagedWakeup, ...] = ()
    staged_wakeup_cancels: tuple[StagedWakeupCancel, ...] = ()
    staged_observations: tuple[Change, ...] = ()


@dataclass(frozen=True, slots=True)
class FrozenStaged:
    """One suspended callback's frozen staged work: no ToolResult was ever produced."""

    staged_state: tuple[StagedStateUpdate, ...] = ()
    staged_events: tuple[EventDraft, ...] = ()
    staged_continuations: tuple[StagedContinuationDraft, ...] = ()
    staged_cancellations: tuple[StagedCancellation, ...] = ()
    staged_observer_receipts: tuple[ObserverReceiptRecord, ...] = ()
    staged_budget: PluginBudget | None | Literal["clear"] = None
    staged_wakeups: tuple[StagedWakeup, ...] = ()
    staged_wakeup_cancels: tuple[StagedWakeupCancel, ...] = ()
    staged_observations: tuple[Change, ...] = ()


class ToolSuspension(BaseException):
    """A tool callback suspended with frozen staged work for the engine to park.

    Raised only by PluginRuntime.invoke's SuspendPark clause, caught only by the
    engine's park-commit site. Carries the suspend intent plus the frozen staged
    outcome; the invoke boundary never writes the store.
    """

    def __init__(self, park: SuspendPark, staged: FrozenStaged) -> None:
        super().__init__(park.park_kind)
        self.park = park
        self.staged = staged


@dataclass(frozen=True, slots=True)
class CommandInvocationOutcome:
    result: CommandResult
    staged_state: tuple[StagedStateUpdate, ...] = ()
    staged_events: tuple[EventDraft, ...] = ()
    staged_continuations: tuple[StagedContinuationDraft, ...] = ()
    staged_cancellations: tuple[StagedCancellation, ...] = ()
    staged_observer_receipts: tuple[ObserverReceiptRecord, ...] = ()
    staged_budget: PluginBudget | None | Literal["clear"] = None
    staged_wakeups: tuple[StagedWakeup, ...] = ()
    staged_wakeup_cancels: tuple[StagedWakeupCancel, ...] = ()
    staged_observations: tuple[Change, ...] = ()
    cancelled: bool = False


#: One invocation's staged side effects: state, events, continuations, cancellations, receipts, budget, wakeups.
type _Staged = tuple[
    tuple[StagedStateUpdate, ...],
    tuple[EventDraft, ...],
    tuple[StagedContinuationDraft, ...],
    tuple[StagedCancellation, ...],
    tuple[ObserverReceiptRecord, ...],
    PluginBudget | None | Literal["clear"],
    tuple[StagedWakeup, ...],
    tuple[StagedWakeupCancel, ...],
]


@dataclass(frozen=True, slots=True)
class CallbackChanges:
    staged_state: tuple[StagedStateUpdate, ...] = ()
    staged_events: tuple[EventDraft, ...] = ()
    staged_continuations: tuple[StagedContinuationDraft, ...] = ()
    staged_cancellations: tuple[StagedCancellation, ...] = ()
    staged_wakeups: tuple[StagedWakeup, ...] = ()
    staged_wakeup_cancels: tuple[StagedWakeupCancel, ...] = ()
    staged_budgets: tuple[tuple[str, PluginBudget | Literal["clear"]], ...] = ()
    errors: tuple[dict[str, str], ...] = ()
