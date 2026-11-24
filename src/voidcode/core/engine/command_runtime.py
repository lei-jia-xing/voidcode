"""One planned command invocation: validation, dispatch, and typed commit.

The core owns catalog resolution, session truth, invocation identity, and the
durable commit authority. This module owns what happens to one admitted plan:
core-command handlers, policy and schema checks for plugin commands, the
synchronous plugin invocation, pending-confirmation staging, and the final
public commit with its completion fact.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol
from uuid import uuid4

import jsonschema

from ...sdk.commands import CommandDescriptor, CommandResult, ConfirmationRequest
from ...sdk.invocation import ToolContext
from ...sdk.json_values import json_wire_object
from ...sdk.observed_effects import MAX_OBSERVED_CHANGES
from ...sdk.services import ContinuationResult, PluginBudget, SessionUsageReport
from ...sdk.tools import ToolDefinition, ToolEffect, denied_under_restriction
from ..coverage import canonical_path
from ..events import EventType
from ..plugins import CommandInvocationOutcome, PluginRuntime
from ..storage.store import (
    ActionConfirmationRecord,
    ConfirmationStatus,
    EventDraft,
    ObserverReceiptRecord,
    SessionRecord,
    SessionStatus,
    SqliteStore,
    StagedBudgetUpdate,
    StagedCancellation,
    StagedConfirmation,
    StagedContinuationDraft,
    StagedStateUpdate,
    StagedWakeup,
    StagedWakeupCancel,
    StaleRevision,
    StoreConflict,
)

if TYPE_CHECKING:
    from .turn_execution import CoreResult

#: Session states in which a busy-gated command may run.
_OPEN_STATUSES = frozenset({SessionStatus.IDLE, SessionStatus.COMPLETED, SessionStatus.FAILED})


@dataclass(frozen=True, slots=True)
class CommandPlan:
    """One command invocation captured before any policy check or plugin lease."""

    descriptor: CommandDescriptor
    arguments: Mapping[str, object]
    expected_revision: str
    effects: frozenset[ToolEffect]
    read_only: bool
    session: SessionRecord
    root_action_id: str | None
    initial_root_budget: int | None
    context: ToolContext


@dataclass(frozen=True, slots=True)
class CommandOutcome:
    """A terminal command result plus the plugin events that reached the commit."""

    result: CommandResult
    staged_events: tuple[EventDraft, ...] = ()

    def needs_deferred_drain(self) -> bool:
        """Whether the committed invocation staged a deferred request its caller now owns."""
        return any(event.event_type == EventType.TOOL_DEFERRED for event in self.staged_events)


class CommandSnapshot(Protocol):
    """The one capability generation a command dispatch may use."""

    @property
    def revision_id(self) -> str: ...

    @property
    def plugin_runtime(self) -> PluginRuntime: ...

    @property
    def tools(self) -> tuple[ToolDefinition, ...]: ...

    def command_of(self, name: str) -> CommandDescriptor | None: ...


class CommandHost(Protocol):
    """The core primitives the command runtime may use, stated explicitly."""

    @property
    def store(self) -> SqliteStore: ...

    @property
    def workspace(self) -> Path: ...

    @property
    def revision_id(self) -> str: ...

    @property
    def auto_drain(self) -> bool: ...

    @property
    def snapshot(self) -> CommandSnapshot | None: ...

    def dispatch(self, description: str) -> AbstractContextManager[CommandSnapshot]: ...

    def check_session(self, session: SessionRecord) -> None: ...

    def session_read_only(self, session_id: str) -> bool: ...

    def capture_root_authorization(self, session: SessionRecord) -> tuple[str, int | None]: ...

    def visible_tool_names_for_session(self, session_id: str, snapshot: CommandSnapshot) -> tuple[str, ...]: ...

    def command_abort(self, context: ToolContext) -> AbstractContextManager[ToolContext]: ...

    def command_help(self) -> CommandResult: ...

    def command_status(self, session: SessionRecord) -> CommandResult: ...

    def run(self, prompt: str, *, session_id: str) -> CoreResult: ...

    def resume(
        self,
        session_id: str,
        *,
        approval_request_id: str | None = None,
        decision: Literal["allow", "deny"] | None = None,
    ) -> CoreResult: ...

    def show(self, session_id: str, *, events: bool = False) -> dict[str, object]: ...

    def usage(self, session_id: str, *, since_sequence: int = 0, until_sequence: int | None = None) -> SessionUsageReport: ...

    def commit_public_staged(
        self,
        session_id: str,
        *,
        events: Sequence[EventDraft],
        run_id: str | None = None,
        status: SessionStatus | None = None,
        staged_state: Sequence[StagedStateUpdate] = (),
        staged_continuations: Sequence[StagedContinuationDraft] = (),
        staged_cancellations: Sequence[StagedCancellation] = (),
        staged_wakeups: Sequence[StagedWakeup] = (),
        staged_wakeup_cancels: Sequence[StagedWakeupCancel] = (),
        staged_budgets: Sequence[StagedBudgetUpdate] = (),
        staged_confirmations: Sequence[StagedConfirmation] = (),
        observer_receipts: Sequence[ObserverReceiptRecord] = (),
        root_action_id: str | None = None,
        initial_root_budget: int | None = None,
    ) -> SessionRecord: ...

    def admits_fresh_work(self, session: SessionRecord) -> bool: ...

    def drain_continuations(self, session_id: str) -> tuple[ContinuationResult, ...]: ...

    def drain_deferred_requests(self, session_id: str) -> None: ...

    def owner_cancel_claimed_work(self, session_id: str, cancellations: Sequence[StagedCancellation], reason: str) -> None: ...


def core_command_result(result: CoreResult) -> CommandResult:
    """Map a native run result into the command variant contract."""
    if result.status in ("completed", "idle", "waiting"):
        return CommandResult(status="ok", output=result.output, data=result.as_payload())
    return CommandResult(
        status="error",
        output=result.output,
        error=result.error or f"run {result.status}",
        data=result.as_payload(),
    )


def command_completion_event(
    workspace: Path,
    name: str,
    owner: str,
    context: ToolContext,
    outcome: CommandInvocationOutcome,
) -> EventDraft:
    """One completion fact, keeping supervisor observations even after a throw or cancel."""
    changes: list[dict[str, object]] = []
    seen: set[tuple[str, str, str | None]] = set()
    for observed in outcome.staged_observations:
        path = canonical_path(workspace, observed.path)
        old_path = canonical_path(workspace, observed.old_path) if observed.old_path is not None else None
        key = (path, observed.operation, old_path)
        if key in seen:
            continue
        seen.add(key)
        changes.append({**observed.as_payload(), "path": path, **({"old_path": old_path} if old_path is not None else {})})
        if len(changes) >= MAX_OBSERVED_CHANGES:
            break
    return EventDraft(
        EventType.COMMAND_COMPLETED,
        f"plugin.{owner}",
        context.actor,
        {"name": name, "result": outcome.result.as_payload(), "observed_changes": changes, "cancelled": outcome.cancelled},
    )


class CommandRuntime:
    """Execute one planned command through the core host's existing primitives."""

    def __init__(self, host: CommandHost) -> None:
        self._host = host

    def invoke(self, plan: CommandPlan) -> CommandResult:
        outcome = self._dispatch(plan)
        if outcome.needs_deferred_drain():
            self._host.drain_deferred_requests(plan.session.session_id)
        return outcome.result

    def confirm(
        self,
        session: SessionRecord,
        request_id: str,
        decision: Literal["allow", "deny"],
        *,
        expected_revision: str,
    ) -> CommandResult:
        """Resolve one pending confirmation by exact ID and state version."""
        self._require_revision(expected_revision)
        record = self._host.store.confirmation(request_id)
        session_id = session.session_id
        if record is None or record.session_id != session_id:
            raise ValueError(f"Confirmation request {request_id!r} not found for session {session_id!r}")
        if record.status is not ConfirmationStatus.PENDING:
            raise ValueError(f"Confirmation request {request_id!r} is not pending (status: {record.status.value})")
        if record.revision_id != self._host.revision_id:
            raise ValueError("Confirmation request belongs to another revision")

        if record.expected_state_version is not None:
            _, current_version = self._host.store.plugin_state(record.owner, "session", session_id)
            if current_version != record.expected_state_version:
                self._host.store.decide_confirmation(request_id, ConfirmationStatus.STALE)
                self._commit_confirmation_decision(session, request_id, record.command_name, "stale")
                raise ValueError(f"Confirmation request is stale: expected state version {record.expected_state_version}, got {current_version}")

        if decision == "deny":
            self._host.store.decide_confirmation(request_id, ConfirmationStatus.DENIED)
            self._commit_confirmation_decision(session, request_id, record.command_name, "deny")
            return CommandResult(status="ok", output="Action denied by user")

        descriptor, snapshot = self._confirmation_command(record)
        self._host.check_session(session)
        read_only = self._host.session_read_only(session_id)
        effects = frozenset(descriptor.effects)
        # The confirmation is provenance, not privilege: the command's declared effects are
        # re-checked against the session's CURRENT stance. A pending write approved after the
        # session moved to read-only (e.g. /plan) must not mutate anything.
        if read_only and denied_under_restriction(effects):
            self._host.store.decide_confirmation(request_id, ConfirmationStatus.STALE)
            self._commit_confirmation_decision(session, request_id, record.command_name, "stale")
            return CommandResult(status="error", error="read-only policy denies command write effects")
        self._host.store.decide_confirmation(request_id, ConfirmationStatus.ALLOWED)
        root_action_id, initial_root_budget = self._host.capture_root_authorization(session)
        context = ToolContext(
            workspace=self._host.workspace,
            session_id=session_id,
            run_id=session.run_id or uuid4().hex,
            invocation_id=uuid4().hex,
            revision_id=self._host.revision_id,
            actor="user",
            logical_turn=uuid4().hex,
            root_action_id=root_action_id,
            initial_root_budget=initial_root_budget,
            origin_visible_tools=self._host.visible_tool_names_for_session(session_id, snapshot),
            origin_readonly=read_only,
        )
        plan = CommandPlan(
            descriptor=descriptor,
            arguments=record.arguments,
            expected_revision=expected_revision,
            effects=effects,
            read_only=read_only,
            session=session,
            root_action_id=root_action_id,
            initial_root_budget=initial_root_budget,
            context=context,
        )
        outcome = self._run_plugin(
            plan,
            decision_event=EventDraft(
                EventType.CONFIRMATION_DECIDED,
                f"plugin.{descriptor.owner}",
                "user",
                {"request_id": request_id, "command_name": record.command_name, "decision": "allow"},
            ),
            stage_pending=False,
        )
        if outcome.needs_deferred_drain():
            self._host.drain_deferred_requests(session_id)
        return outcome.result

    def _confirmation_command(self, record: ActionConfirmationRecord) -> tuple[CommandDescriptor, CommandSnapshot]:
        snapshot = self._host.snapshot
        if snapshot is None:
            raise RuntimeError("command runtime has no admitted capability snapshot")
        descriptor = snapshot.command_of(record.command_name)
        if descriptor is None:
            raise ValueError(f"Command '{record.command_name}' is not admitted")
        if record.owner != descriptor.owner:
            raise ValueError("Confirmation request owner does not match the admitted command owner")
        return descriptor, snapshot

    def _commit_confirmation_decision(
        self,
        session: SessionRecord,
        request_id: str,
        command_name: str,
        decision: Literal["deny", "stale"],
    ) -> None:
        """Record one decided confirmation durably, so clients can reduce a pending request."""
        try:
            self._host.commit_public_staged(
                session.session_id,
                run_id=session.run_id or request_id,
                status=session.status,
                events=(
                    EventDraft(
                        EventType.CONFIRMATION_DECIDED,
                        "core",
                        "user",
                        {"request_id": request_id, "command_name": command_name, "decision": decision},
                    ),
                ),
            )
        except StoreConflict:
            # The decision itself is durable; a concurrent writer only costs the audit fact.
            return

    def _dispatch(self, plan: CommandPlan) -> CommandOutcome:
        self._require_revision(plan.expected_revision)
        match plan.descriptor.owner:
            case "core":
                return self._invoke_core(plan)
            case _:
                return self._invoke_plugin(plan)

    def _require_revision(self, expected_revision: str) -> None:
        actual = self._host.revision_id
        if expected_revision != actual:
            # The public entrypoint checked before planning; this recheck closes the
            # gap between plan capture and dispatch, when activation may swap the snapshot.
            raise StaleRevision(
                f"displayed catalog revision {expected_revision} is stale; refresh the catalog",
                expected_revision,
                actual,
            )

    def _invoke_core(self, plan: CommandPlan) -> CommandOutcome:
        descriptor = plan.descriptor
        if descriptor.argument_schema:
            try:
                jsonschema.Draft202012Validator(json_wire_object(descriptor.argument_schema)).validate(json_wire_object(plan.arguments))
            except jsonschema.ValidationError as val_err:
                return CommandOutcome(CommandResult(status="error", error=f"Command arguments failed schema: {val_err.message}"))
        match descriptor.name:
            case "run":
                prompt = plan.arguments["prompt"]
                if not isinstance(prompt, str):
                    return CommandOutcome(CommandResult(status="error", error="Command arguments failed schema: prompt must be a string"))
                return CommandOutcome(core_command_result(self._host.run(prompt, session_id=plan.session.session_id)))
            case "resume":
                request_id = plan.arguments.get("approval_request_id")
                decision_raw = plan.arguments.get("decision")
                decision: Literal["allow", "deny"] | None = None
                if decision_raw is not None:
                    if decision_raw not in ("allow", "deny"):
                        return CommandOutcome(CommandResult(status="error", error="Command arguments failed schema: decision must be allow or deny"))
                    decision = "allow" if decision_raw == "allow" else "deny"
                return CommandOutcome(
                    core_command_result(
                        self._host.resume(
                            plan.session.session_id,
                            approval_request_id=str(request_id) if request_id is not None else None,
                            decision=decision,
                        )
                    )
                )
            case "show":
                return CommandOutcome(
                    CommandResult(status="ok", data=self._host.show(plan.session.session_id, events=bool(plan.arguments.get("events", False))))
                )
            case "status":
                return CommandOutcome(self._host.command_status(plan.session))
            case "help":
                return CommandOutcome(self._host.command_help())
            case name:
                raise ValueError(f"Unknown core command: {name}")

    def _invoke_plugin(self, plan: CommandPlan) -> CommandOutcome:
        denial = self._plugin_denial(plan)
        if denial is not None:
            return denial
        return self._run_plugin(plan)

    def _plugin_denial(self, plan: CommandPlan) -> CommandOutcome | None:
        descriptor = plan.descriptor
        session_id = plan.session.session_id
        if descriptor.busy_gate and plan.session.status not in _OPEN_STATUSES:
            raise ValueError(f"Session {session_id} is busy and command '{descriptor.name}' requires an idle session")
        self._host.check_session(plan.session)
        if plan.read_only and denied_under_restriction(plan.effects):
            return CommandOutcome(CommandResult(status="error", error="read-only policy denies command write effects"))
        if descriptor.argument_schema:
            try:
                jsonschema.Draft202012Validator(json_wire_object(descriptor.argument_schema)).validate(json_wire_object(plan.arguments))
            except jsonschema.ValidationError as val_err:
                return CommandOutcome(CommandResult(status="error", error=f"Command arguments failed schema: {val_err.message}"))
        return None

    def _run_plugin(
        self,
        plan: CommandPlan,
        *,
        decision_event: EventDraft | None = None,
        stage_pending: bool = True,
    ) -> CommandOutcome:
        descriptor = plan.descriptor
        session_id = plan.session.session_id
        with self._host.dispatch(f"command:{descriptor.name}") as snapshot, self._host.command_abort(plan.context) as context:
            outcome = snapshot.plugin_runtime.invoke_command(
                session_id,
                descriptor.name,
                plan.arguments,
                context=context,
                readonly=plan.read_only,
            )
            match outcome.result.status:
                case "pending_confirmation":
                    if not stage_pending:
                        # A confirmation never re-stages its own renewed request; it reports
                        # the pending result and leaves the recorded decision as it was.
                        return CommandOutcome(outcome.result, outcome.staged_events)
                    request = outcome.result.confirmation
                    if request is None:
                        raise ValueError("pending command result has no confirmation request")
                    self._stage_confirmation(plan, request)
                    return CommandOutcome(outcome.result)
                case "ok" | "error":
                    leading = () if decision_event is None else (decision_event,)
                    self._commit_plugin_outcome(plan, context, outcome, leading_events=leading)
                    return CommandOutcome(outcome.result, outcome.staged_events)
                case _:
                    raise ValueError(f"unsupported command status: {outcome.result.status}")

    def _stage_confirmation(self, plan: CommandPlan, request: ConfirmationRequest) -> None:
        session_id = plan.session.session_id
        latest = self._host.store.session(session_id)
        self._host.commit_public_staged(
            session_id,
            run_id=latest.run_id or plan.context.run_id,
            status=latest.status,
            events=(
                EventDraft(
                    EventType.CONFIRMATION_REQUESTED,
                    f"plugin.{plan.descriptor.owner}",
                    "client",
                    request.as_payload(),
                ),
            ),
            staged_confirmations=(
                StagedConfirmation(
                    request_id=request.request_id,
                    owner=plan.descriptor.owner,
                    command_name=plan.descriptor.name,
                    arguments=request.arguments,
                    ui_type=request.ui_type,
                    ui_data=request.ui_data,
                    expected_state_version=request.expected_state_version,
                ),
            ),
            root_action_id=plan.root_action_id,
            initial_root_budget=plan.initial_root_budget,
        )

    def _commit_plugin_outcome(
        self,
        plan: CommandPlan,
        context: ToolContext,
        outcome: CommandInvocationOutcome,
        *,
        leading_events: Sequence[EventDraft] = (),
    ) -> None:
        descriptor = plan.descriptor
        session_id = plan.session.session_id
        latest = self._host.store.session(session_id)
        completion = command_completion_event(self._host.workspace, descriptor.name, descriptor.owner, context, outcome)
        self._host.commit_public_staged(
            session_id,
            run_id=latest.run_id or plan.context.run_id,
            status=latest.status,
            events=(*leading_events, *outcome.staged_events, completion),
            staged_state=outcome.staged_state,
            staged_continuations=outcome.staged_continuations,
            staged_cancellations=outcome.staged_cancellations,
            staged_wakeups=outcome.staged_wakeups,
            staged_wakeup_cancels=outcome.staged_wakeup_cancels,
            staged_budgets=self._budget_updates(descriptor.owner, outcome.staged_budget),
            observer_receipts=outcome.staged_observer_receipts,
            root_action_id=plan.root_action_id,
            initial_root_budget=plan.initial_root_budget,
        )
        if self._host.auto_drain and outcome.staged_continuations:
            latest = self._host.store.session(session_id)
            if self._host.admits_fresh_work(latest):
                self._host.drain_continuations(session_id)
        if outcome.staged_cancellations:
            self._host.owner_cancel_claimed_work(
                session_id,
                outcome.staged_cancellations,
                f"owner {descriptor.owner} cancelled its durable work",
            )

    @staticmethod
    def _budget_updates(owner: str, staged_budget: PluginBudget | None | Literal["clear"]) -> tuple[StagedBudgetUpdate, ...]:
        if staged_budget == "clear":
            return (StagedBudgetUpdate(owner=owner, action="clear"),)
        if isinstance(staged_budget, PluginBudget):
            return (StagedBudgetUpdate(owner=staged_budget.owner, action="set", budget=staged_budget),)
        return ()
