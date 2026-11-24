"""Unit tests for turn execution domain (TurnDriver, CoreResult, tool call extractors)."""

from __future__ import annotations

import json

import pytest

from voidcode.core.engine.turn_execution import (
    CoreResult,
    TurnDriver,
    _tool_call_from_entry,
    _tool_calls_from_content,
)
from voidcode.core.events import EventType
from voidcode.core.execution import _Cursor, _Execution
from voidcode.core.storage.store import SessionRecord, SessionStatus, StoredEvent
from voidcode.core.usage import budget_block
from voidcode.sdk.provider import ProviderExecutionError
from voidcode.sdk.services import BudgetExhaustion, PluginBudget, SessionUsageReport
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


def _report(
    *,
    input_tokens: int | None = 0,
    output_tokens: int | None = 0,
    cost_usd: float | None = None,
    elapsed_seconds: float | None = None,
) -> SessionUsageReport:
    return SessionUsageReport(
        session_id="s1",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=0 if input_tokens is not None else None,
        uncached_input_tokens=0 if input_tokens is not None else None,
        total_cost_usd=cost_usd,
        elapsed_seconds=elapsed_seconds,
    )


@pytest.mark.parametrize(
    ("budget", "report", "expected"),
    [
        (
            PluginBudget(owner="goal", max_input_tokens=100),
            _report(input_tokens=120, output_tokens=5),
            BudgetExhaustion("owner_tokens", "max_input_tokens", 100, 120, 0, "raise_owner_budget"),
        ),
        (
            PluginBudget(owner="goal", max_output_tokens=50),
            _report(input_tokens=10, output_tokens=50),
            BudgetExhaustion("owner_tokens", "max_output_tokens", 50, 50, 0, "raise_owner_budget"),
        ),
        (
            PluginBudget(owner="goal", max_total_tokens=100),
            _report(input_tokens=60, output_tokens=40),
            BudgetExhaustion("owner_tokens", "max_total_tokens", 100, 100, 0, "raise_owner_budget"),
        ),
        (
            PluginBudget(owner="goal", max_cost_usd=1.25),
            _report(cost_usd=2.5),
            BudgetExhaustion("owner_cost_usd", "max_cost_usd", 1.25, 2.5, 0.0, "raise_owner_budget"),
        ),
        (
            PluginBudget(owner="goal", max_elapsed_seconds=60.0),
            _report(elapsed_seconds=61.5),
            BudgetExhaustion("owner_elapsed_seconds", "max_elapsed_seconds", 60.0, 61.5, 0.0, "raise_owner_budget"),
        ),
    ],
)
def test_budget_block_types_a_reached_owner_cap(budget: PluginBudget, report: SessionUsageReport, expected: BudgetExhaustion) -> None:
    block = budget_block(report, budget)
    assert block is not None and not block.unknown
    assert block.exhaustion == expected
    assert block.message.startswith("budget limit exceeded:")


@pytest.mark.parametrize(
    ("budget", "report"),
    [
        (PluginBudget(owner="goal", max_input_tokens=10), _report(input_tokens=None)),
        (PluginBudget(owner="goal", max_output_tokens=10), _report(output_tokens=None)),
        (PluginBudget(owner="goal", max_total_tokens=10), _report(input_tokens=None)),
        (PluginBudget(owner="goal", max_cost_usd=1.0), _report(cost_usd=None)),
        (PluginBudget(owner="goal", max_elapsed_seconds=30.0), _report(elapsed_seconds=None)),
    ],
)
def test_budget_block_keeps_unknown_metrics_untyped(budget: PluginBudget, report: SessionUsageReport) -> None:
    block = budget_block(report, budget)
    assert block is not None and block.unknown
    assert block.exhaustion is None
    assert block.message.endswith("unknown for owner goal")


def test_budget_admission_attaches_the_typed_owner_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import MagicMock

    host = MagicMock()
    host.session_budgets.return_value = [PluginBudget(owner="goal", max_total_tokens=15)]
    host.get_session_usage.return_value = _report(input_tokens=10, output_tokens=5)
    driver = TurnDriver(host)
    stop = MagicMock(return_value="stopped")
    monkeypatch.setattr(driver, "stop", stop)
    execution = MagicMock()
    execution.session.session_id = "s1"

    assert driver.budget_admission(execution) == "stopped"
    stop.assert_called_once_with(
        execution,
        "budget limit exceeded: total tokens 15 >= 15 for owner goal",
        stop_cause="limit_reached",
        budget_exhaustion=BudgetExhaustion("owner_tokens", "max_total_tokens", 15, 15, 0, "raise_owner_budget"),
    )


def test_budget_admission_keeps_an_unknown_metric_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import MagicMock

    host = MagicMock()
    host.session_budgets.return_value = [PluginBudget(owner="goal", max_total_tokens=10)]
    host.get_session_usage.return_value = _report(input_tokens=None)
    driver = TurnDriver(host)
    fail = MagicMock(return_value="failed")
    stop = MagicMock()
    monkeypatch.setattr(driver, "fail", fail)
    monkeypatch.setattr(driver, "stop", stop)
    execution = MagicMock()
    execution.session.session_id = "s1"

    assert driver.budget_admission(execution) == "failed"
    fail.assert_called_once_with(execution, "budget limit reached: total tokens unknown for owner goal")
    stop.assert_not_called()


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
