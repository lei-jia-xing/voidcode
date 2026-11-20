"""Background-action tool waits: the domain side of deferred background results.

The `BackgroundActionRegistry` owns claim/live/abort state and the driver loop. This
module owns what a *running* driver does while it waits for a tool request it queued:
reconcile lost waits after restart, drain still-owed tool requests for foreground
sessions, and turn one committed deferred result or cancellation into a
`ToolAwaitResult`. It never takes the session run lock: a background driver's commits
must not block (or be blocked by) a model turn.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from uuid import uuid4

from ..sdk.client import BudgetExhaustion
from ..sdk.codec import decode_tool_call, decode_tool_result
from ..sdk.hooks import StopCause
from ..sdk.invocation import ToolContext
from ..sdk.json_values import json_wire_object, own_json_object
from ..sdk.services import ArtifactPage, PluginBudget, ToolAwaitResult, ToolInvocationReceipt
from ..sdk.tools import ConfirmedStop, ExecutionObservation, ToolFailure, UnconfirmedStop
from .background_actions import BackgroundActionRegistry
from .cancellation import _EventAbortSignal
from .coverage import read_artifact_content
from .events import EventType
from .plugins import PluginRuntime
from .storage.store import (
    ContinuationRecord,
    EventDraft,
    SequenceConflict,
    SessionRecord,
    SqliteStore,
    StagedBudgetUpdate,
    StagedCancellation,
    StoreConflict,
    StoredEvent,
)


class BackgroundSnapshot(Protocol):
    """One capability generation the executor may run under."""

    @property
    def revision_id(self) -> str: ...

    @property
    def plugin_runtime(self) -> PluginRuntime: ...


class BackgroundToolContext(Protocol):
    """The driver handle facts this module may read; the engine owns construction."""

    @property
    def action_id(self) -> str: ...

    @property
    def abort_signal(self) -> threading.Event: ...

    @property
    def owner(self) -> str: ...

    @property
    def session_id(self) -> str: ...

    @property
    def revision_id(self) -> str: ...


class BackgroundExecutionHost(Protocol):
    """The core capabilities the background executor may use, stated explicitly."""

    @property
    def store(self) -> SqliteStore: ...

    @property
    def workspace(self) -> Path: ...

    @property
    def closed(self) -> bool: ...

    def ensure_open(self) -> None: ...

    def session(self, session_id: str) -> SessionRecord: ...

    def admits_fresh_work(self, session: SessionRecord) -> bool: ...

    def cancel_background_tool_requests(
        self,
        session_id: str,
        action_id: str,
        *,
        status: Literal["cancelled", "interrupted", "core_lost", "budget_exhausted"],
        stop_cause: StopCause | None,
        reason: str,
    ) -> None: ...

    def drain_deferred_requests(
        self,
        session_id: str,
        *,
        background_action_id: str | None = None,
        action_abort: threading.Event | None = None,
    ) -> None: ...

    def artifact_page(self, digest: str, offset: int, limit: int) -> ArtifactPage: ...

    @property
    def snapshot(self) -> BackgroundSnapshot | None: ...

    def dispatch(self, description: str) -> AbstractContextManager[BackgroundSnapshot]: ...

    def check_session(self, session: SessionRecord) -> None: ...

    def visible_tool_names_for_session(self, session_id: str, snapshot: Any) -> tuple[str, ...]: ...

    def session_read_only(self, session_id: str) -> bool: ...

    def notify_commit(self) -> None: ...

    def owner_cancel_claimed_work(
        self,
        session_id: str,
        cancellations: Sequence[StagedCancellation],
        reason: str,
    ) -> None: ...

    def assert_background_wait_unleased(self) -> None: ...


def _wire_object(value: object, what: str) -> Mapping[str, object]:
    """Require a JSON object from a durable payload, failing visibly when malformed."""
    if not isinstance(value, Mapping):
        raise ValueError(f"{what} must be a JSON object")
    return json_wire_object(value)


def _budget_exhaustion_from_payload(value: object) -> BudgetExhaustion | None:
    if not isinstance(value, Mapping):
        return None
    try:
        return BudgetExhaustion(
            dimension=cast(Any, value["dimension"]),
            source=cast(Any, value["source"]),
            limit=cast(int, value["limit"]),
            consumed=cast(int, value["consumed"]),
            remaining=cast(int, value["remaining"]),
            continue_with=cast(Any, value["continue_with"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("tool wait contains a malformed budget exhaustion") from exc


def _execution_from_payload(value: object) -> ExecutionObservation | None:
    if not isinstance(value, Mapping):
        return None
    signalled = value.get("cancellation_signalled")
    stopped = value.get("execution_stopped")
    side_effect_state = value.get("side_effect_state")
    if type(signalled) is not bool or type(stopped) is not bool:
        raise ValueError("tool wait contains malformed execution metadata")
    if side_effect_state == "settled" and stopped:
        return ConfirmedStop(cancellation_signalled=signalled)
    if side_effect_state == "unknown" and not stopped:
        return UnconfirmedStop(cancellation_signalled=signalled)
    raise ValueError("tool wait contains inconsistent execution metadata")


class BackgroundContext:
    """The long-lived driver handle for one claimed background action.

    The driver keeps only this object across its whole run. Each `invoke` opens a fresh
    owner/revision-bound lease, runs the callback, and commits its staged work atomically —
    so progress is durable invoke by invoke, never at the end of the run. Commits never
    take the session run lock: the driver must not block (or be blocked by) model turns.
    """

    def __init__(
        self,
        execution: BackgroundExecution,
        *,
        action_id: str,
        owner: str,
        session_id: str,
        action: Mapping[str, object],
        revision_id: str,
        root_action_id: str,
        initial_root_budget: int | None,
        origin_visible_tools: tuple[str, ...] | None,
        origin_readonly: bool | None,
        abort: threading.Event,
    ) -> None:
        self._execution = execution
        self._action_id = action_id
        self._owner = owner
        self._session_id = session_id
        self._action = own_json_object(action)
        self._revision_id = revision_id
        self._root_action_id = root_action_id
        self._initial_root_budget = initial_root_budget
        self._origin_visible_tools = origin_visible_tools
        self._origin_readonly = origin_readonly
        self._abort = abort

    @property
    def action_id(self) -> str:
        return self._action_id

    @property
    def owner(self) -> str:
        return self._owner

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def revision_id(self) -> str:
        return self._revision_id

    @property
    def payload(self) -> Mapping[str, object]:
        return self._action

    @property
    def abort_signal(self) -> threading.Event:
        return self._abort

    def invoke[T](self, callback: Callable[[ToolContext, Any], T]) -> T:
        return self._execution.invoke(self, callback)

    def wait_tool(self, receipt: ToolInvocationReceipt) -> ToolAwaitResult:
        self._execution.assert_wait_unleased()
        return self._execution.await_tool(self, receipt)


class BackgroundExecution:
    """Reconcile, drain, and await background tool requests over durable events."""

    def __init__(self, host: BackgroundExecutionHost, background: BackgroundActionRegistry) -> None:
        self._host = host
        self._background = background

    def assert_wait_unleased(self) -> None:
        """Refuse waits from any active plugin lease or Core dispatch on this thread."""
        self._host.assert_background_wait_unleased()

    def drive(self, claimed: ContinuationRecord, abort: threading.Event) -> None:
        """Run one claimed action's owner driver, then settle the row terminally."""
        action_id = claimed.continuation_id
        terminal: Literal["completed", "failed", "cancelled"] = "completed"
        failure: str | None = None
        try:
            if abort.is_set() or self._host.closed:
                raise InterruptedError(f"background action {action_id!r} was cancelled before dispatch")
            action = _wire_object(claimed.payload.get("action"), f"background action {action_id!r} envelope")
            root_action_id = claimed.root_id
            root_budget = claimed.payload.get("root_action_budget")
            if not isinstance(root_action_id, str) or not root_action_id:
                raise StoreConflict(f"background action {action_id!r} has no captured root authorization")
            if root_budget is not None and (type(root_budget) is not int or root_budget < 0):
                raise StoreConflict(f"background action {action_id!r} has an invalid captured root budget")
            raw_authority = claimed.payload.get("tool_wait_authority")
            origin_visible_tools: tuple[str, ...] | None = None
            origin_readonly: bool | None = None
            if isinstance(raw_authority, Mapping):
                raw_tools = raw_authority.get("visible_tools")
                raw_readonly = raw_authority.get("readonly")
                if (
                    not isinstance(raw_tools, (list, tuple))
                    or not all(isinstance(name, str) and name for name in raw_tools)
                    or type(raw_readonly) is not bool
                    or raw_authority.get("revision_id") != claimed.revision_id
                ):
                    raise StoreConflict(f"background action {action_id!r} has malformed captured tool-wait authority")
                origin_visible_tools = tuple(raw_tools)
                origin_readonly = raw_readonly
            elif raw_authority is not None:
                raise StoreConflict(f"background action {action_id!r} has malformed captured tool-wait authority")
            context = BackgroundContext(
                self,
                action_id=action_id,
                owner=claimed.owner,
                session_id=claimed.session_id,
                action=action,
                revision_id=claimed.revision_id,
                root_action_id=root_action_id,
                initial_root_budget=root_budget,
                origin_visible_tools=origin_visible_tools,
                origin_readonly=origin_readonly,
                abort=abort,
            )
            snapshot = self._host.snapshot
            if snapshot is None:
                raise RuntimeError("core has no capability generation")
            driver = snapshot.plugin_runtime.background_driver(claimed.owner)
            if driver is None:
                raise RuntimeError(f"owner {claimed.owner!r} registers no background driver")
            driver(context)
            terminal = "cancelled" if abort.is_set() else "completed"
        except InterruptedError:
            terminal = "cancelled"
            failure = None
        except BaseException as exc:
            # Any driver exit — including SystemExit or an asyncio cancellation escaping
            # the script — settles failed with its real type recorded. This runs on the
            # worker thread only, so it can never swallow the main thread's Ctrl-C, and
            # the live registries below always clear in `finally`.
            terminal = "failed"
            failure = f"{type(exc).__name__}: {exc}"
        try:
            self._host.store.complete_background_action(action_id, status=terminal, error=failure)
        except StoreConflict:
            # The only provable race: a concurrent cancel settled the row first, so the
            # driver's outcome is moot against an already-terminal row. Anything else
            # propagates visibly out of the worker; cleanup still runs in `finally`.
            pass
        finally:
            self._background.settle_driver(action_id)
            snapshot = self._host.snapshot
            if snapshot is not None:
                snapshot.plugin_runtime.background_live_actions.discard(action_id)

    def invoke[T](self, context: BackgroundContext, callback: Callable[[ToolContext, Any], T]) -> T:
        """Run one driver callback under a fresh lease and commit its staged work atomically."""
        from .services import RuntimeServices

        host = self._host
        action_id = context.action_id
        owner = context.owner
        session_id = context.session_id
        revision_id = context.revision_id
        abort = context.abort_signal
        if abort.is_set() or host.closed:
            raise InterruptedError(f"background action {action_id!r} was cancelled")
        staged_cancels: tuple[StagedCancellation, ...] = ()
        staged_events: tuple[EventDraft, ...] = ()
        with host.dispatch(f"background:{owner}") as snapshot:
            if snapshot.revision_id != revision_id:
                raise InterruptedError(f"background action {action_id!r} belongs to a retired revision")
            runtime = snapshot.plugin_runtime
            session = host.store.session(session_id)
            host.check_session(session)
            root_state = host.store.root_budget_state(context._root_action_id)
            if not context._root_action_id or (
                context._initial_root_budget is not None and (root_state is None or root_state[0] < context._initial_root_budget)
            ):
                raise StoreConflict(f"background action {action_id!r} lost its captured finite root authorization")
            if context._initial_root_budget is None and root_state is not None:
                raise StoreConflict(f"background action {action_id!r} captured an uncapped root with a finite budget row")
            effective_origin_tools = None
            effective_origin_readonly = context._origin_readonly
            if context._origin_visible_tools is not None and context._origin_readonly is not None:
                current_visible = set(host.visible_tool_names_for_session(session_id, snapshot))
                effective_origin_tools = tuple(name for name in context._origin_visible_tools if name in current_visible)
                effective_origin_readonly = context._origin_readonly or host.session_read_only(session_id)
            signal = _EventAbortSignal(abort)
            tool_context = ToolContext(
                workspace=host.workspace,
                session_id=session_id,
                run_id=action_id,
                invocation_id=uuid4().hex,
                revision_id=snapshot.revision_id,
                actor="plugin",
                logical_turn=action_id,
                root_action_id=context._root_action_id,
                initial_root_budget=context._initial_root_budget,
                origin_visible_tools=effective_origin_tools,
                origin_readonly=effective_origin_readonly,
                background_action_id=action_id,
                abort_signal=signal,
            )
            lease = runtime._open_lease(  # noqa: SLF001 - the core owns lease lifecycles
                owner=owner,
                session_id=session_id,
                run_id=action_id,
                invocation_id=tool_context.invocation_id,
                readonly=host.session_read_only(session_id) or effective_origin_readonly is True,
                actor="plugin",
                internal_work=True,
                root_action_id=context._root_action_id,
                initial_root_budget=context._initial_root_budget,
                origin_visible_tools=effective_origin_tools,
                origin_readonly=effective_origin_readonly,
                background_action_id=action_id,
                abort_signal=signal,
            )
            services = RuntimeServices(runtime, lease)
            try:
                result = callback(tool_context, services)
            except BaseException:
                runtime._revoke_lease(lease)  # noqa: SLF001 - the core owns lease lifecycles
                raise
            staged = runtime._staged_changes(lease)  # noqa: SLF001 - the core owns lease lifecycles
            runtime._revoke_lease(lease)  # noqa: SLF001 - the core owns lease lifecycles
            if abort.is_set() or host.closed:
                raise InterruptedError(f"background action {action_id!r} was cancelled")
            staged_state, staged_events, staged_conts, staged_cancels, staged_receipts, staged_budget = staged
            staged_budget_updates: list[StagedBudgetUpdate] = []
            if staged_budget == "clear":
                staged_budget_updates.append(StagedBudgetUpdate(owner=owner, action="clear"))
            elif isinstance(staged_budget, PluginBudget):
                staged_budget_updates.append(StagedBudgetUpdate(owner=staged_budget.owner, action="set", budget=staged_budget))
            committed = False
            for _attempt in range(8):
                if abort.is_set() or host.closed:
                    raise InterruptedError(f"background action {action_id!r} was cancelled")
                latest = host.store.session(session_id)
                try:
                    host.store.commit_staged(
                        session_id,
                        expected_sequence=latest.last_event_sequence,
                        expected_run_id=latest.run_id,
                        run_id=latest.run_id or action_id,
                        revision_id=snapshot.revision_id,
                        events=tuple(staged_events),
                        status=latest.status,
                        checkpoint_kind=None,
                        checkpoint_payload=None,
                        staged_state=staged_state,
                        staged_continuations=staged_conts,
                        staged_cancellations=staged_cancels,
                        observer_receipts=staged_receipts,
                        staged_budgets=tuple(staged_budget_updates),
                    )
                    host.notify_commit()
                except SequenceConflict:
                    continue
                committed = True
                break
            if not committed:
                raise SequenceConflict(f"background action {action_id!r} could not commit its staged work")
        self._background.notify_tool_waiters()
        if staged_cancels:
            host.owner_cancel_claimed_work(
                session_id,
                staged_cancels,
                f"owner {owner} cancelled its durable work",
            )
        if any(event.event_type == EventType.TOOL_DEFERRED and event.payload.get("background_action_id") == action_id for event in staged_events):
            host.drain_deferred_requests(session_id, background_action_id=action_id, action_abort=abort)
        return result

    @staticmethod
    def terminal_request_ids(events: Sequence[StoredEvent]) -> set[str]:
        stop_keys = {
            (event.payload.get("background_action_id"), event.payload.get("deferred_request_id"))
            for event in events
            if event.event_type == EventType.RUN_INTERRUPTED
        }
        stop_causes = {"completed", "error", "user_stop", "owner_cancel", "limit_reached", "shutdown"}
        terminal: set[str] = set()
        for event in events:
            request_id = event.payload.get("request_id")
            if not isinstance(request_id, str):
                continue
            if event.event_type == EventType.TOOL_DEFERRED_CANCELLED:
                terminal.add(request_id)
            elif event.event_type == EventType.TOOL_COMPLETED and event.payload.get("deferred") is True:
                terminal.add(request_id)
            elif (
                event.event_type == EventType.TOOL_INTERRUPTED
                and event.payload.get("deferred") is True
                and (event.payload.get("stop_cause") in stop_causes or (event.payload.get("background_action_id"), request_id) in stop_keys)
            ):
                terminal.add(request_id)
        return terminal

    def reconcile_lost(self, session_id: str) -> None:
        events = list(self._host.store.events(session_id))
        terminal_ids = self.terminal_request_ids(events)
        action_ids = {
            event.payload.get("background_action_id")
            for event in events
            if event.event_type == EventType.TOOL_DEFERRED
            and event.payload.get("result_delivery") == "requester"
            and event.payload.get("request_id") not in terminal_ids
            and isinstance(event.payload.get("background_action_id"), str)
        }
        for raw_action_id in action_ids:
            action_id = cast(str, raw_action_id)
            record = self._host.store.background_action(action_id)
            if record is not None and record.status == "pending":
                continue
            if record is not None and record.status == "claimed" and self._background.claim_live(record):
                continue
            if record is None or record.status in {"claimed", "interrupted"}:
                status: Literal["cancelled", "interrupted", "core_lost", "budget_exhausted"] = "core_lost"
                stop_cause: StopCause | None = None
                reason = "background script was lost before its tool request reached a terminal result"
            elif record.status == "cancelled":
                status = "cancelled"
                stop_cause = "owner_cancel"
                reason = "background action was cancelled before its tool request completed"
            elif record.status in {"failed", "completed"}:
                status = "interrupted"
                stop_cause = "error"
                reason = "background driver settled before its tool request reached a terminal result"
            else:
                raise StoreConflict(f"background action {action_id!r} has unknown status {record.status!r}")
            self._host.cancel_background_tool_requests(
                session_id,
                action_id,
                status=status,
                stop_cause=stop_cause,
                reason=reason,
            )

    def drain_pending(self, session_id: str) -> None:
        while self._host.admits_fresh_work(self._host.store.session(session_id)):
            events = list(self._host.store.events(session_id))
            terminal_ids = self.terminal_request_ids(events)
            request = next(
                (
                    event
                    for event in events
                    if event.event_type == EventType.TOOL_DEFERRED
                    and event.payload.get("result_delivery") == "requester"
                    and event.payload.get("request_id") not in terminal_ids
                    and isinstance(event.payload.get("background_action_id"), str)
                ),
                None,
            )
            if request is None:
                return
            action_id = cast(str, request.payload["background_action_id"])
            record = self._host.store.background_action(action_id)
            action_abort = self._background.abort_event(action_id)
            live = self._background.is_live(action_id)
            if record is None or record.status != "claimed" or not live:
                self.reconcile_lost(session_id)
                record = self._host.store.background_action(action_id)
                action_abort = self._background.abort_event(action_id)
                live = self._background.is_live(action_id)
                if record is None or record.status != "claimed" or not live:
                    return
            if action_abort is None:
                raise StoreConflict(f"live background action {action_id!r} has no cancellation binding")
            before = self._host.store.session(session_id).last_event_sequence
            self._host.drain_deferred_requests(session_id, background_action_id=action_id, action_abort=action_abort)
            current = self._host.store.session(session_id)
            if current.last_event_sequence <= before or not self._host.admits_fresh_work(current):
                return

    def await_tool(self, context: BackgroundToolContext, receipt: ToolInvocationReceipt) -> ToolAwaitResult:
        host = self._host
        if not isinstance(receipt, ToolInvocationReceipt):
            raise TypeError("background tool wait requires a ToolInvocationReceipt")
        events = host.store.events(context.session_id)
        request = next(
            (event for event in events if event.event_type == EventType.TOOL_DEFERRED and event.payload.get("request_id") == receipt.request_id),
            None,
        )
        if request is None:
            raise ValueError(f"tool request {receipt.request_id!r} was not committed by this background action")
        call = decode_tool_call(_wire_object(request.payload["call"], "deferred tool call"))
        if (
            request.payload.get("background_action_id") != context.action_id
            or request.payload.get("requester") != context.owner
            or request.payload.get("expected_revision") != context.revision_id
            or request.payload.get("result_delivery") != "requester"
            or call.tool_call_id != receipt.request_id
            or call.tool_name != receipt.tool_name
            or request.payload.get("depth") != receipt.depth
        ):
            raise ValueError("tool request receipt does not match this background action's captured identity")
        while True:
            outcome = self._tool_outcome(context, request)
            if outcome is not None:
                return outcome
            record = host.store.background_action(context.action_id)
            if record is None:
                raise ValueError(f"background action {context.action_id!r} no longer exists")
            if record.owner != context.owner or record.session_id != context.session_id:
                raise ValueError("background action identity changed while awaiting its tool request")
            if host.closed:
                host.cancel_background_tool_requests(
                    context.session_id,
                    context.action_id,
                    status="interrupted",
                    stop_cause="shutdown",
                    reason="core shutdown interrupted the background tool wait",
                )
            elif context.abort_signal.is_set():
                stop_cause = self._background.abort_cause(context.action_id)
                status = "cancelled" if stop_cause == "owner_cancel" else "interrupted"
                host.cancel_background_tool_requests(
                    context.session_id,
                    context.action_id,
                    status=status,
                    stop_cause=stop_cause,
                    reason="background action stopped",
                )
            elif record.status == "interrupted":
                host.cancel_background_tool_requests(
                    context.session_id,
                    context.action_id,
                    status="core_lost",
                    stop_cause=None,
                    reason="background driver did not survive the previous core process",
                )
            elif record.status == "failed":
                host.cancel_background_tool_requests(
                    context.session_id,
                    context.action_id,
                    status="interrupted",
                    stop_cause="error",
                    reason="background driver failed before completing its deferred tool wait",
                )
            elif record.status == "cancelled":
                host.cancel_background_tool_requests(
                    context.session_id,
                    context.action_id,
                    status="cancelled",
                    stop_cause="owner_cancel",
                    reason="background action was cancelled",
                )
            elif record.status == "completed":
                host.cancel_background_tool_requests(
                    context.session_id,
                    context.action_id,
                    status="interrupted",
                    stop_cause="error",
                    reason="background driver completed without a terminal result for its tool request",
                )
            outcome = self._background.wait_for_tool_outcome(lambda: self._tool_outcome(context, request))
            if outcome is not None:
                return outcome

    def _tool_outcome(self, context: BackgroundToolContext, request: StoredEvent) -> ToolAwaitResult | None:
        host = self._host
        request_id = str(request.payload["request_id"])
        request_call = decode_tool_call(_wire_object(request.payload["call"], "deferred tool call"))
        events = host.store.events(context.session_id, after_sequence=request.sequence)
        tool_event = next(
            (
                event
                for event in events
                if event.event_type in {EventType.TOOL_COMPLETED, EventType.TOOL_INTERRUPTED}
                and event.payload.get("deferred") is True
                and event.payload.get("request_id") == request_id
                and event.payload.get("background_action_id") == context.action_id
            ),
            None,
        )
        cancellation = next(
            (
                event
                for event in events
                if event.event_type == EventType.TOOL_DEFERRED_CANCELLED
                and event.payload.get("request_id") == request_id
                and event.payload.get("background_action_id") == context.action_id
            ),
            None,
        )
        if (
            tool_event is not None
            and tool_event.event_type == EventType.TOOL_INTERRUPTED
            and tool_event.payload.get("stop_cause") is None
            and cancellation is not None
            and not any(
                event.event_type == EventType.RUN_INTERRUPTED
                and event.payload.get("deferred_request_id") == request_id
                and event.payload.get("background_action_id") == context.action_id
                for event in events
            )
        ):
            tool_event = None
        if tool_event is not None:
            if _wire_object(tool_event.payload["call"], "deferred result call") != _wire_object(request.payload["call"], "deferred request call"):
                raise ValueError("deferred terminal result changed its request call")
            if tool_event.event_type == EventType.TOOL_INTERRUPTED:
                stop_event = next(
                    (
                        event
                        for event in events
                        if event.event_type == EventType.RUN_INTERRUPTED
                        and event.payload.get("deferred_request_id") == request_id
                        and event.payload.get("background_action_id") == context.action_id
                    ),
                    None,
                )
                stop_cause_value = tool_event.payload.get("stop_cause")
                if stop_cause_value is not None:
                    stop_cause = cast("StopCause | None", stop_cause_value)
                elif stop_event is not None:
                    stop_cause = cast("StopCause | None", stop_event.payload.get("stop_cause"))
                else:
                    return None
                budget_value = tool_event.payload.get("budget_exhaustion")
                if budget_value is None and stop_event is not None:
                    budget_value = stop_event.payload.get("budget_exhaustion")
                terminal_sequence = tool_event.sequence if stop_event is None else stop_event.sequence
            else:
                stop_cause = None
                budget_value = None
                terminal_sequence = tool_event.sequence
            raw_result = _wire_object(tool_event.payload["result"], "deferred tool result")
            result = decode_tool_result(raw_result)
            if result.tool_name != request_call.tool_name:
                raise ValueError("deferred terminal result changed its tool identity")
            reference = result.result_reference
            if reference is not None:
                try:
                    canonical = read_artifact_content(reference, host.artifact_page)
                    complete = decode_tool_result(_wire_object(json.loads(canonical), "canonical tool result"))
                    if complete.tool_name != request_call.tool_name:
                        raise ValueError("canonical tool result changed tool identity")
                    result = replace(complete, result_reference=reference)
                except Exception as exc:
                    raise RuntimeError(f"complete result for tool request {request_id!r} could not be hydrated: {exc}") from exc
            budget = _budget_exhaustion_from_payload(budget_value)
            if tool_event.event_type == EventType.TOOL_COMPLETED:
                status: Literal["completed", "interrupted", "cancelled", "core_lost", "budget_exhausted"] = "completed"
            elif stop_cause == "limit_reached" and budget is not None:
                status = "budget_exhausted"
            elif stop_cause in {"user_stop", "owner_cancel"}:
                status = "cancelled"
            else:
                status = "interrupted"
            return ToolAwaitResult(
                request_id=request_id,
                owner=context.owner,
                action_id=context.action_id,
                session_id=context.session_id,
                revision_id=context.revision_id,
                tool_name=result.tool_name,
                status=status,
                result=result,
                terminal_sequence=terminal_sequence,
                stop_cause=stop_cause,
                budget_exhaustion=budget,
                execution=result.execution if isinstance(result, ToolFailure) else None,
            )
        if cancellation is None:
            return None
        payload = cancellation.payload
        if (
            payload.get("owner") != context.owner
            or payload.get("revision_id") != context.revision_id
            or payload.get("tool_name") != request_call.tool_name
        ):
            raise ValueError("background tool cancellation changed its owner, revision, or tool identity")
        status_value = payload.get("status")
        if status_value not in {"cancelled", "interrupted", "core_lost", "budget_exhausted"}:
            raise ValueError("background tool cancellation event has an unknown terminal status")
        status = cast("Literal['cancelled', 'interrupted', 'core_lost', 'budget_exhausted']", status_value)
        return ToolAwaitResult(
            request_id=request_id,
            owner=context.owner,
            action_id=context.action_id,
            session_id=context.session_id,
            revision_id=context.revision_id,
            tool_name=request_call.tool_name,
            status=status,
            result=None,
            terminal_sequence=cancellation.sequence,
            stop_cause=cast("StopCause | None", payload.get("stop_cause")),
            budget_exhaustion=_budget_exhaustion_from_payload(payload.get("budget_exhaustion")),
            execution=_execution_from_payload(payload.get("execution")),
        )
