"""Unit tests for the Effectiveness plugin: aggregate-only projection over session events."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from voidcode.plugins.effectiveness.effectiveness_plugin import (
    EVENT_PAGE_SIZE,
    EffectivenessCommand,
    create_plugin,
    project_session_effectiveness,
    read_session_events,
    render_summary,
)
from voidcode.sdk.codec import encode_tool_call, encode_tool_result
from voidcode.sdk.commands import CommandInvocation
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.services import EventView, PluginServices, SessionUsageReport
from voidcode.sdk.tools import (
    OutputBounds,
    TextOutput,
    ToolCall,
    ToolDiagnostics,
    ToolFailure,
    ToolResult,
    ToolSuccess,
)

WORKSPACE = Path("/tmp/workspace")
SESSION_ID = "session-1"


# --- Synthetic event streams (core-committed payload shapes) ----------------


def _model_turn(sequence: int, model: str) -> EventView:
    return EventView(
        sequence=sequence,
        event_type="kernel.model_turn",
        run_id="run-1",
        revision_id="rev-1",
        source="provider",
        actor="model",
        payload={"model": model, "provider": model.split("/", 1)[0], "elapsed_ms": 12},
    )


def _outcome(
    sequence: int,
    result: ToolResult,
    *,
    arguments: Mapping[str, object] | None = None,
    batch_sequence: int | None = None,
    event_type: str = "kernel.tool_completed",
) -> EventView:
    payload: dict[str, object] = {
        "batch_sequence": batch_sequence,
        "call": encode_tool_call(ToolCall(tool_name=result.tool_name, arguments=dict(arguments or {}), tool_call_id=f"call-{sequence}")),
        "result": encode_tool_result(result),
        "logical_turn": "turn-1",
    }
    return EventView(
        sequence=sequence,
        event_type=event_type,
        run_id="run-1",
        revision_id="rev-1",
        source="tool",
        actor="model",
        payload=payload,
    )


def _ok(tool: str, text: str = "ok", *, truncated: bool = False, partial: bool = False) -> ToolResult:
    return ToolSuccess(tool, TextOutput(text, bounds=OutputBounds(truncated=truncated, partial=partial)))


def _failure(
    tool: str,
    error: str = "boom",
    *,
    kind: str | None = None,
    guidance: str | None = None,
    truncated: bool = False,
    partial: bool = False,
) -> ToolResult:
    diagnostics = None if kind is None and guidance is None else ToolDiagnostics(kind=kind, guidance=guidance)
    return ToolFailure(tool, error, TextOutput(error, bounds=OutputBounds(truncated=truncated, partial=partial)), diagnostics=diagnostics)


def _usage(**overrides: object) -> SessionUsageReport:
    values: dict[str, object] = {
        "session_id": SESSION_ID,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "uncached_input_tokens": 0,
        "total_cost_usd": 0.0,
        "has_unknown_usage": False,
        "turn_count": 0,
    }
    values.update(overrides)
    return SessionUsageReport(**values)


class MockServices(PluginServices):
    """Read-only PluginServices: a synthetic session event stream plus one usage report."""

    def __init__(self, events: list[EventView] | None = None, usage: SessionUsageReport | None = None) -> None:
        self.events = events or []
        self.usage = usage or _usage()
        self.reads: list[tuple[int, int, bool]] = []

    def read_events(self, *, after_sequence: int = 0, limit: int = 100, include_payload: bool = True) -> tuple[EventView, ...]:
        self.reads.append((after_sequence, limit, include_payload))
        return tuple(event for event in self.events if event.sequence > after_sequence)[:limit]

    def get_usage(self, *, since_sequence: int = 0, until_sequence: int | None = None) -> SessionUsageReport:
        return self.usage

    def get_state(self, scope: Literal["session", "workspace"] = "session") -> tuple[Mapping[str, object] | None, int]:
        raise AssertionError("effectiveness reads no state")

    def replace_state(
        self,
        state: Mapping[str, object],
        expected_version: int,
        scope: Literal["session", "workspace"] = "session",
    ) -> int:
        raise AssertionError("effectiveness writes no state")

    def stage_event(self, event_type: str, payload: Mapping[str, object]) -> None:
        raise AssertionError("effectiveness stages no events")

    def enqueue_continuation(
        self,
        payload: Mapping[str, object],
        *,
        idempotency_key: str | None = None,
        role: Literal["system", "user"] = "system",
    ) -> str:
        raise AssertionError("effectiveness enqueues no continuation")


def _project(events: list[EventView], usage: SessionUsageReport | None = None) -> dict[str, object]:
    report = project_session_effectiveness(workspace_id=str(WORKSPACE), session_id=SESSION_ID, events=events, usage=usage or _usage())
    return report.to_payload()


def _tool(payload: Mapping[str, object], name: str) -> dict[str, object]:
    return next(tool for tool in payload["tools"] if tool["tool"] == name)


def _argument_bytes(arguments: Mapping[str, object]) -> int:
    return len(json.dumps(arguments, ensure_ascii=False, sort_keys=True).encode("utf-8"))


# --- The synthetic session ----------------------------------------------------


def _session_events() -> list[EventView]:
    """One session with reads, a delegation, edits, compaction, approvals, and run requests."""
    return [
        _model_turn(1, "anthropic/claude-x"),
        _outcome(2, _ok("read", "first window", truncated=True), arguments={"path": "/a"}, batch_sequence=1),
        _outcome(3, _ok("read", "the rest"), arguments={"path": "/a"}, batch_sequence=1),
        _outcome(4, _ok("read", "other file", partial=True), arguments={"path": "/b"}, batch_sequence=1),
        _outcome(5, _failure("read", "no such file", kind="not_found"), arguments={"path": "/b"}, batch_sequence=1),
        _outcome(6, _ok("read", "other file again"), arguments={"path": "/b"}, batch_sequence=1),
        _outcome(
            7, _failure("grep", "bad pattern", kind="invalid_pattern", guidance="use a simpler pattern"), arguments={"pattern": "x"}, batch_sequence=1
        ),
        _outcome(8, _ok("task", "child finished"), arguments={"prompt": "do it"}, batch_sequence=1),
        _model_turn(9, "openai/gpt-y"),
        _outcome(10, _ok("edit", "applied"), arguments={"path": "f.py"}, batch_sequence=9),
        _outcome(11, _failure("edit", "ambiguous", kind="ambiguous_match"), arguments={"path": "f.py"}, batch_sequence=9),
        # No model on the event and no batch to attribute it to: excluded from per-model stats.
        _outcome(12, _ok("edit", "applied"), arguments={"path": "f.py"}),
        EventView(
            sequence=13,
            event_type="runtime.context_compacted",
            run_id="run-1",
            revision_id="rev-1",
            source="runtime",
            actor="core",
            payload={"tokens_before": 900},
        ),
        EventView(
            sequence=16,
            event_type="plugin.compaction.context_compacted",
            run_id="run-1",
            revision_id="rev-1",
            source="plugin.compaction",
            actor="plugin",
            payload={"covered_bytes": 4434, "dropped_bytes": 3797, "reason": "manual"},
        ),
        EventView(
            sequence=14,
            event_type="kernel.approval_requested",
            run_id="run-1",
            revision_id="rev-1",
            source="core",
            actor="model",
            payload={"request_id": "r1"},
        ),
        EventView(
            sequence=15,
            event_type="kernel.approval_requested",
            run_id="run-1",
            revision_id="rev-1",
            source="core",
            actor="model",
            payload={"request_id": "r2", "hook_status": "approved"},
        ),
        EventView(
            sequence=16,
            event_type="kernel.user_message",
            run_id="run-1",
            revision_id="rev-1",
            source="client",
            actor="user",
            payload={"content": "first"},
        ),
        EventView(
            sequence=17,
            event_type="kernel.user_message",
            run_id="run-2",
            revision_id="rev-1",
            source="client",
            actor="user",
            payload={"content": "second"},
        ),
        EventView(
            sequence=18,
            event_type="kernel.model_query",
            run_id="run-2",
            revision_id="rev-1",
            source="core",
            actor="plugin",
            payload={"provider": "openai", "model": "gpt-y"},
        ),
        EventView(
            sequence=19,
            event_type="kernel.wakeup_fired",
            run_id="run-2",
            revision_id="rev-1",
            source="core",
            actor="core",
            payload={"owner": "cron"},
        ),
    ]


def test_synthetic_stream_produces_the_legacy_aggregates() -> None:
    payload = _project(_session_events())

    assert payload["tool_call_count"] == 10
    assert payload["success_count"] == 7
    assert payload["error_count"] == 3
    assert payload["success_rate"] == 0.7
    assert payload["session_count"] == 1
    assert payload["unattributed_tool_events"] == 0

    read = _tool(payload, "read")
    assert read["calls"] == 5
    assert read["successes"] == 4
    assert read["errors"] == 1
    assert read["unknown_results"] == 0
    assert read["retries_after_error"] == 1
    assert read["retry_guidance_count"] == 0
    assert read["truncated_results"] == 1
    assert read["partial_results"] == 1
    assert read["error_kinds"] == {"not_found": 1}
    assert read["success_rate"] == 0.8
    assert read["argument_bytes"] == sum(_argument_bytes({"path": path}) for path in ("/a", "/a", "/b", "/b", "/b"))
    assert read["average_argument_bytes"] == read["argument_bytes"] / 5
    assert read["result_bytes"] > 0

    grep = _tool(payload, "grep")
    assert grep["calls"] == 1
    assert grep["successes"] == 0
    assert grep["errors"] == 1
    assert grep["retries_after_error"] == 0
    assert grep["retry_guidance_count"] == 1
    assert grep["error_kinds"] == {"invalid_pattern": 1}

    task = _tool(payload, "task")
    assert task["calls"] == 1
    assert task["successes"] == 1

    edit = _tool(payload, "edit")
    assert edit["calls"] == 3
    assert edit["successes"] == 2
    assert edit["errors"] == 1
    assert edit["error_kinds"] == {"ambiguous_match": 1}

    # Ordered by call count, then name.
    assert [tool["tool"] for tool in payload["tools"]] == ["read", "edit", "grep", "task"]

    # /a is re-read once and /b is re-read on success after its failure: two repeats.
    assert payload["repeated_read_count"] == 2
    # The truncated /a read and the partial /b read are each followed by a completing read.
    assert payload["followup_read_count"] == 2
    assert payload["compaction_count"] == 2
    assert payload["approval_request_count"] == 1
    assert payload["resumed_run_count"] == 1
    assert payload["delegated_task_count"] == 1

    assert payload["privacy"] == {
        "source": "persisted_runtime_events",
        "stores_source_content": False,
        "stores_arguments": False,
        "projection": "aggregate_only",
    }


def test_edit_stats_are_per_model_and_unattributed_edits_are_excluded() -> None:
    payload = _project(_session_events())

    assert [model["model"] for model in payload["models"]] == ["openai/gpt-y"]
    model = payload["models"][0]
    assert model["edit_calls"] == 2
    assert model["edit_successes"] == 1
    assert model["edit_errors"] == 1
    assert model["edit_ambiguous_match_count"] == 1
    assert model["edit_ambiguous_match_rate"] == 0.5
    assert model["edit_error_kinds"] == {"ambiguous_match": 1}


def test_usage_sums_pass_through_and_cache_hit_rate_is_exact() -> None:
    usage = _usage(input_tokens=1000, output_tokens=250, cache_read_tokens=300, uncached_input_tokens=100, total_cost_usd=0.25, turn_count=3)
    block = _project([], usage)["provider_usage"]
    assert block == {
        "input_tokens": 1000,
        "output_tokens": 250,
        "cache_read_tokens": 300,
        "uncached_input_tokens": 100,
        "cache_hit_rate": 0.75,
        "cost_usd": 0.25,
        "has_unknown": False,
    }


def test_unknown_usage_keeps_sums_none_and_sets_has_unknown() -> None:
    partial = _usage(input_tokens=None, output_tokens=None, cache_read_tokens=None, uncached_input_tokens=None, total_cost_usd=None)
    block = _project([], partial)["provider_usage"]
    assert block["input_tokens"] is None
    assert block["output_tokens"] is None
    assert block["cache_read_tokens"] is None
    assert block["uncached_input_tokens"] is None
    assert block["cost_usd"] is None
    assert block["cache_hit_rate"] is None
    assert block["has_unknown"] is True

    # An unknown contributor never becomes a zero total, and a known one still counts as unknown.
    mixed = _project([], _usage(input_tokens=10, cache_read_tokens=None, uncached_input_tokens=None, total_cost_usd=0.5, has_unknown_usage=False))
    mixed_block = mixed["provider_usage"]
    assert mixed_block["input_tokens"] == 10
    assert mixed_block["cache_read_tokens"] is None
    assert mixed_block["cost_usd"] == 0.5
    assert mixed_block["cache_hit_rate"] is None
    assert mixed_block["has_unknown"] is True


def test_unknown_usage_flag_from_the_core_is_preserved() -> None:
    block = _project(
        [], _usage(input_tokens=5, output_tokens=5, cache_read_tokens=0, uncached_input_tokens=0, total_cost_usd=0.0, has_unknown_usage=True)
    )["provider_usage"]
    assert block["input_tokens"] == 5
    assert block["cache_hit_rate"] is None  # zero denominator is unknown, never a fabricated ratio
    assert block["has_unknown"] is True


def test_empty_session_reports_zeroes_without_fabrication() -> None:
    services = MockServices(events=[], usage=_usage())
    result = EffectivenessCommand().invoke(
        CommandInvocation(
            context=ToolContext(workspace=WORKSPACE, session_id=SESSION_ID, run_id="run-1", invocation_id="inv-1", revision_id="rev-1", actor="user"),
            name="effectiveness",
            arguments={},
        ),
        services,
    )
    assert result.status == "ok"
    payload = dict(result.data or {})
    assert payload["tool_call_count"] == 0
    assert payload["success_count"] == 0
    assert payload["error_count"] == 0
    assert payload["success_rate"] is None
    assert payload["tools"] == ()
    assert payload["models"] == ()
    assert payload["unattributed_tool_events"] == 0
    assert payload["repeated_read_count"] == 0
    assert payload["followup_read_count"] == 0
    assert payload["compaction_count"] == 0
    assert payload["approval_request_count"] == 0
    assert payload["resumed_run_count"] == 0
    assert payload["delegated_task_count"] == 0
    assert payload["provider_usage"]["cache_hit_rate"] is None
    assert payload["provider_usage"]["has_unknown"] is False
    assert result.output is not None
    assert result.output.endswith("No persisted tool calls for this session.")
    assert services.reads == [(0, EVENT_PAGE_SIZE, True)]


def test_unreadable_payloads_are_counted_as_unknown_not_fabricated() -> None:
    events = [
        # A truncated payload cannot name a tool at all.
        EventView(
            sequence=1,
            event_type="kernel.tool_completed",
            run_id="run-1",
            revision_id="rev-1",
            source="tool",
            actor="model",
            payload={"truncated": True, "original_bytes": 90000},
            payload_truncated=True,
        ),
        # A payload with no call identity is still attributable through its result, but its
        # missing status is unknown instead of a success or an error.
        EventView(
            sequence=2,
            event_type="kernel.tool_completed",
            run_id="run-1",
            revision_id="rev-1",
            source="tool",
            actor="model",
            payload={"result": {"tool_name": "read"}},
        ),
    ]
    payload = _project(events)
    assert payload["unattributed_tool_events"] == 1
    read = _tool(payload, "read")
    assert read["calls"] == 1
    assert read["successes"] == 0
    assert read["errors"] == 0
    assert read["unknown_results"] == 1
    assert read["argument_bytes"] == 0
    assert payload["tool_call_count"] == 0
    assert payload["success_rate"] is None


def test_interrupted_calls_count_as_errors() -> None:
    events = [
        _outcome(1, _ok("read", "done"), arguments={"path": "/a"}),
        _outcome(
            2,
            _failure("edit", "execution was interrupted; external effects are unknown"),
            arguments={"path": "f.py"},
            event_type="kernel.tool_interrupted",
        ),
    ]
    payload = _project(events)
    assert payload["tool_call_count"] == 2
    assert payload["error_count"] == 1
    assert _tool(payload, "edit")["error_kinds"] == {"unspecified": 1}


def test_events_are_paged_until_a_short_page() -> None:
    events = [_outcome(sequence, _ok("read", "text"), arguments={"path": f"/f{sequence}"}) for sequence in range(1, EVENT_PAGE_SIZE + 51)]
    services = MockServices(events=events)
    report = project_session_effectiveness(
        workspace_id=str(WORKSPACE),
        session_id=SESSION_ID,
        events=read_session_events(services),
        usage=services.usage,
    )
    assert report.tool_call_count == EVENT_PAGE_SIZE + 50
    assert services.reads == [(0, EVENT_PAGE_SIZE, True), (EVENT_PAGE_SIZE, EVENT_PAGE_SIZE, True)]


def test_command_returns_the_projection_and_a_short_summary() -> None:
    services = MockServices(
        events=_session_events(), usage=_usage(input_tokens=100, output_tokens=20, cache_read_tokens=60, uncached_input_tokens=40, total_cost_usd=0.1)
    )
    result = EffectivenessCommand().invoke(
        CommandInvocation(
            context=ToolContext(workspace=WORKSPACE, session_id=SESSION_ID, run_id="run-1", invocation_id="inv-1", revision_id="rev-1", actor="user"),
            name="effectiveness",
            arguments={},
        ),
        services,
    )
    assert result.status == "ok"
    payload = dict(result.data or {})
    assert payload["tool_call_count"] == 10
    assert payload["provider_usage"]["cache_hit_rate"] == 0.6
    lines = (result.output or "").splitlines()
    assert lines[0] == "TOOL EFFECTIVENESS calls=10 success=7 errors=3 success_rate=70.0%"
    assert lines[1] == "SIGNALS repeated_reads=2 followup_reads=2 compactions=2 approvals=1 resumed_runs=1 delegated_tasks=1"
    assert "TOKENS input=100 output=20 cache_read=60 uncached_input=40 cache_hit_rate=60.0% cost_usd=0.1 unknown=False" in lines[2]
    assert lines[3] == "TOOL                         CALLS     OK  ERRORS   RATE  RETRIES  TRUNCATED  ERROR KINDS"
    assert lines[4].startswith("read ")
    assert lines[4].endswith("not_found:1")
    assert render_summary(project_session_effectiveness(workspace_id=str(WORKSPACE), session_id=SESSION_ID, events=[], usage=_usage())).endswith(
        "No persisted tool calls for this session."
    )


def test_create_plugin_registers_the_effectiveness_command() -> None:
    plugin = create_plugin({})
    commands = [command.definition for command in plugin.commands]
    assert [command.name for command in commands] == ["effectiveness"]
    assert commands[0].argument_schema == {"type": "object", "properties": {}, "additionalProperties": False}
    assert commands[0].effects == frozenset({"read"})
    assert commands[0].busy_gate is False
    assert plugin.tools == ()
    assert plugin.context is None
    assert plugin.observer is None
