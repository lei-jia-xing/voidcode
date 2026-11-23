"""Unit tests for tool execution domain (ToolExecutionManager, ToolExecutionHost, cancels_claimed)."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from voidcode.core.cancellation import Cancellation
from voidcode.core.engine.tool_execution import (
    ToolExecutionHost,
    ToolExecutionManager,
    ToolExecutionSnapshot,
    cancels_claimed,
)
from voidcode.core.events import EventType
from voidcode.core.execution import _Cursor, _Execution
from voidcode.core.plugins import FrozenStaged, ToolInvocationOutcome, ToolSuspension
from voidcode.core.storage.store import (
    EventDraft,
    ObserverReceiptRecord,
    SessionRecord,
    SessionStatus,
    StagedBudgetUpdate,
    StagedCancellation,
    StagedContinuationDraft,
    StagedStateUpdate,
    StoredEvent,
)
from voidcode.sdk.codec import decode_tool_result, encode_provider_result
from voidcode.sdk.provider import ProviderTurnResult
from voidcode.sdk.services import PluginBudget
from voidcode.sdk.suspend import SuspendPark
from voidcode.sdk.tools import (
    Change,
    ConfirmedStop,
    TextOutput,
    ToolCall,
    ToolDefinition,
    ToolFailure,
    ToolSuccess,
    UnconfirmedStop,
)


def _make_session(session_id: str = "sess-tool-1") -> SessionRecord:
    return SessionRecord(
        session_id=session_id,
        workspace="/tmp/ws",
        status=SessionStatus.RUNNING,
        active_revision_id="rev-1",
        run_id="run-1",
        turn=1,
        last_event_sequence=1,
    )


def _make_execution(session_id: str = "sess-tool-1", tool_calls: tuple[ToolCall, ...] = ()) -> _Execution:
    sess = _make_session(session_id)
    cursor = _Cursor(logical_turn="turn-1", batch_sequence=1 if tool_calls else None)
    events: list[StoredEvent] = []
    if tool_calls:
        turn_res = ProviderTurnResult(tool_calls=tool_calls)
        events.append(
            StoredEvent(
                session_id,
                1,
                "run-1",
                EventType.MODEL_TURN,
                "provider",
                "model",
                "rev-1",
                {"result": encode_provider_result(turn_res)},
            )
        )
    return _Execution(sess, cursor, events, "run-1")


def test_cancels_claimed() -> None:
    # None claimed id or owner
    cancel = StagedCancellation(owner="own-1", continuation_id="cont-1")
    assert not cancels_claimed(cancel, None, "own-1")
    assert not cancels_claimed(cancel, "cont-1", None)

    # Different owner
    assert not cancels_claimed(cancel, "cont-1", "foreign-owner")

    # Matching owner and exact continuation_id
    assert cancels_claimed(cancel, "cont-1", "own-1")

    # Matching owner and wildcard continuation_id (None)
    wildcard = StagedCancellation(owner="own-1", continuation_id=None)
    assert cancels_claimed(wildcard, "cont-1", "own-1")

    # Mismatched continuation_id
    mismatch = StagedCancellation(owner="own-1", continuation_id="cont-other")
    assert not cancels_claimed(mismatch, "cont-1", "own-1")


def test_tool_execution_manager_invoke_cancelled_aborts() -> None:
    host = MagicMock(spec=ToolExecutionHost)
    default_abort = Cancellation()
    host.default_abort = default_abort
    host.active_abort.return_value = None
    default_abort.set_cancelled(True, reason="user abort", stop_cause="user_stop")

    manager = ToolExecutionManager(host)
    execution = _make_execution()
    call = ToolCall(tool_name="test_tool", arguments={})
    defn = ToolDefinition(name="test_tool", description="", input_schema={})

    snapshot = MagicMock(spec=ToolExecutionSnapshot)

    @contextmanager
    def dispatch_cm(desc: str):
        yield snapshot

    host.dispatch.side_effect = dispatch_cm

    manager.invoke(execution, call, defn)
    host.stop.assert_called_once_with(execution, "user abort", stop_cause="user_stop")


def test_tool_execution_manager_invoke_native_tool() -> None:
    host = MagicMock(spec=ToolExecutionHost)
    host.workspace = Path("/tmp/ws")
    host.default_abort = Cancellation()
    host.active_abort.return_value = None
    host.bound_target.return_value = "rev-target"
    host.prepare_report.return_value = (ToolSuccess("core_reload", TextOutput("ok")), 10, False)

    snapshot = MagicMock(spec=ToolExecutionSnapshot)
    native_def = ToolDefinition(name="core_reload", description="", input_schema={})
    snapshot.native_tools = {"core_reload": native_def}
    snapshot.owner_of.return_value = "core"
    snapshot.definition_of.return_value = native_def

    @contextmanager
    def dispatch_cm(desc: str):
        yield snapshot

    host.dispatch.side_effect = dispatch_cm

    manager = ToolExecutionManager(host)
    call = ToolCall(tool_name="core_reload", arguments={"reason": "reload requested"})
    execution = _make_execution(tool_calls=(call,))

    # Host commit needs to record the intent sequence binding
    def fake_commit(exec_arg, status, kind, *events, checkpoint_bindings=(), **kwargs):
        if checkpoint_bindings:
            exec_arg.cursor.intent_sequence = 2
            exec_arg.events.append(
                StoredEvent("sess-tool-1", 2, "run-1", EventType.TOOL_INTENT, "core", "model", "rev-1", {"invocation_id": "inv-1"})
            )
        return ()

    host.commit.side_effect = fake_commit

    result = manager.invoke(execution, call, native_def)
    assert result is None
    # Verify commit called with intent and report called
    assert host.commit.call_count >= 1


def test_tool_execution_manager_invoke_regular_tool_success() -> None:
    host = MagicMock(spec=ToolExecutionHost)
    host.workspace = Path("/tmp/ws")
    host.default_abort = Cancellation()
    host.active_abort.return_value = None
    host.session_read_only.return_value = False
    host.visible_tools.return_value = ()
    host.prepare_report.return_value = (ToolSuccess("my_tool", TextOutput("ok")), 10, False)

    snapshot = MagicMock(spec=ToolExecutionSnapshot)
    snapshot.native_tools = {}
    snapshot.owner_of.return_value = "plugin-1"
    snapshot.definition_of.return_value = ToolDefinition(name="my_tool", description="", input_schema={})
    snapshot.revision_id = "rev-1"

    tool_res = ToolSuccess("my_tool", TextOutput("ok"))
    outcome = ToolInvocationOutcome(result=tool_res)
    snapshot.plugin_runtime.invoke.return_value = outcome

    @contextmanager
    def dispatch_cm(desc: str):
        yield snapshot

    host.dispatch.side_effect = dispatch_cm

    host.post_tool_hooks.side_effect = lambda exec_arg, snap_arg, c, res: res

    manager = ToolExecutionManager(host)
    call = ToolCall(tool_name="my_tool", arguments={})
    execution = _make_execution(tool_calls=(call,))
    defn = ToolDefinition(name="my_tool", description="", input_schema={})

    # Host commit records intent
    def fake_commit(exec_arg, status, kind, *events, checkpoint_bindings=(), **kwargs):
        if checkpoint_bindings:
            exec_arg.cursor.intent_sequence = 2
            exec_arg.events.append(
                StoredEvent("sess-tool-1", 2, "run-1", EventType.TOOL_INTENT, "core", "model", "rev-1", {"invocation_id": "inv-1"})
            )
        return ()

    host.commit.side_effect = fake_commit

    res = manager.invoke(execution, call, defn)
    assert res is None
    host.post_tool_hooks.assert_called_once()


def test_tool_execution_manager_park_suspended_tool_unattended() -> None:
    host = MagicMock(spec=ToolExecutionHost)
    host.default_abort = Cancellation()
    host.workspace = Path("/tmp/ws")
    host.prepare_report.return_value = (ToolFailure("ask_tool", "unattended"), 10, False)

    snapshot = MagicMock(spec=ToolExecutionSnapshot)
    snapshot.definition_of.return_value = ToolDefinition(name="ask_tool", description="", input_schema={})
    snapshot.owner_of.return_value = "plugin-1"

    @contextmanager
    def dispatch_cm(desc: str):
        yield snapshot

    host.dispatch.side_effect = dispatch_cm

    manager = ToolExecutionManager(host)
    call = ToolCall(tool_name="ask_tool", arguments={})
    execution = _make_execution(tool_calls=(call,))
    execution.interaction = "unattended"

    suspension = ToolSuspension(
        park=SuspendPark(park_kind="ask", intent={}),
        staged=FrozenStaged(),
    )

    res = manager.park_suspended_tool(execution, call, snapshot, suspension, Cancellation())
    assert res is None
    # Report should have been called with failure
    assert host.commit.call_count == 1
    committed_event_draft = host.commit.call_args[0][3]
    assert committed_event_draft.event_type == EventType.TOOL_COMPLETED
    assert committed_event_draft.payload["result"]["status"] == "error"


def test_tool_execution_manager_interrupt_batch() -> None:
    host = MagicMock(spec=ToolExecutionHost)
    host.workspace = Path("/tmp/ws")
    host.prepare_report.return_value = (ToolFailure("tool_a", "interrupted"), 10, False)

    snapshot = MagicMock(spec=ToolExecutionSnapshot)
    snapshot.definition_of.return_value = None
    snapshot.owner_of.return_value = ""

    @contextmanager
    def dispatch_cm(desc: str):
        yield snapshot

    host.dispatch.side_effect = dispatch_cm

    manager = ToolExecutionManager(host)
    call = ToolCall(tool_name="tool_a", arguments={})
    execution = _make_execution(tool_calls=(call,))

    ev = execution.events[0]
    host.pending_call.return_value = (ev, call)

    manager.interrupt_batch(execution)
    assert execution.cursor.batch_sequence is None
    assert host.commit.call_count == 1
    committed_event_draft = host.commit.call_args[0][3]
    assert committed_event_draft.event_type == EventType.TOOL_INTERRUPTED


def _cancelled_invocation(
    result: ToolSuccess | ToolFailure,
    **outcome_fields: object,
) -> tuple[MagicMock, list[tuple[str, tuple[EventDraft, ...], dict[str, object]]], _Execution, ToolCall, ToolDefinition, object]:
    """Invoke one tool against a cancelled run and record every host commit and stop."""
    host = MagicMock(spec=ToolExecutionHost)
    host.workspace = Path("/tmp/ws")
    host.default_abort = Cancellation()
    abort = Cancellation()
    host.active_abort.return_value = abort
    host.session_read_only.return_value = False
    host.visible_tools.return_value = ()
    host.prepare_report.side_effect = lambda reported: (reported, 10, False)
    sentinel = object()
    host.stop.return_value = sentinel

    definition = ToolDefinition(name="my_tool", description="", input_schema={})
    snapshot = MagicMock(spec=ToolExecutionSnapshot)
    snapshot.native_tools = {}
    snapshot.owner_of.return_value = "plugin-1"
    snapshot.definition_of.return_value = definition
    snapshot.revision_id = "rev-1"
    outcome = ToolInvocationOutcome(result=result, **outcome_fields)

    def invoke_outcome(*args: object, **kwargs: object) -> ToolInvocationOutcome:
        # Cancellation lands while the tool is running, after its intent is durable.
        abort.set_cancelled(True, reason="owner stopped the run", stop_cause="owner_cancel")
        return outcome

    snapshot.plugin_runtime.invoke.side_effect = invoke_outcome

    @contextmanager
    def dispatch_cm(desc: str):
        yield snapshot

    host.dispatch.side_effect = dispatch_cm

    timeline: list[tuple[str, tuple[EventDraft, ...], dict[str, object]]] = []

    def fake_commit(exec_arg, status, kind, *events, checkpoint_bindings=(), **kwargs):
        if checkpoint_bindings:
            exec_arg.cursor.intent_sequence = 2
            exec_arg.events.append(
                StoredEvent("sess-tool-1", 2, "run-1", EventType.TOOL_INTENT, "core", "model", "rev-1", {"invocation_id": "inv-1"})
            )
        timeline.append(("commit", events, kwargs))
        return ()

    host.commit.side_effect = fake_commit
    host.stop.side_effect = lambda *args, **kwargs: timeline.append(("stop", (), {})) or sentinel

    call = ToolCall(tool_name="my_tool", arguments={})
    execution = _make_execution(tool_calls=(call,))
    assert ToolExecutionManager(host).invoke(execution, call, definition) is sentinel
    return host, timeline, execution, call, definition, sentinel


@pytest.mark.parametrize(
    "observation",
    [ConfirmedStop(cancellation_signalled=True), UnconfirmedStop(cancellation_signalled=True)],
    ids=["confirmed", "unconfirmed"],
)
def test_cancelled_tool_stop_observation_is_committed_before_stop(observation) -> None:
    staged_state = (StagedStateUpdate("plugin-1", "session", "sess-tool-1", {"stopped": True}, 0, 1),)
    staged_events = (EventDraft("plugin.stop_staged", "plugin", "plugin", {"observed": True}),)
    staged_conts = (StagedContinuationDraft("cont-1", "plugin-1", "@core/test", {"queued": True}),)
    staged_cancels = (StagedCancellation(owner="plugin-1"),)
    staged_receipts = (ObserverReceiptRecord("sess-tool-1", 9, "observer"),)
    staged_budget = PluginBudget(owner="plugin-1", max_total_tokens=10)
    staged_observations = (Change("observed.txt", "update"),)
    result = ToolFailure("my_tool", "external effects unknown", execution=observation)

    host, timeline, execution, _call, _definition, _sentinel = _cancelled_invocation(
        result,
        staged_state=staged_state,
        staged_events=staged_events,
        staged_continuations=staged_conts,
        staged_cancellations=staged_cancels,
        staged_observer_receipts=staged_receipts,
        staged_budget=staged_budget,
        staged_observations=staged_observations,
    )

    # The result commits before the run stop, and the remaining batch is not touched here.
    assert [entry[0] for entry in timeline] == ["commit", "commit", "stop"]
    report_events = timeline[1][1]
    assert [event.event_type for event in report_events] == [EventType.TOOL_INTERRUPTED, "plugin.stop_staged"]
    decoded = decode_tool_result(report_events[0].payload["result"])
    assert isinstance(decoded, ToolFailure)
    assert decoded.execution == observation
    report_kwargs = timeline[1][2]
    assert report_kwargs["staged_state"] == staged_state
    assert report_kwargs["staged_continuations"] == staged_conts
    assert report_kwargs["staged_cancellations"] == staged_cancels
    assert report_kwargs["staged_budgets"] == (StagedBudgetUpdate(owner="plugin-1", action="set", budget=staged_budget),)
    assert report_kwargs["observer_receipts"] == staged_receipts
    prepared = host.prepare_report.call_args.args[0]
    assert isinstance(prepared, ToolFailure)
    assert prepared.changes == staged_observations
    host.post_tool_hooks.assert_not_called()
    host.stop.assert_called_once_with(execution, "tool invocation was cancelled", stop_cause="owner_cancel")


@pytest.mark.parametrize(
    "result",
    [ToolSuccess("my_tool", TextOutput("ok")), ToolFailure("my_tool", "ordinary cancellation")],
    ids=["success", "failure"],
)
def test_cancelled_tool_without_stop_observation_stops_without_report(result: ToolSuccess | ToolFailure) -> None:
    host, timeline, execution, _call, _definition, _sentinel = _cancelled_invocation(result)

    # Only the intent commit and the stop: an ordinary cancelled result is still not committed.
    assert [entry[0] for entry in timeline] == ["commit", "stop"]
    host.prepare_report.assert_not_called()
    host.post_tool_hooks.assert_not_called()
    host.stop.assert_called_once_with(execution, "tool invocation was cancelled", stop_cause="owner_cancel")
