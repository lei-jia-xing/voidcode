"""Unit tests for the session_info panels over scripted committed facts."""

from __future__ import annotations

from pathlib import Path

import pytest

from voidcode.plugins.session_info import create_plugin
from voidcode.sdk.codec import encode_provider_result, encode_tool_call, encode_tool_result
from voidcode.sdk.commands import CommandInvocation
from voidcode.sdk.hooks import RequestWindow, WindowSegment
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.provider import ProviderMessage, ProviderTurnResult
from voidcode.sdk.services import EventView, PluginBudget, SessionUsageReport
from voidcode.sdk.tools import TextOutput, ToolCall, ToolSuccess
from voidcode.sdk.transcript import TranscriptDispatch, TranscriptEntry, TranscriptPage, TranscriptRun, TranscriptTool

_EVENTS: tuple[EventView, ...] = (
    EventView(
        sequence=1, event_type="core.user_message", run_id="run", revision_id="rev", source="client", actor="user", payload={"content": "read a"}
    ),
    EventView(
        sequence=2,
        event_type="core.model_turn",
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
        event_type="core.tool_completed",
        run_id="run",
        revision_id="rev",
        source="core",
        actor="core",
        payload={
            "call": encode_tool_call(ToolCall("read", {"path": "a"}, "call-1")),
            "result": encode_tool_result(ToolSuccess("read", output=TextOutput("contents of a"))),
        },
    ),
    EventView(sequence=4, event_type="core.run_completed", run_id="run", revision_id="rev", source="core", actor="core", payload={}),
)

_WINDOW_EVENT = EventView(
    sequence=5,
    event_type="core.provider_intent",
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

_TRANSCRIPT = (
    TranscriptEntry(1, "run", "rev", "client", ProviderMessage("user", "read a")),
    TranscriptEntry(
        2,
        "run",
        "rev",
        "provider",
        ProviderTurnResult(output="hello\n\n```python\nprint(1)\n```", tool_calls=(ToolCall("read", {"path": "a"}, "call-1"),)),
    ),
    TranscriptEntry(
        3,
        "run",
        "rev",
        "core",
        TranscriptTool(ToolCall("read", {"path": "a"}, "call-1"), ToolSuccess("read", output=TextOutput("contents of a")), False, 2, False),
    ),
    TranscriptEntry(4, "run", "rev", "core", TranscriptRun("completed")),
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

    def read_transcript(self, *, after_sequence=0, limit=100):
        entries = tuple(entry for entry in _TRANSCRIPT if entry.sequence > after_sequence and any(e.sequence == entry.sequence for e in self._events))
        if _WINDOW_EVENT in self._events and after_sequence < 5:
            entries += (
                TranscriptEntry(
                    5,
                    "run",
                    "rev",
                    "core",
                    TranscriptDispatch("provider", "model", RequestWindow.from_payload(_WINDOW_EVENT.payload["request_window"])),
                ),
            )
        page = entries[:limit]
        return TranscriptPage(page, page[-1].sequence if page else after_sequence, len(entries) > limit, len(page))

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
    assert blocks[0]["text"] == "print(1)\n"
    assert "code block 1 (python)" in result.output


def test_copy_without_an_assistant_turn_is_an_explicit_error(tmp_path: Path) -> None:
    services = _FakeServices(events=(_EVENTS[0],))

    result = _result(tmp_path, "copy", services)

    assert result.status == "error"
    assert "no assistant response" in (result.error or "")


def test_panels_only_register_their_commands() -> None:
    plugin = _plugin()

    assert sorted(command.definition.name for command in plugin.commands) == ["context", "copy", "export", "usage"]
    assert plugin.tools == ()
    for command in plugin.commands:
        assert command.definition.effects == frozenset()


def test_usage_does_not_require_history_access(tmp_path: Path) -> None:
    class UsageOnly:
        get_usage = _FakeServices.get_usage

        def get_budget(self):
            return None

    assert _result(tmp_path, "usage", UsageOnly()).status == "ok"


def test_transcript_scan_passes_empty_pages_and_refuses_to_copy_a_stale_response(tmp_path: Path) -> None:
    class Sparse(_FakeServices):
        def __init__(self):
            super().__init__()
            self.cursors = []

        def read_transcript(self, *, after_sequence=0, limit=100):
            self.cursors.append(after_sequence)
            if after_sequence == 0:
                return TranscriptPage((), 200, True, 200)
            return TranscriptPage((_TRANSCRIPT[1],), 201, False, 1)

    sparse = Sparse()
    copied = _result(tmp_path, "copy", sparse)
    assert copied.status == "ok"
    assert sparse.cursors == [0, 200]

    class Capped(_FakeServices):
        def read_transcript(self, *, after_sequence=0, limit=100):
            return TranscriptPage((_TRANSCRIPT[1],) if after_sequence == 0 else (), after_sequence + limit, True, limit)

    capped = _result(tmp_path, "copy", Capped())
    assert capped.status == "error"
    assert "cannot identify the last assistant response" in capped.error


def test_export_supports_nested_immutable_tool_arguments(tmp_path: Path) -> None:
    entry = TranscriptEntry(
        1,
        "run",
        "rev",
        "provider",
        ProviderTurnResult(tool_calls=(ToolCall("custom", {"options": {"names": ["a", "b"]}}),)),
    )

    class Nested(_FakeServices):
        def read_transcript(self, **kwargs):
            return TranscriptPage((entry,), 1, False, 1)

    result = _result(tmp_path, "export", Nested())
    assert result.status == "ok"
    assert '{"options": {"names": ["a", "b"]}}' in result.output


@pytest.mark.parametrize(
    "markdown,expected",
    [
        ("~~~~python\n    keep = 1  \n\n~~~~\n", "    keep = 1  \n\n"),
        ("````python\n```nested\n````\n", "```nested\n"),
        ("```python\n  leading indentation\n```", "  leading indentation\n"),
    ],
)
def test_copy_uses_markdown_fences_and_preserves_code_whitespace(tmp_path: Path, markdown: str, expected: str) -> None:
    class Fenced(_FakeServices):
        def read_transcript(self, **kwargs):
            entry = TranscriptEntry(1, "run", "rev", "provider", ProviderTurnResult(output=markdown))
            return TranscriptPage((entry,), 1, False, 1)

    result = _result(tmp_path, "copy", Fenced())
    assert result.status == "ok"
    assert result.data["blocks"][0]["text"] == expected


def test_context_does_not_describe_a_truncated_history_as_latest(tmp_path: Path) -> None:
    class Capped(_FakeServices):
        def read_transcript(self, *, after_sequence=0, limit=100):
            return TranscriptPage((), after_sequence + limit, True, limit)

    result = _result(tmp_path, "context", Capped())
    assert result.status == "error"
    assert "cannot identify the latest dispatched context" in result.error


def test_usage_presentation_distinguishes_unknown_from_zero_and_budget(tmp_path: Path) -> None:
    from dataclasses import replace

    from voidcode.sdk.views import FieldsViewData

    class Services(_FakeServices):
        def get_usage(self, **kwargs):
            return replace(_USAGE, input_tokens=None, total_cost_usd=None, output_tokens=0, has_unknown_usage=True)

    result = _result(tmp_path, "usage", Services())
    rows = {row.label: row.value for row in FieldsViewData.from_payload(result.presentation.data).rows}
    assert rows["Input tokens"] == "Unknown"
    assert rows["Output tokens"] == "0"
    assert rows["Cost (USD)"] == "Unknown"
    assert rows["Usage coverage"] == "Incomplete or unknown"


def test_context_presentation_labels_character_counts_and_cumulative_usage(tmp_path: Path) -> None:
    from voidcode.sdk.views import MarkdownViewData

    result = _result(tmp_path, "context", _FakeServices(events=(*_EVENTS, _WINDOW_EVENT)))
    view = MarkdownViewData.from_payload(result.presentation.data)
    assert "core-side neutral encoding" in view.content
    assert "cumulative" in view.content
    assert "Segments 0–2 of 2" in view.content
    assert {action.action for action in view.actions} == {"context", "usage"}
    assert _result(tmp_path, "context").presentation.view_type == "notice"


def test_context_segment_navigation_covers_all_segments():
    from dataclasses import replace

    from voidcode.plugins.session_info.panels import _context
    from voidcode.sdk.views import MarkdownViewData

    window = RequestWindow.from_payload(_WINDOW_EVENT.payload["request_window"])
    window = replace(window, segments=window.segments * 23)
    entries = (TranscriptEntry(5, "run", "rev", "core", TranscriptDispatch("provider", "model", window)),)
    offset = 0
    ranges = []
    while True:
        result = _context(entries, _USAGE, False, offset=offset)
        view = MarkdownViewData.from_payload(result.presentation.data)
        ranges.append(next(line for line in view.content.splitlines() if line.startswith("Segments ")))
        next_page = next((action for action in view.actions if action.label == "Next segments"), None)
        if next_page is None:
            break
        offset = next_page.arguments["offset"]
    assert ranges == ["Segments 0–20 of 46", "Segments 20–40 of 46", "Segments 40–46 of 46"]
    assert _context(entries, _USAGE, False, offset=47).status == "error"


def test_session_info_factory_rejects_unknown_fields():
    import pytest

    assert create_plugin({"providers": {}}).commands
    with pytest.raises(ValueError, match="session_info config does not support field: extra"):
        create_plugin({"extra": True})
