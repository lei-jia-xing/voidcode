"""Unit tests for interactive_ask domain types, outcomes, and helper functions."""

from __future__ import annotations

import time
from types import MappingProxyType

from voidcode.core.engine.interactive_ask import (
    AskAnswered,
    AskBinding,
    AskCancelled,
    AskExpired,
    AskOutcome,
    is_ask_binding_expired,
    is_parked_intervening,
    render_ask_answers,
)
from voidcode.core.events import EventType
from voidcode.core.storage.store import StoredEvent
from voidcode.sdk.ask import AskMultiSelect, AskRequest
from voidcode.sdk.views import ChoiceOption, FormField


def test_ask_outcome_discriminated_union() -> None:
    answered: AskOutcome = AskAnswered(values={"q1": "val1"})
    cancelled: AskOutcome = AskCancelled()
    expired: AskOutcome = AskExpired()

    assert answered.status == "answered"
    assert answered.values == {"q1": "val1"}

    assert cancelled.status == "cancelled"
    assert cancelled.values == MappingProxyType({})

    assert expired.status == "expired"
    assert expired.values == MappingProxyType({})

    def classify(outcome: AskOutcome) -> str:
        match outcome:
            case AskAnswered(values=v):
                return f"answered:{len(v)}"
            case AskCancelled():
                return "cancelled"
            case AskExpired():
                return "expired"

    assert classify(answered) == "answered:1"
    assert classify(cancelled) == "cancelled"
    assert classify(expired) == "expired"


def test_is_ask_binding_expired() -> None:
    req_no_timeout = AskRequest(
        request_id="req-1",
        session_id="s1",
        revision_id="rev-1",
        owner="test",
        title="Prompt",
        questions=(FormField(name="q1", label="Q1"),),
        context=(),
        media=(),
        timeout_ms=None,
    )
    event = StoredEvent(
        session_id="s1",
        sequence=1,
        run_id="run-1",
        event_type=EventType.ASK_REQUESTED,
        source="core",
        actor="model",
        revision_id="rev-1",
        payload={},
    )
    binding_no_timeout = AskBinding(event=event, request=req_no_timeout, parked_at_ms=1000, idempotency_key=None)

    assert not is_ask_binding_expired(binding_no_timeout)

    req_with_timeout = AskRequest(
        request_id="req-2",
        session_id="s1",
        revision_id="rev-1",
        owner="test",
        title="Prompt",
        questions=(FormField(name="q1", label="Q1"),),
        context=(),
        media=(),
        timeout_ms=100,
    )
    # Parked in the past
    binding_expired = AskBinding(event=event, request=req_with_timeout, parked_at_ms=int(time.time() * 1000) - 200, idempotency_key="key1")
    assert is_ask_binding_expired(binding_expired)

    # Parked in the future
    binding_fresh = AskBinding(event=event, request=req_with_timeout, parked_at_ms=int(time.time() * 1000) + 10000, idempotency_key="key2")
    assert not is_ask_binding_expired(binding_fresh)


def test_render_ask_answers() -> None:
    request = AskRequest(
        request_id="req-3",
        session_id="s1",
        revision_id="rev-1",
        owner="test",
        title="Survey",
        questions=(
            FormField(name="name", label="User Name"),
            AskMultiSelect(
                name="choices",
                label="Selected Choices",
                options=(ChoiceOption(id="a", label="Option A"), ChoiceOption(id="b", label="Option B")),
            ),
            FormField(name="skipped", label="Optional Field"),
        ),
        context=(),
        media=(),
        timeout_ms=None,
    )
    values = {"name": "Alice", "choices": ["a", "b"]}
    rendered = render_ask_answers(request, values)

    assert "Ask: Survey" in rendered
    assert "User Name: Alice" in rendered
    assert 'Selected Choices: ["a", "b"]' in rendered
    assert "Optional Field: (no answer)" in rendered


def test_is_parked_intervening() -> None:
    allowed_core = StoredEvent(
        session_id="s1",
        sequence=2,
        run_id="run-1",
        event_type=EventType.CHILD_SPAWNED,
        source="core",
        actor="core",
        revision_id="rev-1",
        payload={},
    )
    assert is_parked_intervening(allowed_core)

    allowed_plugin = StoredEvent(
        session_id="s1",
        sequence=3,
        run_id="run-1",
        event_type="plugin.custom_event",
        source="plugin",
        actor="plugin",
        revision_id="rev-1",
        payload={},
    )
    assert is_parked_intervening(allowed_plugin)

    disallowed = StoredEvent(
        session_id="s1",
        sequence=4,
        run_id="run-1",
        event_type=EventType.USER_MESSAGE,
        source="client",
        actor="user",
        revision_id="rev-1",
        payload={},
    )
    assert not is_parked_intervening(disallowed)


def test_resolve_ask_binding_errors_and_success() -> None:
    from unittest.mock import MagicMock

    import pytest

    from voidcode.core.engine.interactive_ask import InteractiveAskHost, InteractiveAskManager
    from voidcode.core.execution import _Cursor, _Execution
    from voidcode.core.storage.store import SessionRecord, SessionStatus

    host = MagicMock(spec=InteractiveAskHost)
    manager = InteractiveAskManager(host)

    sess = SessionRecord(
        session_id="s1",
        workspace="/tmp",
        status=SessionStatus.WAITING,
        active_revision_id="rev-1",
        run_id="r1",
        turn=1,
        last_event_sequence=1,
    )

    # 1. No ask sequence or request id in cursor
    cursor_empty = _Cursor()
    exec_empty = _Execution(sess, cursor_empty, [], "a1")
    with pytest.raises(ValueError, match="no live ask binding"):
        manager.resolve_ask_binding(exec_empty, cursor_empty)

    from voidcode.sdk.views import FormField

    # 2. Mismatched request id
    req = AskRequest(
        request_id="real-req",
        session_id="s1",
        revision_id="rev1",
        owner="test",
        title="Prompt",
        questions=(FormField(name="q1", label="Prompt"),),
        context=(),
        media=(),
        timeout_ms=None,
    )
    event_mismatch = StoredEvent(
        session_id="s1",
        sequence=1,
        run_id="r1",
        event_type=EventType.ASK_REQUESTED,
        source="core",
        actor="model",
        revision_id="rev1",
        payload={"request": req.as_payload(), "parked_at_ms": 1000},
    )
    cursor_mismatch = _Cursor(ask_sequence=1, ask_request_id="other-req")
    exec_mismatch = _Execution(sess, cursor_mismatch, [event_mismatch], "a1")
    with pytest.raises(ValueError, match="does not match its committed request"):
        manager.resolve_ask_binding(exec_mismatch, cursor_mismatch)

    # 3. Dishonest park time
    event_bad_time = StoredEvent(
        session_id="s1",
        sequence=1,
        run_id="r1",
        event_type=EventType.ASK_REQUESTED,
        source="core",
        actor="model",
        revision_id="rev1",
        payload={"request": req.as_payload(), "parked_at_ms": -5},
    )
    cursor_match = _Cursor(ask_sequence=1, ask_request_id="real-req")
    exec_bad_time = _Execution(sess, cursor_match, [event_bad_time], "a1")
    with pytest.raises(ValueError, match="no honest park time"):
        manager.resolve_ask_binding(exec_bad_time, cursor_match)

    # 4. Success
    event_valid = StoredEvent(
        session_id="s1",
        sequence=1,
        run_id="r1",
        event_type=EventType.ASK_REQUESTED,
        source="core",
        actor="model",
        revision_id="rev1",
        payload={"request": req.as_payload(), "parked_at_ms": 1000, "idempotency_key": "k1"},
    )
    exec_valid = _Execution(sess, cursor_match, [event_valid], "a1")
    binding = manager.resolve_ask_binding(exec_valid, cursor_match)
    assert binding.request.request_id == "real-req"
    assert binding.parked_at_ms == 1000
    assert binding.idempotency_key == "k1"


def test_adopt_ask_terminal_branches() -> None:
    from unittest.mock import MagicMock

    import pytest

    from voidcode.core.engine.interactive_ask import InteractiveAskHost, InteractiveAskManager
    from voidcode.core.execution import _Cursor, _Execution
    from voidcode.core.storage.store import AskAnswerConflict, SessionRecord, SessionStatus
    from voidcode.sdk.ask import AskError
    from voidcode.sdk.codec import encode_tool_call
    from voidcode.sdk.tools import ToolCall, ToolFailure, ToolSuccess

    host = MagicMock(spec=InteractiveAskHost)
    manager = InteractiveAskManager(host)

    sess = SessionRecord(
        session_id="s1",
        workspace="/tmp",
        status=SessionStatus.WAITING,
        active_revision_id="rev-1",
        run_id="r1",
        turn=1,
        last_event_sequence=2,
    )
    call = ToolCall(tool_name="ask_user", arguments={"prompt": "test"}, tool_call_id="call-1")
    host.pending.return_value = (None, call)

    intent_event = StoredEvent(
        session_id="s1",
        sequence=1,
        run_id="r1",
        event_type=EventType.TOOL_INTENT,
        source="core",
        actor="model",
        revision_id="rev1",
        payload={"call": encode_tool_call(call)},
    )
    req = AskRequest(
        request_id="req-1",
        session_id="s1",
        revision_id="rev1",
        owner="test",
        title="Prompt",
        questions=(FormField(name="q1", label="Prompt"),),
        context=(),
        media=(),
        timeout_ms=None,
    )
    event_req = StoredEvent(
        session_id="s1",
        sequence=2,
        run_id="r1",
        event_type=EventType.ASK_REQUESTED,
        source="core",
        actor="model",
        revision_id="rev1",
        payload={"request": req.as_payload(), "parked_at_ms": 1000},
    )
    binding = AskBinding(event=event_req, request=req, parked_at_ms=1000, idempotency_key=None)

    # 1. Missing intent sequence
    cursor_no_intent = _Cursor()
    exec_no_intent = _Execution(sess, cursor_no_intent, [intent_event, event_req], "a1")
    with pytest.raises(ValueError, match="requires its parked tool intent"):
        manager.adopt_ask_terminal(exec_no_intent, binding, AskAnswered({}))

    # 2. AskAnswerConflict raises AskError
    cursor = _Cursor(intent_sequence=1, ask_sequence=2, ask_request_id="req-1")
    exec_ok = _Execution(sess, cursor, [intent_event, event_req], "a1")
    host.report.side_effect = AskAnswerConflict("s1", "req-1")
    with pytest.raises(AskError, match="was already answered"):
        manager.adopt_ask_terminal(exec_ok, binding, AskAnswered({}))

    # 3. Successful answered
    host.report.side_effect = None
    manager.adopt_ask_terminal(exec_ok, binding, AskAnswered({}))
    args, kwargs = host.report.call_args
    assert args[1] == call
    assert isinstance(args[2], ToolSuccess)
    assert kwargs["ask_answer_guard"] == "req-1"

    # 4. Cancelled
    manager.adopt_ask_terminal(exec_ok, binding, AskCancelled())
    args, kwargs = host.report.call_args
    assert isinstance(args[2], ToolFailure)
    assert "cancelled by the user" in args[2].error

    # 5. Expired
    manager.adopt_ask_terminal(exec_ok, binding, AskExpired())
    args, kwargs = host.report.call_args
    assert isinstance(args[2], ToolFailure)
    assert "expired without an answer" in args[2].error
