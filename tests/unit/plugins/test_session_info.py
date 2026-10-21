"""Unit tests for the session_info panels over scripted committed facts."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from voidcode.plugins.session_info import create_plugin
from voidcode.sdk.codec import encode_provider_result, encode_tool_call, encode_tool_result
from voidcode.sdk.commands import CommandInvocation
from voidcode.sdk.hooks import RequestWindow, WindowSegment
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.provider import ProviderTurnResult
from voidcode.sdk.services import EventView, PluginBudget, SessionUsageReport
from voidcode.sdk.tools import TextOutput, ToolCall, ToolSuccess

_EVENTS: tuple[EventView, ...] = (
    EventView(
        sequence=1, event_type="kernel.user_message", run_id="run", revision_id="rev", source="client", actor="user", payload={"content": "read a"}
    ),
    EventView(
        sequence=2,
        event_type="kernel.model_turn",
        run_id="run",
        revision_id="rev",
        source="provider",
        actor="model",
        payload={
            "result": encode_provider_result(
                ProviderTurnResult(output="hello\n\n```python\nprint(1)\n```", tool_calls=(ToolCall("read", {"path": "a"}, "call-1"),))
            )
        },
    ),
    EventView(
        sequence=3,
        event_type="kernel.tool_completed",
        run_id="run",
        revision_id="rev",
        source="core",
        actor="core",
        payload={
            "call": encode_tool_call(ToolCall("read", {"path": "a"}, "call-1")),
            "result": encode_tool_result(ToolSuccess("read", output=TextOutput("contents of a"))),
        },
    ),
    EventView(sequence=4, event_type="kernel.run_completed", run_id="run", revision_id="rev", source="core", actor="core", payload={}),
)

_WINDOW_EVENT = EventView(
    sequence=5,
    event_type="kernel.provider_intent",
    run_id="run",
    revision_id="rev",
    source="core",
    actor="core",
    payload={
        "request_window": RequestWindow(
            message_characters=42,
            tool_schema_characters=7,
            tool_count=1,
            segments=(
                WindowSegment(role="user", characters=5, sha256="a" * 64),
                WindowSegment(role="tool", characters=37, sha256="b" * 64, tool_name="read", tool_calls=1, reasoning_characters=3),
            ),
        ).as_payload()
    },
)

_USAGE = SessionUsageReport(
    session_id="session-1",
    input_tokens=1200,
    output_tokens=300,
    cache_read_tokens=400,
    uncached_input_tokens=800,
    total_cost_usd=0.0123,
    has_unknown_usage=False,
    turn_count=2,
    elapsed_seconds=7.5,
    since_sequence=0,
    until_sequence=None,
    through_sequence=4,
)


class _FakeServices:
    def __init__(self, events: tuple[EventView, ...] = _EVENTS, budget: PluginBudget | None = None) -> None:
        self._events = events
        self._budget = budget

    def read_events(self, *, after_sequence: int = 0, limit: int = 100, include_payload: bool = True) -> tuple[EventView, ...]:
        _ = include_payload
        return tuple(event for event in self._events if event.sequence > after_sequence)[:limit]

    def get_usage(self, *, since_sequence: int = 0, until_sequence: int | None = None) -> SessionUsageReport:
        _ = since_sequence, until_sequence
        return _USAGE

    def get_budget(self) -> PluginBudget | None:
        return self._budget


def _plugin():
    return create_plugin({})


def _invocation(tmp_path: Path, name: str) -> CommandInvocation:
    context = ToolContext(
        workspace=tmp_path,
        session_id="session-1",
        run_id="run",
        invocation_id="invocation",
        revision_id="rev",
        actor="user",
    )
    return CommandInvocation(context=context, name=name)


def _result(tmp_path: Path, name: str, services: object | None = None):
    command = next(command for command in _plugin().commands if command.definition.name == name)
    return command.invoke(_invocation(tmp_path, name), services if services is not None else _FakeServices())  # type: ignore[arg-type]


def test_session_status_reports_identity_counts_and_budget(tmp_path: Path) -> None:
    budget = PluginBudget(owner="goal", since_sequence=0, max_total_tokens=1000)

    result = _result(tmp_path, "session_status", _FakeServices(budget=budget))

    assert result.status == "ok"
    assert "session-1" in result.output
    assert result.data is not None
    assert result.data["last_event_sequence"] == 4
    assert result.data["event_counts"]["kernel.model_turn"] == 1
    assert result.data["budget"]["max_total_tokens"] == 1000
    assert result.data["usage"]["total_cost_usd"] == 0.0123


def test_usage_panel_reports_measured_values(tmp_path: Path) -> None:
    result = _result(tmp_path, "usage")

    assert result.status == "ok"
    assert "input tokens: 1200" in result.output
    assert "cost usd: 0.0123" in result.output
    assert result.data is not None
    assert result.data["usage"]["uncached_input_tokens"] == 800


def test_context_panel_reports_the_measured_request_window(tmp_path: Path) -> None:
    services = _FakeServices(events=(*_EVENTS, _WINDOW_EVENT))

    result = _result(tmp_path, "context", services)

    assert result.status == "ok"
    assert result.data is not None
    assert result.data["dispatch_sequence"] == 5
    assert result.data["message_characters"] == 42
    assert result.data["tool_schema_characters"] == 7
    assert result.data["tool_count"] == 1
    assert result.data["total_characters"] == 49
    assert [segment["role"] for segment in result.data["segments"]] == ["user", "tool"]
    assert result.data["segments"][1]["tool_name"] == "read"
    assert result.data["segments"][1]["reasoning_characters"] == 3
    assert result.data["measured_input_tokens"] == 1200
    assert "last dispatch 5" in result.output
    assert "42 chars" in result.output
    # No heuristic is ever printed: token facts come from provider-reported usage only.
    assert "estimate" not in result.output
    assert "chars/4" not in result.output
    assert "tokens" not in result.output.replace("measured input tokens: 1200 (cache read: 400)", "")


def test_context_panel_without_a_measured_dispatch_says_so(tmp_path: Path) -> None:
    result = _result(tmp_path, "context")

    assert result.status == "ok"
    assert result.data is not None
    assert tuple(result.data["segments"]) == ()
    assert result.data["dispatch_sequence"] is None
    assert "no dispatched provider request has been measured" in result.output


def test_export_renders_markdown_transcript(tmp_path: Path) -> None:
    result = _result(tmp_path, "export")

    assert result.status == "ok"
    assert "# Session export" in result.output
    assert "read a" in result.output
    assert "hello" in result.output
    assert "tool read [ok]" in result.output
    assert "contents of a" in result.output


def test_copy_lists_fenced_blocks_from_the_last_response(tmp_path: Path) -> None:
    result = _result(tmp_path, "copy")

    assert result.status == "ok"
    assert result.data is not None
    blocks = result.data["blocks"]
    assert len(blocks) == 1
    assert blocks[0]["text"] == "print(1)"
    assert "code block 1 (python)" in result.output


def test_copy_without_an_assistant_turn_is_an_explicit_error(tmp_path: Path) -> None:
    services = _FakeServices(events=(_EVENTS[0],))

    result = _result(tmp_path, "copy", services)

    assert result.status == "error"
    assert "no assistant response" in (result.error or "")


def test_panels_only_register_their_commands() -> None:
    plugin = _plugin()

    assert sorted(command.definition.name for command in plugin.commands) == ["context", "copy", "export", "session_status", "usage"]
    assert plugin.tools == ()
    for command in plugin.commands:
        assert command.definition.effects == frozenset()


def test_event_identity_is_preserved_in_status_data(tmp_path: Path) -> None:
    result = _result(tmp_path, "session_status")

    assert result.data is not None
    assert isinstance(result.data["event_counts"], Mapping)
    assert result.data["revision_id"] == "rev"
