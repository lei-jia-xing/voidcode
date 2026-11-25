"""Turn execution contracts: results, host protocols, and shared driver state."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass
from typing import Any, Literal, Protocol

from ...sdk.client import BudgetExhaustion
from ...sdk.hooks import StopCause
from ...sdk.json_values import json_wire_object
from ...sdk.observed_effects import Change
from ...sdk.provider import ProviderMessage
from ...sdk.services import PluginBudget, SessionUsageReport
from ...sdk.tools import ToolCall, ToolDefinition, ToolResult
from ..activation import Admission, ExecutionConfig
from ..cancellation import Cancellation
from ..execution import _Execution
from ..plugins import PluginRuntime
from ..storage.store import (
    CheckpointKind,
    ContinuationStatus,
    EventDraft,
    ObserverReceiptRecord,
    SessionRecord,
    SessionStatus,
    StagedBudgetUpdate,
    StagedCancellation,
    StagedContinuationDraft,
    StagedStateUpdate,
    StagedWakeup,
    StagedWakeupCancel,
    StoredEvent,
)
from .commit_pipeline import CheckpointBinding, RootBudgetGrant

MODEL_CONFIGURATION_GUIDANCE = "no model configured or registered"

type CoreRunStatus = SessionStatus | Literal["queued"]


@dataclass(frozen=True, slots=True)
class CoreResult:
    session_id: str
    run_id: str
    revision_id: str
    status: CoreRunStatus
    output: str | None = None
    error: str | None = None
    pending_approval: Mapping[str, object] | None = None
    pending_ask: Mapping[str, object] | None = None
    stop_cause: StopCause | None = None
    budget_exhaustion: BudgetExhaustion | None = None

    def as_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "session_id": self.session_id,
            "run_id": self.run_id,
            "revision_id": self.revision_id,
            "status": self.status.value if isinstance(self.status, SessionStatus) else self.status,
            "output": self.output,
            "error": self.error,
            "pending_approval": None if self.pending_approval is None else json_wire_object(self.pending_approval),
        }
        if self.pending_ask is not None:
            payload["pending_ask"] = json_wire_object(self.pending_ask)
        if self.stop_cause is not None:
            payload["stop_cause"] = self.stop_cause
        if self.budget_exhaustion is not None:
            payload["budget_exhaustion"] = asdict(self.budget_exhaustion)
        return payload


class TurnExecutionSnapshot(Protocol):
    @property
    def revision_id(self) -> str: ...
    @property
    def config(self) -> ExecutionConfig: ...
    @property
    def plugin_runtime(self) -> PluginRuntime: ...
    @property
    def native_tools(self) -> Mapping[str, ToolDefinition]: ...
    @property
    def tools(self) -> tuple[ToolDefinition, ...]: ...
    def definition_of(self, name: str) -> ToolDefinition | None: ...
    def validator_of(self, name: str) -> Any | None: ...
    def owner_of(self, tool_name: str) -> str: ...


class TurnExecutionHost(Protocol):
    @property
    def revision_id(self) -> str: ...
    @property
    def default_abort(self) -> Cancellation: ...
    def active_abort(self, session_id: str) -> Cancellation | None: ...
    @property
    def snapshot(self) -> TurnExecutionSnapshot: ...
    def dispatch(self, description: str) -> AbstractContextManager[TurnExecutionSnapshot]: ...
    def session_read_only(self, session_id: str) -> bool: ...
    def visible_tools(self, execution: _Execution, snapshot: TurnExecutionSnapshot) -> tuple[ToolDefinition, ...]: ...
    def denial(self, call: ToolCall, definition: ToolDefinition, *, read_only: bool) -> str | None: ...
    def gate_denial(self, execution: _Execution, snapshot: TurnExecutionSnapshot, call: ToolCall, *, readonly: bool) -> str | None: ...
    def needs_approval(self, definition: ToolDefinition) -> bool: ...
    def has_approval_policy(self, session_id: str, tool_name: str, arguments: Mapping[str, object]) -> bool: ...
    def prepare_activation_target(self) -> tuple[str, Admission]: ...
    def pending_activation_request(self, execution: _Execution) -> Mapping[str, object] | None: ...
    def activate(self, *, session_id: str, source: str, request_id: str, target_revision_id: str) -> Mapping[str, object]: ...
    def session_budgets(self, session_id: str) -> Sequence[Any]: ...
    def get_session_usage(
        self,
        session_id: str,
        *,
        since_sequence: int | None = None,
        session: SessionRecord | None = None,
    ) -> SessionUsageReport: ...
    def cancel_pending_continuations(self, session_id: str, reason: str) -> None: ...
    def merge_child_messages(self, execution: _Execution) -> None: ...
    def lifecycle_events(
        self,
        execution: _Execution,
        snapshot: TurnExecutionSnapshot,
        kind: Literal["session_start", "session_end"],
        *,
        status: Literal["completed", "failed", "interrupted", ""] = "",
        error: str | None = None,
    ) -> tuple[EventDraft, ...]: ...
    def project_messages(
        self,
        execution: _Execution,
        *,
        model: str,
        tool_schema_characters: int,
        allow_pending: bool = False,
    ) -> tuple[ProviderMessage, ...]: ...
    def settle_unsettled_dispatches(self, execution: _Execution) -> None: ...
    def report_provider_progress(
        self,
        session_id: str,
        *,
        run_id: str,
        dispatch_id: str,
        dispatch_sequence: int,
        channel: str,
        text_delta: str | None,
    ) -> None: ...
    def invoke(self, execution: _Execution, call: ToolCall, definition: ToolDefinition) -> CoreResult | None: ...
    def invoke_readonly_batch(
        self,
        execution: _Execution,
        calls: tuple[ToolCall, ...],
        definitions: tuple[ToolDefinition, ...],
    ) -> CoreResult | None: ...
    def report(
        self,
        execution: _Execution,
        call: ToolCall,
        result: ToolResult,
        *,
        interrupted: bool = False,
        staged_state: tuple[StagedStateUpdate, ...] = (),
        staged_events: tuple[EventDraft, ...] = (),
        staged_conts: tuple[StagedContinuationDraft, ...] = (),
        staged_cancels: tuple[StagedCancellation, ...] = (),
        staged_budget: PluginBudget | None | Literal["clear"] = None,
        staged_receipts: tuple[ObserverReceiptRecord, ...] = (),
        staged_wakeups: tuple[StagedWakeup, ...] = (),
        staged_wakeup_cancels: tuple[StagedWakeupCancel, ...] = (),
        budget_exhaustion: BudgetExhaustion | None = None,
        staged_observations: tuple[Change, ...] = (),
        pinned_run_id: str | None = None,
        enforce_pin: bool = False,
        prefix_events: tuple[EventDraft, ...] = (),
        ask_answer_guard: str | None = None,
    ) -> None: ...
    def interrupt_batch(
        self,
        execution: _Execution,
        *,
        budget_exhaustion: BudgetExhaustion | None = None,
        pinned_run_id: str | None = None,
        enforce_pin: bool = False,
    ) -> None: ...
    def commit(
        self,
        execution: _Execution,
        status: SessionStatus,
        kind: CheckpointKind | None,
        *events: EventDraft,
        staged_state: Sequence[StagedStateUpdate] = (),
        staged_continuations: Sequence[StagedContinuationDraft] = (),
        staged_cancellations: Sequence[StagedCancellation] = (),
        staged_wakeups: Sequence[StagedWakeup] = (),
        staged_wakeup_cancels: Sequence[StagedWakeupCancel] = (),
        staged_budgets: Sequence[StagedBudgetUpdate] = (),
        observer_receipts: Sequence[ObserverReceiptRecord] = (),
        completed_continuation_id: str | None = None,
        completed_continuation_status: ContinuationStatus = ContinuationStatus.COMPLETED,
        reserve_root: bool = False,
        root_budget_grant: RootBudgetGrant | tuple[str, int, str] | None = None,
        start_turn: bool = False,
        checkpoint_bindings: Sequence[CheckpointBinding | tuple[str, str]] = (),
        pinned_run_id: str | None = None,
        enforce_pin: bool = False,
        ask_answer_guard: str | None = None,
    ) -> tuple[StoredEvent, ...]: ...
    def measured_run_elapsed_fields(self, execution: _Execution) -> dict[str, int]: ...
    def store_session(self, session_id: str) -> SessionRecord: ...
    def store_events(self, session_id: str) -> Sequence[StoredEvent]: ...


class TurnDriverState:
    """Host binding shared by the turn-execution responsibility mixins."""

    def __init__(self, host: TurnExecutionHost) -> None:
        self._host = host
