"""Unit tests for tool execution domain (ToolExecutionManager, ToolExecutionHost, cancels_claimed)."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

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
    SessionRecord,
    SessionStatus,
    StagedCancellation,
    StoredEvent,
)
from voidcode.sdk.codec import encode_provider_result
from voidcode.sdk.provider import ProviderTurnResult
from voidcode.sdk.suspend import SuspendPark
from voidcode.sdk.tools import (
    TextOutput,
    ToolCall,
    ToolDefinition,
    ToolFailure,
    ToolSuccess,
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
