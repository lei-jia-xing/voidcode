"""Unit tests for turn execution domain (TurnDriver, CoreResult, tool call extractors)."""

from __future__ import annotations

import json

import pytest

from voidcode.core.engine.turn_execution import (
    CoreResult,
    _tool_call_from_entry,
    _tool_calls_from_content,
)
from voidcode.core.events import EventType
from voidcode.core.execution import _Cursor, _Execution
from voidcode.core.storage.store import SessionRecord, SessionStatus, StoredEvent
from voidcode.sdk.provider import ProviderExecutionError
from voidcode.sdk.services import BudgetExhaustion
from voidcode.sdk.tools import ToolCall


def test_core_result_dataclass_and_payload() -> None:
    res = CoreResult(
        session_id="s1",
        run_id="r1",
        revision_id="rev1",
        status="ok",
        output="hello",
        pending_approval={"action": "test"},
        pending_ask={"question": "sure?"},
        stop_cause="user_stop",
        budget_exhaustion=BudgetExhaustion(
            dimension="provider_turns",
            source="max_steps",
            limit=10,
            consumed=10,
            remaining=0,
            continue_with="resume_with_max_steps",
        ),
    )

    assert res.session_id == "s1"
    assert res.run_id == "r1"
    assert res.output == "hello"
    assert res.status == "ok"
    assert res.stop_cause == "user_stop"

    payload = res.as_payload()
    assert payload["session_id"] == "s1"
    assert payload["run_id"] == "r1"
    assert payload["status"] == "ok"
    assert payload["output"] == "hello"
    assert payload["pending_approval"] == {"action": "test"}
    assert payload["pending_ask"] == {"question": "sure?"}
    assert payload["stop_cause"] == "user_stop"
    assert payload["budget_exhaustion"] == {
        "dimension": "provider_turns",
        "source": "max_steps",
        "limit": 10,
        "consumed": 10,
        "remaining": 0,
        "continue_with": "resume_with_max_steps",
    }


def test_tool_calls_from_content_valid() -> None:
    items = [
        {"tool_name": "bash", "arguments": {"command": "ls"}, "tool_call_id": "call-1"},
        {"tool_name": "read_file", "arguments": {"path": "a.txt"}},
    ]
    calls = _tool_calls_from_content(json.dumps({"tool_calls": items}), provider_name="p", model_name="m")
    assert len(calls) == 2
    assert calls[0] == ToolCall(tool_name="bash", arguments={"command": "ls"}, tool_call_id="call-1")
    assert calls[1] == ToolCall(tool_name="read_file", arguments={"path": "a.txt"}, tool_call_id=None)


def test_tool_calls_from_content_malformed() -> None:
    # Not JSON
    with pytest.raises(ProviderExecutionError, match="provider stream tool content was not JSON"):
        _tool_calls_from_content("invalid json", provider_name="p", model_name="m")

    # Top-level is list, not object
    with pytest.raises(ProviderExecutionError, match="provider stream tool content was not an object"):
        _tool_calls_from_content(json.dumps([1, 2]), provider_name="p", model_name="m")

    # Item missing tool_name
    with pytest.raises(ProviderExecutionError, match="provider stream tool content was malformed"):
        _tool_calls_from_content(json.dumps({"tool_calls": [{"arguments": {}}]}), provider_name="p", model_name="m")

    # Item with non-dict arguments
    with pytest.raises(ProviderExecutionError, match="provider stream tool content arguments were not an object"):
        _tool_calls_from_content(
            json.dumps({"tool_calls": [{"tool_name": "bash", "arguments": 123}]}),
            provider_name="p",
            model_name="m",
        )

    # Item with invalid tool_call_id
    with pytest.raises(ProviderExecutionError, match="provider stream tool content id was malformed"):
        _tool_calls_from_content(
            json.dumps({"tool_calls": [{"tool_name": "bash", "arguments": {}, "tool_call_id": 999}]}),
            provider_name="p",
            model_name="m",
        )


def test_tool_call_from_entry_valid() -> None:
    entry = {"tool_name": "edit", "arguments": {"file": "x.py"}, "tool_call_id": "c1"}
    call = _tool_call_from_entry(entry)
    assert call.tool_name == "edit"
    assert call.arguments == {"file": "x.py"}
    assert call.tool_call_id == "c1"


def test_tool_call_from_entry_malformed() -> None:
    with pytest.raises(ProviderExecutionError, match="provider stream ended with an incomplete tool call"):
        _tool_call_from_entry({"tool_name": "", "arguments": {}})

    with pytest.raises(ProviderExecutionError, match="provider stream tool call id was malformed"):
        _tool_call_from_entry({"tool_name": "cmd", "arguments": {}, "tool_call_id": ""})


def test_turn_driver_pending_resolution() -> None:
    from unittest.mock import MagicMock

    from voidcode.core.engine.turn_execution import TurnDriver

    host = MagicMock()
    driver = TurnDriver(host)

    sess = SessionRecord(
        session_id="s1",
        workspace="/tmp",
        status=SessionStatus.RUNNING,
        active_revision_id="rev1",
        run_id="r1",
        turn=1,
        last_event_sequence=2,
    )
    cursor = _Cursor(deferred_sequence=1)
    event = StoredEvent(
        session_id="s1",
        sequence=1,
        run_id="r1",
        event_type=EventType.TOOL_DEFERRED,
        source="core",
        actor="assistant",
        revision_id="rev1",
        payload={"call": {"tool_name": "echo", "arguments": {"msg": "hi"}, "tool_call_id": "c1"}},
    )
    execution = _Execution(sess, cursor, [event], "r1")

    ev, call = driver.pending(execution)
    assert ev == event
    assert call.tool_name == "echo"
    assert call.arguments == {"msg": "hi"}
