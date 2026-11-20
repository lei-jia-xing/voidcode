"""Typed decision gates: native approvals and plugin confirmations.

Contracts only: frozen dataclasses plus a pure reducer. No sockets, no
storage, no ``Core`` — and no raw event payloads. The outer layer decodes
stream events and run receipts into :data:`GateChange`, then folds them with
:func:`reduce_gates`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from voidcode.sdk.ask import AskRequest
from voidcode.sdk.commands import ConfirmationRequest
from voidcode.sdk.tools import ToolCall
from voidcode.sdk.views import ConfirmationViewData, parse_view_data


@dataclass(frozen=True, slots=True)
class ApprovalGate:
    """One parked native tool approval: the resume identity plus its call."""

    call: ToolCall
    request_id: str
    run_id: str
    revision_id: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.call, ToolCall):
            raise ValueError("approval gate requires a ToolCall")
        if not self.call.tool_name or not isinstance(self.call.tool_name, str):
            raise ValueError("approval gate requires a non-empty tool_name")
        if not isinstance(self.request_id, str) or not self.request_id:
            raise ValueError("approval gate requires a non-empty request_id")
        if not isinstance(self.run_id, str) or not self.run_id:
            raise ValueError("approval gate requires a non-empty run_id")
        if not isinstance(self.revision_id, str):
            raise ValueError("approval gate revision_id must be a string")

    @property
    def tool_name(self) -> str:
        return self.call.tool_name

    @property
    def arguments(self) -> object:
        return self.call.arguments


@dataclass(frozen=True, slots=True)
class ConfirmationGate:
    """One pending plugin confirmation plus the revision it was displayed under."""

    request: ConfirmationRequest
    revision_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.request, ConfirmationRequest):
            raise ValueError("confirmation gate requires a ConfirmationRequest")
        if not isinstance(self.revision_id, str):
            raise ValueError("confirmation gate revision_id must be a string")

    @property
    def request_id(self) -> str:
        return self.request.request_id

    @property
    def view(self) -> ConfirmationViewData:
        """The typed dialog; non-confirmation requests fail visibly."""
        if self.request.ui_type != "confirmation":
            raise ValueError(f"confirmation gate requires ui_type 'confirmation', got {self.request.ui_type!r}")
        data = parse_view_data(self.request.ui_type, self.request.ui_data)
        if not isinstance(data, ConfirmationViewData):
            raise ValueError("confirmation gate ui_data is not confirmation view data")
        return data

    @staticmethod
    def from_request(request: ConfirmationRequest, *, revision_id: str) -> ConfirmationGate:
        return ConfirmationGate(request=request, revision_id=revision_id)


@dataclass(frozen=True, slots=True)
class AskGate:
    """One pending model-initiated ask plus the revision it was displayed under."""

    request: AskRequest
    revision_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.request, AskRequest):
            raise ValueError("ask gate requires an AskRequest")
        if not isinstance(self.revision_id, str):
            raise ValueError("ask gate revision_id must be a string")

    @property
    def request_id(self) -> str:
        return self.request.request_id

    @staticmethod
    def from_request(request: AskRequest, *, revision_id: str) -> AskGate:
        return AskGate(request=request, revision_id=revision_id)


type PendingGate = ApprovalGate | ConfirmationGate | AskGate


@dataclass(frozen=True, slots=True)
class ApprovalRequested:
    gate: ApprovalGate


@dataclass(frozen=True, slots=True)
class ConfirmationRequested:
    gate: ConfirmationGate


@dataclass(frozen=True, slots=True)
class AskRequested:
    gate: AskGate


@dataclass(frozen=True, slots=True)
class GateDecided:
    """One terminal decision: durable decided fact or accepted server receipt."""

    request_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.request_id, str) or not self.request_id:
            raise ValueError("decided gate requires a non-empty request_id")


@dataclass(frozen=True, slots=True)
class ActivationCommitted:
    to_revision_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.to_revision_id, str):
            raise ValueError("activation revision must be a string")


@dataclass(frozen=True, slots=True)
class RunTerminated:
    """One run end: clears only the approval parked by that run."""

    run_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not self.run_id:
            raise ValueError("terminated run requires a non-empty run_id")


type GateChange = ApprovalRequested | ConfirmationRequested | AskRequested | GateDecided | ActivationCommitted | RunTerminated


@dataclass(frozen=True, slots=True)
class GateState:
    """Pending gates plus decided-id tombstones; decisions never resurrect."""

    approval: ApprovalGate | None = None
    confirmations: tuple[ConfirmationGate, ...] = ()
    asks: tuple[AskGate, ...] = ()
    decided: frozenset[str] = frozenset()
    #: Cross-kind arrival order of confirmation/ask request ids.
    arrival: tuple[str, ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        if self.approval is not None and not isinstance(self.approval, ApprovalGate):
            raise ValueError("gate state approval must be an ApprovalGate or None")
        object.__setattr__(self, "confirmations", tuple(self.confirmations))
        for gate in self.confirmations:
            if not isinstance(gate, ConfirmationGate):
                raise ValueError("gate state confirmations must be ConfirmationGate entries")
        object.__setattr__(self, "asks", tuple(self.asks))
        for gate in self.asks:
            if not isinstance(gate, AskGate):
                raise ValueError("gate state asks must be AskGate entries")
        object.__setattr__(self, "decided", frozenset(self.decided))
        object.__setattr__(self, "arrival", tuple(self.arrival))


def reduce_gates(state: GateState, change: GateChange) -> GateState:
    """Fold one typed change; same-request repeats never reset pending UI."""
    if isinstance(change, ApprovalRequested):
        gate = change.gate
        if gate.request_id in state.decided:
            return state
        if state.approval is not None and state.approval.request_id == gate.request_id:
            return state
        return GateState(approval=gate, confirmations=state.confirmations, asks=state.asks, decided=state.decided, arrival=state.arrival)
    if isinstance(change, ConfirmationRequested):
        gate = change.gate
        if gate.request_id in state.decided:
            return state
        if any(entry.request_id == gate.request_id for entry in state.confirmations):
            return state
        arrival = state.arrival if gate.request_id in state.arrival else (*state.arrival, gate.request_id)
        return GateState(approval=state.approval, confirmations=(*state.confirmations, gate), asks=state.asks, decided=state.decided, arrival=arrival)
    if isinstance(change, AskRequested):
        gate = change.gate
        if gate.request_id in state.decided:
            return state
        if any(entry.request_id == gate.request_id for entry in state.asks):
            return state
        arrival = state.arrival if gate.request_id in state.arrival else (*state.arrival, gate.request_id)
        return GateState(approval=state.approval, confirmations=state.confirmations, asks=(*state.asks, gate), decided=state.decided, arrival=arrival)
    if isinstance(change, GateDecided):
        request_id = change.request_id
        approval = None if state.approval is not None and state.approval.request_id == request_id else state.approval
        confirmations = tuple(entry for entry in state.confirmations if entry.request_id != request_id)
        asks = tuple(entry for entry in state.asks if entry.request_id != request_id)
        arrival = tuple(entry for entry in state.arrival if entry != request_id)
        if (
            request_id in state.decided
            and approval is state.approval
            and len(confirmations) == len(state.confirmations)
            and len(asks) == len(state.asks)
        ):
            return state
        return GateState(approval=approval, confirmations=confirmations, asks=asks, decided=state.decided | {request_id}, arrival=arrival)
    if isinstance(change, ActivationCommitted):
        dropped = {entry.request_id for entry in state.confirmations if entry.revision_id != change.to_revision_id}
        dropped |= {entry.request_id for entry in state.asks if entry.revision_id != change.to_revision_id}
        confirmations = tuple(entry for entry in state.confirmations if entry.revision_id == change.to_revision_id)
        asks = tuple(entry for entry in state.asks if entry.revision_id == change.to_revision_id)
        arrival = tuple(entry for entry in state.arrival if entry not in dropped)
        approval = state.approval
        if approval is not None:
            dropped.add(approval.request_id)
            approval = None
        if not dropped:
            return state
        return GateState(approval=approval, confirmations=confirmations, asks=asks, decided=state.decided | dropped, arrival=arrival)
    if isinstance(change, RunTerminated):
        approval = state.approval
        if approval is None or approval.run_id != change.run_id:
            return state
        return GateState(
            approval=None, confirmations=state.confirmations, asks=state.asks, decided=state.decided | {approval.request_id}, arrival=state.arrival
        )
    raise ValueError(f"unknown gate change: {type(change).__name__}")


def pending_gates(state: GateState) -> tuple[PendingGate, ...]:
    """Ordered pending gates: the blocking approval first, then asks and confirmations in arrival order."""
    by_id: dict[str, PendingGate] = {}
    for gate in state.confirmations:
        by_id[gate.request_id] = gate
    for gate in state.asks:
        by_id[gate.request_id] = gate
    ranked = [by_id[request_id] for request_id in state.arrival if request_id in by_id]
    ranked.extend(gate for gate in (*state.confirmations, *state.asks) if gate.request_id not in state.arrival)
    if state.approval is None:
        return tuple(ranked)
    return (state.approval, *ranked)


__all__ = [
    "ActivationCommitted",
    "ApprovalGate",
    "ApprovalRequested",
    "AskGate",
    "AskRequested",
    "ConfirmationGate",
    "ConfirmationRequested",
    "GateChange",
    "GateDecided",
    "GateState",
    "PendingGate",
    "RunTerminated",
    "pending_gates",
    "reduce_gates",
]
